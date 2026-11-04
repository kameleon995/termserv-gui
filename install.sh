#!/bin/sh
# Install termserv on Ubuntu / Debian / Raspberry Pi OS (arm64 or amd64).
# Run as root from the repository root: sudo ./install.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then echo "run as root (sudo ./install.sh)" >&2; exit 1; fi
cd "$(dirname "$0")"

echo "==> Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  conserver-server conserver-client openssh-server python3 python3-venv

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
ln -sf /opt/termserv/bin/termservctl /usr/local/bin/termservctl

install -d -m 755 /usr/local/lib/termserv /etc/termserv /var/log/conserver
install -m 755 scripts/serial-attach /usr/local/lib/termserv/serial-attach
[ -f /etc/termserv/conserver.passwd ] || install -m 600 /dev/null /etc/termserv/conserver.passwd
if [ ! -f /etc/termserv/ports.yaml ]; then
  install -m 644 examples/ports.yaml /etc/termserv/ports.yaml
  echo "    created /etc/termserv/ports.yaml from the example; edit usb_path values (termservctl usb)"
fi

getent group serial >/dev/null || groupadd --system serial
echo "    add users with: usermod -aG serial <user>"

install -m 644 systemd/termserv-conserver.service systemd/termserv-sshd.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable termserv-conserver.service termserv-sshd.service

echo "==> Generating config and starting services"
termservctl apply
echo "==> Done. Try: ssh -p 2001 <user>@$(hostname)"
