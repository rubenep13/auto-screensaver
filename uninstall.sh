#!/bin/bash
# Removes the systemd user service (headless mode). The shell plugin, if
# installed, keeps running the daemon; remove it with
#   omarchy plugin remove rubenep13.auto-screensaver
set -euo pipefail
systemctl --user disable --now auto-screensaver.service 2>/dev/null || true
rm -f ~/.config/systemd/user/auto-screensaver.service
systemctl --user daemon-reload
echo "auto-screensaver systemd service removed"
echo "environment kept in ${XDG_DATA_HOME:-$HOME/.local/share}/auto-screensaver (delete it by hand if unwanted)"
