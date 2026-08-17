"""Floating always-on-top monitor for Claude session + weekly usage across accounts.

Seeds itself from Claude Code's own credential files: every time you /login to a
different account, that account's tokens get captured and tracked from then on.
Run with pythonw (double-click the .pyw) for no console window.
"""
import json, math, os, socket, sys, threading, time, queue, webbrowser
import urllib.request, urllib.error
from datetime import datetime, timezone

REPO_URL = "https://github.com/cloudx99/claude-usage-monitor"

HOME = os.path.expanduser("~")
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store.json")
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# urllib's default UA (Python-urllib/x.y) is refused by Cloudflare with 403 error 1010.
# Any honest UA passes — this does not need to impersonate the official CLI.
USER_AGENT = "claude-usage-monitor/1.0"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Must match Claude Code's own TOKEN_URL — selftest asserts this against its cli.js.
# console.anthropic.com serves a 403 here, which is what a wrong host looks like.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# The refresh body must carry `scope`; omitting it is rejected. Claude Code's own default.
DEFAULT_SCOPES = ["user:profile", "user:inference", "user:sessions:claude_code",
                  "user:mcp_servers", "user:file_upload"]

# (ui family, numeric family). Resolved against the installed set at startup — naming a
# missing family makes Tk silently substitute Times New Roman, which looks like a bug.
FONT_SETS = {
    "Display": ("Segoe UI Variable Display", "Cascadia Mono"),
    "Segoe": ("Segoe UI", "Consolas"),
    "Bahnschrift": ("Bahnschrift", "Cascadia Mono"),
}
FONT_FALLBACK = ("Segoe UI", "Consolas")

UI_VERSION = 2  # bumping this drops a saved height so a new layout can size itself
DEFAULTS = {"poll": 60, "theme": "dark", "font": 9, "w": 268, "h": None, "pos": None,
            "dirs": [], "ui": UI_VERSION, "fontset": "Display"}
MIN_W, MIN_H = 200, 90
MIN_POLL, MAX_BACKOFF, STAGGER = 30, 900, 0.25  # every account costs a request per poll

CHROMA = "#010203"  # keyed out for rounded corners; must not appear in any theme
THEMES = {
    "dark": dict(bg="#17181c", card="#1e2026", edge="#2b2e37", fg="#e6e8ee", dim="#868b99",
                 track="#2b2e37", ok="#3ecf8e", warn="#e0a458", crit="#f0616d"),
    "light": dict(bg="#f2f3f7", card="#ffffff", edge="#e0e2ea", fg="#1b1d24", dim="#6d7280",
                  track="#e6e8ef", ok="#12a06a", warn="#b3701a", crit="#d0384a"),
    "black": dict(bg="#000000", card="#0c0d10", edge="#1c1e24", fg="#ececf1", dim="#757a87",
                  track="#1c1e24", ok="#3ecf8e", warn="#e0a458", crit="#f0616d"),
}
ACCENTS = ["#6ea8fe", "#c792ea", "#3ecf8e", "#e0a458", "#f0616d", "#5ad1e6"]


def default_dirs():
    d = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    return [d] + [x for x in os.environ.get("CLAUDE_MONITOR_DIRS", "").split(os.pathsep) if x]


# ---------- store ----------

def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)  # atomic; store holds refresh tokens


def load_store():
    s = read_json(STORE) or {}
    s.setdefault("accounts", {})
    cfg = s.setdefault("cfg", {})
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    if cfg.get("ui") != UI_VERSION:  # a saved height belongs to the layout that made it
        cfg["ui"], cfg["h"] = UI_VERSION, None
    return s


def harvest(store):
    """Pull tokens out of each Claude config dir into our store, keyed by email."""
    for d in default_dirs() + list(store["cfg"]["dirs"]):
        cred = read_json(os.path.join(d, ".credentials.json")) or {}
        oa = cred.get("claudeAiOauth")
        if not oa or not oa.get("refreshToken"):
            continue
        # .claude.json sits beside the dir (~/.claude.json), or inside a custom dir
        prof = read_json(d + ".json") or read_json(os.path.join(d, ".claude.json")) or {}
        acct = prof.get("oauthAccount") or {}
        email = acct.get("emailAddress")
        if not email:
            continue
        cur = store["accounts"].setdefault(email, {})
        # the config dir is authoritative while Claude Code is using it
        cur.update(
            access_token=oa["accessToken"],
            refresh_token=oa["refreshToken"],
            expires_at=oa.get("expiresAt", 0),
            scopes=oa.get("scopes") or " ".join(DEFAULT_SCOPES),
            plan=oa.get("subscriptionType") or acct.get("organizationType", ""),
            source_dir=d,
        )
    return store


# ---------- api ----------

def api_request(url, data=None, headers=None):
    """Every outbound request goes through here so User-Agent can never be omitted."""
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    return urllib.request.Request(url, data, h, method="POST" if data else "GET")


def owns_dir(cred, old_refresh):
    """True only if the config dir still holds the exact token we just rotated.

    Every account harvested from ~/.claude shares that source_dir, but the dir belongs to
    whichever account is logged in right now. Writing another account's tokens into it
    would clobber that live login, so match on the token, never on the path.
    """
    return bool(cred) and (cred.get("claudeAiOauth") or {}).get("refreshToken") == old_refresh


class RefreshFailed(Exception):
    """Refresh endpoint rejected us — kept distinct so it can't be read as a usage error."""
    def __init__(self, code):
        self.code = code
        super().__init__(f"refresh HTTP {code}")


def refresh(acct):
    """Rotate tokens. Writes back to the source config dir so Claude Code stays logged in."""
    old = acct["refresh_token"]
    req = api_request(TOKEN_URL, json.dumps({
        "grant_type": "refresh_token", "refresh_token": acct["refresh_token"],
        "client_id": CLIENT_ID, "scope": acct.get("scopes") or " ".join(DEFAULT_SCOPES),
        }).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise RefreshFailed(e.code) from e
    acct["access_token"] = d["access_token"]
    acct["refresh_token"] = d.get("refresh_token", acct["refresh_token"])
    acct["scopes"] = d.get("scope") or acct.get("scopes")
    acct["expires_at"] = int(time.time() * 1000) + int(d.get("expires_in", 3600)) * 1000

    path = os.path.join(acct.get("source_dir") or "", ".credentials.json")
    cred = read_json(path)
    if owns_dir(cred, old):
        cred["claudeAiOauth"].update(accessToken=acct["access_token"],
                                     refreshToken=acct["refresh_token"],
                                     scopes=acct["scopes"],
                                     expiresAt=acct["expires_at"])
        write_json(path, cred)
    return acct


def fetch_usage(token):
    req = api_request(USAGE_URL, headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def needs_refresh(acct, now_ms=None, skew_ms=300_000):
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    return acct.get("expires_at", 0) - skew_ms <= now_ms


def refresh_cooldown(fails):
    """Wait before retrying a failed refresh: 1m, 2m, 4m … capped.

    Without this a dead or throttled account re-hits the auth endpoint every single
    poll, which is what got this app rate limited in the first place.
    """
    return min(MAX_BACKOFF, 60 * 2 ** min(max(fails, 1) - 1, 8))


def try_refresh(acct, now=None):
    """refresh() wrapped in a per-account cooldown. Raises RefreshFailed while cooling."""
    now = time.time() if now is None else now
    if now < acct.get("retry_at", 0):
        raise RefreshFailed(acct.get("last_refresh_code", 0))
    try:
        refresh(acct)
    except RefreshFailed as e:
        acct["fail_count"] = acct.get("fail_count", 0) + 1
        acct["last_refresh_code"] = e.code
        acct["retry_at"] = now + refresh_cooldown(acct["fail_count"])
        raise
    for k in ("fail_count", "last_refresh_code", "retry_at"):
        acct.pop(k, None)
    return acct


def poll_account(acct):
    """-> (session_pct, weekly_pct, session_reset, weekly_reset)."""
    if needs_refresh(acct):
        try_refresh(acct)
    try:
        u = fetch_usage(acct["access_token"])
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        try_refresh(acct)  # stale despite the clock; a re-login may have revoked it
        u = fetch_usage(acct["access_token"])
    lim = {l["kind"]: l for l in u.get("limits") or []}
    s, w = lim.get("session", {}), lim.get("weekly_all", {})
    return (s.get("percent", 0), w.get("percent", 0), s.get("resets_at"), w.get("resets_at"))


# ---------- formatting ----------

def until(iso):
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = (t - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "now"
    d, rem = divmod(int(secs), 86400)
    h, m = divmod(rem // 60, 60)
    return f"{d}d {h}h" if d else (f"{h}h {m:02d}m" if h else f"{m}m")


def bar_color(pct, th):
    return th["ok"] if pct < 50 else th["warn"] if pct < 80 else th["crit"]


def fmt_poll(secs):
    """-> (value, unit) for the settings spinbox."""
    return (secs // 60, "minutes") if secs >= 60 and secs % 60 == 0 else (secs, "seconds")


def claude_code_bundle():
    """Claude Code's own JS bundle, if it's installed — the source of truth for endpoints."""
    for p in (os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules",
                           "@anthropic-ai", "claude-code", "cli.js"),
              os.path.join(HOME, ".local", "share", "claude", "cli.js"),
              os.path.join(HOME, ".claude", "local", "node_modules",
                           "@anthropic-ai", "claude-code", "cli.js")):
        if os.path.isfile(p):
            return p
    return None


def retry_after(err):
    """Seconds requested by a 429's Retry-After header, if it gave a usable one."""
    try:
        return max(0, int(float(err.headers.get("Retry-After"))))
    except (AttributeError, TypeError, ValueError):
        return 0


def backoff_secs(prev, wanted=0):
    """Next 429 wait: honour Retry-After when given, else double from 60s."""
    return min(MAX_BACKOFF, max(MIN_POLL, wanted) if wanted else (prev * 2 if prev else 60))


# ---------- drawing ----------

def round_rect_points(x1, y1, x2, y2, r):
    """Corner points for a smoothed polygon. Tk has no rounded-rect primitive.

    Edge endpoints are doubled: a spline through single points bows the straight
    runs inward, so the panel edges come out visibly curved instead of flat.
    """
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r,
            x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


def fit_text(cv, item, max_right):
    """Ellipsise a canvas text item until it clears max_right."""
    txt = cv.itemcget(item, "text")
    while txt and cv.bbox(item)[2] > max_right:
        txt = txt[:-1]
        cv.itemconfig(item, text=txt.rstrip() + "…")
    return txt


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    return cv.create_polygon(round_rect_points(x1, y1, x2, y2, r), smooth=True, **kw)


try:
    from PIL import Image, ImageDraw, ImageTk
    HAVE_PIL = True
except ImportError:                     # still runs, just without smoothing
    HAVE_PIL = False

SS = 3                                  # supersample factor for the Pillow path


class Paint:
    """Geometry buffer, rasterised one of two ways.

    Tk's canvas has no antialiasing whatsoever, so every curve, circle and diagonal
    comes out stair-stepped. Collecting the geometry lets Pillow draw it at SSx and
    downsample, which is the only way to get smooth edges without a canvas rewrite.
    """

    def __init__(self):
        self.ops = []

    def rrect(self, box, r, fill=None, outline=None, width=1):
        self.ops.append(("rrect", tuple(box), r, fill, outline, width))

    def oval(self, box, fill=None, outline=None, width=1):
        self.ops.append(("oval", tuple(box), None, fill, outline, width))

    def line(self, pts, fill, width=1):
        self.ops.append(("line", tuple(pts), None, fill, None, width))

    def poly(self, pts, fill):
        self.ops.append(("poly", tuple(pts), None, fill, None, 0))

    def arc(self, box, start, extent, fill, width=1):
        self.ops.append(("arc", tuple(box), (start, extent), fill, None, width))


def paint_pil(ops, W, H, bg=None, radius=0, keyed=None):
    """Draw at SSx and downsample. `keyed` re-hardens the outer corners: an antialiased
    edge against the keyed-out colour leaves a halo that the transparency can't remove.
    bg=None gives a transparent RGBA sprite, so an icon cannot square off a rounded edge."""
    W, H = max(1, int(W)), max(1, int(H))
    im = Image.new("RGB", (W * SS, H * SS), bg) if bg else \
        Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for kind, geom, extra, fill, outline, width in ops:
        g = [v * SS for v in geom]
        w = max(1, int(round(width * SS)))
        if kind == "rrect":
            x1, y1, x2, y2 = g
            r = max(0, min(extra * SS, (x2 - x1) / 2, (y2 - y1) / 2))
            d.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=fill,
                                outline=outline, width=w)
        elif kind == "oval":
            d.ellipse(g, fill=fill, outline=outline, width=w)
        elif kind == "line":
            d.line(g, fill=fill, width=w)
            for cx, cy in ((g[0], g[1]), (g[2], g[3])):   # PIL has no round caps
                d.ellipse([cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2], fill=fill)
        elif kind == "poly":
            d.polygon(g, fill=fill)
        elif kind == "arc":
            st, ex = extra                               # Tk is CCW/y-up, PIL is CW/y-down
            d.arc(g, start=-(st + ex), end=-st, fill=fill, width=w)
    im = im.resize((W, H), Image.LANCZOS)
    if keyed is not None:
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=radius, fill=255)
        out = Image.new("RGB", (W, H), keyed)
        out.paste(im, (0, 0), mask)
        im = out
    return im


def paint_tk(cv, ops, tags=()):
    """Fallback rasteriser: same geometry, straight onto the canvas, aliased."""
    for kind, geom, extra, fill, outline, width in ops:
        if kind == "rrect":
            cv.create_polygon(round_rect_points(*geom, extra), smooth=True, tags=tags,
                              fill=fill or "", outline=outline or "", width=width)
        elif kind == "oval":
            cv.create_oval(*geom, fill=fill or "", outline=outline or "", width=width,
                           tags=tags)
        elif kind == "line":
            cv.create_line(*geom, fill=fill, width=width, capstyle="round", tags=tags)
        elif kind == "poly":
            cv.create_polygon(*geom, fill=fill, outline=fill, tags=tags)
        elif kind == "arc":
            cv.create_arc(*geom, start=extra[0], extent=extra[1], style="arc",
                          outline=fill, width=width, tags=tags)


def icon_close(p, x, y, s, color):
    for dx in (-1, 1):
        p.line((x - s * dx, y - s, x + s * dx, y + s), color, 1.8)


def icon_gear(p, x, y, s, color):
    """Three sliders. A toothed cog is mush at 14px even with smoothing."""
    for dy, kx in ((-s * .62, -s * .32), (0, s * .34), (s * .62, -s * .06)):
        p.line((x - s, y + dy, x + s, y + dy), color, 1.6)
        p.oval((x + kx - 2.4, y + dy - 2.4, x + kx + 2.4, y + dy + 2.4),
               fill=color, outline=color)


def icon_refresh(p, x, y, s, color):
    start, extent = 35, 275
    p.arc((x - s, y - s, x + s, y + s), start, extent, color, 1.8)
    a = math.radians(start + extent)                     # head sits at the end of the sweep
    hx, hy = x + math.cos(a) * s, y - math.sin(a) * s
    tx, ty = -math.sin(a), -math.cos(a)                  # tangent, in screen coords
    nx, ny = math.cos(a), -math.sin(a)                   # radial
    h, w = s * .95, s * .5
    p.poly((hx + tx * h, hy + ty * h, hx - nx * w, hy - ny * w,
            hx + nx * w, hy + ny * w), color)


def icon_logo(p, x, y, s, color):
    """Three rising bars — a usage meter, not a generic app blob."""
    for i, f in enumerate((.45, .75, 1.0)):
        bx = x + (i - 1) * s * .82
        p.rrect((bx - s * .27, y + s - 2 * s * f, bx + s * .27, y + s), 1, fill=color)


def icon_grip(p, x, y, s, color):
    for o in (.4, .72, 1.0):        # o=0 used to emit a zero-length line, drawing nothing
        p.line((x - s * o, y, x, y - s * o), color, 1.5)


HEADER_H, FOOTER_H, GAP = 38, 26, 10
CARD_PAD, NAME_H, ROW_GAP = 16, 22, 15


def fsz(fs, d=0):
    """Derived font size with a legibility floor — fs-4 at font 7 is a 3pt smudge."""
    return max(6, fs + d)


def bar_h(fs):
    """Gauge thickness. 9px against a 600px run reads as a hairline, not a bar."""
    return max(10, fs + 2)


def card_height(fs):
    """Card interior: padding, name row, then two gauges with room to breathe."""
    return 2 * CARD_PAD + NAME_H + ROW_GAP + 2 * bar_h(fs) + ROW_GAP


def layout_cards(H, n, fs):
    """-> (first_y, card_h). Spare height is fed back into the cards.

    Without this a window taller than its content draws a dead band at the bottom,
    which is what a fixed card height looks like once the user drags the grip.
    """
    if n <= 0:
        return HEADER_H + GAP, 0
    avail = H - HEADER_H - FOOTER_H - GAP * (n + 1)
    return HEADER_H + GAP, max(card_height(fs), avail / n)


def content_height(n, fs):
    """Height that fits n cards exactly — the autosize target."""
    return HEADER_H + GAP * (max(n, 1) + 1) + max(n, 1) * card_height(fs) + FOOTER_H


# ---------- app ----------

class Monitor:
    def __init__(self, tk):
        self.tk = tk
        self.store = load_store()
        self.cfg = self.store["cfg"]
        self.rows, self.settings, self.status = [], None, "starting…"
        self.last_poll, self.backoff = 0.0, 0
        self.q, self.stop, self.wake = queue.Queue(), threading.Event(), threading.Event()

        self.root = tk.Tk()
        self.root.title("Claude Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.rounded = True
        try:
            self.root.attributes("-transparentcolor", CHROMA)  # Windows-only, gives corners
        except tk.TclError:
            self.rounded = False
        self.root.configure(bg=CHROMA if self.rounded else THEMES[self.cfg["theme"]]["bg"])

        self.cv = tk.Canvas(self.root, highlightthickness=0, bd=0,
                            bg=CHROMA if self.rounded else THEMES[self.cfg["theme"]]["bg"])
        self.cv.pack(fill="both", expand=True)
        self.drag, self.resizing, self._fonts, self._icons = {}, False, {}, {}
        self.uif, self.monof = self.resolve_fonts()
        self.cv.bind("<Button-1>", self.press)
        self.cv.bind("<B1-Motion>", self.move)
        self.cv.bind("<ButtonRelease-1>", self.release)
        self.root.bind("<Escape>", lambda e: self.bye())
        self.root.protocol("WM_DELETE_WINDOW", self.bye)

        pos = self.cfg["pos"] or f"+{self.root.winfo_screenwidth() - self.cfg['w'] - 24}+40"
        self.root.geometry(f"{self.cfg['w']}x{self.cfg['h'] or 120}{pos}")
        self.render()

        threading.Thread(target=self.worker, daemon=True).start()
        self.root.after(200, self.pump)

    # --- window ---
    def press(self, e):
        self.drag.update(x=e.x_root - self.root.winfo_x(), y=e.y_root - self.root.winfo_y())

    def move(self, e):
        """Canvas-level drag. Resizing routes through here too — see grip_press."""
        if self.resizing:
            w = max(MIN_W, self.drag["gw"] + e.x_root - self.drag["gx"])
            h = max(MIN_H, self.drag["gh"] + e.y_root - self.drag["gy"])
            self.cfg["w"], self.cfg["h"] = w, h  # explicit h disables autosizing
            self.root.geometry(f"{w}x{h}")
            self.render()
        else:
            self.root.geometry(f"+{e.x_root - self.drag['x']}+{e.y_root - self.drag['y']}")

    def grip_press(self, e):
        # Only a flag is set here. render() deletes every canvas item, so an item-level
        # <B1-Motion> binding dies on the first redraw — the widget-level one survives.
        self.resizing = True
        self.drag.update(gx=e.x_root, gy=e.y_root,
                         gw=self.root.winfo_width(), gh=self.root.winfo_height())
        return "break"

    def release(self, e):
        if self.resizing:
            self.resizing = False
            self.render()          # final pass at full quality
        self.resizing = False

    def tint(self, tag, color):
        """Recolour an icon per item type: ovals and open arcs have no fill to set."""
        cv = self.cv
        for it in cv.find_withtag(tag):
            kind = cv.type(it)
            if kind in ("line", "text"):
                cv.itemconfig(it, fill=color)
            elif kind == "polygon":
                cv.itemconfig(it, fill=color, outline=color)
            else:                                  # oval / arc / rectangle
                cv.itemconfig(it, outline=color)
                if cv.itemcget(it, "fill"):        # filled knobs need the fill too
                    cv.itemconfig(it, fill=color)

    def icon_img(self, draw, s, color, bg=None):
        """Icons are their own images so hover can swap one instead of repainting all."""
        key = (draw.__name__, s, color, bg)
        img = self._icons.get(key)
        if img is None:
            p, box = Paint(), int(2 * s + 10)
            draw(p, box / 2, box / 2, s, color)
            img = self._icons[key] = ImageTk.PhotoImage(paint_pil(p.ops, box, box, bg))
        return img

    def button(self, tag, draw, th, x, y, s, fn):
        """Hit-target that lights up on hover and swallows the drag binding."""
        cv, grp = self.cv, tag + "_g"
        if HAVE_PIL:
            cold, hot = (self.icon_img(draw, s, th[k]) for k in ("dim", "fg"))
            cv.create_image(x, y, image=cold, tags=(tag, grp))   # image = full-rect hit area
            cv.tag_bind(grp, "<Enter>", lambda e: (
                cv.itemconfig(tag, image=hot), cv.config(cursor="hand2")))
            cv.tag_bind(grp, "<Leave>", lambda e: (
                cv.itemconfig(tag, image=cold), cv.config(cursor="")))
        else:
            p = Paint()
            draw(p, x, y, s, th["dim"])
            paint_tk(cv, p.ops, (tag, grp))
            box = cv.bbox(tag)
            if box:
                # An item with fill="" has no interior, so it is click-through. Paint the
                # pad in the background colour and sink it under the glyph instead.
                hit = cv.create_rectangle(box[0] - 6, box[1] - 6, box[2] + 6, box[3] + 6,
                                          outline="", fill=th["bg"], tags=(grp,))
                cv.tag_lower(hit, cv.find_withtag(tag)[0])
            cv.tag_bind(grp, "<Enter>", lambda e: (
                self.tint(tag, th["fg"]), cv.config(cursor="hand2")))
            cv.tag_bind(grp, "<Leave>", lambda e: (
                self.tint(tag, th["dim"]), cv.config(cursor="")))
        cv.tag_bind(grp, "<Button-1>", lambda e: (fn(), "break")[1])
        return grp

    # --- rendering ---
    def render(self):
        th, fs, W = THEMES[self.cfg["theme"]], self.cfg["font"], self.cfg["w"]
        cv, pad = self.cv, 12
        cv.delete("all")
        cv.config(bg=CHROMA if self.rounded else th["bg"])

        n = len(self.rows)
        # Never let a saved size cut the content off: a bigger font or an extra account
        # raises the floor, and the window has to follow it.
        need = content_height(n, fs)
        H = max(int(self.cfg["h"]), need) if self.cfg["h"] else need
        W = max(MIN_W, int(W))
        self.p = p = Paint()
        p.rrect((1, 1, W - 2, H - 2), 13 if self.rounded else 0,
                fill=th["bg"], outline=th["edge"])

        hy = HEADER_H / 2
        icon_logo(p, pad + 7, hy, 6, th["fg"])
        cv.create_text(pad + 21, hy, text="CLAUDE USAGE", anchor="w", fill=th["fg"],
                       font=(self.uif, fsz(fs, -2), "bold"))
        y, ch = layout_cards(H, n, fs)
        if not n:
            cv.create_text(W / 2, (y + H - FOOTER_H) / 2, text="waiting for first poll…",
                           fill=th["dim"], font=(self.uif, fsz(fs, -1)))
        for i, r in enumerate(self.rows):
            self.card(r, i, y, ch, th, fs, W, pad)
            y += ch + GAP

        fy = H - FOOTER_H / 2 - 3  # footer pinned to the bottom, never mid-panel
        dot = th["crit"] if self.backoff else th["ok"]
        p.oval((pad + 1, fy - 3.5, pad + 8, fy + 3.5), fill=dot, outline=dot)
        cv.create_text(pad + 15, fy, text=self.status, anchor="w", fill=th["dim"],
                       font=(self.uif, fsz(fs, -3)))

        self.blit(W, H, th)     # background under the text, which Tk already antialiases

        # kept off the corner arc — at (W-7,H-7) it drew half outside the rounded edge
        grp = self.button("ic_grip", icon_grip, th, W - 12, H - 12, 5, lambda: None)
        cv.tag_bind(grp, "<Button-1>", self.grip_press)
        cv.tag_bind(grp, "<Enter>", lambda e: cv.config(cursor="size_nw_se"))
        self.button("ic_close", icon_close, th, W - pad - 6, hy, 5, self.bye)
        self.button("ic_gear", icon_gear, th, W - pad - 29, hy, 7, self.open_settings)
        self.button("ic_sync", icon_refresh, th, W - pad - 52, hy, 7, self.force_poll)

        if (W, H) != (self.root.winfo_width(), self.root.winfo_height()):
            self.root.geometry(f"{W}x{int(H)}")

    def blit(self, W, H, th):
        """Rasterise the collected geometry and sink it beneath the text.

        Supersampling costs ~40ms at 770x525 and ~100ms at 1200x900 — fine per poll,
        far too slow for a drag firing 60 motion events a second. Drop to the aliased
        path while resizing and repaint smoothly once the button comes up.
        """
        if HAVE_PIL and not self.resizing:
            im = paint_pil(self.p.ops, W, H, CHROMA if self.rounded else th["bg"],
                           radius=13 if self.rounded else 0,
                           keyed=CHROMA if self.rounded else None)
            self._bg = ImageTk.PhotoImage(im)     # must outlive the call or Tk drops it
            item = self.cv.create_image(0, 0, anchor="nw", image=self._bg)
        else:
            paint_tk(self.cv, self.p.ops, ("bg",))
            item = "bg"
        self.cv.tag_lower(item)

    def card(self, r, i, top, ch, th, fs, W, pad):
        cv, p, accent = self.cv, self.p, ACCENTS[i % len(ACCENTS)]
        x1, x2 = pad, W - pad
        p.rrect((x1, top, x2, top + ch), 10, fill=th["card"])
        # inset past the corner radius, or the stripe pokes outside the card's rounded edge
        p.rrect((x1, top + 12, x1 + 3, top + ch - 12), 1.5, fill=accent)

        # centre the content block so a stretched card stays balanced
        off = (ch - card_height(fs)) / 2
        inner, right = x1 + CARD_PAD, x2 - CARD_PAD
        cy = top + off + CARD_PAD + NAME_H / 2
        name = r["email"].split("@")[0]
        av = NAME_H / 2
        p.oval((inner, cy - av, inner + 2 * av, cy + av), fill=accent, outline=accent)
        cv.create_text(inner + av, cy, text=name[:1].upper(), fill=th["card"],
                       font=(self.uif, fsz(fs, -1), "bold"))
        nm = cv.create_text(inner + 2 * av + 11, cy, text=name, anchor="w", fill=th["fg"],
                            font=(self.uif, fsz(fs, 1), "bold"))
        limit = right
        if r.get("plan"):
            t = cv.create_text(right - 7, cy, text=r["plan"].upper(), anchor="e",
                               fill=th["dim"], font=(self.uif, fsz(fs, -3), "bold"))
            b = cv.bbox(t)
            p.rrect((b[0] - 8, b[1] - 4, b[2] + 8, b[3] + 4), 9, outline=th["edge"])
            limit = b[0] - 16
        fit_text(cv, nm, limit)   # long names used to run under the plan pill

        bh = bar_h(fs)
        gy = top + off + CARD_PAD + NAME_H + ROW_GAP + bh / 2
        if r.get("error"):
            cv.create_text(inner, gy, text=r["error"], anchor="w", fill=th["crit"],
                           font=(self.uif, fsz(fs, -1)))
            return
        # Size both rows against the widest of the two, so the bars share one right edge.
        # Per-row sizing leaves them ragged whenever the reset strings differ in width.
        rows = [("5h", r["session"], until(r["s_reset"]) or "-"),
                ("7d", r["weekly"], until(r["w_reset"]) or "-")]
        fr, fp = fsz(fs, -2), fsz(fs)
        rw = max(self.fw(self.monof, fr, t) for _, _, t in rows)
        pw = max(self.fw(self.uif, fp, f"{p:.0f}%", "bold") for _, p, _ in rows)
        px = right - rw - 14                      # shared right edge of the percentages
        bx1 = inner + self.fw(self.monof, fr, "5h") + 12
        for label, pct, reset in rows:
            self.gauge(label, pct, reset, gy, th, fr, fp, bx1, px - pw - 14, px, right, bh)
            gy += bh + ROW_GAP

    def resolve_fonts(self):
        """Pick the configured pairing, falling back per-family to what is installed."""
        from tkinter import font as tkfont
        fams = set(tkfont.families())
        ui, mono = FONT_SETS.get(self.cfg.get("fontset"), FONT_SETS["Segoe"])
        return (ui if ui in fams else FONT_FALLBACK[0],
                mono if mono in fams else FONT_FALLBACK[1])

    def set_fontset(self, name):
        self.cfg["fontset"] = name
        self.uif, self.monof = self.resolve_fonts()
        self._fonts.clear()                      # measurement cache is keyed by family
        self.root.after_idle(self.restyle)

    def fw(self, family, size, text, weight="normal"):
        """Measured text width. Cached — a Font object per render per row adds up."""
        key = (family, size, weight)
        f = self._fonts.get(key)
        if f is None:
            from tkinter import font as tkfont
            f = self._fonts[key] = tkfont.Font(family=family, size=size, weight=weight)
        return f.measure(text)

    def gauge(self, label, pct, reset, gy, th, fr, fp, x1, x2, px, right, bh):
        cv = self.cv
        cv.create_text(x1 - 12 - self.fw(self.monof, fr, label), gy, text=label, anchor="w",
                       fill=th["dim"], font=(self.monof, fr))
        cv.create_text(right, gy, text=reset, anchor="e", fill=th["dim"],
                       font=(self.monof, fr))
        cv.create_text(px, gy, text=f"{pct:.0f}%", anchor="e", fill=th["fg"],
                       font=(self.uif, fp, "bold"))
        if x2 - x1 < 16:
            return                                             # too narrow to draw honestly
        r = bh / 2
        self.p.rrect((x1, gy - r, x2, gy + r), r, fill=th["track"])
        w = (x2 - x1) * min(max(pct, 0), 100) / 100
        if w > 1:
            self.p.rrect((x1, gy - r, x1 + max(w, 2 * r), gy + r), r,
                         fill=bar_color(pct, th))

    # --- polling ---
    def worker(self):
        while not self.stop.is_set():
            due = self.last_poll + max(MIN_POLL, self.cfg["poll"], self.backoff)
            gap = due - time.monotonic()
            if gap > 0:
                # Cap the sleep so a settings change is noticed without costing a request:
                # waking here re-reads cfg, it never triggers a poll on its own.
                self.wake.wait(min(gap, 5))
                self.wake.clear()
                continue

            self.last_poll = time.monotonic()
            harvest(self.store)
            rows, limited = [], 0
            for i, (email, acct) in enumerate(sorted(self.store["accounts"].items())):
                if i:
                    time.sleep(STAGGER)  # don't fire N accounts as one burst
                try:
                    s, w, sr, wr = poll_account(acct)
                    rows.append(dict(email=email, plan=acct.get("plan", ""), session=s,
                                     weekly=w, s_reset=sr, w_reset=wr))
                except RefreshFailed as e:
                    # 400 = invalid_grant, the refresh token really is dead. Anything else
                    # is our side failing, so say so rather than blaming the account.
                    # No global backoff here: the per-account cooldown already stops the
                    # retries, and healthy accounts should keep updating normally.
                    wait = int(max(0, acct.get("retry_at", 0) - time.time()))
                    rows.append(dict(email=email, plan=acct.get("plan", ""), error={
                        400: "re-login needed",
                        429: f"auth throttled — retry in {wait}s",
                        0: f"waiting to retry ({wait}s)",
                    }.get(e.code, f"refresh failed HTTP {e.code} — retry in {wait}s")))
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        limited = max(limited, retry_after(e))
                        rows.append(dict(email=email, plan=acct.get("plan", ""),
                                         error="rate limited"))
                        continue
                    rows.append(dict(email=email, plan=acct.get("plan", ""),
                                     error="re-login needed" if e.code in (400, 401)
                                     else f"HTTP {e.code}"))
                except Exception as e:
                    rows.append(dict(email=email, plan=acct.get("plan", ""), error=str(e)[:60]))

            has_429 = any(r.get("error") == "rate limited" for r in rows)
            self.backoff = backoff_secs(self.backoff, limited) if has_429 else 0
            write_json(STORE, self.store)
            self.q.put(rows)

    def force_poll(self):
        """User-initiated refresh. Only ever called from a button, never from typing."""
        self.last_poll = 0.0
        self.wake.set()

    def pump(self):
        try:
            self.rows = self.q.get_nowait()
        except queue.Empty:
            pass
        else:
            self.status = (f"rate limited — backing off {self.backoff}s" if self.backoff
                           else "updated " + time.strftime("%H:%M"))
            try:
                self.render()
            except Exception as e:
                # A draw error must not take the update loop down with it: under pythonw
                # there is no console, so the panel would just freeze on stale numbers.
                self.status = f"draw error: {str(e)[:40]}"
        self.root.after(300, self.pump)

    # --- settings ---
    # Tk's OptionMenu and Spinbox ignore theming and render as grey Windows chrome, which
    # looks broken against the panel. These two replace both with themed equivalents.
    def segmented(self, parent, options, current, th, fs, on_pick):
        tk = self.tk
        f = tk.Frame(parent, bg=th["track"], padx=2, pady=2)
        cells, live = {}, [False]

        def pick(v):
            for k, c in cells.items():
                on = k == v
                c.config(bg=th["card"] if on else th["track"],
                         fg=th["fg"] if on else th["dim"])
            if live[0]:
                on_pick(v)

        for o in options:
            c = tk.Label(f, text=str(o), font=(self.uif, fsz(fs, -1)), padx=9, pady=2,
                         cursor="hand2")
            c.pack(side="left")
            c.bind("<Button-1>", lambda e, v=o: pick(v))
            cells[o] = c
        pick(current)
        live[0] = True          # paint the initial state without firing a restyle
        return f

    def stepper(self, parent, var, lo, hi, step, th, fs, on_change):
        tk = self.tk
        f = tk.Frame(parent, bg=th["track"])

        def bump(d):
            try:
                n = int(var.get())
            except ValueError:
                n = lo
            var.set(str(max(lo, min(hi, n + d * step()))))
            on_change()

        for txt, d in (("−", -1), ("+", 1)):
            b = tk.Label(f, text=txt, font=(self.uif, fs, "bold"), bg=th["track"],
                         fg=th["dim"], padx=7, cursor="hand2")
            b.pack(side="right" if d > 0 else "left")
            b.bind("<Button-1>", lambda e, d=d: bump(d))
            b.bind("<Enter>", lambda e, b=b: b.config(fg=th["fg"]))
            b.bind("<Leave>", lambda e, b=b: b.config(fg=th["dim"]))
        e = tk.Entry(f, textvariable=var, width=4, justify="center", relief="flat", bd=0,
                     bg=th["track"], fg=th["fg"], insertbackground=th["fg"],
                     font=(self.uif, fs), highlightthickness=0)
        e.pack(side="left", pady=3)
        return f

    def open_settings(self):
        tk, th, fs = self.tk, THEMES[self.cfg["theme"]], self.cfg["font"]
        if self.settings and self.settings.winfo_exists():
            self.settings.lift()
            return
        s = self.settings = tk.Toplevel(self.root)
        s.attributes("-topmost", True)
        s.overrideredirect(True)          # match the panel instead of wearing Windows chrome
        s.configure(bg=th["bg"], highlightthickness=1, highlightbackground=th["edge"])
        s.geometry(f"+{self.root.winfo_x()}+{self.root.winfo_y() + 40}")

        def close():
            write_json(STORE, self.store)
            s.destroy()
            self.settings = None

        # header: title + close, draggable like the panel
        head = tk.Frame(s, bg=th["bg"])
        head.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(head, text="SETTINGS", bg=th["bg"], fg=th["fg"],
                 font=(self.uif, fsz(fs, -2), "bold")).pack(side="left")
        x = tk.Canvas(head, width=18, height=18, bg=th["bg"], highlightthickness=0,
                      cursor="hand2")
        x.pack(side="right")
        if HAVE_PIL:
            cold, hot = (self.icon_img(icon_close, 5, th[k]) for k in ("dim", "fg"))
            x.create_image(9, 9, image=cold, tags="x")
            x.bind("<Enter>", lambda e: x.itemconfig("x", image=hot))
            x.bind("<Leave>", lambda e: x.itemconfig("x", image=cold))
        else:
            pp = Paint()
            icon_close(pp, 9, 9, 5, th["dim"])
            paint_tk(x, pp.ops, ("x",))
        x.tag_bind("x", "<Button-1>", lambda e: close())
        x.bind("<Button-1>", lambda e: close())
        drag = {}
        for w in (head, head.winfo_children()[0]):
            w.bind("<Button-1>", lambda e: drag.update(
                x=e.x_root - s.winfo_x(), y=e.y_root - s.winfo_y()))
            w.bind("<B1-Motion>", lambda e: s.geometry(
                f"+{e.x_root - drag.get('x', 0)}+{e.y_root - drag.get('y', 0)}"))

        body = tk.Frame(s, bg=th["bg"], padx=12, pady=6)
        body.pack(fill="both", expand=True)

        def row(txt, r):
            tk.Label(body, text=txt, bg=th["bg"], fg=th["dim"], font=(self.uif, fsz(fs, -1)),
                     anchor="w").grid(row=r, column=0, sticky="w", pady=5, padx=(0, 14))

        def btn(parent, txt, fn):
            b = tk.Label(parent, text=txt, font=(self.uif, fsz(fs, -1)), bg=th["card"],
                         fg=th["fg"], padx=10, pady=4, cursor="hand2")
            b.bind("<Button-1>", lambda e: fn())
            b.bind("<Enter>", lambda e: b.config(bg=th["track"]))
            b.bind("<Leave>", lambda e: b.config(bg=th["card"]))
            return b

        val, unit = fmt_poll(self.cfg["poll"])
        self.v_num, self.v_unit = tk.StringVar(value=str(val)), tk.StringVar(value=unit)
        row("Refresh every", 0)
        box = tk.Frame(body, bg=th["bg"])
        box.grid(row=0, column=1, sticky="w")
        self.stepper(box, self.v_num, 1, 999,
                     lambda: 1 if self.v_unit.get() == "minutes" else 15,
                     th, fs, self.apply_poll).pack(side="left")
        self.segmented(box, ["seconds", "minutes"], unit, th, fs,
                       lambda v: (self.v_unit.set(v), self.apply_poll())).pack(side="left",
                                                                              padx=6)
        self.v_num.trace_add("write", lambda *_: self.apply_poll())

        row("Theme", 1)
        # after_idle: restyle destroys this window, and doing that inside the click
        # handler of a widget it owns tears the widget out from under Tk.
        self.segmented(body, list(THEMES), self.cfg["theme"], th, fs,
                       lambda v: (self.cfg.update(theme=v), self.root.after_idle(self.restyle))
                       ).grid(row=1, column=1, sticky="w")

        row("Font", 2)
        self.segmented(body, list(FONT_SETS), self.cfg.get("fontset", "Segoe"), th, fs,
                       self.set_fontset).grid(row=2, column=1, sticky="w")

        row("Font size", 3)
        self.v_font = tk.StringVar(value=str(fs))
        self.stepper(body, self.v_font, 7, 16, lambda: 1, th, fs,
                     self.apply_font).grid(row=3, column=1, sticky="w")
        self.v_font.trace_add("write", lambda *_: self.apply_font())

        row("Accounts", 4)
        acc = tk.Frame(body, bg=th["bg"])
        acc.grid(row=4, column=1, sticky="w")
        btn(acc, "+ Add account", self.add_account).pack(side="left", padx=(0, 6))
        btn(acc, "Watch folder…", self.add_dir).pack(side="left")

        self.hint = tk.Label(body, text=f"tracking {len(self.store['accounts'])} account(s)",
                             bg=th["bg"], fg=th["dim"], font=(self.uif, fsz(fs, -2)),
                             anchor="w", wraplength=300, justify="left")
        self.hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 6))

        tk.Frame(body, bg=th["edge"], height=1).grid(row=6, column=0, columnspan=2,
                                                     sticky="ew", pady=(0, 6))
        link = tk.Label(body, text=REPO_URL.replace("https://", ""), bg=th["bg"], fg=th["ok"],
                        font=(self.uif, fsz(fs, -2)), cursor="hand2")
        link.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))
        link.bind("<Button-1>", lambda e: webbrowser.open(REPO_URL))
        s.bind("<Escape>", lambda e: close())

    def restyle(self):
        self.render()
        if self.settings and self.settings.winfo_exists():
            self.settings.destroy()
            self.settings = None
            self.open_settings()

    def apply_poll(self):
        try:
            n = max(1, int(self.v_num.get()))
        except ValueError:
            return
        # No poll here: this fires on every keystroke. The worker picks the new
        # interval up within 5s on its own.
        self.cfg["poll"] = max(MIN_POLL, n * (60 if self.v_unit.get() == "minutes" else 1))

    def apply_font(self):
        try:
            n = int(self.v_font.get())
        except ValueError:
            return
        if 7 <= n <= 16 and n != self.cfg["font"]:
            self.cfg["font"] = n      # render clamps the height up if the new size needs it
            self.root.after_idle(self.restyle)

    def add_account(self):
        """Accounts arrive by logging in; this captures whoever is logged in right now."""
        before = set(self.store["accounts"])
        new = set(harvest(self.store)["accounts"]) - before
        write_json(STORE, self.store)
        self.force_poll()
        self.hint.config(text=("added " + ", ".join(new)) if new else
                         (f"tracking {len(self.store['accounts'])} account(s) — run /login "
                          "in Claude Code with another account, then click this again."))

    def add_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Pick a .claude config folder", parent=self.settings)
        if not d:
            return
        d = os.path.normpath(d)
        if d not in self.cfg["dirs"]:
            self.cfg["dirs"].append(d)
        self.add_account()

    def bye(self):
        self.stop.set()
        self.wake.set()
        self.cfg["pos"] = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        write_json(STORE, self.store)
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        self.stop.set()
        self.wake.set()


def main():
    import tkinter as tk
    # A second copy would silently double the request rate. The socket releases itself
    # when the process dies, so there is no stale lock to clean up after a crash.
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 49517))
    except OSError:
        from tkinter import messagebox
        tk.Tk().withdraw()
        messagebox.showinfo("Claude Usage", "Already running — check your other monitors.")
        return
    Monitor(tk).run()
    lock.close()


def uitest():
    """Draw every card state at every size, so a layout bug fails here not on screen."""
    import tempfile, tkinter as tk
    globals()["STORE"] = os.path.join(tempfile.mkdtemp(), "store.json")  # never the real one
    globals()["harvest"] = lambda s: s                 # no credentials, no network
    globals()["poll_account"] = lambda a: (0, 0, None, None)

    m = Monitor(tk)
    m.root.withdraw()
    m.stop.set()
    m.wake.set()
    rows = [
        dict(email="a@x.com", plan="pro", session=0, weekly=0, s_reset=None, w_reset=None),
        dict(email="an-extremely-long-account-name-here@example.com", plan="max20x",
             session=70, weekly=68, s_reset="2026-08-18T03:21:00+00:00", w_reset=None),
        dict(email="c@x.com", plan="", session=150, weekly=100,      # over 100% must clamp
             s_reset="1999-01-01T00:00:00+00:00", w_reset="2026-08-20T00:00:00+00:00"),
        dict(email="d@x.com", plan="pro", error="refresh failed HTTP 403 — retry in 60s"),
    ]
    n = 0
    for theme in THEMES:
        for fs in (7, 12, 16):
            for w, h in ((MIN_W, None), (268, None), (584, 400), (900, 1000), (MIN_W, MIN_H)):
                m.cfg.update(theme=theme, font=fs, w=w, h=h)
                for k in range(len(rows) + 1):
                    m.rows = rows[:k]
                    m.render()
                    assert m.cv.bbox("all"), (theme, fs, w, h, k)   # something was drawn
                    n += 1
    # Icons are drawn, not glyphs, so a bad coordinate just quietly renders nothing.
    # The grip shipped with a zero-length line for exactly this reason.
    for name, fn, s in (("close", icon_close, 4), ("gear", icon_gear, 7),
                        ("refresh", icon_refresh, 7), ("logo", icon_logo, 6),
                        ("grip", icon_grip, 5)):
        p = Paint()
        fn(p, 60, 60, s, "#ffffff")
        assert p.ops, name
        xs = [v for _, g, _, _, _, _ in p.ops for v in g[0::2]]
        ys = [v for _, g, _, _, _, _ in p.ops for v in g[1::2]]
        assert max(xs) - min(xs) >= s and max(ys) - min(ys) >= s, (name, "too small")
        for kind, g, _, _, _, _ in p.ops:
            if kind == "line":
                assert (g[0] - g[2]) ** 2 + (g[1] - g[3]) ** 2 > 1, (name, "zero-length")
            if kind in ("oval", "rrect"):
                assert g[2] - g[0] > 2 and g[3] - g[1] > 2, (name, "sub-stroke shape", g)
        if HAVE_PIL:                                   # and it must actually rasterise
            assert paint_pil(p.ops, 40, 40, "#000000").size == (40, 40), name

    # Naming a font that is not installed makes Tk silently substitute Times New Roman,
    # so every pairing must resolve to something actually present.
    from tkinter import font as tkfont
    fams = set(tkfont.families())
    for name in FONT_SETS:
        m.cfg["fontset"] = name
        m.uif, m.monof = m.resolve_fonts()
        m._fonts.clear()
        assert m.uif in fams and m.monof in fams, (name, m.uif, m.monof)
        m.rows = rows
        for theme in THEMES:
            m.cfg.update(theme=theme, font=11, w=420, h=None)
            m.render()
            n += 1
    m.cfg["fontset"] = "no-such-set"
    assert m.resolve_fonts() == FONT_FALLBACK                  # unknown set degrades safely
    m.cfg["fontset"] = "Display"
    m.uif, m.monof = m.resolve_fonts()

    # Resize must survive a redraw. It did not: render() deletes every canvas item, so the
    # item-level <B1-Motion> binding died on the first motion event and the grip went dead.
    class Ev:
        def __init__(self, x, y):
            self.x_root, self.y_root = x, y

    m.rows = rows[:2]
    m.cfg.update(w=300, h=300, font=12, theme="dark")
    m.grip_press(Ev(500, 500))
    assert m.resizing
    m.drag.update(gw=300, gh=300)       # winfo_* is unreliable while withdrawn
    m.move(Ev(600, 560))
    assert (m.cfg["w"], m.cfg["h"]) == (400, 360), (m.cfg["w"], m.cfg["h"])
    m.move(Ev(560, 540))                # a second motion must still land
    assert (m.cfg["w"], m.cfg["h"]) == (360, 340)
    m.move(Ev(0, 0))
    assert (m.cfg["w"], m.cfg["h"]) == (MIN_W, MIN_H)   # clamped, never inverted
    m.release(Ev(0, 0))
    assert not m.resizing
    m.resizing = True                   # the aliased fast path must also render cleanly
    m.render()
    m.release(Ev(0, 0))
    assert not m.resizing
    before = (m.cfg["w"], m.cfg["h"])
    m.press(Ev(700, 700))
    m.move(Ev(720, 730))                # plain drag moves the window, never resizes it
    assert (m.cfg["w"], m.cfg["h"]) == before

    m.root.destroy()
    print(f"uitest ok - {n} renders, resize + drag verified")


def selftest():
    now = 1_000_000_000_000
    assert needs_refresh({"expires_at": now}, now)                     # expired
    assert needs_refresh({"expires_at": now + 60_000}, now)            # inside skew
    assert not needs_refresh({"expires_at": now + 600_000}, now)       # fresh
    assert needs_refresh({}, now)                                      # missing == refresh

    th = THEMES["dark"]
    assert bar_color(0, th) == bar_color(49, th) == th["ok"]
    assert bar_color(50, th) == th["warn"] and bar_color(80, th) == bar_color(150, th) == th["crit"]
    for name, t in THEMES.items():
        assert set(t) == set(THEMES["dark"]), name                     # no theme misses a key
        assert CHROMA not in t.values(), name                          # keyed-out colour must
        assert t["bg"] != t["card"], name                              # not be paintable

    assert until(None) == "" and until("garbage") == ""
    assert until("1999-01-01T00:00:00+00:00") == "now"
    # keep offsets off a minute boundary: clock granularity decides the rounding there
    far = datetime.now(timezone.utc).timestamp() + 86400 * 2 + 3600 * 5 + 90
    assert until(datetime.fromtimestamp(far, timezone.utc).isoformat()) == "2d 5h"
    soon = datetime.now(timezone.utc).timestamp() + 1830
    assert until(datetime.fromtimestamp(soon, timezone.utc).isoformat()) == "30m"

    assert fmt_poll(60) == (1, "minutes") and fmt_poll(600) == (10, "minutes")
    assert fmt_poll(30) == (30, "seconds") and fmt_poll(90) == (90, "seconds")

    assert backoff_secs(0) == 60 and backoff_secs(60) == 120        # doubles from 60
    assert backoff_secs(600) == MAX_BACKOFF == backoff_secs(MAX_BACKOFF)  # and caps
    assert backoff_secs(0, 120) == 120 and backoff_secs(600, 45) == 45    # Retry-After wins
    assert backoff_secs(0, 5) == MIN_POLL                           # never faster than the floor
    assert backoff_secs(0, 99999) == MAX_BACKOFF

    class E:                                                        # header shapes seen in the wild
        def __init__(self, v): self.headers = {"Retry-After": v}
    assert retry_after(E("30")) == 30 and retry_after(E("30.7")) == 30
    assert retry_after(E(None)) == retry_after(E("Wed, 21 Oct 2026 07:28:00 GMT")) == 0
    assert retry_after(object()) == 0                               # no headers at all

    # a radius larger than the box must not invert the corners
    big = round_rect_points(0, 0, 10, 10, 99)
    assert min(big) == 0 and max(big) == 10 and len(big) % 2 == 0
    sq = round_rect_points(0, 0, 20, 10, 0)
    assert set(zip(sq[::2], sq[1::2])) == {(0, 0), (20, 0), (20, 10), (0, 10)}  # r=0 = square
    # straight edges need doubled endpoints or the spline bows them inward
    pts = round_rect_points(0, 0, 100, 40, 10)
    assert pts.count(10) >= 4 and len(pts) > 24

    # A window taller than its content must feed the slack into the cards, not leave a
    # dead band at the bottom — that is what the fixed-height cards looked like.
    for n in (1, 3, 6):
        exact = content_height(n, 12)
        top, ch = layout_cards(exact, n, 12)
        assert abs(ch - card_height(12)) < 1e-6, (n, ch)             # exact fit, no stretch
        assert abs(top + n * ch + (n - 1) * GAP + GAP + FOOTER_H - exact) < 1e-6
        _, tall = layout_cards(exact + 300, n, 12)
        assert tall > ch and abs(n * (tall - ch) - 300) < 1e-6        # slack fully absorbed
        _, squeezed = layout_cards(MIN_H, n, 12)
        assert squeezed == card_height(12)                           # never collapses
    assert layout_cards(400, 0, 12)[1] == 0                          # no rows, no cards

    # Cloudflare answers urllib's default UA with 403 (error 1010), and that failure only
    # shows up against the live endpoint — so pin it here on every request we build.
    for r in (api_request(TOKEN_URL, b"{}", {"Content-Type": "application/json"}),
              api_request(USAGE_URL, headers={"Authorization": "Bearer x"})):
        ua = r.get_header("User-agent")
        assert ua == USER_AGENT and not ua.startswith("Python-urllib"), ua
    assert api_request(USAGE_URL).get_method() == "GET"
    assert api_request(TOKEN_URL, b"{}").get_method() == "POST"

    assert refresh_cooldown(1) == 60 and refresh_cooldown(2) == 120   # doubles
    assert refresh_cooldown(0) == 60                                  # never zero-wait
    assert refresh_cooldown(99) == MAX_BACKOFF                        # and caps

    # A failing refresh must not be retried on the next poll — that is what got us
    # 429'd. Second call short-circuits without touching the network.
    calls = []
    real, globals()["refresh"] = refresh, lambda a: calls.append(1) or (_ for _ in ()).throw(
        RefreshFailed(429))
    try:
        acct = {"refresh_token": "x"}
        for _ in range(3):
            try:
                try_refresh(acct, now=1000.0)
            except RefreshFailed as e:
                last = e.code
        assert len(calls) == 1, f"refresh retried while cooling ({len(calls)} calls)"
        assert last == 429 and acct["retry_at"] == 1060.0
    finally:
        globals()["refresh"] = real

    live = {"claudeAiOauth": {"refreshToken": "tok-live"}}
    assert owns_dir(live, "tok-live")                  # same account: safe to write back
    assert not owns_dir(live, "tok-other")             # another account owns the dir now
    assert not owns_dir(None, "tok-live") and not owns_dir({}, "tok-live")

    s = load_store()
    assert set(DEFAULTS) <= set(s["cfg"]) and isinstance(s["accounts"], dict)
    assert harvest(s) is s                                             # tolerates any dir state

    # A wrong TOKEN_URL only shows up as a live 403 hours later, once a token expires.
    # Claude Code ships the real values, so check ours against them while we can.
    bundle = claude_code_bundle()
    if bundle:
        with open(bundle, encoding="utf-8", errors="ignore") as f:
            src = f.read()
        assert TOKEN_URL in src, f"TOKEN_URL drifted from Claude Code's value — see {bundle}"
        assert CLIENT_ID in src, f"CLIENT_ID drifted from Claude Code's value — see {bundle}"
        for sc in DEFAULT_SCOPES:
            assert f'"{sc}"' in src, f"scope {sc} not in Claude Code's list — see {bundle}"
        print(f"endpoints + scopes match {os.path.basename(bundle)}")
    else:
        print("claude-code bundle not found — endpoint drift unchecked")
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        uitest()
    else:
        main()
