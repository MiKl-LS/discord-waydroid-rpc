# Waydroid Discord Rich Presence

Show the currently running Waydroid Android app as Discord Rich Presence,
similar to how BlueStacks shows game activity.  Uses **two systemd
services** — a root-level service for foreground detection and a
user-level service for Discord IPC — communicating via a shared file.

Discord clears the Rich Presence the moment the IPC connection drops, so
the user-level service keeps a persistent connection alive.

## Architecture

```
┌─ systemd system service (root) ───────────────────────┐
│ waydroid-rpc-root.service                              │
│ waydroid-rpc.py --root-daemon                          │
│   └─ poll loop (10s)                                   │
│        ├─ waydroid status              → session check  │
│        ├─ waydroid shell dumpsys window → pkg           │
│        └─ write /tmp/waydroid-rpc/foreground.json      │
└────────────────────────────────────────────────────────┘
                           │
              foreground.json (raw package)
                           │
┌─ systemd --user service (desktop user) ────────────────┐
│ waydroid-rpc-user.service                               │
│ waydroid-rpc.py --user-daemon                           │
│   └─ poll loop (10s)                                    │
│        ├─ read foreground.json                          │
│        ├─ waydroid app list               → name cache  │
│        ├─ resolve display name                          │
│        └─ Discord IPC (persistent connection)           │
│             └─ /run/user/<uid>/discord-ipc-0            │
└────────────────────────────────────────────────────────┘
```

-   **Root daemon** — runs as root, checks `waydroid status` first, then
    polls `waydroid shell dumpsys window` (last `mCurrentFocus`/`mFocusedApp`
    entry).  Ignores `com.android.launcher3` (treated as idle).  Writes raw
    package name to shared file.  No privilege dropping needed.
-   **User daemon** — runs in your user session, reads the foreground file,
    runs `waydroid app list` directly (no root needed), resolves display
    names, keeps a persistent Discord IPC connection, and updates Rich
    Presence on transitions.  Uses the package name as the state/secondary
    line.
-   **App-name cache** in `~/.cache/waydroid-rpc/apps.json`, refreshed
    periodically from `waydroid app list`.

## Prerequisites

-   Python 3.8+
-   Waydroid installed and a container running
-   Discord
-   `pip install pypresence pyyaml`

## Discord Application Setup

1.  Go to https://discord.com/developers/applications and click **New
    Application**.
2.  Give it a name (e.g. "Waydroid").
3.  In the left sidebar click **Rich Presence**.
4.  Under **Art Assets** upload at least one image — this will be the
    `large_image` shown in the Rich Presence card.
    -   The default asset key configured in `config.yaml` is `waydroid`.
        Upload an image named `waydroid` (the key is the image name, without
        the file extension) or change `default_icon_key` to match your upload.
5.  Copy the **Client ID** from the **General Information** page
    (top-left under **Application ID**).

## Permission Model

Two services handle different parts:

| Component | Runs as | Purpose |
|-----------|---------|---------|
| `waydroid-rpc.py --root-daemon` | **root** (system service) | Checks `waydroid status`, runs `dumpsys window`, writes raw package to foreground.json |
| `waydroid-rpc.py --user-daemon` | **you** (`--user` unit) | Runs `waydroid app list`, resolves names, connects to Discord IPC, updates Rich Presence |

The root daemon only runs `waydroid shell dumpsys window` (which requires root).  The user
daemon runs as your desktop user and calls `waydroid app list` directly,
inheriting `XDG_RUNTIME_DIR` from your login session naturally for
Discord IPC access.

## Installation

```bash
# 1. Copy the script
sudo cp waydroid-rpc.py /usr/local/bin/waydroid-rpc.py
sudo chmod +x /usr/local/bin/waydroid-rpc.py

# 2. Create config directory
sudo mkdir -p /etc/waydroid-rpc

# 3. Copy config and edit it
sudo cp config.yaml /etc/waydroid-rpc/config.yaml
sudo nano /etc/waydroid-rpc/config.yaml   # fill in discord_client_id

# 4. Copy systemd units
sudo cp etc/systemd/system/waydroid-rpc-root.service /etc/systemd/system/
mkdir -p ~/.config/systemd/user
cp etc/systemd/user/waydroid-rpc-user.service ~/.config/systemd/user/

# 5. Reload, enable, and start both services
sudo systemctl daemon-reload
sudo systemctl enable --now waydroid-rpc-root.service          # root daemon
systemctl --user daemon-reload
systemctl --user enable --now waydroid-rpc-user.service        # user daemon
```

## Verification

```bash
# Root daemon status
sudo systemctl status waydroid-rpc-root.service
sudo journalctl -u waydroid-rpc-root.service -f

# User daemon status
systemctl --user status waydroid-rpc-user.service
journalctl --user -u waydroid-rpc-user.service -f

# Check the shared foreground file
cat /tmp/waydroid-rpc/foreground.json
```

## Configuration

All options live in `/etc/waydroid-rpc/config.yaml`:

| Key | Default | Used by | Description |
|------|---------|---------|-------------|
| `discord_client_id` | — | user | Discord application Client ID (**required**) |
| `idle_behavior` | `clear` | user | `clear` or `show_idle` |
| `default_icon_key` | `waydroid` | user | Discord art-asset key for the large image |
| `app_list_cache_ttl_hours` | `24` | user | How often to re-fetch `waydroid app list` |
| `poll_interval_seconds` | `10` | both | Seconds between checks |
| `paths.foreground_file` | `/tmp/waydroid-rpc/foreground.json` | both | Shared foreground state |
| `paths.cache_file` | `~/.cache/waydroid-rpc/apps.json` | user | App-name cache |


## Tuning the Check Interval

Edit `/etc/waydroid-rpc/config.yaml` and change `poll_interval_seconds`, then
restart both services:

```bash
sudo systemctl restart waydroid-rpc-root.service
systemctl --user restart waydroid-rpc-user.service
```

## Debugging

```bash
cat /tmp/waydroid-rpc/foreground.json
sudo journalctl -u waydroid-rpc-root.service -f
```

## Uninstall

```bash
systemctl --user disable --now waydroid-rpc-user.service
sudo systemctl disable --now waydroid-rpc-root.service
sudo rm /usr/local/bin/waydroid-rpc.py
sudo rm -rf /etc/waydroid-rpc
sudo rm /etc/systemd/system/waydroid-rpc-root.service
rm ~/.config/systemd/user/waydroid-rpc-user.service
sudo systemctl daemon-reload
systemctl --user daemon-reload
```

## Logging

Output from the root daemon is captured by the system journal; output from
the user daemon by the per-user journal.

```bash
sudo journalctl -u waydroid-rpc-root.service -f
journalctl --user -u waydroid-rpc-user.service -f
```

Both services use `Restart=on-failure` with a 5-second delay.

## Files

| Path | Purpose |
|------|---------|
| `/usr/local/bin/waydroid-rpc.py` | Combined script (root & user modes) |
| `/etc/waydroid-rpc/config.yaml` | Shared configuration |
| `/tmp/waydroid-rpc/foreground.json` | Shared foreground state (root writes raw package, user reads and resolves) |
| `~/.cache/waydroid-rpc/apps.json` | Cached `waydroid app list` output |
| `/etc/systemd/system/waydroid-rpc-root.service` | Root daemon (system service) |
| `~/.config/systemd/user/waydroid-rpc-user.service` | User daemon (`--user` unit) |
