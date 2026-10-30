#!/usr/bin/env python3
"""
hypr-wellbeing — a "Digital Wellbeing" style app-usage tracker + limiter for Hyprland.

Subcommands:
  track                 Run the tracking daemon (call this from Hyprland autostart / systemd)
  dashboard              Live full-screen terminal UI: weekly bars + today's pie chart
  report                 Print today's (or N days') usage as a table + ascii bars
  html [-o FILE]         Write an HTML usage report (bar chart, no internet needed)
  status                 Show what's focused right now + today's total for it
  limits show            Print the configured per-app daily limits
  limits set APP SECS    Set (or update) a daily limit in seconds for an app (by wm class)
  limits set APP 45m     Same, accepts 30s / 45m / 2h / 1h30m style durations
  limits unset APP       Remove a limit

Storage:
  ~/.local/share/hypr-wellbeing/usage.db      (sqlite)
  ~/.config/hypr-wellbeing/limits.json        (limit config)

Requires: hyprctl (Hyprland), notify-send (libnotify) — both are already on any
Omarchy / stock Hyprland install. No third-party python packages needed
(the dashboard uses the `curses` module, which ships with Python's stdlib).

Design note on resource use: the daemon does NOT poll. It opens a single
long-lived connection to Hyprland's own event socket and blocks on it, so it
uses ~0% CPU and only a few MB of RAM while idle — it only wakes up when you
actually switch windows, or once every LIMIT_CHECK_SECONDS to check limits.
If that socket isn't available for some reason it falls back to lightweight
polling automatically.
"""

import argparse
import json
import math
import os
import re
import shutil
import socket as socket_module
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "hypr-wellbeing"
CONFIG_DIR = Path.home() / ".config" / "hypr-wellbeing"
DB_PATH = DATA_DIR / "usage.db"
LIMITS_PATH = CONFIG_DIR / "limits.json"

FALLBACK_POLL_SECONDS = 5   # only used if the Hyprland event socket isn't available
LIMIT_CHECK_SECONDS = 15    # how often we re-check limits against accumulated time
WARN_FRACTION = 0.9         # send a "5 min left" style warning at 90% of the limit

WEEKDAY_LABELS = {
    0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu",
    4: "Fri", 5: "Sat", 6: "Sun",
}


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def get_db():
    ensure_dirs()
    con = sqlite3.connect(DB_PATH)
    # WAL + NORMAL sync keeps this daemon light on disk I/O (and therefore RAM
    # spent on page cache churn) without risking corruption.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app TEXT NOT NULL,
            title TEXT,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_app ON sessions(app)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts)")
    con.commit()
    return con


def load_limits():
    if not LIMITS_PATH.exists():
        return {}
    try:
        return json.loads(LIMITS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_limits(limits):
    ensure_dirs()
    LIMITS_PATH.write_text(json.dumps(limits, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# hyprland helpers
# --------------------------------------------------------------------------- #

def require_hyprctl():
    if shutil.which("hyprctl") is None:
        sys.exit("hyprctl not found — this tool only works inside a running Hyprland session.")


def get_active_window():
    """One-shot query via hyprctl. Used only at startup and as a polling fallback."""
    try:
        out = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None, None
    if out.returncode != 0 or not out.stdout.strip():
        return None, None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None, None
    app = data.get("class") or data.get("initialClass") or "unknown"
    title = data.get("title", "")
    return app, title


def hypr_event_socket_path():
    """Path to Hyprland's IPC event socket (.socket2.sock), or None if unavailable."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        return None
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    path = Path(runtime_dir) / "hypr" / sig / ".socket2.sock"
    return path if path.exists() else None


def notify(title, body, urgency="normal"):
    if shutil.which("notify-send") is None:
        print(f"[notify] {title}: {body}")
        return
    subprocess.run(
        ["notify-send", "-u", urgency, "-a", "Hypr Wellbeing", title, body],
        check=False,
    )


def close_app(app_class):
    """Best-effort: ask Hyprland to close every window of this class."""
    subprocess.run(
        ["hyprctl", "dispatch", "closewindow", f"class:^({re.escape(app_class)})$"],
        check=False,
    )


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #

def day_bounds(d):
    start = datetime.combine(d, datetime.min.time())
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def today_bounds():
    return day_bounds(date.today())


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_short(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}"
    return f"{m}m"


def parse_duration(text):
    """Accepts plain seconds ('1800') or shorthand like '45m', '2h', '1h30m'."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    total = 0
    for value, unit in re.findall(r"(\d+)\s*([hms])", text):
        value = int(value)
        total += value * {"h": 3600, "m": 60, "s": 1}[unit]
    if total == 0:
        raise ValueError(f"couldn't parse duration: {text!r}")
    return total


def overlap_seconds(rows, start_ts, end_ts, now):
    """Sum overlap of (start_ts_row, end_ts_row_or_None) rows with [start_ts, end_ts)."""
    total = 0
    for s, e in rows:
        e = e if e is not None else now
        s2, e2 = max(s, start_ts), min(e, end_ts)
        if e2 > s2:
            total += e2 - s2
    return total


def today_totals(con, include_open_session=True):
    """Return {app: seconds_today}, including time-in-progress for the currently open session."""
    start_ts, end_ts = today_bounds()
    totals = {}

    rows = con.execute(
        """SELECT app, start_ts, end_ts FROM sessions
           WHERE end_ts IS NOT NULL AND start_ts < ? AND end_ts > ?""",
        (end_ts, start_ts),
    ).fetchall()
    for app, s, e in rows:
        s = max(s, start_ts)
        e = min(e, end_ts)
        totals[app] = totals.get(app, 0) + max(0, e - s)

    if include_open_session:
        row = con.execute(
            "SELECT app, start_ts FROM sessions WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            app, s = row
            s = max(s, start_ts)
            now = int(time.time())
            totals[app] = totals.get(app, 0) + max(0, now - s)

    return totals


def daily_totals(con, days=7):
    """Return [(date, total_seconds_that_day), ...] oldest → newest, `days` long."""
    now = int(time.time())
    out = []
    for i in range(days - 1, -1, -1):
        d = date.today() - timedelta(days=i)
        start_ts, end_ts = day_bounds(d)
        rows = con.execute(
            """SELECT start_ts, end_ts FROM sessions
               WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?)""",
            (end_ts, start_ts),
        ).fetchall()
        out.append((d, overlap_seconds(rows, start_ts, end_ts, now)))
    return out


def current_session_info(con):
    """(app, title, elapsed_seconds) for the open session, or (None, None, 0)."""
    row = con.execute(
        "SELECT app, title, start_ts FROM sessions WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None, None, 0
    app, title, start_ts = row
    return app, title, max(0, int(time.time()) - start_ts)


# --------------------------------------------------------------------------- #
# daemon (event-driven, falls back to polling)
# --------------------------------------------------------------------------- #

class SessionTracker:
    """Wraps the open/close-session bookkeeping so both backends share it."""

    def __init__(self, con):
        self.con = con
        self.current_app = None
        self.current_title = None

    def close_current(self):
        self.con.execute(
            "UPDATE sessions SET end_ts=? WHERE end_ts IS NULL", (int(time.time()),)
        )
        self.con.commit()

    def open_new(self, app, title):
        self.con.execute(
            "INSERT INTO sessions (app, title, start_ts, end_ts) VALUES (?, ?, ?, NULL)",
            (app, title, int(time.time())),
        )
        self.con.commit()

    def update(self, app, title):
        if app == self.current_app:
            return
        if self.current_app is not None:
            self.close_current()
        if app is not None:
            self.open_new(app, title)
        self.current_app, self.current_title = app, title


def check_limits(con, warned_apps_today, killed_apps_today):
    limits = load_limits()
    if not limits:
        return
    totals = today_totals(con)
    for app, limit_seconds in limits.items():
        used = totals.get(app, 0)
        if used == 0:
            continue
        if used >= limit_seconds and app not in killed_apps_today:
            killed_apps_today.add(app)
            notify(
                f"Time's up: {app}",
                f"You've hit your {fmt_duration(limit_seconds)} daily limit for {app}.",
                urgency="critical",
            )
            close_app(app)
        elif used >= limit_seconds * WARN_FRACTION and app not in warned_apps_today:
            warned_apps_today.add(app)
            remaining = limit_seconds - used
            notify(
                f"Almost at your limit: {app}",
                f"{fmt_duration(remaining)} left today before {app} gets closed.",
                urgency="normal",
            )


def cmd_track(args):
    require_hyprctl()
    con = get_db()
    tracker = SessionTracker(con)

    warned_apps_today, killed_apps_today = set(), set()
    last_reset_day = date.today()
    last_limit_check = 0.0

    def maybe_reset_day():
        nonlocal last_reset_day
        if date.today() != last_reset_day:
            warned_apps_today.clear()
            killed_apps_today.clear()
            last_reset_day = date.today()

    def maybe_check_limits():
        nonlocal last_limit_check
        now = time.time()
        if now - last_limit_check >= LIMIT_CHECK_SECONDS:
            last_limit_check = now
            maybe_reset_day()
            check_limits(con, warned_apps_today, killed_apps_today)

    sock_path = hypr_event_socket_path()
    if sock_path is None and not args.force_poll:
        print("note: Hyprland event socket not found — falling back to polling mode.")

    try:
        if sock_path is not None and not args.force_poll:
            print(f"tracking via Hyprland event socket ({sock_path})… db at {DB_PATH}")
            _track_via_events(tracker, maybe_check_limits)
        else:
            print(f"tracking via polling every {args.poll_interval}s… db at {DB_PATH}")
            _track_via_polling(tracker, maybe_check_limits, args.poll_interval)
    except KeyboardInterrupt:
        tracker.close_current()
        print("stopped.")


def _track_via_polling(tracker, maybe_check_limits, poll_interval):
    app, title = get_active_window()
    tracker.update(app, title)
    while True:
        time.sleep(poll_interval)
        app, title = get_active_window()
        tracker.update(app, title)
        maybe_check_limits()


def _connect_event_socket():
    """Block (with backoff) until Hyprland's event socket is available, then connect."""
    delay = 1
    while True:
        path = hypr_event_socket_path()
        if path is not None:
            try:
                sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
                sock.connect(str(path))
                return sock
            except OSError:
                pass
        time.sleep(delay)
        delay = min(delay * 2, 30)


def _track_via_events(tracker, maybe_check_limits):
    import select

    app, title = get_active_window()
    tracker.update(app, title)

    sock = _connect_event_socket()
    buf = b""
    try:
        while True:
            readable, _, _ = select.select([sock], [], [], LIMIT_CHECK_SECONDS)
            if readable:
                try:
                    data = sock.recv(4096)
                except OSError:
                    data = b""
                if not data:
                    tracker.close_current()
                    tracker.current_app = None
                    sock.close()
                    sock = _connect_event_socket()
                    buf = b""
                    continue
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode(errors="ignore")
                    if text.startswith("activewindow>>"):
                        payload = text[len("activewindow>>"):]
                        cls, _, ttl = payload.partition(",")
                        cls = cls.strip() or None
                        tracker.update(cls, ttl)
            maybe_check_limits()
    finally:
        try:
            sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# plain-text reporting (report / html / status)
# --------------------------------------------------------------------------- #

def cmd_status(args):
    con = get_db()
    app, title, elapsed = current_session_info(con)
    totals = today_totals(con)
    if app:
        print(f"Focused: {app}  ({title})  — this session: {fmt_duration(elapsed)}")
        print(f"Today's total for {app}: {fmt_duration(totals.get(app, 0))}")
    else:
        print("Nothing focused (or not running inside Hyprland).")
    limits = load_limits()
    if app in limits:
        used = totals.get(app, 0)
        print(f"Limit: {fmt_duration(limits[app])}  |  remaining: {fmt_duration(max(0, limits[app]-used))}")


def _range_totals(con, days):
    start_ts, _ = today_bounds()
    start_ts -= (days - 1) * 86400
    _, end_ts = today_bounds()
    now = int(time.time())
    totals = {}
    rows = con.execute(
        """SELECT app, start_ts, end_ts FROM sessions
           WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?)""",
        (end_ts, start_ts),
    ).fetchall()
    for app, s, e in rows:
        e = e if e is not None else now
        s2, e2 = max(s, start_ts), min(e, end_ts)
        if e2 > s2:
            totals[app] = totals.get(app, 0) + (e2 - s2)
    return totals


def cmd_report(args):
    con = get_db()
    totals = _range_totals(con, args.days)

    if not totals:
        print("No usage recorded yet." if args.days == 1 else f"No usage recorded in the last {args.days} days.")
        return

    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    grand_total = sum(totals.values())
    max_seconds = ordered[0][1]
    bar_width = 30

    label = "Today" if args.days == 1 else f"Last {args.days} days"
    print(f"\n{label} — total screen time: {fmt_duration(grand_total)}\n")
    for app, secs in ordered:
        bar_len = int(bar_width * secs / max_seconds) if max_seconds else 0
        bar = "█" * bar_len
        print(f"{app:<20} {fmt_duration(secs):>10}  {bar}")

    limits = load_limits()
    if limits:
        print("\nLimits:")
        for app, lim in limits.items():
            used = totals.get(app, 0)
            flag = "  ⚠ OVER" if used >= lim else ""
            print(f"  {app:<20} {fmt_duration(used):>10} / {fmt_duration(lim)}{flag}")
    print()


def cmd_html(args):
    con = get_db()
    totals = _range_totals(con, args.days)
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    grand_total = sum(totals.values())
    max_seconds = ordered[0][1] if ordered else 1
    limits = load_limits()

    palette = ["#7c9cff", "#ff9c7c", "#7cffb0", "#ffe27c", "#d17cff",
               "#7cf0ff", "#ff7ca8", "#c4ff7c"]

    rows_html = []
    for i, (app, secs) in enumerate(ordered):
        pct = (secs / max_seconds) * 100 if max_seconds else 0
        color = palette[i % len(palette)]
        over = ""
        if app in limits and secs >= limits[app]:
            over = ' <span class="over">OVER LIMIT</span>'
        limit_note = f" / limit {fmt_duration(limits[app])}" if app in limits else ""
        rows_html.append(f"""
        <div class="row">
          <div class="label">{app}{over}</div>
          <div class="track"><div class="fill" style="width:{pct:.1f}%;background:{color}"></div></div>
          <div class="value">{fmt_duration(secs)}{limit_note}</div>
        </div>""")

    label = "Today" if args.days == 1 else f"Last {args.days} days"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Hypr Wellbeing — {label}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; padding: 40px 24px; min-height: 100vh;
    background: #14161c; color: #e8e8ee;
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex; justify-content: center;
  }}
  .card {{ width: 100%; max-width: 720px; }}
  h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 4px; }}
  .total {{ color: #9aa0b4; margin-bottom: 28px; font-size: 15px; }}
  .row {{ display: grid; grid-template-columns: 130px 1fr 150px; align-items: center;
          gap: 14px; margin-bottom: 14px; }}
  .label {{ font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .track {{ background: #23262f; border-radius: 8px; height: 14px; overflow: hidden; }}
  .fill {{ height: 100%; border-radius: 8px; }}
  .value {{ font-size: 13px; color: #b7bccb; text-align: right; }}
  .over {{ color: #ff6b6b; font-size: 11px; font-weight: 600; margin-left: 6px; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{label}</h1>
    <div class="total">Total screen time: {fmt_duration(grand_total)}</div>
    {"".join(rows_html) if rows_html else "<p>No usage recorded yet.</p>"}
  </div>
</body>
</html>"""

    out_path = Path(args.output).expanduser()
    out_path.write_text(html)
    print(f"wrote {out_path}")


# --------------------------------------------------------------------------- #
# limits CLI
# --------------------------------------------------------------------------- #

def cmd_limits_show(args):
    limits = load_limits()
    if not limits:
        print("No limits set. Add one with: hypr_wellbeing.py limits set <app-class> <duration>")
        return
    print("Daily limits (match hyprctl 'class'):")
    for app, secs in limits.items():
        print(f"  {app:<20} {fmt_duration(secs)}")


def cmd_limits_set(args):
    limits = load_limits()
    seconds = parse_duration(args.duration)
    limits[args.app] = seconds
    save_limits(limits)
    print(f"Set limit: {args.app} → {fmt_duration(seconds)}/day")


def cmd_limits_unset(args):
    limits = load_limits()
    if args.app in limits:
        del limits[args.app]
        save_limits(limits)
        print(f"Removed limit for {args.app}")
    else:
        print(f"No limit was set for {args.app}")


# --------------------------------------------------------------------------- #
# live terminal dashboard (curses) — weekly bars + today's pie chart
# --------------------------------------------------------------------------- #

EIGHTHS = " ▁▂▃▄▅▆▇█"  # index 0..8 -> empty..full eighth-block glyphs
PIE_PALETTE_COLORS = None  # filled in once curses is available (see _dashboard_main)


def _bar_column(total, max_total, height):
    """Return `height` glyphs (top→bottom) for one vertical bar, 8ths-of-a-cell precision."""
    frac = (total / max_total) if max_total > 0 else 0.0
    filled_eighths = round(frac * height * 8)
    glyphs = []
    for row in range(height):
        eighths_below = (height - 1 - row) * 8
        seg = max(0, min(8, filled_eighths - eighths_below))
        glyphs.append(EIGHTHS[seg])
    return glyphs


def _pie_cells(apps_sorted, radius_x, radius_y):
    """Yield (dx, dy, app) for every filled cell of a text pie chart."""
    total = sum(v for _, v in apps_sorted)
    if total <= 0:
        return
    boundaries = []
    cum = 0.0
    for app, val in apps_sorted:
        start = cum
        cum += val / total
        boundaries.append((app, start, cum))

    for dy in range(-radius_y, radius_y + 1):
        ny = dy / radius_y
        for dx in range(-radius_x, radius_x + 1):
            nx = dx / radius_x
            if nx * nx + ny * ny > 1.0:
                continue
            angle = math.atan2(-ny, nx)
            if angle < 0:
                angle += 2 * math.pi
            frac = angle / (2 * math.pi)
            chosen = boundaries[-1][0]
            for app, s, e in boundaries:
                if s <= frac < e:
                    chosen = app
                    break
            yield dx, dy, chosen


def cmd_dashboard(args):
    try:
        import curses
    except ImportError:
        sys.exit("The 'curses' module isn't available in this Python build.")
    curses.wrapper(_dashboard_main)


def _dashboard_main(stdscr):
    import curses

    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    color_ids = [curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                 curses.COLOR_MAGENTA, curses.COLOR_BLUE, curses.COLOR_RED,
                 curses.COLOR_WHITE]
    for i, c in enumerate(color_ids):
        curses.init_pair(i + 1, c, -1)
    curses.init_pair(90, curses.COLOR_WHITE, -1)   # today's bar / accents
    curses.init_pair(91, curses.COLOR_BLACK, -1)   # dim

    con = get_db()
    stdscr.nodelay(True)
    stdscr.timeout(1000)  # wake once a second to redraw the live timer

    while True:
        ch = stdscr.getch()
        if ch in (ord("q"), 27, ord("Q")):
            return
        _draw_dashboard(stdscr, con, color_ids)


def _draw_dashboard(stdscr, con, color_ids):
    import curses

    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 20 or w < 90:
        msg = "Terminal too small — resize to at least 90x20"
        stdscr.addstr(h // 2, max(0, (w - len(msg)) // 2), msg)
        stdscr.refresh()
        return

    def safe_addstr(y, x, text, attr=0):
        # Clip defensively so a too-long string can never spill past the last
        # column — on some terminals that wraps into the row below instead of
        # just failing, which looks like stray characters in the wrong place.
        if y < 0 or y >= h or x < 0 or x >= w - 1:
            return
        text = text[: w - 1 - x]
        if not text:
            return
        try:
            stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    now_str = datetime.now().strftime("%H:%M:%S — %Y-%m-%d")
    title = "🧠  Hypr Wellbeing — Live Dashboard"
    safe_addstr(0, 2, title, curses.A_BOLD)
    safe_addstr(0, max(0, w - len(now_str) - 2), now_str)

    app, wtitle, elapsed = current_session_info(con)
    if app:
        line = f"Now: {app}   ({fmt_duration(elapsed)} this session)"
    else:
        line = "Nothing focused right now"
    safe_addstr(1, 2, line, curses.color_pair(90) | curses.A_BOLD)

    # ---------------- layout constants ----------------
    chart_h = 8            # rows used by the weekly bars
    col_w = 7               # width of each day column
    header_row = 3           # "Last 7 days:" / "Today:" row
    bars_top = header_row + 1
    week_left = 2
    week_right = week_left + 7 * col_w

    rx, ry = 8, 4             # pie radius (x roughly 2x y — terminal cells are ~2x taller than wide)
    pie_x0 = week_right + 4 + rx
    pie_y0 = bars_top + ry    # vertically align pie center with the bars

    # ---------------- weekly bar chart (left half) ----------------
    week = daily_totals(con, days=7)
    max_total = max((t for _, t in week), default=0)

    safe_addstr(header_row, week_left, "Last 7 days:", curses.A_BOLD)
    columns = [_bar_column(t, max_total, chart_h) for _, t in week]
    for row in range(chart_h):
        y = bars_top + row
        for i, (_, total) in enumerate(week):
            glyph = columns[i][row]
            is_today = i == len(week) - 1
            attr = (curses.color_pair(90) | curses.A_BOLD) if is_today else curses.color_pair(1)
            safe_addstr(y, week_left + i * col_w, glyph.center(col_w - 1), attr)
    label_y = bars_top + chart_h
    for i, (d, total) in enumerate(week):
        is_today = i == len(week) - 1
        day_name = "Today" if is_today else WEEKDAY_LABELS[d.weekday()]
        attr = curses.A_BOLD if is_today else 0
        safe_addstr(label_y, week_left + i * col_w, day_name.center(col_w - 1), attr)
        safe_addstr(label_y + 1, week_left + i * col_w, fmt_short(total).center(col_w - 1))

    # ---------------- today's pie chart + legend (right half) -----
    totals = today_totals(con)
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    color_of_app = {app_: color_ids[i % len(color_ids)] for i, (app_, _) in enumerate(ordered)}

    safe_addstr(header_row, pie_x0 - rx, "Today:", curses.A_BOLD)
    if not ordered:
        safe_addstr(pie_y0, pie_x0 - 8, "No usage yet")
    else:
        for dx, dy, app_ in _pie_cells(ordered, rx, ry):
            color_id = color_of_app.get(app_, 7)
            safe_addstr(pie_y0 + dy, pie_x0 + dx, "█", curses.color_pair(color_id))

        legend_x = pie_x0 + rx + 3
        grand_total = sum(totals.values()) or 1
        limits = load_limits()
        for i, (app_, secs) in enumerate(ordered[:10]):
            pct = 100 * secs / grand_total
            color_id = color_of_app.get(app_, 7)
            over = " !" if app_ in limits and secs >= limits[app_] else ""
            text = f"{app_[:10]:<10} {fmt_short(secs):>5} {pct:3.0f}%{over}"
            safe_addstr(bars_top + i, legend_x, "●", curses.color_pair(color_id) | curses.A_BOLD)
            safe_addstr(bars_top + i, legend_x + 2, text)

    footer_y = h - 2
    grand = sum(totals.values())
    safe_addstr(footer_y, 2, f"Today's total: {fmt_duration(grand)}", curses.A_BOLD)
    safe_addstr(footer_y + 1, 2, "q to quit   •   live, updates every second", curses.A_DIM)

    stdscr.refresh()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(prog="hypr_wellbeing", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    tp = sub.add_parser("track", help="Run the tracking daemon")
    tp.add_argument("--poll-interval", type=int, default=FALLBACK_POLL_SECONDS,
                     help="Seconds between polls, only used if event-socket tracking is unavailable")
    tp.add_argument("--force-poll", action="store_true",
                     help="Use polling even if the Hyprland event socket is available (debugging)")
    tp.set_defaults(func=cmd_track)

    dp = sub.add_parser("dashboard", help="Live full-screen dashboard (weekly bars + today's pie chart)")
    dp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("status", help="Show the currently focused app and its usage today")
    sp.set_defaults(func=cmd_status)

    rp = sub.add_parser("report", help="Print a usage report")
    rp.add_argument("--days", type=int, default=1, help="How many days back (default: 1 = today)")
    rp.set_defaults(func=cmd_report)

    hp = sub.add_parser("html", help="Write an HTML usage report")
    hp.add_argument("-o", "--output", default="~/hypr-wellbeing-report.html")
    hp.add_argument("--days", type=int, default=1)
    hp.set_defaults(func=cmd_html)

    lp = sub.add_parser("limits", help="Manage per-app daily limits")
    lsub = lp.add_subparsers(dest="limits_command", required=True)

    lshow = lsub.add_parser("show", help="Show configured limits")
    lshow.set_defaults(func=cmd_limits_show)

    lset = lsub.add_parser("set", help="Set a daily limit, e.g. 'discord 1h30m'")
    lset.add_argument("app", help="wm class as reported by 'hyprctl activewindow -j'")
    lset.add_argument("duration", help="e.g. 1800, 45m, 2h, 1h30m")
    lset.set_defaults(func=cmd_limits_set)

    lunset = lsub.add_parser("unset", help="Remove a limit")
    lunset.add_argument("app")
    lunset.set_defaults(func=cmd_limits_unset)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
