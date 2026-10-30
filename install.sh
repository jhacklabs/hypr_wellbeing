#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="$HOME/.config/systemd/user"

mkdir -p "$BIN_DIR" "$SYSTEMD_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp "$SCRIPT_DIR/hypr_wellbeing.py" "$BIN_DIR/hypr_wellbeing.py"
chmod +x "$BIN_DIR/hypr_wellbeing.py"

cp "$SCRIPT_DIR/hypr-wellbeing.service" "$SYSTEMD_DIR/hypr-wellbeing.service"

systemctl --user daemon-reload
systemctl --user enable --now hypr-wellbeing.service

echo "Installed."
echo "  Binary:  $BIN_DIR/hypr_wellbeing.py"
echo "  Service: systemctl --user status hypr-wellbeing.service"
echo
echo "Try it now:"
echo "  hypr_wellbeing.py status"
echo "  hypr_wellbeing.py report"
echo "  hypr_wellbeing.py limits set discord 45m"
echo "  hypr_wellbeing.py html -o ~/wellbeing.html   # then open it in a browser"
