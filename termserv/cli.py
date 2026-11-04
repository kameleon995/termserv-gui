"""termservctl: command-line front end to the shared core.

The web UI and TUI call the same functions; this exists for scripting and
for the proof of concept.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import apply as applymod
from . import discover, model, render


def _load(args) -> model.Config:
    return model.load(args.file)


def cmd_list(args) -> int:
    cfg = _load(args)
    status = discover.console_status(port=cfg.settings.conserver_port) if args.status else {}
    released = model.released_ports()
    print(f"{'PORT':<5} {'SSH':<6} {'NAME':<16} {'DEVICE':<28} {'BAUD':<7} {'PAR':<5} {'ON':<3} STATUS")
    for p in sorted(cfg.ports, key=lambda p: p.number):
        st = status.get(p.console)
        st_txt = f"{st.state} {','.join(st.users)}".strip() if st else ""
        if p.number in released:
            st_txt = "released (local use)"
        elif p.ondemand and args.status and not st_txt:
            st_txt = "ondemand"
        print(
            f"{p.number:<5} {cfg.ssh_port(p):<6} {p.name or '-':<16} {p.device_path():<28} "
            f"{p.baud:<7} {p.parity:<5} {'yes' if p.enabled else 'no':<3} {st_txt}"
        )
    return 0


def cmd_set(args) -> int:
    cfg = _load(args)
    p = cfg.port(args.number)
    if p is None:
        p = model.Port(number=args.number)
        cfg.ports.append(p)
    for field in ("name", "device", "baud", "parity", "usb_path"):
        v = getattr(args, field)
        if v is not None:
            setattr(p, field, v)
    if args.groups is not None:
        p.groups = [g for g in args.groups.split(",") if g]
    if args.ondemand is not None:
        p.ondemand = args.ondemand
    if args.enable:
        p.enabled = True
    if args.disable:
        p.enabled = False
    model.save(cfg, args.file)
    print(f"saved port {p.number} to {args.file}")
    return 0


def cmd_remove(args) -> int:
    cfg = _load(args)
    before = len(cfg.ports)
    cfg.ports = [p for p in cfg.ports if p.number != args.number]
    if len(cfg.ports) == before:
        print(f"port {args.number} not found", file=sys.stderr)
        return 1
    model.save(cfg, args.file)
    print(f"removed port {args.number}")
    return 0


def cmd_render(args) -> int:
    cfg = _load(args)
    which = {
        "conserver": render.conserver_cf,
        "sshd": render.sshd_config,
        "udev": render.udev_rules,
    }[args.what]
    sys.stdout.write(which(cfg))
    return 0


def cmd_check(args) -> int:
    applymod.check(_load(args))
    print("ok")
    return 0


def cmd_apply(args) -> int:
    for line in applymod.apply(_load(args), reload=not args.no_reload):
        print(line)
    return 0


def cmd_release(args) -> int:
    for line in applymod.set_released(_load(args), args.number, True):
        print(line)
    print(f"port {args.number} device is free; run 'termservctl grab {args.number}' when done "
          "(a reboot also gives it back)")
    return 0


def cmd_grab(args) -> int:
    for line in applymod.set_released(_load(args), args.number, False):
        print(line)
    return 0


def cmd_regen_conserver(args) -> int:
    """Used by termserv-conserver.service at start: rebuild conserver.cf so ports
    released before a reboot come back."""
    files = applymod.check(_load(args))
    model.write_atomic(applymod.Paths().conserver_cf, files["conserver_cf"])
    return 0


def cmd_usb(args) -> int:
    devs = discover.usb_serial_devices()
    if not devs:
        print("no USB serial adapters found")
    for d in devs:
        print(f"{d.node:<14} {d.id_path:<48} {d.vendor} {d.model} {d.serial}".rstrip())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="termservctl", description="Manage serial-over-SSH ports")
    ap.add_argument("-f", "--file", type=Path, default=model.DEFAULT_PORTS_FILE, help="ports.yaml path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="show the port table")
    s.add_argument("--status", action="store_true", help="include live conserver status")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("set", help="add or change a port")
    s.add_argument("number", type=int)
    s.add_argument("--name")
    s.add_argument("--device")
    s.add_argument("--baud", type=int)
    s.add_argument("--parity", choices=model.PARITIES)
    s.add_argument("--usb-path", dest="usb_path")
    s.add_argument("--groups", help="comma-separated Linux groups allowed on this port")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--ondemand", dest="ondemand", action="store_true", default=None,
                   help="open the device only while someone is connected")
    g.add_argument("--no-ondemand", dest="ondemand", action="store_false",
                   help="keep the device open and logged all the time (default)")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("remove", help="delete a port")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("render", help="print a generated config file")
    s.add_argument("what", choices=["conserver", "sshd", "udev"])
    s.set_defaults(func=cmd_render)

    sub.add_parser("check", help="validate and syntax-check generated config").set_defaults(func=cmd_check)

    s = sub.add_parser("apply", help="write config and reload services")
    s.add_argument("--no-reload", action="store_true", help="write files only")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("release", help="free one port for local use (e.g. screen) until grab or reboot")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("grab", help="take a released port back")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_grab)

    sub.add_parser("regen-conserver", help=argparse.SUPPRESS).set_defaults(func=cmd_regen_conserver)

    sub.add_parser("usb", help="list USB serial adapters and their USB paths").set_defaults(func=cmd_usb)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (model.ConfigError, applymod.ApplyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
