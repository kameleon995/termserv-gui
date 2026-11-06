# termserv administrator guide

This guide is for the person who installs and runs a termserv box. People who only
connect to consoles need the [user manual](user-manual.md) instead; to onboard them,
send the [welcome email](email-template.md) (Polish, for IT admins on a shared account).

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installing](#installing)
- [Setting up ports](#setting-up-ports)
- [Users and access](#users-and-access)
- [Managing termserv](#managing-termserv)
- [Day-to-day tasks](#day-to-day-tasks)
- [Updates](#updates)
- [Security model](#security-model)
- [Files, services and ports](#files-services-and-ports)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)
- [Reference: ports.yaml](#reference-portsyaml)
- [Reference: termservctl](#reference-termservctl)

## How it works

termserv turns a small Linux board (Jetson Orin Nano, Raspberry Pi, any Ubuntu or
Debian machine) into a serial console server. Each serial port gets its own SSH port:
`ssh -p 2001 user@host` lands on port 1, `-p 2002` on port 2, and so on.

```
ssh -p 2000+N ──> termserv sshd ──ForceCommand──> serial-attach ──> conserver (localhost) ──> /dev/portN ──> device
```

| Piece | Role |
| --- | --- |
| `termserv-sshd` | A second OpenSSH instance, separate from the admin sshd on port 22. It is the only network entry point to the consoles. It authenticates Linux users, checks per-port groups and never gives a shell: every login runs `serial-attach` for that port's console. |
| `serial-attach` | Small script that connects the SSH session to one conserver console, or explains why it can't (port released, no terminal). |
| `termserv-conserver` | conserver, listening on `127.0.0.1` only. It owns the serial devices: line settings, timestamped logs of every port, one writer plus read-only watchers, break signals. |
| udev rule | Gives each USB socket a stable name, `/dev/portN`, so a port is a physical socket rather than whatever `/dev/ttyUSBx` the kernel picked. |
| `/etc/termserv/ports.yaml` | The single source of truth. Everything above is generated from it. |
| Front ends | `termservctl` (CLI), `termserv-tui` (text UI over SSH), the built-in web UI (`termserv-web`, HTTPS on 8443) and an optional Cockpit plugin. All four use the same Python package, so they validate and behave the same way. |

Changes are made in two steps. Editing a port **saves** it to `ports.yaml`. **Apply**
then renders the sshd, conserver and udev config, syntax-checks it in a temporary
directory, and only if that passes replaces the live files and reloads the services.
A broken `ports.yaml` never reaches the running system.

## Requirements

- Ubuntu, Debian or Raspberry Pi OS, arm64 or amd64, with systemd.
- Root (sudo) access and internet access during install (apt and PyPI).
- USB-serial adapters (FTDI, CH340, CP210x, PL2303 ...) or built-in UARTs.
- Network access to SSH ports `2000+N` for users, and to 8443 (web UI) or 9090
  (Cockpit) for admins. Open them in any firewall in front of the box.

## Installing

From a clone of the repository:

```sh
git clone https://github.com/kameleon995/termserv-gui.git
cd termserv-gui
sudo ./install.sh
```

Keep the clone: `termservctl update --full` pulls new termserv code from it later.

`install.sh` runs seven steps and prints progress as it goes:

1. Installs packages: `conserver-server`, `conserver-client`, `openssh-server`,
   `python3`, `python3-venv`, `openssl`, `unattended-upgrades`, `util-linux`.
2. Disables `brltty` and `ModemManager` (they grab USB-serial adapters on Ubuntu) and
   masks the distro's `conserver-server` (termserv runs its own).
3. Builds termserv into `/opt/termserv`, links `termservctl` and `termserv-tui` into
   `/usr/local/bin`, creates `/etc/termserv/ports.yaml` from
   [examples/ports.yaml](../examples/ports.yaml) if there is none, creates the
   `serial` group and `/var/log/conserver` (readable by root and `adm` only).
4. Installs the web UI's PAM file and a self-signed certificate
   (`/etc/termserv/web.crt`, valid 10 years, for the box's current IPs).
5. Installs and enables the systemd services.
6. Turns on automatic security updates (see [Updates](#updates)).
7. Runs `termservctl apply` and prints the addresses to use.

Running `install.sh` again upgrades in place and keeps `ports.yaml`, the certificate
and the logs.

## Setting up ports

1. Plug the USB-serial adapters into the sockets you want to use.
2. Find the USB path of each socket:

   ```sh
   termservctl usb
   ```

   ```
   /dev/ttyUSB0   platform-3610000.usb-usb-0:1.2:1.0   FTDI FT232R USB UART A10K3XYZ
   /dev/ttyUSB1   platform-3610000.usb-usb-0:1.3:1.0   QinHeng CH340
   ```

   The middle column is the socket, not the adapter: any adapter you later plug into
   that socket becomes the same port.

3. Configure each port and apply:

   ```sh
   sudo termservctl set 1 --name router1 --baud 115200 --usb-path platform-3610000.usb-usb-0:1.2:1.0
   sudo termservctl set 2 --name switch1 --baud 9600  --usb-path platform-3610000.usb-usb-0:1.3:1.0
   sudo termservctl apply
   ```

   In the web UI, TUI and Cockpit the port form offers a dropdown of the USB sockets
   that have an adapter in them, so you don't have to copy paths by hand.

4. Test it: `termservctl connect 1` (or from another machine,
   `ssh -p 2001 you@box`).

Tips:

- Leave `flow` at `none` unless the device is configured for flow control. Most
  network gear (Cisco, Juniper, Dell, Aruba ...) uses no flow control; turning XON/XOFF
  on for such a device makes output stall or lose characters.
- A port that is not on a USB socket (a built-in UART, say) uses `--device /dev/ttyTHS1`
  instead of `--usb-path`.
- `--ondemand` opens the device only while someone is connected. The device is free
  the rest of the time, but nothing is logged while nobody is attached.
- `--no-log` stops recording a port's console output (for a device whose console
  shows secrets you don't want on disk, say). It is per port and on by default; the
  form has a **Log console output** checkbox. With it off the log viewers show
  nothing new and conserver's replay (`Ctrl-E c r`) has nothing to replay. The old
  `portN.log` is left in place until logrotate removes it; delete it by hand if it
  must go now. `--log` turns it back on. Apply to make the change live.
- `--disable` closes the port's SSH port and drops its console without deleting it.
- The SSH port is always `base_port + number` (2000 + N by default).

## Users and access

Users log in with their normal Linux account on the termserv box (password or SSH
key). Access is by group:

| Who | Needs |
| --- | --- |
| Uses every serial port | member of `serial` (`settings.ssh_group`) |
| Uses a restricted port | member of one of that port's `groups` |
| Uses the web UI | member of `sudo` (`settings.admin_group`) |
| Uses the TUI / changes config with termservctl | sudo rights |
| Uses Cockpit | any login to view; "administrative access" to change |

Add a console user:

```sh
sudo adduser alice                 # if they have no account yet
sudo usermod -aG serial alice
```

Restrict a port to a group:

```sh
sudo groupadd serial-lab
sudo usermod -aG serial,serial-lab bob
sudo termservctl set 2 --groups serial-lab
sudo termservctl apply
```

A user must be in `serial` (it gates the whole sshd) **and**, for a restricted port,
in one of the port's groups. Group changes take effect on the user's next SSH login.

The web UI's **Users** page shows who is in each access group and adds or removes
people with a click. Cockpit links to its own **Accounts** page instead.

To let a user log in with a key, put their public key in
`~alice/.ssh/authorized_keys` as usual; the termserv sshd uses the same files as the
system sshd. `root` can never log in on a serial port.

Then send them the [welcome email](email-template.md).

## Managing termserv

### Web UI

Open `https://<box>:8443` and log in with an account in the `sudo` group. The
certificate is self-signed, so the browser asks once; replace
`/etc/termserv/web.crt` and `web.key` with your own and run
`sudo systemctl restart termserv-web` if you like.

- **Ports**: live table (state, who is connected, missing adapters), add, edit,
  delete, release/grab, per-port log viewer with **Clear log**, **Connect** (an `ssh://` link for your
  own terminal) and **Copy ssh**. A banner shows when saved changes are not live yet;
  **Apply changes** activates them.
- **Restart services** (under the service list) restarts conserver, sshd and the web
  UI. Everyone on a serial port is disconnected and web users log in again.
- **Users**: group membership, as above.

Login is limited to `settings.admin_group`. After `web_max_failures` (5) failed logins
from one address, it is locked out for `web_lockout_minutes` (10). A session ends after
`web_idle_minutes` (60) idle, after 8 hours at most, on logout, or when the service
restarts. Change these with `sudo termservctl settings --web-idle-minutes 30` etc.;
they take effect without a restart.

Turn the web UI off with `sudo termservctl web disable`.

### TUI

```sh
ssh admin@box
sudo termserv-tui
```

| Key | Action |
| --- | --- |
| `a` | add a port |
| `e` / Enter | edit the selected port |
| `c` | connect to the selected port's console (`Ctrl-E c .` comes back) |
| `d` | delete |
| `r` | release / grab |
| `l` | console log (`x` there clears it) |
| `u` | users |
| `s` | settings (web UI and Cockpit on/off, timeouts, lockout) |
| `p` | apply changes |
| `R` | restart services |
| `q` | quit |

In forms, arrow keys or Tab move between fields; Enter or Space opens a dropdown.

### Cockpit plugin

```sh
sudo apt install cockpit
sudo termservctl cockpit enable
sudo termservctl web disable        # optional: use Cockpit only
```

Open `https://<box>:9090`, log in and choose **Serial ports**. It has the same port
table and actions as the web UI and does everything through `termservctl`.

### CLI

`termservctl` does everything the UIs do; see the [reference](#reference-termservctl).
Commands that change something need sudo; `list`, `usb` and `render` do not.

## Day-to-day tasks

**See who is connected**

```sh
termservctl list --status
```

**Read a console log**

```sh
sudo termservctl log 1                    # end of port 1's log
sudo less /var/log/conserver/port1.log    # whole log, timestamped hourly and on attach/detach
```

Logs record everything typed and printed, including passwords typed at the device, so
only root and `adm` can read them. The distro's logrotate job rotates them.
Ports with logging turned off (`--no-log`) are not recorded.

**Clear a console log**

```sh
sudo termservctl clear-log 1              # asks first; --yes skips the question
```

This empties `port1.log` and deletes its rotated copies (`port1.log.1`,
`port1.log.2.gz`, ...). It cannot be undone. The live file is truncated rather than
deleted, so conserver keeps writing to it and nothing needs restarting. The web UI,
TUI (`x` in the log view) and Cockpit have the same **Clear log** action in the log
view, with a confirmation.

**Use a port locally (cable test, firmware flash)**

```sh
sudo termservctl release 1        # conserver lets go of /dev/port1; other ports keep running
sudo screen /dev/port1 115200     # or minicom, picocom, a flashing tool ...
sudo termservctl grab 1           # give it back to termserv (a reboot also does this)
```

While a port is released, users who connect to it get a message saying so.

**Swap an adapter**: unplug it and plug the new one into the same socket. The udev
rule points `/dev/portN` at it and conserver reopens it within a minute. Nothing to
reconfigure.

**Kick a stuck writer**: a user can take write access with `Ctrl-E c f`. To drop
everyone, restart the services (below) or release and grab the port.

**Restart everything**

```sh
sudo termservctl restart
```

Restarts conserver, sshd and the web UI. Everyone connected to a serial port is
disconnected. Restarting only `termserv-sshd` (for example after `apply`) keeps open
serial sessions.

## Updates

Security updates install themselves:

| Component | Updated by |
| --- | --- |
| OpenSSH, conserver, PAM, OpenSSL, Python (distro packages) | `unattended-upgrades`, daily |
| FastAPI, uvicorn, Textual and other Python libraries | `termserv-update.timer`, weekly |
| Cockpit | `unattended-upgrades` |
| Jetson kernel and firmware (`nvidia-l4t-*`) | by hand with JetPack (excluded on purpose) |

- After a package upgrade, an apt hook restarts any termserv daemon still running old
  code. Restarting sshd keeps sessions; restarting conserver drops them.
- `termserv-update` builds a new environment from library releases at least 7 days
  old, runs the test suite in it, switches over, and rolls back if the web UI does not
  come back. Log: `journalctl -u termserv-update`.
- Urgent library CVE: `sudo termservctl update --now` (no 7-day wait; tests and
  rollback still apply). Urgent package CVE: `sudo unattended-upgrade -v`.
- New termserv code: `sudo termservctl update --full` pulls the clone `install.sh` was
  run from and re-runs `install.sh` if anything changed. The weekly timer never does
  this.
- conserver is in Ubuntu's *universe* archive; its security fixes come through Ubuntu
  Pro (free for up to 5 machines): `sudo pro attach`.
- Kernel and libc fixes need a reboot. To allow nightly automatic reboots, uncomment
  `Automatic-Reboot` in `/etc/apt/apt.conf.d/52termserv-unattended-upgrades`.
- Turn library updates off: `sudo systemctl disable --now termserv-update.timer`.

## Security model

- The termserv sshd is the only network entry point. conserver listens on
  `127.0.0.1` and trusts only local clients.
- sshd: `AllowGroups serial`, `PermitRootLogin no`, `DisableForwarding yes`, no
  tunnels, no user rc, and `ForceCommand` on every port (the global fallback is
  `/bin/false`). There is no way to get a shell through it.
- conserver runs with `limited *`, which removes the escapes that would leave a port:
  `Ctrl-E c |` (run a command on the server), `Ctrl-E c ;` (switch console, which
  would bypass per-port groups) and `Ctrl-E c z`.
- `termservctl connect` and the TUI's `c` go through the same sshd, so the same rules
  apply to admins.
- Console logs are mode 750/640, group `adm`.
- Web UI: PAM login limited to the admin group, per-address lockout, server-side
  sessions with an HttpOnly/Secure/SameSite=Strict cookie, strict CSP, no framing, no
  caching. There is no web shell.
- The admin sshd on port 22 is untouched; harden it as you normally would.

## Files, services and ports

| Path | What |
| --- | --- |
| `/etc/termserv/ports.yaml` | port table and settings (edit through the tools) |
| `/etc/termserv/sshd_config`, `conserver.cf`, `banner` | generated, do not edit |
| `/etc/udev/rules.d/99-termserv.rules` | generated `/dev/portN` rules |
| `/etc/termserv/web.crt`, `web.key` | web UI certificate |
| `/etc/termserv/source` | path of the clone used by `update --full` |
| `/var/log/conserver/portN.log` | console logs |
| `/run/termserv/released/` | released-port markers (cleared on reboot) |
| `/opt/termserv/` | installed code and Python environments |
| `/usr/local/bin/termservctl`, `termserv-tui` | commands |
| `/usr/local/lib/termserv/` | `serial-attach`, `termserv-update`, `restart-outdated` |

| Service | Listens on |
| --- | --- |
| `termserv-sshd` | TCP `2000+N` for each enabled port |
| `termserv-conserver` | `127.0.0.1:782` |
| `termserv-web` | TCP 8443 (`settings.web_port`) |
| `cockpit` (optional) | TCP 9090 |
| `termserv-update.timer` | weekly library update |

Logs: `journalctl -u termserv-sshd` (logins, at `LogLevel VERBOSE`),
`journalctl -u termserv-conserver`, `journalctl -u termserv-web`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| User gets `Permission denied` | Did they type `user@` before the host? Without it ssh sends their local username. Are they in `serial` (and the port's group)? `id alice`. Did they log in again after being added? |
| `Connection refused` on 200N | Is the port enabled and applied? `termservctl list`, `systemctl status termserv-sshd`. Firewall? With no port enabled, termserv-sshd is stopped on purpose. |
| Port shows the adapter missing | `termservctl usb`: is the adapter in the socket set in `usb_path`? `ls -l /dev/portN`. Is `brltty` back (`systemctl status brltty`)? |
| Connected, but no output / garbage | Wrong baud or parity; try 9600 and 115200. Press Enter a few times. Swap TX/RX (null-modem vs. straight cable). |
| Output stalls or drops characters | Set `flow` to `none` (the default) unless the device really uses flow control. |
| "port N is released for local use" | `sudo termservctl grab N`. |
| "a terminal is required" | The client ran without a TTY; use `ssh -t`. |
| `apply` fails | It prints the validation or syntax error; nothing live was changed. `sudo termservctl check` re-runs the checks. |
| Web UI unreachable | `termservctl web status`, `journalctl -u termserv-web`. Locked out after failed logins? Wait `web_lockout_minutes`. |
| Running `conserver-server` by hand says "no consoles found" | Expected; termserv runs its own `termserv-conserver`. |

## Uninstalling

```sh
sudo ./install.sh --uninstall              # asks before deleting config, logs and the serial group
sudo ./install.sh --uninstall --keep-data  # keeps /etc/termserv, /var/log/conserver, the serial group
sudo ./install.sh --purge                  # deletes everything without asking
```

This removes the services, timer, Cockpit plugin, udev rules, PAM and apt
configuration, `/opt/termserv` and the commands, and unmasks the distro's
`conserver-server`. Distro packages stay installed; `brltty` and `ModemManager` stay
disabled (`sudo systemctl enable --now ModemManager brltty` brings them back).

## Reference: ports.yaml

```yaml
settings:
  base_port: 2000          # SSH port = base_port + number
  ssh_group: serial        # group allowed on the serial sshd
  admin_group: sudo        # group allowed in the web UI
  log_dir: /var/log/conserver
  conserver_port: 782      # localhost only
  web_enabled: true
  web_port: 8443
  web_idle_minutes: 60
  web_max_failures: 5
  web_lockout_minutes: 10
  cockpit_enabled: false
ports:
  - number: 1
    name: router1
    baud: 115200
    parity: none
    enabled: true
    usb_path: platform-3610000.usb-usb-0:1.2:1.0
  - number: 2
    name: switch1
    baud: 9600
    groups: [serial-lab]
    usb_path: platform-3610000.usb-usb-0:1.3:1.0
```

| Port field | Values | Meaning |
| --- | --- | --- |
| `number` | 1..512 | port N: SSH port `base_port+N`, console `portN`, device `/dev/portN` |
| `name` | letters, digits, `_ . -`, max 32 | optional alias; `termservctl connect` accepts it |
| `usb_path` | udev `ID_PATH` | the USB socket; creates `/dev/portN` |
| `device` | `/dev/...` | explicit device for ports not on a USB socket (ignored when `usb_path` is set) |
| `baud` | 1200 ... 921600 | line speed (8 data bits, 1 stop bit) |
| `parity` | `none`, `even`, `odd` | |
| `flow` | `none` (default), `xonxoff`, `rtscts` | flow control |
| `groups` | list of groups | restrict the port to these groups (default: `ssh_group`) |
| `enabled` | `true` / `false` | `false` closes the SSH port and drops the console |
| `ondemand` | `true` / `false` | open the device only while someone is connected (no logging otherwise) |
| `log` | `true` (default) / `false` | `false` records nothing to `log_dir/portN.log` for this port |

Prefer editing through the tools; they validate before saving. If you edit the file by
hand, run `sudo termservctl check` and then `sudo termservctl apply`.

## Reference: termservctl

```
termservctl list [--status]                     show ports (and who is connected)
termservctl set N [options]                     add or change a port
    --name NAME --device DEV --usb-path PATH --baud B --parity none|even|odd
    --flow none|xonxoff|rtscts --groups g1,g2 --enable|--disable --ondemand|--no-ondemand
    --log|--no-log
termservctl remove N                            delete a port
termservctl apply [--no-reload]                 validate, write config, reload services
termservctl check                               validate and syntax-check without changing anything
termservctl render sshd|conserver|udev          print a generated file
termservctl usb                                 list USB serial adapters and their USB paths
termservctl connect N|NAME [--user U]           open a console from this box (alias: open)
termservctl release N                           free a port for local use until grab or reboot
termservctl grab N                              take a released port back
termservctl restart                             restart conserver, sshd and the web UI
termservctl update [--now] [--full]             update libraries now; --full also updates termserv
termservctl web enable|disable|status           built-in web UI on/off
termservctl cockpit enable|disable|status       Cockpit plugin on/off
termservctl settings [--web-idle-minutes N] [--web-max-failures N] [--web-lockout-minutes N]
termservctl log N                               end of a port's console log
termservctl clear-log N [--yes]                 empty a port's log and delete its rotated copies
termservctl state | save-port                   JSON interface used by the Cockpit plugin
```

Every command takes `-f FILE` to work on another `ports.yaml`.
