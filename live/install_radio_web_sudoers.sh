#!/usr/bin/env bash
# install_radio_web_sudoers.sh — one-time setup so the web viewer's "Reset Radio"
# button can restart the radio service WITHOUT storing a sudo password anywhere.
#
# It writes /etc/sudoers.d/radio-web granting the service user passwordless sudo
# for EXACTLY ONE command: `systemctl restart <service>`. Nothing else. No
# password is stored in code, on disk, or in the app — the sudoers rule is the
# only privilege granted, and it is scoped to a single unit.
#
# Usage (run once, as root, on the radio host):
#   sudo bash live/install_radio_web_sudoers.sh                 # user=$SUDO_USER, service=radio-web
#   sudo bash live/install_radio_web_sudoers.sh deepwave        # explicit user
#   sudo bash live/install_radio_web_sudoers.sh deepwave radio-web
#
# The service name must match RADIO_SERVICE_NAME used by striqt_web_server.py
# (default "radio-web"). The web server calls:  sudo -n systemctl restart <service>
#
# To undo:  sudo rm /etc/sudoers.d/radio-web

set -euo pipefail

SERVICE_USER="${1:-${SUDO_USER:-$(id -un)}}"
SERVICE_NAME="${2:-radio-web}"
SUDOERS_FILE="/etc/sudoers.d/radio-web"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (use: sudo bash $0 ...)" >&2
    exit 1
fi

# Resolve the absolute systemctl path so the rule is exact (sudoers matches the
# full command path).
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"

echo "Service user : $SERVICE_USER"
echo "Service name : $SERVICE_NAME"
echo "systemctl    : $SYSTEMCTL"
echo "Writing rule : $SUDOERS_FILE"

# Write to a temp file, validate with visudo, then install atomically. An invalid
# sudoers file can lock you out of sudo entirely, so NEVER install unvalidated.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<EOF
# Managed by live/install_radio_web_sudoers.sh — lets the striqt web viewer's
# "Reset Radio" button restart the radio service with no password. Scoped to a
# single command; do not broaden.
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE_NAME
EOF

if ! visudo -cf "$TMP"; then
    echo "ERROR: generated sudoers rule failed validation; not installing." >&2
    exit 1
fi

install -m 0440 -o root -g root "$TMP" "$SUDOERS_FILE"
echo "Installed. Verifying passwordless restart is now permitted…"

if sudo -n -u "$SERVICE_USER" true 2>/dev/null; then
    :  # sudo -n works for the user in general
fi
# Best-effort check: confirm the exact command is allowed for the user.
if su -s /bin/bash -c "sudo -n -l $SYSTEMCTL restart $SERVICE_NAME" "$SERVICE_USER" >/dev/null 2>&1; then
    echo "OK: '$SERVICE_USER' may run 'sudo -n systemctl restart $SERVICE_NAME' without a password."
else
    echo "WARNING: verification could not confirm the rule (this can be normal depending"
    echo "         on host policy). Test manually as '$SERVICE_USER':"
    echo "           sudo -n systemctl restart $SERVICE_NAME"
fi
