#!/usr/bin/env bash
set -euo pipefail

ROOT="${BASH_SOURCE[0]%/*}"

# ------------------------------------------------------------------ config
SCRIPT_SRC="$ROOT/waydroid-rpc.py"
CONFIG_SRC="$ROOT/config.yaml"
SERVICE_SRC="$ROOT/etc/systemd/system/waydroid-rpc-root.service"
USER_SERVICE_SRC="$ROOT/etc/systemd/user/waydroid-rpc-user.service"

SCRIPT_DST="/usr/local/bin/waydroid-rpc.py"
CONFIG_DIR="/etc/waydroid-rpc"
CONFIG_DST="$CONFIG_DIR/config.yaml"
SERVICE_DST="/etc/systemd/system/waydroid-rpc-root.service"

# ------------------------------------------------------------------- checks
if [ "$EUID" -ne 0 ]; then
    echo ":: This script must be run as root (sudo)." >&2
    exit 1
fi

for src in "$SCRIPT_SRC" "$CONFIG_SRC" "$SERVICE_SRC"; do
    if [ ! -f "$src" ]; then
        echo ":: Required file not found: $src" >&2
        echo "   Run this script from the project root directory." >&2
        exit 1
    fi
done

# --------------------------------------------------------------- copy files
echo ":: Installing script → $SCRIPT_DST"
cp "$SCRIPT_SRC" "$SCRIPT_DST"
chmod 755 "$SCRIPT_DST"

echo ":: Creating config directory"
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_DST" ]; then
    echo ":: Config already exists at $CONFIG_DST — not overwriting"
else
    echo ":: Copying config → $CONFIG_DST"
    cp "$CONFIG_SRC" "$CONFIG_DST"
fi

echo ":: Copying systemd service (root) → $SERVICE_DST"
cp "$SERVICE_SRC" "$SERVICE_DST"

# User service — try to install for the user who ran sudo
SUDO_USER="${SUDO_USER:-}"
if [ -n "$SUDO_USER" ]; then
    USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    if [ -n "$USER_HOME" ] && [ -d "$USER_HOME" ]; then
        USER_UNIT_DIR="$USER_HOME/.config/systemd/user"
        mkdir -p "$USER_UNIT_DIR"
        echo ":: Copying user service → $USER_UNIT_DIR/"
        cp "$USER_SERVICE_SRC" "$USER_UNIT_DIR/waydroid-rpc-user.service"
        chown -R "$SUDO_USER:" "$USER_HOME/.config/systemd"
    fi
else
    echo ":: SUDO_USER not set — install the user unit manually:"
    echo "   mkdir -p ~/.config/systemd/user"
    echo "   cp etc/systemd/user/waydroid-rpc-user.service ~/.config/systemd/user/"
fi

# --------------------------------------------------------- systemd reload
echo ":: Reloading systemd"
systemctl daemon-reload

# ---------------------------------------------------------------- summary
echo ""
echo "=== Done ==="
echo ""
echo "  1. Edit $CONFIG_DST and fill in:"
echo "       discord_client_id  (from Discord Developer Portal)"
echo ""
echo "  2. Enable both services:"
echo "       sudo systemctl enable --now waydroid-rpc-root.service"
echo "       systemctl --user enable --now waydroid-rpc-user.service"
echo ""
echo "  3. Check status:"
echo "       sudo journalctl -u waydroid-rpc-root.service -f"
echo "       journalctl --user -u waydroid-rpc-user.service -f"
