#!/bin/bash
# Headless mode: run the daemon as a systemd user service instead of inside
# omarchy-shell. Use this on setups without the Omarchy shell plugin, or when
# you prefer systemd's watchdog and journal. With the plugin enabled, its
# Service.qml notices the unit is active and does not start a second daemon.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

bin/auto-screensaver setup
bin/auto-screensaver test

CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/auto-screensaver
mkdir -p "$CONFIG_DIR"
[[ -f $CONFIG_DIR/config.toml ]] || cp config.example.toml "$CONFIG_DIR/config.toml"

mkdir -p ~/.config/systemd/user
sed "s|@DIR@|$PWD|g" systemd/auto-screensaver.service > ~/.config/systemd/user/auto-screensaver.service
systemctl --user daemon-reload
systemctl --user enable auto-screensaver.service
systemctl --user restart auto-screensaver.service
systemctl --user --no-pager status auto-screensaver.service | head -5

echo
echo "Logs:   journalctl --user -u auto-screensaver -f"
echo "Test:   bin/auto-screensaver once"
echo "Config: $CONFIG_DIR/config.toml (reloaded on change)"
