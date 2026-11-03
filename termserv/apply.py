"""Validate, write and activate generated config.

Order matters: render everything, syntax-check it in a temp dir, and only then
replace the live files and reload services. A bad ports.yaml never reaches the
running system.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import render
from .model import Config, write_atomic

SSHD_SERVICE = "termserv-sshd.service"
CONSERVER_SERVICE = "termserv-conserver.service"


@dataclass
class Paths:
    conserver_cf: Path = Path("/etc/termserv/conserver.cf")
    sshd_config: Path = Path("/etc/termserv/sshd_config")
    udev_rules: Path = Path("/etc/udev/rules.d/99-termserv.rules")


class ApplyError(RuntimeError):
    pass


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ApplyError(f"{' '.join(cmd)} failed:\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def check(cfg: Config) -> dict[str, str]:
    """Render and syntax-check everything. Returns the rendered files."""
    cfg.validate()
    files = {
        "conserver_cf": render.conserver_cf(cfg),
        "sshd_config": render.sshd_config(cfg),
        "udev_rules": render.udev_rules(cfg),
    }
    with tempfile.TemporaryDirectory() as tmp:
        cf = Path(tmp) / "conserver.cf"
        cf.write_text(files["conserver_cf"])
        if shutil.which("conserver"):
            _run(["conserver", "-S", "-C", str(cf)])
        if files["sshd_config"] and shutil.which("sshd"):
            sc = Path(tmp) / "sshd_config"
            sc.write_text(files["sshd_config"])
            _run([shutil.which("sshd") or "sshd", "-t", "-f", str(sc)])
    return files


def apply(cfg: Config, paths: Paths = Paths(), reload: bool = True) -> list[str]:
    """Write config and reload services. Returns a log of what was done."""
    files = check(cfg)
    log: list[str] = []
    write_atomic(paths.conserver_cf, files["conserver_cf"])
    log.append(f"wrote {paths.conserver_cf}")
    if files["sshd_config"]:
        write_atomic(paths.sshd_config, files["sshd_config"])
        log.append(f"wrote {paths.sshd_config}")
    old_udev = paths.udev_rules.read_text() if paths.udev_rules.exists() else None
    if old_udev != files["udev_rules"]:
        write_atomic(paths.udev_rules, files["udev_rules"])
        log.append(f"wrote {paths.udev_rules}")
        if reload:
            _run(["udevadm", "control", "--reload"], check=False)
            _run(["udevadm", "trigger", "--subsystem-match=tty"], check=False)
            log.append("reloaded udev rules")

    if not reload:
        return log
    # conserver re-reads its config on SIGHUP without dropping sessions.
    _run(["systemctl", "reload-or-restart", CONSERVER_SERVICE])
    log.append(f"reloaded {CONSERVER_SERVICE}")
    if files["sshd_config"]:
        # Restart (not reload) so Port changes take effect; open sessions survive
        # because the unit uses KillMode=process.
        _run(["systemctl", "enable", SSHD_SERVICE], check=False)
        _run(["systemctl", "restart", SSHD_SERVICE])
        log.append(f"restarted {SSHD_SERVICE}")
    else:
        _run(["systemctl", "stop", SSHD_SERVICE], check=False)
        log.append(f"no ports enabled: stopped {SSHD_SERVICE}")
    return log
