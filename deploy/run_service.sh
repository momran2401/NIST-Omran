#!/usr/bin/env bash
# Service entrypoint — picks the frontend from RADIO_MODE (set in
# /etc/radio-web/radio.env by setup.sh). Runs inside the repo's venv when one
# exists, else the system python3.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PY="$REPO_ROOT/.venv/bin/python3"
[[ -x "$PY" ]] || PY="$(command -v python3)"

PORT="${RADIO_PORT:-8000}"
DEVICE_ARGS=()
[[ -n "${RADIO_DEVICE:-}" ]] && DEVICE_ARGS=(--device "$RADIO_DEVICE")
# RADIO_EXTRA_ARGS: optional extra CLI flags (e.g. "--quantize --fps 10")
read -r -a EXTRA <<< "${RADIO_EXTRA_ARGS:-}"

case "${RADIO_MODE:-web}" in
    kiosk)
        exec "$PY" "$REPO_ROOT/live/striqt_kiosk.py" \
            --port "$PORT" "${DEVICE_ARGS[@]}" -- "${EXTRA[@]}"
        ;;
    web|hotspot|ethernet|*)
        # hotspot/ethernet differ only in NETWORK config (done at setup time);
        # the service itself is always the web server.
        exec "$PY" "$REPO_ROOT/live/striqt_web_server.py" \
            --port "$PORT" "${DEVICE_ARGS[@]}" "${EXTRA[@]}"
        ;;
esac
