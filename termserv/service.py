"""High-level operations shared by the web UI, the TUI and termservctl.

Everything that changes the system goes through here, so all front ends
validate the same way and behave the same way.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import apply as applymod
from . import discover, frontends, model, render

SERVICES = (applymod.CONSERVER_SERVICE, applymod.SSHD_SERVICE, "termserv-web.service")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Store:
    """Where the port table and generated files live. Tests point this at a temp dir."""

    def __init__(self, ports_file: Path | str = model.DEFAULT_PORTS_FILE, paths: applymod.Paths | None = None):
        self.ports_file = Path(ports_file)
        self.paths = paths or applymod.Paths()

    def load(self) -> model.Config:
        return model.load(self.ports_file)

    def save(self, cfg: model.Config) -> None:
        model.save(cfg, self.ports_file)


# --- port table ------------------------------------------------------------

@dataclass
class PortView:
    port: model.Port
    ssh_port: int
    state: str  # up | down | idle | released | disabled | unknown
    users: list[str] = field(default_factory=list)
    device_present: bool = False
    socket: str = ""  # "USB 2.1" when the port follows a USB socket
    tty: str = ""  # adapter currently behind the port, e.g. "ttyUSB0"

    @property
    def number(self) -> int:
        return self.port.number


def port_views(cfg: model.Config, live: bool = True) -> list[PortView]:
    status = discover.console_status(port=cfg.settings.conserver_port) if live else {}
    released = model.released_ports()
    views = []
    for p in sorted(cfg.ports, key=lambda p: p.number):
        st = status.get(p.console)
        if not p.enabled:
            state = "disabled"
        elif p.number in released:
            state = "released"
        elif st:
            state = st.state
            # conserver reports an on-demand port nobody uses as "down"; that is normal
            if p.ondemand and state == "down":
                state = "idle"
        else:
            state = "unknown"
        views.append(PortView(
            port=p,
            ssh_port=cfg.ssh_port(p),
            state=state,
            users=st.users if st else [],
            device_present=os.path.exists(p.device_path()),
            socket=discover.usb_socket_label(p.usb_path) if p.usb_path else "",
            tty=_tty_behind(p.device_path()),
        ))
    return views


def _tty_behind(path: str) -> str:
    real = os.path.realpath(path)
    return os.path.basename(real) if real != path and os.path.exists(real) else ""


@dataclass
class SocketChoice:
    id_path: str
    label: str  # what the dropdown shows
    used_by: int | None = None  # another port already on this socket
    connected: bool = True


def socket_choices(cfg: model.Config, for_port: int | None = None, current: str = "") -> list[SocketChoice]:
    """Dropdown entries for picking a USB socket: connected adapters first, plus the
    port's current socket if nothing is plugged into it right now."""
    used = {p.usb_path: p.number for p in cfg.ports if p.usb_path and p.number != for_port}
    choices = []
    for d in discover.usb_serial_devices():
        if not d.id_path:
            continue
        desc = " · ".join(x for x in (os.path.basename(d.node), d.model or d.vendor, d.serial) if x)
        choices.append(SocketChoice(d.id_path, f"{d.socket} — {desc}", used.get(d.id_path)))
    if current and current not in {c.id_path for c in choices}:
        choices.insert(0, SocketChoice(current, f"{discover.usb_socket_label(current)} — nothing plugged in now",
                                       used.get(current), connected=False))
    return sorted(choices, key=lambda c: c.label)


def save_port(store: Store, data: dict, original_number: int | None = None) -> model.Config:
    """Create or replace one port from form data. Raises model.ConfigError on bad input."""
    cfg = store.load()
    port = parse_port(data)
    if original_number is not None and original_number != port.number:
        if cfg.port(port.number) is not None:
            raise model.ConfigError(f"port {port.number} already exists")
        if original_number in model.released_ports():
            raise model.ConfigError("grab the port before renumbering it")
        cfg.ports = [p for p in cfg.ports if p.number != original_number]
    if original_number is None and cfg.port(port.number) is not None:
        raise model.ConfigError(f"port {port.number} already exists")
    cfg.ports = [p for p in cfg.ports if p.number != port.number] + [port]
    store.save(cfg)  # validates the whole table
    return cfg


def parse_port(data: dict) -> model.Port:
    def as_int(key: str, default: int | None = None) -> int:
        v = str(data.get(key, "") or "").strip()
        if not v:
            if default is None:
                raise model.ConfigError(f"{key} is required")
            return default
        try:
            return int(v)
        except ValueError:
            raise model.ConfigError(f"{key} must be a number") from None

    def as_bool(key: str) -> bool:
        return str(data.get(key, "")).lower() in ("1", "true", "on", "yes")

    groups = data.get("groups", "")
    if isinstance(groups, str):
        groups = [g for g in re.split(r"[,\s]+", groups) if g]
    return model.Port(
        number=as_int("number"),
        name=str(data.get("name", "") or "").strip(),
        device=str(data.get("device", "") or "").strip(),
        baud=as_int("baud", 115200),
        parity=str(data.get("parity", "none") or "none"),
        enabled=as_bool("enabled"),
        groups=list(groups),
        usb_path=str(data.get("usb_path", "") or "").strip(),
        ondemand=as_bool("ondemand"),
    )


def delete_port(store: Store, number: int) -> model.Config:
    cfg = store.load()
    if cfg.port(number) is None:
        raise model.ConfigError(f"port {number} does not exist")
    cfg.ports = [p for p in cfg.ports if p.number != number]
    store.save(cfg)
    model.set_released(number, False)
    return cfg


def next_free_number(cfg: model.Config) -> int:
    used = {p.number for p in cfg.ports}
    return next(n for n in range(1, 513) if n not in used)


# --- applying --------------------------------------------------------------

def pending_changes(store: Store) -> list[str]:
    """Generated files that differ from what is on disk, i.e. saved but not applied."""
    try:
        cfg = store.load()
    except model.ConfigError:
        return ["ports.yaml (invalid)"]
    wanted = {
        store.paths.conserver_cf: render.conserver_cf(cfg, model.released_ports()),
        store.paths.sshd_config: render.sshd_config(cfg, banner_path=str(store.paths.banner)),
        store.paths.udev_rules: render.udev_rules(cfg),
    }
    changed = []
    for path, text in wanted.items():
        current = path.read_text() if path.exists() else None
        if path == store.paths.sshd_config and not text:
            continue  # no ports: the file is left alone and the service stopped
        if current != text:
            changed.append(path.name)
    return changed


def apply_changes(store: Store) -> list[str]:
    return applymod.apply(store.load(), paths=store.paths)


def release(store: Store, number: int) -> list[str]:
    return applymod.set_released(store.load(), number, True, paths=store.paths)


def grab(store: Store, number: int) -> list[str]:
    return applymod.set_released(store.load(), number, False, paths=store.paths)


# --- connecting to a port from this machine --------------------------------

def find_port(cfg: model.Config, which: str | int) -> model.Port:
    """A port by number or by name. Raises model.ConfigError if there is none."""
    text = str(which).strip()
    port = cfg.port(int(text)) if text.isdigit() else next((p for p in cfg.ports if p.name == text), None)
    if port is None:
        raise model.ConfigError(f"no port {text!r}")
    return port


def connect_user() -> str:
    """Who to log in to the serial sshd as: the user behind sudo, else the current user.

    The serial sshd refuses root (PermitRootLogin no), so root without sudo has to
    say who to connect as.
    """
    if os.geteuid() == 0:
        user = os.environ.get("SUDO_USER", "")
        if user and user != "root":
            return user
        raise model.ConfigError("the serial ports do not accept root; run this with sudo from "
                                "your own account, or name a user (termservctl connect N --user NAME)")
    return pwd.getpwuid(os.geteuid()).pw_name


def connect_command(cfg: model.Config, which: str | int, user: str) -> list[str]:
    """The ssh command that opens a port on this machine, after the same checks sshd makes.

    It goes through the termserv sshd like any remote user, so the per-port groups,
    ForceCommand serial-attach and conserver's "limited *" all still apply; this only
    saves typing the port number and the address.
    """
    port = find_port(cfg, which)
    if not port.enabled:
        raise model.ConfigError(f"port {port.number} is disabled")
    if port.number in model.released_ports():
        raise model.ConfigError(f"port {port.number} is released for local use; grab it back first")
    try:
        pwd.getpwnam(user)
    except KeyError:
        raise model.ConfigError(f"no such user {user!r}") from None
    allowed = port.groups or [cfg.settings.ssh_group]
    if not any(user in group_members(g) for g in allowed):
        raise model.ConfigError(f"{user} may not use port {port.number}: not in "
                                f"{' or '.join(allowed)} (add them under Users)")
    if not shutil.which("ssh"):
        raise model.ConfigError("ssh is not installed (apt install openssh-client)")
    return ["ssh", "-t", "-p", str(cfg.ssh_port(port)), "-l", user, "localhost"]


def connect(cfg: model.Config, which: str | int, user: str | None = None) -> int:
    """Open a port in this terminal and wait until the session ends. Returns ssh's exit code.

    When running as root (sudo), ssh runs as the target user, so it uses that user's
    keys, agent and known_hosts and sshd sees an ordinary login.
    """
    user = user or connect_user()
    cmd = connect_command(cfg, which, user)
    kwargs: dict = {}
    if os.geteuid() == 0:
        pw = pwd.getpwnam(user)
        env = {k: os.environ[k] for k in ("TERM", "LANG", "LC_ALL", "SSH_AUTH_SOCK") if k in os.environ}
        env.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name,
                   PATH="/usr/local/bin:/usr/bin:/bin")
        kwargs = dict(user=pw.pw_uid, group=pw.pw_gid,
                      extra_groups=os.getgrouplist(pw.pw_name, pw.pw_gid), env=env)
    return subprocess.run(cmd, **kwargs).returncode


# --- management front ends and web login settings ---------------------------

SETTING_FIELDS = ("web_enabled", "cockpit_enabled", "web_idle_minutes", "web_lockout_minutes", "web_max_failures")


def frontend_status(store: Store) -> frontends.FrontendStatus:
    return frontends.status(store.load(), store.paths.cockpit_dir)


def save_settings(store: Store, data: dict) -> list[str]:
    """Change front-end / web-login settings and make the system match right away.

    data holds any of SETTING_FIELDS. Raises model.ConfigError on bad input; nothing
    is saved then.
    """
    cfg = store.load()
    s = cfg.settings
    for key, value in data.items():
        if key not in SETTING_FIELDS:
            raise model.ConfigError(f"unknown setting {key!r}")
        if key.endswith("_enabled"):
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
        else:
            try:
                value = int(str(value).strip())
            except ValueError:
                raise model.ConfigError(f"{key.replace('_', ' ')} must be a number") from None
        setattr(s, key, value)
    store.save(cfg)  # validates
    return frontends.sync(cfg, store.paths.cockpit_dir)


# --- logs and services -----------------------------------------------------

def log_tail(cfg: model.Config, number: int, max_bytes: int = 64_000) -> str:
    """Last part of a port's conserver log, with terminal control codes removed."""
    path = Path(cfg.settings.log_dir) / f"port{int(number)}.log"
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = ANSI_RE.sub("", text)
    if size > max_bytes:
        text = text.split("\n", 1)[-1]  # drop the partial first line
    return text


def services_status() -> dict[str, str]:
    if not shutil.which("systemctl") or not os.path.isdir("/run/systemd/system"):
        return {s: "unknown" for s in SERVICES}
    out = {}
    for s in SERVICES:
        r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True)
        out[s] = r.stdout.strip() or "unknown"
    return out


# --- users -----------------------------------------------------------------

@dataclass
class UserView:
    name: str
    groups: set[str]


def login_users() -> list[str]:
    """Regular Linux accounts (UID 1000+ with a real shell)."""
    users = []
    for pw in pwd.getpwall():
        if 1000 <= pw.pw_uid < 65534 and not pw.pw_shell.endswith(("nologin", "false")):
            users.append(pw.pw_name)
    return sorted(users)


def access_groups(cfg: model.Config) -> list[str]:
    groups = [cfg.settings.ssh_group]
    for p in cfg.ports:
        groups += [g for g in p.groups if g not in groups]
    return groups


def group_members(group: str) -> set[str]:
    try:
        g = grp.getgrnam(group)
    except KeyError:
        return set()
    members = set(g.gr_mem)
    for pw in pwd.getpwall():  # users whose primary group it is
        if pw.pw_gid == g.gr_gid:
            members.add(pw.pw_name)
    return members


def users_matrix(cfg: model.Config) -> tuple[list[str], list[UserView]]:
    groups = access_groups(cfg)
    members = {g: group_members(g) for g in groups}
    users = sorted(set(login_users()) | set().union(*members.values()))
    return groups, [UserView(u, {g for g in groups if u in members[g]}) for u in users]


def set_membership(cfg: model.Config, user: str, group: str, member: bool) -> None:
    if group not in access_groups(cfg):
        raise model.ConfigError(f"{group} is not a termserv access group")
    try:
        pwd.getpwnam(user)
    except KeyError:
        raise model.ConfigError(f"no such user {user!r}") from None
    if member:
        try:
            grp.getgrnam(group)
        except KeyError:
            subprocess.run(["groupadd", "--system", group], check=True, capture_output=True)
        cmd = ["gpasswd", "-a", user, group]
    else:
        cmd = ["gpasswd", "-d", user, group]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise model.ConfigError(r.stderr.strip() or f"{' '.join(cmd)} failed")


def is_admin(cfg: model.Config, user: str) -> bool:
    if user == "root":
        return True
    return user in group_members(cfg.settings.admin_group)
