"""Port table model: the single source of truth (ports.yaml)."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

DEFAULT_PORTS_FILE = Path("/etc/termserv/ports.yaml")
# Ports handed back for local use (termservctl release). Lives in /run on
# purpose: a reboot gives every port back to termserv.
RELEASE_DIR = Path(os.environ.get("TERMSERV_RELEASE_DIR", "/run/termserv/released"))

PARITIES = ("none", "even", "odd")
BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
GROUP_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# udev ID_PATH values look like "platform-3610000.usb-usb-0:1.2:1.0"
USB_PATH_RE = re.compile(r"^[A-Za-z0-9:._-]+$")


class ConfigError(ValueError):
    """Raised when ports.yaml is invalid. Message lists every problem."""


@dataclass
class Port:
    number: int
    name: str = ""
    device: str = ""
    baud: int = 115200
    parity: str = "none"
    enabled: bool = True
    # Extra Linux groups allowed on this port. Empty = settings.ssh_group.
    groups: list[str] = field(default_factory=list)
    # udev ID_PATH of the USB-serial adapter; when set, a udev rule creates /dev/portN.
    usb_path: str = ""
    # Open the device only while someone is connected (frees it otherwise, but
    # nothing is logged while nobody is attached).
    ondemand: bool = False

    @property
    def console(self) -> str:
        """conserver console name. Fixed by number so SSH port N always maps here."""
        return f"port{self.number}"

    def device_path(self) -> str:
        # A USB socket wins over a typed device path: the udev rule makes /dev/portN
        # point at whatever adapter is in that socket.
        if self.usb_path:
            return f"/dev/port{self.number}"
        return self.device or f"/dev/port{self.number}"


@dataclass
class Settings:
    base_port: int = 2000
    ssh_group: str = "serial"
    log_dir: str = "/var/log/conserver"
    conserver_port: int = 782
    # Linux group whose members may use the web UI (Ubuntu's admins are in "sudo").
    admin_group: str = "sudo"
    web_port: int = 8443
    # Web UI sessions end after this many minutes without activity.
    web_idle_minutes: int = 60
    # After web_max_failures failed logins from one address, logins from it are
    # refused for web_lockout_minutes.
    web_max_failures: int = 5
    web_lockout_minutes: int = 10
    # Which management front ends are on. The TUI and termservctl always work.
    web_enabled: bool = True
    cockpit_enabled: bool = False


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    ports: list[Port] = field(default_factory=list)

    def ssh_port(self, port: Port) -> int:
        return self.settings.base_port + port.number

    def enabled_ports(self) -> list[Port]:
        return sorted((p for p in self.ports if p.enabled), key=lambda p: p.number)

    def port(self, number: int) -> Port | None:
        return next((p for p in self.ports if p.number == number), None)

    # --- validation -------------------------------------------------------

    def validate(self) -> None:
        errors: list[str] = []
        s = self.settings
        if not (1024 <= s.base_port <= 65000):
            errors.append(f"settings.base_port {s.base_port} must be 1024..65000")
        if not GROUP_RE.match(s.ssh_group):
            errors.append(f"settings.ssh_group {s.ssh_group!r} is not a valid group name")
        if not GROUP_RE.match(s.admin_group):
            errors.append(f"settings.admin_group {s.admin_group!r} is not a valid group name")
        if not (1 <= s.web_idle_minutes <= 1440):
            errors.append(f"settings.web_idle_minutes {s.web_idle_minutes} must be 1..1440")
        if not (1 <= s.web_max_failures <= 100):
            errors.append(f"settings.web_max_failures {s.web_max_failures} must be 1..100")
        if not (1 <= s.web_lockout_minutes <= 1440):
            errors.append(f"settings.web_lockout_minutes {s.web_lockout_minutes} must be 1..1440")
        for key in ("web_enabled", "cockpit_enabled"):
            if not isinstance(getattr(s, key), bool):
                errors.append(f"settings.{key} must be true or false")
        if not (1 <= s.web_port <= 65535):
            errors.append(f"settings.web_port {s.web_port} must be 1..65535")
        if not s.log_dir.startswith("/"):
            errors.append("settings.log_dir must be an absolute path")

        seen_numbers: set[int] = set()
        seen_names: set[str] = set()
        seen_usb: dict[str, int] = {}
        seen_dev: dict[str, int] = {}
        for p in self.ports:
            where = f"port {p.number}"
            if not isinstance(p.number, int) or not (1 <= p.number <= 512):
                errors.append(f"{where}: number must be 1..512")
            if p.number in seen_numbers:
                errors.append(f"{where}: duplicate port number")
            seen_numbers.add(p.number)
            if p.name:
                if not NAME_RE.match(p.name):
                    errors.append(f"{where}: name {p.name!r} may use letters, digits, '_', '.', '-' (max 32)")
                if p.name in seen_names:
                    errors.append(f"{where}: duplicate name {p.name!r}")
                seen_names.add(p.name)
            if p.baud not in BAUDS:
                errors.append(f"{where}: baud {p.baud} not one of {', '.join(map(str, BAUDS))}")
            if p.parity not in PARITIES:
                errors.append(f"{where}: parity must be one of {', '.join(PARITIES)}")
            if p.device and not p.device.startswith("/dev/"):
                errors.append(f"{where}: device must be under /dev/")
            if p.device and re.search(r"[\s;{}\"']", p.device):
                errors.append(f"{where}: device path contains invalid characters")
            for g in p.groups:
                if not GROUP_RE.match(g):
                    errors.append(f"{where}: group {g!r} is not a valid group name")
            if p.usb_path and not USB_PATH_RE.match(p.usb_path):
                errors.append(f"{where}: usb_path {p.usb_path!r} has invalid characters")
            dev = p.device_path()
            if p.enabled and dev in seen_dev:
                errors.append(f"{where}: device {dev} already used by port {seen_dev[dev]}")
            if p.enabled:
                seen_dev.setdefault(dev, p.number)
            if p.usb_path:
                if p.usb_path in seen_usb:
                    errors.append(f"{where}: USB socket already used by port {seen_usb[p.usb_path]}")
                seen_usb.setdefault(p.usb_path, p.number)
            if s.base_port + p.number > 65535:
                errors.append(f"{where}: SSH port {s.base_port + p.number} exceeds 65535")
        if errors:
            raise ConfigError("\n".join(errors))

    # --- (de)serialisation -------------------------------------------------

    def to_dict(self) -> dict:
        ports = []
        for p in sorted(self.ports, key=lambda p: p.number):
            d = asdict(p)
            # keep the file tidy: drop empty optional fields
            for k in ("name", "device", "usb_path"):
                if not d[k]:
                    d.pop(k)
            if not d["groups"]:
                d.pop("groups")
            if not d["ondemand"]:
                d.pop("ondemand")
            ports.append(d)
        return {"settings": asdict(self.settings), "ports": ports}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Config":
        data = data or {}
        try:
            settings = Settings(**(data.get("settings") or {}))
            ports = [Port(**p) for p in (data.get("ports") or [])]
        except TypeError as e:  # unknown key
            raise ConfigError(f"unknown field in ports.yaml: {e}") from e
        cfg = cls(settings=settings, ports=ports)
        cfg.validate()
        return cfg


def load(path: Path | str = DEFAULT_PORTS_FILE) -> Config:
    path = Path(path)
    if not path.exists():
        return Config()
    with path.open() as f:
        return Config.from_dict(yaml.safe_load(f))


def save(cfg: Config, path: Path | str = DEFAULT_PORTS_FILE) -> None:
    cfg.validate()
    write_atomic(Path(path), yaml.safe_dump(cfg.to_dict(), sort_keys=False))


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --- released ports (runtime state, not config) ----------------------------

def released_ports(release_dir: Path | None = None) -> set[int]:
    d = release_dir or RELEASE_DIR
    out = set()
    if d.is_dir():
        for f in d.iterdir():
            m = re.fullmatch(r"port(\d+)", f.name)
            if m:
                out.add(int(m.group(1)))
    return out


def set_released(number: int, released: bool, release_dir: Path | None = None) -> None:
    d = release_dir or RELEASE_DIR
    marker = d / f"port{number}"
    if released:
        d.mkdir(parents=True, exist_ok=True)
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
