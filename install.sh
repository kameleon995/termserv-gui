#!/bin/sh
# Install termserv on Ubuntu / Debian / Raspberry Pi OS (arm64 or amd64).
# Run as root from the repository root: sudo ./install.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then echo "run as root (sudo ./install.sh)" >&2; exit 1; fi
cd "$(dirname "$0")"

echo "==> Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  conserver-server conserver-client openssh-server python3 python3-venv openssl

echo "==> Stopping services that grab USB serial adapters"
# brltty claims CH340/CH341 adapters; ModemManager probes new serial ports.
for svc in brltty brltty-udev ModemManager; do
  systemctl disable --now "$svc" 2>/dev/null || true
done
# The distro conserver service would compete with ours for the ports.
systemctl disable --now conserver-server 2>/dev/null || true

echo "==> Installing termserv into /opt/termserv"
python3 -m venv /opt/termserv
/opt/termserv/bin/pip install -q .
/opt/termserv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
ln -sf /opt/termserv/bin/termservctl /usr/local/bin/termservctl
ln -sf /opt/termserv/bin/termserv-tui /usr/local/bin/termserv-tui

install -d -m 755 /usr/local/lib/termserv /etc/termserv /var/log/conserver
install -m 755 scripts/serial-attach /usr/local/lib/termserv/serial-attach
[ -f /etc/termserv/conserver.passwd ] || install -m 600 /dev/null /etc/termserv/conserver.passwd
if [ ! -f /etc/termserv/ports.yaml ]; then
  install -m 644 examples/ports.yaml /etc/termserv/ports.yaml
  echo "    created /etc/termserv/ports.yaml from the example; edit usb_path values (termservctl usb)"
fi

getent group serial >/dev/null || groupadd --system serial
echo "    add users with: usermod -aG serial <user>"

echo "==> Web UI: PAM login and a self-signed HTTPS certificate"
install -m 644 systemd/termserv-web.pam /etc/pam.d/termserv-web
if [ ! -f /etc/termserv/web.crt ]; then
  ips=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sed 's/^/IP:/' | paste -sd, -)
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=termserv" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1${ips:+,$ips}" \
    -keyout /etc/termserv/web.key -out /etc/termserv/web.crt 2>/dev/null
  chmod 600 /etc/termserv/web.key
fi

install -m 644 systemd/termserv-conserver.service systemd/termserv-sshd.service \
  systemd/termserv-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable termserv-conserver.service termserv-sshd.service termserv-web.service

echo "==> Generating config and starting services"
termservctl apply
systemctl restart termserv-web.service
port=$(/opt/termserv/bin/python -c "from termserv import model; print(model.load().settings.web_port)")
addr=$(/opt/termserv/bin/python -c "from termserv.discover import primary_ip; print(primary_ip())")
echo "==> Done."
echo "    Serial ports: ssh -p 2001 <user>@$addr"
echo "    Web UI:       https://$addr:$port  (log in as a member of the 'sudo' group)"
echo "    TUI:          sudo termserv-tui"
