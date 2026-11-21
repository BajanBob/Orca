#!/usr/bin/env bash
# Orca on Raspberry Pi: installs Orca and the SDR/GNSS stack, sets up a systemd
# service so the node starts on boot and runs headless. Tested on Raspberry Pi OS
# (Bookworm) on a Pi 4 / Pi 5. Run with: sudo bash pi-setup.sh
set -euo pipefail
echo "== Orca Raspberry Pi setup =="

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo"; exit 1; fi

apt-get update
apt-get install -y python3 python3-pip python3-venv git \
    soapysdr-tools python3-soapysdr \
    soapysdr-module-rtlsdr soapysdr-module-airspy soapysdr-module-hackrf \
    gpsd gpsd-clients rtl-sdr

id orca >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -G plugdev,dialout orca
install -d -o orca -g orca /opt/orca

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cp -r "$HERE/orca" "$HERE/config" "$HERE/requirements.txt" /opt/orca/
chown -R orca:orca /opt/orca
[ -f /opt/orca/config/site.yaml ] || cp /opt/orca/config/site.sdr.yaml /opt/orca/config/site.yaml

pip3 install --break-system-packages -r /opt/orca/requirements.txt

install -m 644 "$HERE/packaging/orca.service" /etc/systemd/system/orca.service
systemctl daemon-reload
systemctl enable orca.service

echo
echo "Installed. Next:"
echo "  1. sudo nano /opt/orca/config/site.yaml     # set your SDR, GNSS, site"
echo "  2. sudo systemctl start orca"
echo "  3. open http://<pi-ip>:8088 on a touchscreen, tablet or phone"
echo "  logs: journalctl -u orca -f"
