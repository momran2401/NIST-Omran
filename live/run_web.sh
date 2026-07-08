#!/usr/bin/env bash
# run_web.sh — Start the striqt web viewer server + Cloudflare Tunnel.
#
# Usage:
#   bash live/run_web.sh                  # real AIR8201B radio (default device)
#   bash live/run_web.sh --device pluto   # PlutoSDR host (P3-1)
#   bash live/run_web.sh --device auto    # enumerate SoapySDR, pick the radio
#   bash live/run_web.sh --demo           # synthetic IQ, no hardware
#   bash live/run_web.sh --quantize       # uint8 waterfall (smaller frames)
#
# Extra args are passed directly to striqt_web_server.py, so you can combine:
#   bash live/run_web.sh --demo --fps 10 --quantize --channels 1
#
# Requirements:
#   pip install fastapi 'uvicorn[standard]'
#   cloudflared in PATH  (see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PORT=${PORT:-8000}

# --- Check dependencies ---
if ! command -v cloudflared &>/dev/null; then
    echo ""
    echo "ERROR: 'cloudflared' not found in PATH."
    echo ""
    echo "Install it on the Deepwave / AIR-T (ARM64):"
    echo "  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \\"
    echo "       -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
    echo ""
    echo "Or on x86-64:"
    echo "  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \\"
    echo "       -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
    echo ""
    echo "Re-run this script after installing cloudflared."
    exit 1
fi

python3 -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "ERROR: fastapi or uvicorn not installed."
    echo "Run:  pip install fastapi 'uvicorn[standard]'"
    exit 1
}

# --- Start the Python web server in the background ---
echo ""
echo "Starting striqt web server on port ${PORT}…"
python3 "$SCRIPT_DIR/striqt_web_server.py" --port "$PORT" "$@" &
SERVER_PID=$!

# Give uvicorn a moment to bind
sleep 1.5

# --- Start Cloudflare Tunnel ---
echo ""
echo "Starting Cloudflare Tunnel → http://localhost:${PORT}"
echo "(The public URL will appear below. Share it to view from any browser.)"
echo ""
cloudflared tunnel --url "http://localhost:${PORT}" &
TUNNEL_PID=$!

# --- Wait; clean up both on exit ---
cleanup() {
    echo ""
    echo "Shutting down…"
    kill "$SERVER_PID" "$TUNNEL_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait "$SERVER_PID"
