# Hypr Wellbeing

A "Digital Wellbeing" style app-usage tracker + daily limiter for **Hyprland**
(Arch, Omarchy, or anything else running Hyprland). Pure Python, stdlib only —
no pip installs needed.

## How it works

- The daemon does **not** poll. It opens one long-lived connection to
  Hyprland's own IPC event socket (`.socket2.sock`) and just blocks on it —
  so it wakes up only when you actually switch windows, or once every 15s to
  check limits. That means ~0% CPU at idle and a few MB of RAM, the whole
  time it's running. (If that socket isn't available for some reason — e.g.
  you're not inside Hyprland yet — it automatically falls back to polling
  `hyprctl` every few seconds instead.)
- Each focus change is logged (app `class` + start/end time) into a local
  SQLite database.
- You set optional daily limits per app. When you get close to the limit you
  get a notification; when you hit it, you get a critical notification and
  the app's windows are closed automatically.
- `report` / `html` give you a quick text or browser summary.
- `dashboard` opens a **live full-screen terminal UI** (like `top`, Claude
  Code, or Ollama's CLI) showing the last 7 days as a bar chart and today's
  breakdown as an actual ASCII pie chart, updating once a second.

Nothing leaves your machine — it's all local files under
`~/.local/share/hypr-wellbeing/` and `~/.config/hypr-wellbeing/`.

### Why it's light on RAM/CPU

- Event-driven, not polling — the process is asleep (blocked on a socket
  read) almost all the time instead of waking up and spawning `hyprctl`
  every few seconds.
- SQLite runs in WAL mode with `synchronous=NORMAL`, so writes (which only
  happen on an actual window switch, not on a timer) are cheap.
- No third-party Python packages — no pandas/numpy/GUI toolkit sitting in
  memory, just the stdlib (`sqlite3`, `curses`, `socket`).
- The systemd unit pins it to `Nice=19` / idle I/O priority and caps it at
  `MemoryMax=80M` as a hard ceiling (in practice it sits far below that —
  check for yourself with `systemctl --user status hypr-wellbeing.service`
  or `ps -o rss,cmd -p $(pgrep -f hypr_wellbeing.py)`).

## Install

```bash
chmod +x install.sh
./install.sh
```

This copies the script to `~/.local/bin/hypr_wellbeing.py`, installs a
systemd **user** service, and starts the tracking daemon in the background.
Make sure `~/.local/bin` is on your `PATH` (Omarchy's default shell config
already has it).

If you'd rather start it from your Hyprland config instead of systemd, add
to `~/.config/hypr/hyprland.conf`:

```
exec-once = ~/.local/bin/hypr_wellbeing.py track
```

(then skip/remove the systemd service so you don't run it twice).

## Usage

```bash
# what's focused right now, and today's total for it
hypr_wellbeing.py status

# live full-screen dashboard: last 7 days as bars + today as a pie chart
hypr_wellbeing.py dashboard
# (press q to quit)

# today's usage, as a plain terminal bar chart (non-interactive, good for scripts)
hypr_wellbeing.py report

# last 7 days
hypr_wellbeing.py report --days 7

# a nice HTML report you can open in a browser
hypr_wellbeing.py html -o ~/wellbeing.html

# set a daily limit — durations accept 1800 / 45m / 2h / 1h30m
hypr_wellbeing.py limits set discord 1h
hypr_wellbeing.py limits set steam 2h30m

# see configured limits
hypr_wellbeing.py limits show

# remove a limit
hypr_wellbeing.py limits unset discord
```

## Finding the right app name

Limits are keyed by Hyprland's window `class`, not the friendly app name.
Focus the app and run:

```bash
hyprctl activewindow -j | grep class
```

Use that value (e.g. `discord`, `steam`, `firefox`, `code`) in `limits set`.

## Notes / limitations

- The dashboard needs a terminal at least 90x20 — it'll tell you to resize
  if it's smaller.

- Enforcement is a "close the window" action via `hyprctl dispatch
  closewindow`, not a kernel-level block — a determined user can just reopen
  the app immediately after (this is meant as a nudge/friction tool, like
  Android's Digital Wellbeing, not a parental-control lock).
- No idle detection yet: if you leave an app focused and walk away, that
  time still counts. A natural follow-up would be piping in
  `hypridle`/`swayidle` state to pause tracking when the screen is idle —
  ask if you want that added.
- All data stays local in SQLite; `report --days N` and the HTML report both
  read from the same database, so history builds up automatically the longer
  the daemon runs.

## Uninstall

```bash
systemctl --user disable --now hypr-wellbeing.service
rm ~/.config/systemd/user/hypr-wellbeing.service
rm ~/.local/bin/hypr_wellbeing.py
# optional — deletes your usage history and limits:
rm -rf ~/.local/share/hypr-wellbeing ~/.config/hypr-wellbeing
```
