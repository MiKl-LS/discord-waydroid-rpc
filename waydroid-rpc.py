#!/usr/bin/env python3
"""
Waydroid Discord Rich Presence — split daemon architecture.

Two systemd services communicate via a shared JSON file:
  1. **Root daemon** (``--root-daemon``) — polls ``waydroid shell dumpsys
     activity``, writes the raw package name to ``foreground.json``.
  2. **User daemon** (``--user-daemon``) — reads ``foreground.json``,
     resolves display names via ``waydroid app list``, and keeps a
     persistent Discord IPC connection to update Rich Presence.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:
    yaml = None

try:
    from pypresence import Presence
    from pypresence.exceptions import DiscordNotFound, PyPresenceException
except ImportError:
    Presence = None
    DiscordNotFound = PyPresenceException = Exception

logger = logging.getLogger("waydroid-rpc")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = "/etc/waydroid-rpc/config.yaml"
FOREGROUND_DIR = Path("/tmp/waydroid-rpc")

APP_LIST_CACHE_TTL_HOURS = 24
DEFAULT_POLL_INTERVAL_SECONDS = 10

# Sentinel for the launcher/home-screen state
STATE_IDLE = "__idle__"
IGNORED_PACKAGES = {"com.android.launcher3"}

# ---------------------------------------------------------------------------
# I/O helpers  (atomic writes via tempfile + rename)
# ---------------------------------------------------------------------------


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: Any) -> None:
    _ensure_parent(path)
    tmp = path.with_suffix(".tmp." + os.urandom(4).hex())
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        tmp.rename(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Optional[Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: str) -> Dict[str, Any]:
    if not Path(path).exists():
        logger.error("Config file not found: %s", path)
        sys.exit(1)

    if yaml is None:
        logger.error("PyYAML is not installed")
        sys.exit(1)

    try:
        with open(path) as f:
            config: Dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        logger.error("Failed to parse config %s: %s", path, e)
        sys.exit(1)

    if not config.get("discord_client_id"):
        logger.error("discord_client_id is required in config")
        sys.exit(1)

    config.setdefault("idle_behavior", "clear")
    config.setdefault("default_icon_key", "waydroid")
    config.setdefault("app_list_cache_ttl_hours", APP_LIST_CACHE_TTL_HOURS)
    config.setdefault("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)

    p = config.setdefault("paths", {})
    p.setdefault("cache_file", str(Path.home() / ".cache" / "waydroid-rpc" / "apps.json"))
    p.setdefault("foreground_file", str(FOREGROUND_DIR / "foreground.json"))
    # Resolve ~ in any path value
    for k in list(p):
        p[k] = str(Path(p[k]).expanduser())
    config["paths"] = p

    return config


# ---------------------------------------------------------------------------
# Waydroid command helpers
# ---------------------------------------------------------------------------

WAYDROID_BIN = "/usr/bin/waydroid"


def _run_waydroid(args, timeout=15):
    try:
        r = subprocess.run(
            [WAYDROID_BIN] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning("waydroid binary not found at %s", WAYDROID_BIN)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("waydroid %s timed out after %ss", " ".join(args), timeout)
        return None

    if r.returncode != 0:
        logger.debug("waydroid %s exited %d: %s", " ".join(args), r.returncode, r.stderr.strip())
        return None
    return r.stdout


def _session_active() -> bool:
    """Check ``waydroid status`` for ``Session: RUNNING``."""
    output = _run_waydroid(["status"])
    if output is None:
        return False
    return "RUNNING" in output


def get_foreground_package() -> Optional[str]:
    """Return package name, ``STATE_IDLE``, or ``None`` if waydroid is unreachable.

    Uses ``dumpsys window`` — the last ``mCurrentFocus`` or
    ``mFocusedApp`` entry is the current focused window.
    """
    if not _session_active():
        logger.debug("Waydroid session not active")
        return None

    output = _run_waydroid(["shell", "dumpsys", "window"])
    if output is None:
        return None

    last_pkg = None
    for line in output.splitlines():
        if "mCurrentFocus=Window{" in line:
            start = line.index("Window{") + 7
            end = line.rindex("}")
            inner = line[start:end]
            parts = inner.rsplit(maxsplit=1)
            if len(parts) >= 2:
                last_pkg = parts[-1].split("/")[0]
        elif "mFocusedApp=ActivityRecord{" in line:
            m = re.search(r"ActivityRecord\{[^}]*\s+(\S+?)/", line)
            if m:
                last_pkg = m.group(1)

    return last_pkg if last_pkg is not None and last_pkg not in IGNORED_PACKAGES else STATE_IDLE


# ---------------------------------------------------------------------------
# waydroid app list parser
# ---------------------------------------------------------------------------

def parse_app_list_output(text: str) -> Dict[str, str]:
    """Parse ``waydroid app list`` output into {packageName: display_name}.

    The real output has no blank line between records — each record begins
    with ``Name:``.  We split on that boundary.
    """
    apps: Dict[str, str] = {}
    for block in re.split(r"\n(?=Name: )", text.strip()):
        if not block.strip():
            continue
        name = None
        pkg = None
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("Name: "):
                name = line[6:]
            elif line.startswith("packageName: "):
                pkg = line[13:]
        if name and pkg:
            apps[pkg] = name
    return apps


def fetch_app_list() -> Optional[Dict[str, str]]:
    """Fetch installed apps via ``waydroid app list``.

    Runs ``waydroid app list`` directly (no privilege drop needed — this
    is called from the user daemon which already runs as the desktop user).
    """
    try:
        r = subprocess.run(
            [WAYDROID_BIN, "app", "list"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.warning("waydroid app list timed out")
        return None

    if r.returncode != 0:
        logger.debug("waydroid app list exited %d: %s", r.returncode, r.stderr.strip())
        return None
    return parse_app_list_output(r.stdout)


def _current_display_name(config: Dict[str, Any]) -> str:
    """Return the display name of the currently running app."""
    fg = _foreground_json(Path(config["paths"]["foreground_file"]))
    state = fg.get("state", "unavailable")
    if state != "app":
        return state
    package = fg.get("package")
    if not package:
        return "unavailable"

    cache_path = Path(config["paths"]["cache_file"])
    data = read_json(cache_path)
    apps = data.get("apps", {}) if isinstance(data, dict) else {}

    if package in apps:
        return apps[package]

    fresh = fetch_app_list()
    if fresh is not None:
        atomic_write_json(cache_path, {"last_updated": time.time(), "apps": fresh})
        if package in fresh:
            return fresh[package]

    return package


def _show_current(config: Dict[str, Any]) -> None:
    print(_current_display_name(config))

# ---------------------------------------------------------------------------
# Shared foreground file
# Written by the root daemon (waydroid detection), read by the user daemon
# (Discord IPC).  Stored in /tmp/waydroid-rpc/.
# ---------------------------------------------------------------------------

FOREGROUND_FILE_MODE = 0o644
EMPTY_FOREGROUND: Dict[str, Any] = {
    "state": "unavailable",
    "package": None,
    "session_start": None,
    "updated_at": 0,
}


def _foreground_json(fg_path: Path) -> Dict[str, Any]:
    data = read_json(fg_path)
    return {**EMPTY_FOREGROUND, **(data if isinstance(data, dict) else {})}


def _write_foreground(fg_path: Path, data: Dict[str, Any]) -> None:
    _ensure_parent(fg_path)
    data["updated_at"] = time.time()
    tmp = fg_path.with_suffix(".tmp." + os.urandom(4).hex())
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, FOREGROUND_FILE_MODE)
        tmp.rename(fg_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Discord IPC  (used only by the user-level daemon)
# ---------------------------------------------------------------------------


def connect_discord(client_id: str) -> Optional[Presence]:
    """Connect to Discord IPC.  XDG_RUNTIME_DIR is inherited from the
    user's session naturally since we run as a ``--user`` unit."""
    if Presence is None:
        logger.error("pypresence is not installed")
        return None
    try:
        rpc = Presence(client_id)
        rpc.connect()
        return rpc
    except (DiscordNotFound, PyPresenceException, OSError) as e:
        logger.info("Discord not reachable: %s", e)
        return None


def _presence_update(
    rpc: Presence,
    details: str,
    state_text: str,
    icon_key: str,
    start: Optional[float],
) -> None:
    rpc.update(
        details=details,
        state=state_text,
        large_image=icon_key,
        large_text=details,
        start=int(start) if start else None,
    )


def _presence_clear(rpc: Presence) -> None:
    rpc.clear()


# ---------------------------------------------------------------------------
# Root daemon  — detects foreground app, writes foreground.json
# ---------------------------------------------------------------------------


_shutdown_flag = False


def _signal_handler(signum: int, frame) -> None:
    global _shutdown_flag
    logger.info("Received signal %d — shutting down", signum)
    _shutdown_flag = True


def _interruptible_sleep(seconds: int) -> None:
    for _ in range(seconds):
        if _shutdown_flag:
            break
        time.sleep(1)


def run_root_daemon(config: Dict[str, Any]) -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    fg_path = Path(config["paths"]["foreground_file"])
    interval = config["poll_interval_seconds"]
    last_package: Optional[str] = None

    logger.info("Root daemon started (poll interval: %ds)", interval)

    while not _shutdown_flag:
        package = get_foreground_package()

        if package is None:
            _write_foreground(fg_path, {
                "state": "unavailable",
                "package": None,
                "session_start": None,
            })
            logger.debug("Foreground: unavailable")
        else:
            session_start: Optional[float] = None
            if package == STATE_IDLE:
                state = "idle"
            else:
                state = "app"
                if package != last_package:
                    session_start = time.time()
                else:
                    fg = _foreground_json(fg_path)
                    session_start = fg.get("session_start") or time.time()

            _write_foreground(fg_path, {
                "state": state,
                "package": package if state == "app" else None,
                "session_start": session_start,
            })
            if state == "app":
                logger.debug("Foreground: %s", package)
            else:
                logger.debug("Foreground: %s", state)

            last_package = package

        _interruptible_sleep(interval)

    logger.info("Root daemon stopped")


# ---------------------------------------------------------------------------
# User daemon  — reads foreground.json, updates Discord Rich Presence
# ---------------------------------------------------------------------------


def run_user_daemon(config: Dict[str, Any]) -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    fg_path = Path(config["paths"]["foreground_file"])
    interval = config["poll_interval_seconds"]
    rpc: Optional[Presence] = None
    last_state: Optional[str] = None
    last_package: Optional[str] = None

    logger.info("User daemon started (poll interval: %ds)", interval)

    while not _shutdown_flag:
        fg = _foreground_json(fg_path)
        state = fg.get("state", "unavailable")
        package = fg.get("package")

        if state == last_state and package == last_package:
            logger.debug("No change (%s)", state)
            _interruptible_sleep(interval)
            continue

        if rpc is None:
            rpc = connect_discord(config["discord_client_id"])
            if rpc is None:
                logger.info("Discord unavailable — deferring update")
                last_state = state
                last_package = package
                _interruptible_sleep(interval)
                continue

        try:
            if state == "unavailable" or (state == "idle" and config["idle_behavior"] == "clear"):
                _presence_clear(rpc)
                logger.info("Presence cleared (%s)", state)
            elif state == "idle":
                _presence_update(rpc, "Idle", "", config["default_icon_key"], None)
                logger.info("Presence set to Idle")
            elif state == "app" and package:
                name = _current_display_name(config)
                start = fg.get("session_start")
                _presence_update(rpc, name, package, config["default_icon_key"], start)
                logger.info("Presence updated: %s (%s)", name, package)

            last_state = state
            last_package = package
        except (PyPresenceException, OSError) as e:
            logger.error("Discord RPC error: %s", e)
            rpc = None

        _interruptible_sleep(interval)

    logger.info("Shutting down user daemon")
    if rpc is not None:
        try:
            _presence_clear(rpc)
            rpc.close()
        except Exception:
            pass
    logger.info("Done")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Waydroid Discord Rich Presence")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Config file path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--root-daemon",
        action="store_true",
        help="Run the root-level foreground detection daemon",
    )
    parser.add_argument(
        "--user-daemon",
        action="store_true",
        help="Run the user-level Discord Rich Presence daemon",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="Show the currently running app and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s: %(message)s",
        stream=sys.stdout,
    )

    config = load_config(args.config)

    if args.current:
        _show_current(config)
    elif args.root_daemon:
        run_root_daemon(config)
    elif args.user_daemon:
        run_user_daemon(config)
    else:
        parser.print_help()
        sys.exit(1)


def _current_display_name(config: Dict[str, Any]) -> str:
    """Return the display name of the currently running app."""
    fg = _foreground_json(Path(config["paths"]["foreground_file"]))
    state = fg.get("state", "unavailable")
    if state != "app":
        return state
    package = fg.get("package")
    if not package:
        return "unavailable"

    cache_path = Path(config["paths"]["cache_file"])
    data = read_json(cache_path)
    apps = data.get("apps", {}) if isinstance(data, dict) else {}

    if package in apps:
        return apps[package]

    fresh = fetch_app_list()
    if fresh is not None:
        atomic_write_json(cache_path, {"last_updated": time.time(), "apps": fresh})
        if package in fresh:
            return fresh[package]

    return package


def _show_current(config: Dict[str, Any]) -> None:
    print(_current_display_name(config))


if __name__ == "__main__":
    main()
