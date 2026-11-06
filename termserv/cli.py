"""termservctl: command-line front end to the shared core.

The web UI and TUI call the same functions; this exists for scripting and
for the proof of concept.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import apply as applymod
from . import discover, frontends, model, render, service


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
    for field in ("name", "device", "baud", "parity", "flow", "usb_path"):
        v = getattr(args, field)
        if v is not None:
            setattr(p, field, v)
    if args.groups is not None:
        p.groups = [g for g in args.groups.split(",") if g]
    if args.ondemand is not None:
        p.ondemand = args.ondemand
    if args.log is not None:
        p.log = args.log
    if args.enable:
        p.enabled = True
    if args.disable:
        p.enabled = False
    model.save(cfg, args.file)
    print(f"saved port {p.number} to {args.file}")
    return 0


def cmd_remove(args) -> int:
    service.delete_port(_store(args), args.number)  # also drops a release marker
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


def cmd_connect(args) -> int:
    """Open a serial console from this machine: ssh to the termserv sshd on localhost."""
    cfg = _load(args)
    port = service.find_port(cfg, args.port)
    user = args.user or service.connect_user()
    print(f"connecting to port {port.number} as {user} (ssh -p {cfg.ssh_port(port)} {user}@localhost); "
          "Ctrl-E c . disconnects", file=sys.stderr)
    return service.connect(cfg, port.number, user)


def cmd_restart(args) -> int:
    _print_log(service.restart_services(_store(args)))
    return 0


# termservctl update is another name for termserv-update (installed by install.sh).
UPDATE_SCRIPT = "/usr/local/lib/termserv/termserv-update"


def cmd_update(args) -> int:
    argv = [UPDATE_SCRIPT] + (["--now"] if args.now else []) + (["--full"] if args.full else [])
    try:
        os.execv(UPDATE_SCRIPT, argv)  # replaces this process; does not return
    except OSError as e:
        print(f"error: cannot run {UPDATE_SCRIPT}: {e.strerror} (re-run install.sh)", file=sys.stderr)
        return 1
    return 0  # only reached when execv is stubbed out in tests


# --- front ends and settings -------------------------------------------------

def _store(args) -> service.Store:
    return service.Store(args.file)


def _print_log(log: list[str]) -> None:
    for line in log:
        print(line)


def cmd_frontend(args) -> int:
    """termservctl web|cockpit enable|disable|status"""
    key = "web_enabled" if args.cmd == "web" else "cockpit_enabled"
    store = _store(args)
    if args.action == "status":
        st = service.frontend_status(store)
        if args.cmd == "web":
            print(f"web UI: {'enabled' if st.web_enabled else 'disabled'} (service {st.web_active}), "
                  f"port {store.load().settings.web_port}")
        else:
            print(f"Cockpit plugin: {'enabled' if st.cockpit_enabled else 'disabled'}, "
                  f"{'installed' if st.cockpit_plugin_installed else 'not installed'}; "
                  f"Cockpit {'present' if st.cockpit_installed else 'not installed'}")
        return 0
    log = service.save_settings(store, {key: args.action == "enable"})
    print(f"{'web UI' if args.cmd == 'web' else 'Cockpit plugin'} {args.action}d")
    _print_log(log)
    return 0


def cmd_settings(args) -> int:
    store = _store(args)
    changes = {k: getattr(args, k) for k in service.SETTING_FIELDS if getattr(args, k, None) is not None}
    if changes:
        _print_log(service.save_settings(store, changes))
    s = store.load().settings
    print(f"web UI:              {'on' if s.web_enabled else 'off'} (port {s.web_port})")
    print(f"Cockpit plugin:      {'on' if s.cockpit_enabled else 'off'}")
    print(f"web idle timeout:    {s.web_idle_minutes} min")
    print(f"web login lockout:   {s.web_lockout_minutes} min after {s.web_max_failures} failed tries")
    return 0


# --- machine-readable interface (used by the Cockpit plugin) ------------------

def cmd_state(args) -> int:
    store = _store(args)
    cfg = store.load()
    views = service.port_views(cfg)
    ports = []
    for v in views:
        d = asdict(v.port)
        d.update(ssh_port=v.ssh_port, state=v.state, users=v.users, device_present=v.device_present,
                 socket=v.socket, tty=v.tty, device_path=v.port.device_path())
        ports.append(d)
    out = {
        "address": discover.primary_ip(),
        "settings": asdict(cfg.settings),
        "ports": ports,
        "pending": service.pending_changes(store),
        "services": service.services_status(),
        "restart_question": service.restart_question(service.pending_changes(store)),
        "next_free": service.next_free_number(cfg),
        "bauds": list(model.BAUDS),
        "parities": list(model.PARITIES),
        "flows": list(model.FLOWS),
    }
    if args.sockets_for is not None:
        port = cfg.port(args.sockets_for) if args.sockets_for else None
        out["sockets"] = [asdict(c) for c in service.socket_choices(
            cfg, args.sockets_for or None, port.usb_path if port else "")]
    json.dump(out, sys.stdout)
    return 0


def cmd_save_port(args) -> int:
    data = json.load(sys.stdin)
    service.save_port(_store(args), data, original_number=args.original)
    print("saved")
    return 0


def cmd_log(args) -> int:
    sys.stdout.write(service.log_tail(_load(args), args.number))
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
    s.add_argument("--flow", choices=model.FLOWS, help="flow control (default none)")
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
    g = s.add_mutually_exclusive_group()
    g.add_argument("--log", dest="log", action="store_true", default=None,
                   help="record console output to the port's log file (default)")
    g.add_argument("--no-log", dest="log", action="store_false",
                   help="do not record console output for this port")
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

    sub.add_parser("restart", help="apply unapplied changes, then restart termserv's services "
                   "(conserver, sshd, web UI); drops open console sessions and web logins").set_defaults(func=cmd_restart)

    s = sub.add_parser("release", help="free one port for local use (e.g. screen) until grab or reboot")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("grab", help="take a released port back")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_grab)

    s = sub.add_parser("connect", aliases=["open"],
                       help="open a serial console here (ssh to this machine's port 2000+N)")
    s.add_argument("port", help="port number or name")
    s.add_argument("--user", "-u", help="log in as this user (default: you, or the user behind sudo)")
    s.set_defaults(func=cmd_connect)

    sub.add_parser("regen-conserver", help=argparse.SUPPRESS).set_defaults(func=cmd_regen_conserver)

    sub.add_parser("usb", help="list USB serial adapters and their USB paths").set_defaults(func=cmd_usb)

    s = sub.add_parser("update", help="update termserv's Python libraries now; --full also updates termserv (runs termserv-update)")
    s.add_argument("--now", action="store_true",
                   help="urgent CVE fix: take library releases from the last 7 days too")
    s.add_argument("--full", action="store_true",
                   help="also git pull termserv's own code and re-run install.sh if it changed")
    s.set_defaults(func=cmd_update)

    for name, what in (("web", "the built-in web UI"), ("cockpit", "the Cockpit plugin")):
        s = sub.add_parser(name, help=f"turn {what} on or off")
        s.add_argument("action", choices=["enable", "disable", "status"])
        s.set_defaults(func=cmd_frontend)

    s = sub.add_parser("settings", help="show or change web UI / Cockpit settings")
    s.add_argument("--web-idle-minutes", dest="web_idle_minutes", type=int,
                   help="log web users out after this many idle minutes (1-1440)")
    s.add_argument("--web-lockout-minutes", dest="web_lockout_minutes", type=int,
                   help="how long an address is locked out after too many failed logins (1-1440)")
    s.add_argument("--web-max-failures", dest="web_max_failures", type=int,
                   help="failed logins allowed before the lockout (1-100)")
    s.set_defaults(func=cmd_settings)

    s = sub.add_parser("state", help="print everything as JSON (for the Cockpit plugin)")
    s.add_argument("--sockets-for", type=int, metavar="N",
                   help="include USB socket choices for port N (0 = a new port)")
    s.set_defaults(func=cmd_state)

    s = sub.add_parser("save-port", help="add/replace a port from JSON on stdin (for the Cockpit plugin)")
    s.add_argument("--original", type=int, help="number of the port being edited")
    s.set_defaults(func=cmd_save_port)

    s = sub.add_parser("log", help="print the end of a port's console log")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_log)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (model.ConfigError, applymod.ApplyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
