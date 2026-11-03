"""Find USB-serial adapters and read live console status."""

from __future__ import annotations

import glob
import os
import re
import subprocess
from dataclasses import dataclass


@dataclass
class SerialDevice:
    node: str  # /dev/ttyUSB0
    id_path: str  # udev ID_PATH, stable per USB socket
    vendor: str = ""
    model: str = ""
    serial: str = ""

    @property
    def socket(self) -> str:
        return usb_socket_label(self.id_path)


_SOCKET_RE = re.compile(r"usb(?:v\d+)?-\d+:([\d.]+):\d+\.(\d+)$")


def usb_socket_label(id_path: str) -> str:
    """Short name for a USB socket from its udev ID_PATH.

    "platform-3610000.usb-usb-0:2.1:1.0" -> "USB 2.1" (hub port chain 2 then 1).
    Interface numbers above 0 (multi-port adapters) are shown too: "USB 2.4 #1".
    """
    m = _SOCKET_RE.search(id_path or "")
    if not m:
        return id_path or ""
    label = f"USB {m.group(1)}"
    if m.group(2) != "0":
        label += f" #{m.group(2)}"
    return label


def _udev_props(node: str) -> dict[str, str]:
    try:
        out = subprocess.run(
            ["udevadm", "info", "-q", "property", "-n", node],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    props = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        props[k] = v
    return props


def usb_serial_devices() -> list[SerialDevice]:
    devs = []
    for node in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        p = _udev_props(node)
        devs.append(
            SerialDevice(
                node=node,
                id_path=p.get("ID_PATH", ""),
                vendor=p.get("ID_VENDOR_FROM_DATABASE") or p.get("ID_VENDOR", ""),
                model=p.get("ID_MODEL_FROM_DATABASE") or p.get("ID_MODEL", ""),
                serial=p.get("ID_SERIAL_SHORT", ""),
            )
        )
    return devs


@dataclass
class ConsoleStatus:
    console: str
    state: str  # "up" / "down" / ...
    users: list[str]


def console_status(master: str = "127.0.0.1", port: int = 782) -> dict[str, ConsoleStatus]:
    """Parse `console -u`: one line per console, e.g. 'port1  up   rw alice@...'."""
    try:
        out = subprocess.run(
            ["console", "-M", master, "-p", str(port), "-u"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "USER": os.environ.get("USER", "root")},
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, state, rest = parts[0], parts[1], parts[2:]
        users = [u for u in rest if "@" in u]
        result[name] = ConsoleStatus(name, state, users)
    return result


def primary_ip(fallback: str = "<address>") -> str:
    """The address other machines most likely reach this one on: the source address
    of the default route. Nothing is sent. Falls back to a placeholder when the
    machine has no network (or only loopback)."""
    import socket

    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ip = s.getsockname()[0]
        except OSError:
            continue
        if ip and not ip.startswith(("127.", "0.")) and ip not in ("::1", "::"):
            return f"[{ip}]" if ":" in ip else ip
    return fallback
