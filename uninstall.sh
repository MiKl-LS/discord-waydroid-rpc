#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DST="/usr/local/bin/waydroid-rpc.py"
CONFIG_DIR="/etc/waydroid-rpc"
SERVICE_DST="/etc/systemd/system/waydroid-rpc-root.service"

# ------------------------------------------------------------------- checks
if [ "$EUID" -ne 0 ]; then
    echo ":: This script must be run as root (sudo)." >&2
    exit 1
fi

# ------------------------------------------------------------- disable services
SUDO_USER="${SUDO_USER:-}"

echo ":: Stopping and disabling system service"
systemctl disable --now waydroid-rpc-root.service 2>/dev/null || true

if [ -n "$SUDO_USER" ]; then
    echo ":: Stopping and disabling user service (as $SUDO_USER)"
    runuser -u "$SUDO_USER" -- systemctl --user disable --now waydroid-rpc-user.service 2>/dev/null || true
fi

# ------------------------------------------------------------ remove files
echo ":: Removing files"
rm -f "$SCRIPT_DST"
rm -f "$SERVICE_DST"

if [ -n "$SUDO_USER" ]; then
    USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    if [ -n "$USER_HOME" ]; then
        rm -f "$USER_HOME/.config/systemd/user/waydroid-rpc-user.service"
        rmdir "$USER_HOME/.config/systemd/user" 2>/dev/null || true
    fi
fi

echo ":: Removing config directory"
rm -rf "$CONFIG_DIR"

# --------------------------------------------------------- systemd reload
echo ":: Reloading systemd"
systemctl daemon-reload

echo ""
echo "=== Uninstalled ==="
echo "  (If the user unit was not removed, run:"
echo "   systemctl --user disable --now waydroid-rpc-user.service"
echo "   rm ~/.config/systemd/user/waydroid-rpc-user.service)"
