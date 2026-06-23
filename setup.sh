#!/usr/bin/env bash
# Setup script for running pluto_standalone.py on a Raspberry Pi 5.
# Run once after cloning the repo:  bash setup.sh
set -e

echo "==> Installing SoapySDR and PlutoSDR plugin via apt..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-soapysdr \
    soapysdr-module-plutosdr \
    python3-pyqt5 \
    libgl1

echo "==> Installing Python dependencies via pip..."
# striqt from upstream NIST repo, CPU-only (no [gpu] extra -- no CUDA on the Pi)
python3 -m pip install --upgrade \
    "striqt @ git+https://github.com/usnistgov/striqt" \
    pyqtgraph \
    numpy \
    psutil

echo ""
echo "==> Checking striqt import..."
python3 - <<'PYCHECK'
import sys

try:
    import striqt
    print(f"  striqt OK: {striqt.__file__}")
except Exception as e:
    print(f"  ERROR: striqt import failed: {e}", file=sys.stderr)
    sys.exit(1)

try:
    from striqt.sensor.lib.sources.deepwave import Airstack1Source
    has_from_spec = hasattr(Airstack1Source, 'from_spec')
    has_arm_spec  = hasattr(Airstack1Source, 'arm_spec')
    print(f"  Airstack1Source.from_spec: {has_from_spec}")
    print(f"  Airstack1Source.arm_spec:  {has_arm_spec}")
    if not has_from_spec or not has_arm_spec:
        print("  WARNING: installed striqt is missing from_spec or arm_spec.", file=sys.stderr)
        print("  The live scripts need the upstream GitHub version.", file=sys.stderr)
        print("  Try: pip install --upgrade 'striqt @ git+https://github.com/usnistgov/striqt'", file=sys.stderr)
except Exception as e:
    print(f"  ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYCHECK

echo ""
echo "==> Checking SoapySDR Pluto enumeration (Pluto must be plugged in via USB)..."
python3 - <<'SOAPYCHECK'
import sys
try:
    import SoapySDR
    found = SoapySDR.Device.enumerate()
    drivers = [d.get('driver', '?') for d in found]
    print(f"  Devices found: {drivers if drivers else 'none'}")
    if any('plutosdr' in d.lower() for d in drivers):
        print("  PlutoSDR detected -- ready to run.")
    else:
        print("  WARNING: No PlutoSDR found. Is it plugged in?", file=sys.stderr)
        print("  If not connected yet, this is fine -- re-run after plugging in.", file=sys.stderr)
except Exception as e:
    print(f"  ERROR enumerating SoapySDR devices: {e}", file=sys.stderr)
SOAPYCHECK

echo ""
echo "Setup complete."
echo "Plug in the PlutoSDR over USB, then run:"
echo "  python3 live/pluto_standalone.py"
