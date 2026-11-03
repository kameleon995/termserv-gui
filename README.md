# termserv

Turn a small Linux board (Jetson Orin Nano, Raspberry Pi) into a serial console
server: `ssh -p 2001 user@host` lands on serial port 1, `-p 2002` on port 2, and so on.

```
ssh -p 2000+N ──> termserv sshd ──ForceCommand──> serial-attach ──> conserver (localhost) ──> /dev/portN ──> device
                        ▲                                               ▲
                        └──────── generated from /etc/termserv/ports.yaml ┘   (termservctl, web UI, TUI)
```

- **OpenSSH** (a second, dedicated instance) is the only network front door. It
  authenticates Linux users, enforces per-port groups and never gives a shell.
- **conserver** owns the serial ports: per-port baud/parity, timestamped logs of
  every port, one writer plus any number of read-only watchers, break signals.
- **ports.yaml** is the single source of truth. `termservctl` (and later the web
  UI and TUI) edit it, generate the sshd, conserver and udev config, syntax-check
  them, and only then reload the services.

Status: proof of concept. The web UI (FastAPI + htmx) and TUI (Textual) come next
and will reuse the `termserv` Python package.

## Install

On Ubuntu / Debian / Raspberry Pi OS, from a clone of this repo:

```sh
sudo ./install.sh
sudo usermod -aG serial alice          # who may use the serial ports
termservctl usb                        # find the USB path of each adapter
sudo termservctl set 1 --name router1 --baud 115200 --usb-path platform-3610000.usb-usb-0:1.2:1.0
sudo termservctl apply
ssh -p 2001 alice@consolehost
```

`install.sh` disables `brltty` and `ModemManager`, which grab USB-serial adapters
on Ubuntu, and the distro's own `conserver-server` service (termserv runs its own,
bound to localhost).

## Using a port

| Keys | Action |
| --- | --- |
| `Ctrl-E c .` | disconnect |
| `Ctrl-E c f` | force write access (if someone else holds it) |
| `Ctrl-E c l 0` | send a serial break |
| `Ctrl-E c ?` | help |

The first user on a port gets write access; later users watch read-only.
Logs are in `/var/log/conserver/portN.log`.

## ports.yaml

See [examples/ports.yaml](examples/ports.yaml). SSH port = `base_port + number`.

| Field | Meaning |
| --- | --- |
| `number` | port number N (SSH port 2000+N, conserver console `portN`) |
| `name` | optional alias, e.g. `router1` |
| `usb_path` | udev `ID_PATH` of the USB socket; generates a `/dev/portN` symlink, so a port is a physical socket |
| `device` | explicit device path instead (default `/dev/portN`) |
| `baud`, `parity` | line settings (8 data bits, 1 stop bit) |
| `groups` | Linux groups allowed on this port (default: `settings.ssh_group`) |
| `enabled` | `false` closes the SSH port and drops the console |

## termservctl

```
termservctl list [--status]     show ports (and who is connected)
termservctl set N [--name ...]  add or change a port
termservctl remove N
termservctl usb                 list USB serial adapters and their USB paths
termservctl render sshd|conserver|udev
termservctl check               validate + syntax-check generated config
termservctl apply [--no-reload]
```

## Tests

```sh
pip install -e '.[test]'
python3 -m pytest tests/test_core.py          # unit tests, safe anywhere
```

`tests/e2e` builds the whole stack with virtual serial ports (socat pty pairs and a
fake device) and checks it over real SSH: port mapping, per-port groups, no shell,
no forwarding, shared read-only watching, logging. It creates users and writes
`/etc/termserv`, so run it only in a throwaway container or VM:

```sh
sudo TERMSERV_E2E=1 python3 -m pytest -v tests/e2e
```

## Notes

- Restarting `termserv-sshd` after a config change keeps open serial sessions
  (`KillMode=process`).
- With no ports enabled, `termserv-sshd` is stopped instead of falling back to port 22.
- Ubuntu's `pam_motd` may still print the message of the day on connect.
