"""Turn the management front ends on and off: the built-in web UI and the Cockpit plugin.

The settings live in ports.yaml (settings.web_enabled, settings.cockpit_enabled);
sync() makes the system match them. termservctl and the TUI always work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .model import Config

WEB_SERVICE = "termserv-web.service"
PLUGIN_SOURCE = Path(__file__).parent / "cockpit"
COCKPIT_DIR = Path("/usr/share/cockpit")
PLUGIN_NAME = "termserv"


@dataclass
class FrontendStatus:
    web_enabled: bool
    web_active: str  # systemctl is-active output, or "unknown" without systemd
    cockpit_enabled: bool
    cockpit_installed: bool  # Cockpit itself
    cockpit_plugin_installed: bool


def _systemd() -> bool:
    return os.path.isdir("/run/systemd/system") and shutil.which("systemctl") is not None


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def cockpit_installed(cockpit_dir: Path = COCKPIT_DIR) -> bool:
    return (cockpit_dir / "base1").is_dir()  # shipped by cockpit-bridge; plugins need it


def plugin_dir(cockpit_dir: Path = COCKPIT_DIR) -> Path:
    return cockpit_dir / PLUGIN_NAME


def install_plugin(cockpit_dir: Path = COCKPIT_DIR) -> bool:
    """Copy the plugin into Cockpit's package directory. Returns True if anything changed."""
    dest = plugin_dir(cockpit_dir)
    files = sorted(p.name for p in PLUGIN_SOURCE.iterdir() if p.is_file())
    if dest.is_dir() and all((dest / f).exists() and (dest / f).read_bytes() == (PLUGIN_SOURCE / f).read_bytes()
                             for f in files) and sorted(os.listdir(dest)) == files:
        return False
    tmp = dest.with_name(f".{PLUGIN_NAME}.new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(PLUGIN_SOURCE, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return True


def remove_plugin(cockpit_dir: Path = COCKPIT_DIR) -> bool:
    dest = plugin_dir(cockpit_dir)
    if not dest.exists():
        return False
    shutil.rmtree(dest)
    return True


def status(cfg: Config, cockpit_dir: Path = COCKPIT_DIR) -> FrontendStatus:
    active = (_systemctl("is-active", WEB_SERVICE).stdout.strip() or "unknown") if _systemd() else "unknown"
    return FrontendStatus(
        web_enabled=cfg.settings.web_enabled,
        web_active=active,
        cockpit_enabled=cfg.settings.cockpit_enabled,
        cockpit_installed=cockpit_installed(cockpit_dir),
        cockpit_plugin_installed=(plugin_dir(cockpit_dir) / "manifest.json").exists(),
    )


def sync(cfg: Config, cockpit_dir: Path = COCKPIT_DIR, restart_web: bool = False) -> list[str]:
    """Start/stop the web UI and install/remove the Cockpit plugin to match cfg.

    The web UI re-reads its timeout and lockout settings by itself, so it only
    needs a restart when restart_web is set (e.g. after its port changed).
    """
    log: list[str] = []
    s = cfg.settings
    if _systemd():
        if s.web_enabled:
            was_active = _systemctl("is-active", WEB_SERVICE).stdout.strip() == "active"
            _systemctl("enable", WEB_SERVICE)
            r = _systemctl("restart" if (restart_web and was_active) else "start", WEB_SERVICE)
            if r.returncode != 0:
                log.append(f"web UI failed to start: {r.stderr.strip() or 'see journalctl -u ' + WEB_SERVICE}")
            elif not was_active:
                log.append("web UI started")
            elif restart_web:
                log.append("web UI restarted")
        else:
            if _systemctl("is-enabled", WEB_SERVICE).stdout.strip() == "enabled" or \
                    _systemctl("is-active", WEB_SERVICE).stdout.strip() == "active":
                _systemctl("disable", "--now", WEB_SERVICE)
                log.append("web UI stopped and disabled")
    if s.cockpit_enabled:
        if not cockpit_installed(cockpit_dir):
            log.append("Cockpit plugin is on, but Cockpit is not installed (sudo apt install cockpit)")
        elif install_plugin(cockpit_dir):
            log.append(f"Cockpit plugin installed in {plugin_dir(cockpit_dir)}")
    elif remove_plugin(cockpit_dir):
        log.append("Cockpit plugin removed")
    return log
