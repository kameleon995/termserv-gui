#!/bin/sh
# Install termserv on Ubuntu / Debian / Raspberry Pi OS (arm64 or amd64).
# Run as root from the repository root: sudo ./install.sh
#
#   sudo ./install.sh                          install or upgrade
#   sudo ./install.sh --uninstall              remove termserv; asks before deleting
#                                              /etc/termserv (ports.yaml, certificate),
#                                              console logs and the serial group
#   sudo ./install.sh --uninstall --keep-data  same, keeping those without asking
#   sudo ./install.sh --purge                  remove everything, without asking
set -eu

mode=install
purge=ask
for arg in "$@"; do
  case "$arg" in
    --uninstall) mode=uninstall ;;
    --purge) mode=uninstall; purge=yes ;;
    --keep-data) mode=uninstall; [ "$purge" = yes ] || purge=no ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: install.sh [--uninstall [--keep-data] | --purge]" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then echo "run as root (sudo ./install.sh)" >&2; exit 1; fi
cd "$(dirname "$0")"

if [ "$mode" = uninstall ]; then
  # Reverses everything below. Distro packages (openssh-server, conserver, ...) stay:
  # other software may need them.
  [ "$purge" = ask ] && [ ! -t 0 ] && purge=no  # no terminal to ask on: keep data
  ask() {  # ask "question": yes with --purge, no with --keep-data, otherwise prompt (default no)
    case "$purge" in yes) return 0 ;; no) return 1 ;; esac
    printf '    %s [y/N] ' "$1"
    read -r answer || answer=
    case "$answer" in [yY]*) return 0 ;; *) return 1 ;; esac
  }
  step_n=0
  total=5
  step() { step_n=$((step_n + 1)); echo ">>> ($step_n of $total) $*"; }

  step "Stopping and removing systemd services"
  units="termserv-update.timer termserv-update.service termserv-web.service termserv-sshd.service termserv-conserver.service"
  if command -v systemctl >/dev/null; then
    # shellcheck disable=SC2086
    systemctl disable --now $units 2>/dev/null || true
  fi
  for u in $units; do rm -f "/etc/systemd/system/$u"; done
  if command -v systemctl >/dev/null; then
    systemctl daemon-reload 2>/dev/null || true
    # shellcheck disable=SC2086
    systemctl reset-failed $units 2>/dev/null || true
  fi

  step "Removing the Cockpit plugin, udev rules, PAM and apt configuration"
  rm -rf /usr/share/cockpit/termserv /usr/share/cockpit/.termserv.new
  if [ -f /etc/udev/rules.d/99-termserv.rules ]; then
    rm -f /etc/udev/rules.d/99-termserv.rules
    udevadm control --reload 2>/dev/null || true
    udevadm trigger --subsystem-match=tty 2>/dev/null || true
  fi
  rm -f /etc/pam.d/termserv-web
  rm -f /etc/apt/apt.conf.d/52termserv-unattended-upgrades /etc/apt/apt.conf.d/99termserv-restart

  step "Removing termserv from /opt/termserv and /usr/local"
  rm -f /usr/local/bin/termservctl /usr/local/bin/termserv-tui /usr/local/sbin/termserv-update
  rm -rf /usr/local/lib/termserv /opt/termserv /opt/termserv.legacy /run/termserv

  step "Configuration, console logs and the serial group"
  # Generated files and the checkout pointer go; what the admin made stays unless asked.
  rm -f /etc/termserv/conserver.cf /etc/termserv/sshd_config /etc/termserv/banner /etc/termserv/source
  if [ -d /etc/termserv ]; then
    if ask "Delete /etc/termserv (ports.yaml, web certificate, conserver.passwd)?"; then
      rm -rf /etc/termserv
      echo "    removed /etc/termserv"
    else
      echo "    kept /etc/termserv (a later install.sh reuses it)"
    fi
  fi
  if [ -d /var/log/conserver ]; then
    if ask "Delete the console logs in /var/log/conserver?"; then
      rm -rf /var/log/conserver
      echo "    removed /var/log/conserver"
    else
      echo "    kept /var/log/conserver"
    fi
  fi
  if getent group serial >/dev/null; then
    members=$(getent group serial | cut -d: -f4)
    if ask "Delete the 'serial' group${members:+ (members: $members)}?"; then
      groupdel serial && echo "    removed group serial"
    else
      echo "    kept group serial"
    fi
  fi

  step "Restoring distro services"
  # Unmask the distro conserver so it can be used again; it stays disabled, as
  # install.sh found it may never have been enabled.
  systemctl unmask conserver-server 2>/dev/null || true
  echo ">>> termserv removed."
  echo "    install.sh disabled brltty and ModemManager; if you need them again:"
  echo "      sudo systemctl enable --now ModemManager brltty"
  echo "    Packages install.sh added (conserver-server, conserver-client, ...) were left installed."
  exit 0
fi

step_n=0
total=7
started=$(date +%s)
step() { step_n=$((step_n + 1)); echo ">>> ($step_n of $total) $*"; }

step "Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  conserver-server conserver-client openssh-server python3 python3-venv openssl \
  unattended-upgrades util-linux

step "Stopping services that grab USB serial adapters"
# brltty claims CH340/CH341 adapters; ModemManager probes new serial ports.
for svc in brltty brltty-udev ModemManager; do
  systemctl disable --now "$svc" 2>/dev/null || true
done
# The distro conserver service would compete with ours for the ports. Masked, not
# just disabled, so a package upgrade can never start it again.
systemctl disable --now conserver-server 2>/dev/null || true
systemctl mask conserver-server 2>/dev/null || true

step "Installing termserv into /opt/termserv"
install -d -m 755 /usr/local/lib/termserv /etc/termserv
install -m 755 scripts/serial-attach scripts/termserv-update scripts/restart-outdated /usr/local/lib/termserv/
ln -sf /usr/local/lib/termserv/termserv-update /usr/local/sbin/termserv-update
# Older installs had the venv directly in /opt/termserv; move it aside.
legacy=""
if [ -f /opt/termserv/pyvenv.cfg ]; then
  legacy=/opt/termserv.legacy
  rm -rf "$legacy"
  mv /opt/termserv "$legacy"
fi
install -d -m 755 /opt/termserv /opt/termserv/dist
# Build the termserv wheel and keep it, with the tests, so termserv-update can
# rebuild the environment with newer libraries later without this checkout.
build=$(mktemp -d)
python3 -m venv "$build/venv"
"$build/venv/bin/python" -m pip --disable-pip-version-check -q wheel --no-deps -w "$build/dist" .
rm -f /opt/termserv/dist/termserv-*.whl
cp "$build"/dist/termserv-*.whl /opt/termserv/dist/
rm -rf "$build" /opt/termserv/tests
mkdir -p /opt/termserv/tests
cp tests/test_*.py /opt/termserv/tests/
chmod -R a+rX /opt/termserv/tests
if ! /usr/local/lib/termserv/termserv-update --install; then
  echo "termserv-update failed; nothing was changed" >&2
  if [ -n "$legacy" ]; then rm -rf /opt/termserv; mv "$legacy" /opt/termserv; fi
  exit 1
fi
for cmd in termservctl termserv-tui; do
  ln -sf /opt/termserv/current/bin/$cmd /usr/local/bin/$cmd
done
[ -n "$legacy" ] && rm -rf "$legacy"
# Remember this checkout so termserv-update can pull new termserv code from it.
pwd -P >/etc/termserv/source
if command -v git >/dev/null && rev=$(git -c safe.directory="$(pwd -P)" rev-parse HEAD 2>/dev/null); then
  echo "$rev" >/opt/termserv/.source-rev
else
  rm -f /opt/termserv/.source-rev
fi

# Console logs record everything typed on the ports, so only root and adm may read them.
install -d -m 750 -g adm /var/log/conserver
chmod 750 /var/log/conserver
find /var/log/conserver -type f -exec chmod o-rwx {} +
[ -f /etc/termserv/conserver.passwd ] || install -m 600 /dev/null /etc/termserv/conserver.passwd
if [ ! -f /etc/termserv/ports.yaml ]; then
  install -m 644 examples/ports.yaml /etc/termserv/ports.yaml
  echo "    created /etc/termserv/ports.yaml from the example; edit usb_path values (termservctl usb)"
fi

getent group serial >/dev/null || groupadd --system serial
echo "    add users with: usermod -aG serial <user>"

step "Web UI: PAM login and a self-signed HTTPS certificate"
install -m 644 systemd/termserv-web.pam /etc/pam.d/termserv-web
if [ ! -f /etc/termserv/web.crt ]; then
  ips=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sed 's/^/IP:/' | paste -sd, -)
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=termserv" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1${ips:+,$ips}" \
    -keyout /etc/termserv/web.key -out /etc/termserv/web.crt 2>/dev/null
  chmod 600 /etc/termserv/web.key
fi

step "Installing systemd services"
install -m 644 systemd/termserv-conserver.service systemd/termserv-sshd.service \
  systemd/termserv-web.service systemd/termserv-update.service systemd/termserv-update.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable termserv-conserver.service termserv-sshd.service
# pick up unit changes (log permissions, pid file for log rotation)
systemctl try-restart termserv-conserver.service || true

step "Automatic security updates"
# Ubuntu packages (OpenSSH, conserver, PAM, OpenSSL, Python): daily via unattended-upgrades,
# then restart-outdated restarts our daemons. Python libraries: weekly termserv-update.
install -m 644 apt/52termserv-unattended-upgrades apt/99termserv-restart /etc/apt/apt.conf.d/
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl enable --now termserv-update.timer

step "Generating config and starting services"
# apply also starts/stops the web UI and installs/removes the Cockpit plugin
# to match settings.web_enabled / settings.cockpit_enabled in ports.yaml.
termservctl apply
setting() { /opt/termserv/current/bin/python -c "from termserv import model; print(model.load().settings.$1)"; }
[ "$(setting web_enabled)" = True ] && systemctl restart termserv-web.service  # pick up new code
addr=$(/opt/termserv/current/bin/python -c "from termserv.discover import primary_ip; print(primary_ip())")
t=$(( $(date +%s) - started ))
echo ">>> Done ($((t / 60))m$((t % 60))s)."
echo "    Serial ports: ssh -p 2001 <user>@$addr"
if [ "$(setting web_enabled)" = True ]; then
  echo "    Web UI:       https://$addr:$(setting web_port)  (log in as a member of the 'sudo' group)"
else
  echo "    Web UI:       off (sudo termservctl web enable)"
fi
if [ "$(setting cockpit_enabled)" = True ]; then
  echo "    Cockpit:      https://$addr:<Cockpit port, default 9090> -> Serial ports"
else
  echo "    Cockpit:      plugin off (sudo apt install cockpit && sudo termservctl cockpit enable)"
fi
echo "    TUI:          sudo termserv-tui"
echo "    Updates:      automatic (security packages daily, Python libraries weekly); new termserv code: sudo termservctl update --full"
