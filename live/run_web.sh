#!/usr/bin/env bash
# run_web.sh — Start the striqt web viewer server, optionally with a
# Cloudflare Tunnel for internet access.
#
# Usage:
#   bash live/run_web.sh                  # LAN-only web server (no tunnel)
#   bash live/run_web.sh --tunnel         # + Cloudflare Tunnel (public URL)
#   bash live/run_web.sh --device pluto   # PlutoSDR host
#   bash live/run_web.sh --device auto    # enumerate SoapySDR, pick the radio
#   bash live/run_web.sh --demo           # synthetic IQ, no hardware
#   bash live/run_web.sh --quantize       # uint8 waterfall (smaller frames)
#
# All non---tunnel args pass straight to striqt_web_server.py, so combine:
#   bash live/run_web.sh --tunnel --demo --fps 10 --quantize --channels 1
#
# Requirements:
#   pip install -r live/requirements.txt          (or: bash setup.sh --deps-only)
#   --tunnel additionally needs cloudflared in PATH.
#
# Authentication (three roles):
#   Production deployments should use setup.sh, which GENERATES credentials
#   into /etc/radio-web/radio.env. For ad-hoc runs you can override here:
#     ADMIN_USER/ADMIN_PASS, VIEWER_USER/VIEWER_PASS, INTERN_USER/INTERN_PASS
#     RADIO_SESSION_SECRET   cookie-signing key, e.g. "$(openssl rand -hex 32)"
#     RADIO_AUTH_DISABLE=1   auth OFF for local/demo; everyone becomes admin.
#   Without overrides the server falls back to the built-in dev defaults and
#   prints a loud warning — do not expose that publicly.
#
# "Reset Radio" button (admin-only): restarts the systemd unit named by
#   RADIO_SERVICE_NAME (default "radio-web") via `sudo -n systemctl restart`.
#   One-time host setup:  sudo bash live/install_radio_web_sudoers.sh <user>

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PORT=${PORT:-8000}

# Prefer the setup.sh venv when it exists.
PY="$REPO_ROOT/.venv/bin/python3"
[[ -x "$PY" ]] || PY="$(command -v python3)"

# --tunnel is ours; everything else goes to the server.
TUNNEL=0
ARGS=()
for a in "$@"; do
    if [[ "$a" == "--tunnel" ]]; then TUNNEL=1; else ARGS+=("$a"); fi
done

"$PY" -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "ERROR: fastapi or uvicorn not installed."
    echo "Run:  pip install -r live/requirements.txt   (or: bash setup.sh --deps-only)"
    exit 1
}

if [[ $TUNNEL -eq 1 ]] && ! command -v cloudflared &>/dev/null; then
    echo "WARNING: --tunnel requested but 'cloudflared' is not in PATH."
    echo "Install (ARM64):"
    echo "  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \\"
    echo "       -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
    echo "Continuing WITHOUT the tunnel (LAN-only)."
    TUNNEL=0
fi

echo ""
echo "Starting striqt web server on port ${PORT}…"
"$PY" "$SCRIPT_DIR/striqt_web_server.py" --port "$PORT" "${ARGS[@]}" &
SERVER_PID=$!

TUNNEL_PID=""
if [[ $TUNNEL -eq 1 ]]; then
    sleep 1.5
    echo ""
    echo "Starting Cloudflare Tunnel → http://localhost:${PORT}"
    echo "(The public URL will appear below. Share it to view from any browser.)"
    echo ""
    cloudflared tunnel --url "http://localhost:${PORT}" &
    TUNNEL_PID=$!
else
    echo "(LAN-only. Add --tunnel for a public Cloudflare URL.)"
fi

cleanup() {
    echo ""
    echo "Shutting down…"
    kill "$SERVER_PID" ${TUNNEL_PID:+"$TUNNEL_PID"} 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait "$SERVER_PID"
