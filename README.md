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
- **ports.yaml** is the single source of truth. `termservctl`, the web
  UI and the TUI edit it, generate the sshd, conserver and udev config, syntax-check
  them, and only then reload the services.

Manage it from a **web UI** (HTTPS, log in with your Linux account), a **TUI** over
SSH (`sudo termserv-tui`) or the `termservctl` CLI. All three use the same
`termserv` Python package, so they validate and behave the same way.

Documentation: [administrator guide](docs/admin-guide.md) ·
[user manual](docs/user-manual.md) · [welcome email (Polish, shared account)](docs/email-template.md)

![Web UI: port table](docs/screenshots/web-ports.png)

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

Then open `https://<host>:8443` and log in with a Linux account in the `sudo`
group. The certificate is self-signed, so the browser asks you to accept it once.

`install.sh` disables `brltty` and `ModemManager`, which grab USB-serial adapters
on Ubuntu, and the distro's own `conserver-server` service (termserv runs its own,
bound to localhost).

### Uninstall

```sh
sudo ./install.sh --uninstall              # asks before deleting config, console logs, the serial group
sudo ./install.sh --uninstall --keep-data  # keep /etc/termserv, /var/log/conserver and the serial group
sudo ./install.sh --purge                  # delete everything without asking
```

This stops and removes the termserv services and timer, the Cockpit plugin, the udev
rules, the PAM and apt configuration, `/opt/termserv` and the commands in `/usr/local`,
and unmasks the distro's `conserver-server`. With no terminal to ask on it keeps the
data. Distro packages stay installed, and `brltty` / `ModemManager` stay disabled
(`sudo systemctl enable --now ModemManager brltty` to bring them back).

## Security updates

Updates install themselves; nothing in this repository needs to change for a CVE fix.

| Component | Comes from | Updated by |
| --- | --- | --- |
| OpenSSH (`termserv-sshd`), conserver, PAM, OpenSSL, Python | Ubuntu/Debian packages | `unattended-upgrades`, daily (security pocket) |
| FastAPI, uvicorn, Jinja2, Textual and other Python libraries | PyPI, in `/opt/termserv/venv-*` | `termserv-update.timer`, weekly |
| Cockpit (if you install it) | Ubuntu packages | `unattended-upgrades` |
| Jetson kernel and firmware (`nvidia-l4t-*`) | NVIDIA | by hand, with JetPack (deliberately excluded) |

- After every package upgrade, `restart-outdated` (an apt hook) restarts any termserv
  daemon still running replaced code. Restarting `termserv-sshd` keeps open sessions;
  restarting `termserv-conserver` drops them (users reconnect).
- `termserv-update` builds a new environment with library releases that are at least
  7 days old, runs the test suite in it as `nobody`, switches over, and goes back to
  the previous environment if the web UI does not come back up. Log:
  `journalctl -u termserv-update`.
- Urgent CVE in a Python library: `sudo termserv-update --now` (no 7-day delay; tests
  and rollback still apply; `sudo termservctl update --now` does the same). Urgent CVE
  in a package: `sudo unattended-upgrade -v`.
- conserver is in Ubuntu's *universe* archive, which gets security fixes through
  Ubuntu Pro (free for up to 5 personal machines): `sudo pro attach`. Once attached,
  unattended-upgrades installs those fixes too.
- Kernel and libc fixes need a reboot. To let the machine reboot itself at night,
  uncomment `Automatic-Reboot` in `/etc/apt/apt.conf.d/52termserv-unattended-upgrades`.
- New termserv code (merged to main): `sudo termservctl update --full` runs
  `git pull --ff-only` in the checkout `install.sh` was run from (as the checkout's
  owner) and, if it moved, re-runs `install.sh`. The new code is tested before it goes
  live; if anything fails, the current version stays. The weekly timer does not do this.
- Turn the library updates off with `sudo systemctl disable --now termserv-update.timer`.


| Keys | Action |
| --- | --- |
| `Ctrl-E c .` | disconnect |
| `Ctrl-E c f` | force write access (if someone else holds it) |
| `Ctrl-E c l 0` | send a serial break |
| `Ctrl-E c ?` | help |

The first user on a port gets write access; later users watch read-only.
Logs are in `/var/log/conserver/portN.log` (unless logging is turned off for the port).

## Web UI

- **Ports**: live table (state, who is connected, missing adapters), add, edit,
  delete, release/grab, per-port log viewer. Edits are saved to `ports.yaml`; a
  banner shows when they are not live yet, and **Apply changes** activates them.
- **Users**: which Linux accounts are in each access group (`serial` plus any
  per-port groups); click to add or remove. Takes effect on the next SSH login.
- Login uses PAM (`/etc/pam.d/termserv-web`) and is limited to
  `settings.admin_group` (default `sudo`). After `settings.web_max_failures`
  (default 5) failed logins from one address, it is locked out for
  `settings.web_lockout_minutes` (default 10).
- Sessions live on the server; the browser only gets a random id in a cookie with
  no expiry date (HttpOnly, Secure, SameSite=Strict), so it is gone when the
  browser closes. A session ends after `settings.web_idle_minutes` (default 60)
  without activity (the auto-refreshing port table does not count as activity),
  after 8 hours at most, on logout (the old id stops working at once) and when
  the service restarts. Pages set a strict Content-Security-Policy (no inline
  scripts), deny framing and are never cached.
- **Restart services** (under the service list on the Ports page) restarts
  conserver, the SSH daemon and the web UI, applying any unapplied changes first. Everyone on a serial port is
  disconnected and web users have to log in again. The TUI (`R`), the Cockpit
  plugin and `sudo termservctl restart` do the same.
- Service: `termserv-web`, port `settings.web_port` (default 8443), certificate in
  `/etc/termserv/web.crt` (replace it with your own if you like).
- Timeout and lockout changes (from the TUI or `termservctl settings`) take effect
  without a restart; nobody is logged out.
- Turn it off with `sudo termservctl web disable` (or in the TUI settings, `s`).
  The TUI and termservctl keep working; the Cockpit plugin can replace it.

### Connecting from the browser

Each port has **Connect** and **Copy ssh**. Connect is an `ssh://user@host:port`
link that opens your own terminal's SSH client (most systems have a handler for
`ssh://`; on Windows, Windows Terminal or PuTTY can register one). Copy ssh copies
`ssh -p 200N user@host` for pasting. Nothing runs in the browser: there is no web
shell. The Cockpit plugin has the same two actions.

## Cockpit plugin

An alternative web UI inside [Cockpit](https://cockpit-project.org/), the Linux
web console: a **Serial ports** page with the same port table, add/edit, delete,
release/grab, log viewer and **Apply changes**. It uses Cockpit's login, HTTPS
and sudo, and does everything through `termservctl`, so validation is the same
as everywhere else.

```sh
sudo apt install cockpit
sudo termservctl cockpit enable   # copies the plugin to /usr/share/cockpit/termserv
sudo termservctl web disable      # optional: Cockpit only
```

Open `https://<address>:9090`, log in, and choose **Serial ports**. Viewing works
for any user; changes need **administrative access** (the "Limited access"
button at the top). For user/group membership, the page links to Cockpit's
**Accounts**. Cockpit's own idle timeout is set in `/etc/cockpit/cockpit.conf`
(`[Session] IdleTimeout=`, in minutes, default 15).

## TUI

```sh
sudo termserv-tui
```

Keys: `a` add, `e`/Enter edit, `c` connect, `d` delete, `r` release/grab, `l` log, `u` users,
`s` settings (web UI and Cockpit on/off, web idle timeout, failed tries, lockout),
`p` apply, `R` restart services, `q` quit. In forms and dialogs, the arrow keys move between fields and
buttons (Tab works too); Enter or Space opens a dropdown.

`c` opens the selected port's console right in the TUI's terminal, as the user who
ran `sudo` (ssh to `localhost` on the port's SSH port); `Ctrl-E c .` brings you back.

![TUI: editing a port](docs/screenshots/tui-edit.png)

## Using a port locally (cable tests)

```sh
sudo termservctl release 1        # conserver lets go of port 1; other ports keep running
sudo screen /dev/port1 115200     # or add yourself to dialout to skip sudo
sudo termservctl grab 1           # back under termserv (a reboot also does this)
```

While a port is released, `ssh -p 2001` says so instead of connecting.

## ports.yaml

See [examples/ports.yaml](examples/ports.yaml). SSH port = `base_port + number`.

| Field | Meaning |
| --- | --- |
| `number` | port number N (SSH port 2000+N, conserver console `portN`) |
| `name` | optional alias, e.g. `router1` |
| `usb_path` | udev `ID_PATH` of the USB socket; generates a `/dev/portN` symlink, so a port is a physical socket |
| `device` | explicit device path for ports not on a USB socket (ignored when `usb_path` is set) |
| `baud`, `parity` | line settings (8 data bits, 1 stop bit) |
| `flow` | flow control: `none` (default), `xonxoff` or `rtscts`. Leave at `none` unless the device is configured for flow control |
| `groups` | Linux groups allowed on this port (default: `settings.ssh_group`) |
| `enabled` | `false` closes the SSH port and drops the console |
| `ondemand` | `true` opens the device only while someone is connected, so it is free otherwise; nothing is logged while nobody is attached |
| `log` | `false` stops recording the port's console output to `portN.log` (default `true`); `termservctl set N --no-log` / `--log` |

## termservctl

```
termservctl list [--status]     show ports (and who is connected)
termservctl set N [--name ...]  add or change a port
termservctl remove N
termservctl release N           free port N for local use (screen, minicom) until grab or reboot
termservctl grab N              take a released port back
termservctl connect N|NAME [--user U]   open port N's console here (alias: open)
termservctl usb                 list USB serial adapters and their USB paths
termservctl update [--now] [--full]   run termserv-update now; --full also pulls and installs new termserv code
termservctl render sshd|conserver|udev
termservctl check               validate + syntax-check generated config
termservctl apply [--no-reload]
termservctl restart             apply unapplied changes, then restart conserver, sshd and the web UI (drops open sessions)
termservctl web enable|disable|status       built-in web UI on/off
termservctl cockpit enable|disable|status   Cockpit plugin on/off
termservctl settings [--web-idle-minutes N] [--web-max-failures N] [--web-lockout-minutes N]
termservctl state | save-port | log N       JSON/text interface used by the Cockpit plugin
```

## Tests

```sh
pip install -e '.[test]'
python3 -m pytest tests/test_core.py tests/test_web.py tests/test_tui.py   # safe anywhere
```

`tests/e2e` builds the whole stack with virtual serial ports (socat pty pairs and a
fake device) and checks it over real SSH: port mapping, per-port groups, no shell,
no forwarding, shared read-only watching, logging. It creates users and writes
`/etc/termserv`, so run it only in a throwaway container or VM:

```sh
sudo TERMSERV_E2E=1 python3 -m pytest -v tests/e2e
```

## Notes

- `set`, `remove`, `release`, `grab`, `apply` and `check` need sudo; `list`, `usb`
  and `render` do not.
- The service names are `termserv-conserver` and `termserv-sshd`. Ubuntu's own
  `conserver-server` stays disabled; started by hand it exits with "no consoles found".

- Restarting `termserv-sshd` after a config change keeps open serial sessions
  (`KillMode=process`).
- With no ports enabled, `termserv-sshd` is stopped instead of falling back to port 22.
- Always connect as `ssh <username>@host -p 200N`. Without `<username>@`, ssh sends
  your local username and the login fails. A pre-login banner
  (`/etc/termserv/banner`, written by `apply`) says so. The client hides banners
  when run with `-q` or `LogLevel=ERROR`.
- Ubuntu's `pam_motd` may still print the message of the day on connect.
- conserver runs with `limited *`, so the console escapes that would leave a port are
  refused: `Ctrl-E c |` (run a command on this server), `Ctrl-E c ;` (switch to another
  port) and `Ctrl-E c z`.
- `termservctl connect` and the TUI's `c` are shortcuts for `ssh -p 200N <you>@localhost`:
  they go through the termserv sshd like any remote login, so per-port groups,
  `PermitRootLogin no`, `ForceCommand serial-attach` and `limited *` all still apply.
  Under sudo, ssh runs as the user behind sudo (their keys, agent and known_hosts).
- Console logs (`/var/log/conserver`) record everything typed, so only root and the
  `adm` group can read them. The distro's logrotate job rotates them.
