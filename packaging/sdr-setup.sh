#!/usr/bin/env bash
# Orca SDR Edition setup: install SoapySDR and a driver for your receiver, then Orca.
# Receive only. Tested on Debian/Ubuntu and Raspberry Pi OS. Run with: bash sdr-setup.sh
set -euo pipefail

echo "== Orca SDR Edition setup =="
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if command -v apt-get >/dev/null 2>&1; then
    echo "Installing SoapySDR and common drivers (sudo may prompt)..."
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip python3-venv \
        soapysdr-tools python3-soapysdr \
        soapysdr-module-rtlsdr soapysdr-module-airspy soapysdr-module-hackrf \
        soapysdr-module-uhd gpsd gpsd-clients || true
else
    echo "Non-apt system: install SoapySDR, its Python bindings and your device driver"
    echo "with your package manager (e.g. 'brew install soapysdr' on macOS), then rerun."
fi

echo "Installing Orca and its Python dependencies..."
python3 -m pip install --user -r "$HERE/requirements.txt"

echo
echo "Detected SDR devices:"
SoapySDRUtil --find 2>/dev/null || echo "  (none found — plug in a receiver and check with: SoapySDRUtil --find)"

echo
echo "Done. Next:"
echo "  1. cp config/site.sdr.yaml config/site.yaml   # then edit for your device"
echo "  2. python3 -m orca --config config/site.yaml"
echo "  3. open http://<this-host>:8088 on a touchscreen, tablet or phone"
