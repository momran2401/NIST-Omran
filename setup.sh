#!/usr/bin/env bash
# ============================================================================
# NIST-Omran radio viewer — one-shot installer / setup TUI.
#
#   sudo bash setup.sh              # interactive (whiptail TUI when available)
#   sudo bash setup.sh --defaults   # no questions: web mode, port 8000
#   bash setup.sh --deps-only       # just python deps into ./.venv (no root)
#
# What it does (idempotent — safe to re-run):
#   1. Detects distro/arch; installs system deps via apt when available
#      (SoapySDR + common SDR driver modules, avahi mDNS, NetworkManager).
#   2. Creates ./.venv and installs live/requirements.txt (+ striqt, optional).
#   3. Asks (TUI) for: default mode (web / hotspot / ethernet / kiosk /
#      terminal), port, mDNS hostname, credentials, hotspot SSID/password,
#      autostart. --defaults answers everything with safe defaults.
#   4. Writes /etc/radio-web/radio.env (0600) with GENERATED credentials and
#      session secret — no default passwords in production.
#   5. Installs + enables the radio-web systemd unit, the Reset-Radio sudoers
#      rule, and (mode-dependent) a NetworkManager hotspot or shared-ethernet
#      profile so a connected laptop gets an address automatically.
#   6. Runs a post-install health check against /health.
#
# Never touches striqt/ (read-only upstream library).
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
ENV_DIR="/etc/radio-web"
ENV_FILE="$ENV_DIR/radio.env"
UNIT_FILE="/etc/systemd/system/radio-web.service"
SERVICE_NAME="radio-web"

MODE="web"           # web | hotspot | ethernet | kiosk | terminal
PORT="8000"
MDNS_HOST="radio"
DEVICE=""            # empty = server default (air8201b); or pluto/auto/...
AUTOSTART="yes"
HOTSPOT_SSID="radio-viewer"
HOTSPOT_PASS=""
INSTALL_STRIQT="ask"
ASSUME_DEFAULTS=0
DEPS_ONLY=0

for arg in "$@"; do
    case "$arg" in
        --defaults)  ASSUME_DEFAULTS=1 ;;
        --deps-only) DEPS_ONLY=1 ;;
        --help|-h)   grep '^#' "$0" | head -25; exit 0 ;;
        *) echo "unknown option: $arg (see --help)" >&2; exit 1 ;;
    esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }

# ── 0. Environment detection ────────────────────────────────────────────────
HAVE_APT=0;     command -v apt-get   >/dev/null && HAVE_APT=1
HAVE_SYSTEMD=0; command -v systemctl >/dev/null && HAVE_SYSTEMD=1
HAVE_NMCLI=0;   command -v nmcli     >/dev/null && HAVE_NMCLI=1
IS_ROOT=0;      [[ ${EUID} -eq 0 ]] && IS_ROOT=1
ARCH="$(uname -m)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"

say "NIST-Omran radio viewer setup  (arch: $ARCH, user: $SERVICE_USER)"
[[ $HAVE_APT -eq 1 ]]     || warn "no apt-get — system packages must be installed manually"
[[ $HAVE_SYSTEMD -eq 1 ]] || warn "no systemd — service autostart will be skipped"

# ── 1. System packages (apt) ────────────────────────────────────────────────
install_system_deps() {
    [[ $HAVE_APT -eq 1 && $IS_ROOT -eq 1 ]] || {
        warn "skipping apt packages (need root + apt). Required: python3-venv,"
        warn "python3-soapysdr + your radio's soapysdr-module-*, avahi-daemon."
        return 0
    }
    say "Installing system packages (apt)…"
    apt-get update -qq
    # Core set. Driver modules are best-effort — not every distro ships all.
    apt-get install -y python3-venv python3-pip python3-dev whiptail curl \
        avahi-daemon libgl1 2>/dev/null || true
    apt-get install -y python3-soapysdr soapysdr-tools 2>/dev/null \
        || warn "python3-soapysdr not available — install SoapySDR manually"
    for mod in soapysdr-module-plutosdr soapysdr-module-rtlsdr \
               soapysdr-module-uhd soapysdr-module-hackrf; do
        apt-get install -y "$mod" 2>/dev/null || true
    done
    # Network modes need NetworkManager.
    if [[ "$MODE" == "hotspot" || "$MODE" == "ethernet" ]]; then
        apt-get install -y network-manager 2>/dev/null || true
    fi
    # Kiosk mode shows the web UI in a local Chromium-family browser.
    if [[ "$MODE" == "kiosk" ]] && ! command -v chromium >/dev/null \
            && ! command -v chromium-browser >/dev/null; then
        apt-get install -y chromium 2>/dev/null \
            || apt-get install -y chromium-browser 2>/dev/null \
            || warn "no Chromium available — install a browser or set kiosk aside"
    fi
}

# ── 2. Python virtualenv ────────────────────────────────────────────────────
install_python_deps() {
    say "Creating venv + installing Python deps…"
    if [[ ! -d "$REPO_ROOT/.venv" ]]; then
        python3 -m venv --system-site-packages "$REPO_ROOT/.venv"
    fi
    # --system-site-packages so the venv sees the apt python3-soapysdr binding.
    "$REPO_ROOT/.venv/bin/pip" install --upgrade pip -q
    "$REPO_ROOT/.venv/bin/pip" install -q -r "$REPO_ROOT/live/requirements.txt"
    if [[ "$INSTALL_STRIQT" != "no" ]]; then
        say "Installing striqt (acquisition/analysis library) — may take a while…"
        "$REPO_ROOT/.venv/bin/pip" install -q \
            'striqt @ git+https://github.com/usnistgov/striqt' \
            || warn "striqt install failed — --demo mode still works; on the AIR-T use its pixi env instead"
    fi
    # Offline plot assets: the repo vendors uPlot; restore it when missing so
    # hotspot/ethernet modes never depend on a CDN.
    if [[ ! -s "$REPO_ROOT/live/web/vendor/uPlot.min.js" ]]; then
        say "Fetching vendored uPlot (missing from checkout)…"
        mkdir -p "$REPO_ROOT/live/web/vendor"
        curl -fsSL https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js \
            -o "$REPO_ROOT/live/web/vendor/uPlot.min.js" 2>/dev/null || true
        curl -fsSL https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css \
            -o "$REPO_ROOT/live/web/vendor/uPlot.min.css" 2>/dev/null || true
    fi
    # Sanity: can the core import?
    "$REPO_ROOT/.venv/bin/python3" - <<'PYCHECK' || warn "core import check failed"
import sys
sys.path.insert(0, "live")
import core
from core import devices
print("  live/core import OK")
try:
    found = devices.discover()
    print(f"  radios detected: {[f['label'] for f in found] or 'none'}")
except RuntimeError as e:
    print(f"  (device discovery unavailable: {e})")
PYCHECK
}

# ── 3. Interactive questions ────────────────────────────────────────────────
ask_tui() {
    [[ $ASSUME_DEFAULTS -eq 1 ]] && return 0
    if command -v whiptail >/dev/null && [[ -t 0 ]]; then
        MODE=$(whiptail --title "Radio viewer setup" --nocancel --menu \
            "Default mode (started on boot / by 'systemctl start radio-web'):" \
            18 72 5 \
            web      "Web server on the existing network (default)" \
            hotspot  "Web server + own Wi-Fi access point (no internet needed)" \
            ethernet "Web server + plug-and-play Ethernet (laptop direct)" \
            kiosk    "Web UI fullscreen on the radio's own display" \
            terminal "No service — run the curses monitor manually" \
            3>&1 1>&2 2>&3) || true
        PORT=$(whiptail --title "Port" --nocancel --inputbox \
            "Web server port:" 9 50 "$PORT" 3>&1 1>&2 2>&3) || true
        MDNS_HOST=$(whiptail --title "Hostname" --nocancel --inputbox \
            "mDNS hostname (reach the radio at <name>.local):" 9 60 \
            "$MDNS_HOST" 3>&1 1>&2 2>&3) || true
        DEVICE=$(whiptail --title "Radio" --nocancel --menu \
            "Which radio will this host drive?" 17 64 6 \
            ""       "AIR8201B (default)" \
            air7201b "AIR7201B" \
            air7101b "AIR7101B" \
            pluto    "PlutoSDR" \
            auto     "Auto-detect at startup (SoapySDR enumeration)" \
            demo     "Demo (synthetic IQ, no hardware)" \
            3>&1 1>&2 2>&3) || true
        if [[ "$MODE" == "hotspot" ]]; then
            HOTSPOT_SSID=$(whiptail --nocancel --inputbox \
                "Hotspot SSID:" 9 50 "$HOTSPOT_SSID" 3>&1 1>&2 2>&3) || true
            HOTSPOT_PASS=$(whiptail --nocancel --passwordbox \
                "Hotspot password (min 8 chars; empty = generate):" 9 60 \
                3>&1 1>&2 2>&3) || true
        fi
        if whiptail --title "Autostart" --yesno \
            "Enable the radio-web service to start on boot?" 8 55; then
            AUTOSTART="yes"; else AUTOSTART="no"; fi
        if whiptail --title "striqt" --yesno \
            "Install the striqt library from GitHub into the venv?\n(Choose No on the AIR-T if its pixi env already provides striqt.)" 10 66; then
            INSTALL_STRIQT="yes"; else INSTALL_STRIQT="no"; fi
    else
        echo "(whiptail/tty unavailable — plain prompts; Enter accepts defaults)"
        read -rp "Mode [web/hotspot/ethernet/kiosk/terminal] ($MODE): " a || true
        MODE="${a:-$MODE}"
        read -rp "Port ($PORT): " a || true;             PORT="${a:-$PORT}"
        read -rp "mDNS hostname ($MDNS_HOST): " a || true; MDNS_HOST="${a:-$MDNS_HOST}"
        read -rp "Device [air8201b default/air7201b/air7101b/pluto/auto/demo] (): " a || true
        DEVICE="${a:-}"; [[ "$DEVICE" == "air8201b" ]] && DEVICE=""
        read -rp "Autostart on boot? [yes/no] ($AUTOSTART): " a || true
        AUTOSTART="${a:-$AUTOSTART}"
        read -rp "Install striqt from GitHub? [yes/no] (yes): " a || true
        INSTALL_STRIQT="${a:-yes}"
    fi
    [[ "$INSTALL_STRIQT" == "ask" ]] && INSTALL_STRIQT="yes"
    return 0
}

# ── 4. Credentials + environment file ──────────────────────────────────────
genpw() { openssl rand -hex 12 2>/dev/null || head -c24 /dev/urandom | base64 | tr -d '+/=' ; }

write_env_file() {
    [[ $IS_ROOT -eq 1 ]] || { warn "not root — skipping $ENV_FILE"; return 0; }
    say "Writing $ENV_FILE (credentials are GENERATED, not the repo defaults)…"
    mkdir -p "$ENV_DIR"
    local admin_pass viewer_pass intern_pass secret
    if [[ -f "$ENV_FILE" ]] && grep -q ADMIN_PASS "$ENV_FILE"; then
        echo "  existing credentials kept (delete $ENV_FILE to regenerate)"
        # Refresh only the mode/port/device lines, preserving secrets.
        sed -i -e "s/^RADIO_MODE=.*/RADIO_MODE=\"$MODE\"/" \
               -e "s/^RADIO_PORT=.*/RADIO_PORT=\"$PORT\"/" \
               -e "s/^RADIO_DEVICE=.*/RADIO_DEVICE=\"$DEVICE\"/" "$ENV_FILE"
        return 0
    fi
    admin_pass="$(genpw)"; viewer_pass="$(genpw)"; intern_pass="$(genpw)"
    secret="$(openssl rand -hex 32 2>/dev/null || genpw)"
    cat > "$ENV_FILE" <<EOF
# Generated by setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ) — mode/creds for radio-web.
# Edit + 'systemctl restart radio-web' to apply. chmod 600 — keep it that way.
# Values are quoted for systemd EnvironmentFile parsing.
RADIO_MODE="$MODE"
RADIO_PORT="$PORT"
RADIO_DEVICE="$DEVICE"
RADIO_SERVICE_NAME="$SERVICE_NAME"
RADIO_EXTRA_ARGS=""
ADMIN_USER="admin"
ADMIN_PASS="$admin_pass"
VIEWER_USER="viewer"
VIEWER_PASS="$viewer_pass"
INTERN_USER="intern"
INTERN_PASS="$intern_pass"
RADIO_SESSION_SECRET="$secret"
EOF
    chmod 600 "$ENV_FILE"
    CREDS_NOTE="admin / $admin_pass   viewer / $viewer_pass   intern / $intern_pass"
}

# ── 5. systemd unit + sudoers + mDNS ───────────────────────────────────────
install_service() {
    [[ $IS_ROOT -eq 1 && $HAVE_SYSTEMD -eq 1 ]] || { warn "skipping systemd unit"; return 0; }
    [[ "$MODE" == "terminal" ]] && { echo "  terminal mode — no service installed"; return 0; }
    say "Installing systemd unit $UNIT_FILE…"
    sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
        -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        -e "s|@RADIO_MODE@|$MODE|g" \
        "$REPO_ROOT/deploy/radio-web.service.template" > "$UNIT_FILE"
    chmod +x "$REPO_ROOT/deploy/run_service.sh"
    systemctl daemon-reload
    say "Installing Reset-Radio sudoers rule…"
    bash "$REPO_ROOT/live/install_radio_web_sudoers.sh" "$SERVICE_USER" "$SERVICE_NAME" \
        || warn "sudoers install failed — the Reset Radio button won't work"
    if [[ -n "$MDNS_HOST" ]]; then
        say "mDNS: radio will be reachable at ${MDNS_HOST}.local"
        hostnamectl set-hostname "$MDNS_HOST" 2>/dev/null \
            || warn "could not set hostname (set it manually for ${MDNS_HOST}.local)"
        systemctl enable --now avahi-daemon 2>/dev/null || warn "avahi not available"
    fi
    # Open the port when UFW is enforcing (common on Ubuntu images).
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "^Status: active"; then
        ufw allow "$PORT/tcp" >/dev/null 2>&1 \
            && echo "  ufw: allowed $PORT/tcp" \
            || warn "could not add the ufw rule for $PORT/tcp"
    fi
    if [[ "$AUTOSTART" == "yes" ]]; then
        systemctl enable "$SERVICE_NAME" >/dev/null
        systemctl restart "$SERVICE_NAME"
        echo "  service enabled + started"
    else
        systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
        echo "  autostart disabled (start manually: systemctl start $SERVICE_NAME)"
    fi
}

# ── 6. Network profiles (hotspot / plug-and-play ethernet) ─────────────────
setup_network() {
    [[ $IS_ROOT -eq 1 ]] || return 0
    case "$MODE" in
    hotspot)
        [[ $HAVE_NMCLI -eq 1 ]] || { warn "hotspot needs NetworkManager (nmcli)"; return 0; }
        local wifi_dev
        wifi_dev="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')"
        [[ -n "$wifi_dev" ]] || { warn "no Wi-Fi interface found — hotspot skipped (USB dongle needed?)"; return 0; }
        [[ -n "$HOTSPOT_PASS" ]] || HOTSPOT_PASS="$(genpw)"
        say "Configuring Wi-Fi access point '$HOTSPOT_SSID' on $wifi_dev…"
        nmcli connection delete radio-hotspot >/dev/null 2>&1 || true
        nmcli connection add type wifi ifname "$wifi_dev" con-name radio-hotspot \
            autoconnect yes ssid "$HOTSPOT_SSID" \
            802-11-wireless.mode ap 802-11-wireless.band bg \
            ipv4.method shared wifi-sec.key-mgmt wpa-psk \
            wifi-sec.psk "$HOTSPOT_PASS" >/dev/null
        HOTSPOT_NOTE="SSID: $HOTSPOT_SSID   password: $HOTSPOT_PASS   URL: http://10.42.0.1:$PORT"
        ;;
    ethernet)
        [[ $HAVE_NMCLI -eq 1 ]] || { warn "ethernet mode needs NetworkManager (nmcli)"; return 0; }
        local eth_dev
        eth_dev="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="ethernet"{print $1; exit}')"
        [[ -n "$eth_dev" ]] || { warn "no ethernet interface found"; return 0; }
        say "Configuring plug-and-play (shared) Ethernet on $eth_dev…"
        # ipv4.method=shared: the radio serves DHCP on this port, so a directly
        # connected laptop configures itself — open http://10.42.0.1:PORT (or
        # http://<hostname>.local:PORT via mDNS).
        nmcli connection delete radio-ethernet >/dev/null 2>&1 || true
        nmcli connection add type ethernet ifname "$eth_dev" con-name radio-ethernet \
            autoconnect yes ipv4.method shared >/dev/null
        ETHERNET_NOTE="plug a laptop into $eth_dev and open http://10.42.0.1:$PORT (or http://${MDNS_HOST}.local:$PORT)"
        ;;
    esac
}

# ── 7. Health check ────────────────────────────────────────────────────────
health_check() {
    [[ "$MODE" == "terminal" ]] && return 0
    [[ $HAVE_SYSTEMD -eq 1 && $IS_ROOT -eq 1 && "$AUTOSTART" == "yes" ]] || return 0
    say "Post-install health check…"
    for _ in $(seq 1 20); do
        if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then
            curl -fsS "http://localhost:$PORT/health" | head -c 400; echo
            echo "  HEALTHY."
            return 0
        fi
        sleep 1
    done
    warn "service did not answer /health in 20 s — check: journalctl -u $SERVICE_NAME -n 50"
}

# ── main ────────────────────────────────────────────────────────────────────
if [[ $DEPS_ONLY -eq 1 ]]; then
    INSTALL_STRIQT="${INSTALL_STRIQT/ask/yes}"
    install_python_deps
    exit 0
fi

ask_tui
install_system_deps
install_python_deps
write_env_file
install_service
setup_network
health_check

say "Setup complete."
echo "  mode:      $MODE"
echo "  device:    ${DEVICE:-air8201b (default)}"
[[ "$MODE" != "terminal" ]] && echo "  URL:       http://${MDNS_HOST}.local:$PORT  (or the host's IP)"
[[ -n "${CREDS_NOTE:-}" ]]    && echo "  logins:    $CREDS_NOTE"
[[ -n "${HOTSPOT_NOTE:-}" ]]  && echo "  hotspot:   $HOTSPOT_NOTE"
[[ -n "${ETHERNET_NOTE:-}" ]] && echo "  ethernet:  $ETHERNET_NOTE"
[[ -n "${CREDS_NOTE:-}" ]]    && echo "  (credentials are stored only in $ENV_FILE — save them now)"
echo "  logs:      journalctl -u $SERVICE_NAME -f"
echo "  terminal:  python3 live/striqt_standalone_terminal.py --demo"
