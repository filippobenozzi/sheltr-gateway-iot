#!/usr/bin/env bash
set -euo pipefail

APP_NAME="algodomoiot"
APP_USER="${APP_USER:-algodomoiot}"
APP_GROUP="${APP_GROUP:-$APP_USER}"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
NEWT_SERVICE_FILE="/etc/systemd/system/newt.service"
ENV_FILE="/etc/default/${APP_NAME}"
NEWT_ENV_FILE="/etc/default/newt"
ADMIN_DIR="/usr/local/lib/algodomoiot-admin"
SUDOERS_FILE="/etc/sudoers.d/${APP_NAME}-admin"
NEED_REBOOT=0

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Esegui come root: sudo ./install_raspberry.sh"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3
fi

if ! command -v fuser >/dev/null 2>&1; then
  apt-get update
  apt-get install -y psmisc
fi

if ! command -v curl >/dev/null 2>&1; then
  apt-get update
  apt-get install -y curl
fi

if ! command -v sudo >/dev/null 2>&1; then
  apt-get update
  apt-get install -y sudo
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi

if getent group dialout >/dev/null 2>&1; then
  usermod -a -G dialout "${APP_USER}" || true
fi

if ! command -v newt >/dev/null 2>&1; then
  echo "Installo newt..."
  curl -fsSL https://static.pangolin.net/get-newt.sh | bash || true
fi

# Disabilita in modo persistente il getty sulla seriale
for unit in serial-getty@ttyS0.service serial-getty@serial0.service; do
  systemctl disable --now "${unit}" >/dev/null 2>&1 || true
  systemctl mask "${unit}" >/dev/null 2>&1 || true
done

# Rimuove console seriale dal kernel cmdline (persistente ai reboot)
CMDLINE_FILE=""
if [[ -f /boot/firmware/cmdline.txt ]]; then
  CMDLINE_FILE="/boot/firmware/cmdline.txt"
elif [[ -f /boot/cmdline.txt ]]; then
  CMDLINE_FILE="/boot/cmdline.txt"
fi

if [[ -n "${CMDLINE_FILE}" ]]; then
  CURRENT_CMDLINE="$(tr -d '\n' < "${CMDLINE_FILE}")"
  UPDATED_CMDLINE="$(printf '%s\n' "${CURRENT_CMDLINE}" \
    | sed -E 's/(^| )console=serial0,[^ ]+//g; s/(^| )console=ttyAMA0,[^ ]+//g; s/(^| )console=ttyS0,[^ ]+//g; s/[[:space:]]+/ /g; s/^ //; s/ $//')"

  if [[ "${UPDATED_CMDLINE}" != "${CURRENT_CMDLINE}" && -n "${UPDATED_CMDLINE}" ]]; then
    cp "${CMDLINE_FILE}" "${CMDLINE_FILE}.bak.$(date +%Y%m%d%H%M%S)"
    printf '%s\n' "${UPDATED_CMDLINE}" > "${CMDLINE_FILE}"
    NEED_REBOOT=1
  fi
fi

mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}" "${ADMIN_DIR}"

# Copia codice applicativo (esclude file pesanti/non necessari al runtime)
tar \
  --exclude='.git' \
  --exclude='.tmp_ocr' \
  --exclude='protocollo-1.6.pdf' \
  -C "${SRC_DIR}" -cf - . | tar -C "${INSTALL_DIR}" -xf -

if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
  cp "${INSTALL_DIR}/data/config.json" "${CONFIG_DIR}/config.json"
fi

if [[ ! -f "${CONFIG_DIR}/state.json" ]]; then
  cp "${INSTALL_DIR}/data/state.json" "${CONFIG_DIR}/state.json"
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${INSTALL_DIR}/deploy/algodomoiot.env" "${ENV_FILE}"
fi

if [[ ! -f "${NEWT_ENV_FILE}" ]]; then
  cp "${INSTALL_DIR}/deploy/newt.env" "${NEWT_ENV_FILE}"
fi

install -m 644 "${INSTALL_DIR}/deploy/algodomoiot.service" "${SERVICE_FILE}"
install -m 644 "${INSTALL_DIR}/deploy/newt.service" "${NEWT_SERVICE_FILE}"
install -m 750 "${INSTALL_DIR}/deploy/admin_control.sh" "${ADMIN_DIR}/admin_control.sh"
install -m 750 "${INSTALL_DIR}/deploy/apply_network.sh" "${ADMIN_DIR}/apply_network.sh"

cat > "${SUDOERS_FILE}" <<EOF
${APP_USER} ALL=(root) NOPASSWD: ${ADMIN_DIR}/admin_control.sh *
EOF

chown -R "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}" "${CONFIG_DIR}"
chown root:root "${ADMIN_DIR}/admin_control.sh" "${ADMIN_DIR}/apply_network.sh" "${SUDOERS_FILE}"
chmod 755 "${ADMIN_DIR}"
chmod 750 "${ADMIN_DIR}/admin_control.sh" "${ADMIN_DIR}/apply_network.sh"
chmod 440 "${SUDOERS_FILE}"
chmod 750 "${INSTALL_DIR}" "${CONFIG_DIR}"
chmod 640 "${CONFIG_DIR}/config.json" "${CONFIG_DIR}/state.json"
chmod 644 "${ENV_FILE}" "${SERVICE_FILE}" "${NEWT_SERVICE_FILE}" "${NEWT_ENV_FILE}"

if command -v visudo >/dev/null 2>&1; then
  visudo -cf "${SUDOERS_FILE}"
fi

systemctl daemon-reload
systemctl enable --now "${APP_NAME}.service"
systemctl enable newt.service
systemctl restart "${APP_NAME}.service"

if grep -q '^NEWT_ENABLED=1' "${NEWT_ENV_FILE}" \
  && grep -q '^NEWT_ID="[^"]\\+"' "${NEWT_ENV_FILE}" \
  && grep -q '^NEWT_SECRET="[^"]\\+"' "${NEWT_ENV_FILE}" \
  && grep -q '^NEWT_ENDPOINT="[^"]\\+"' "${NEWT_ENV_FILE}"; then
  systemctl restart newt.service || true
else
  systemctl stop newt.service >/dev/null 2>&1 || true
fi

echo
systemctl --no-pager --full status "${APP_NAME}.service" || true
echo
systemctl --no-pager --full status newt.service || true

echo
echo "Installazione completata."
echo "Config: ${CONFIG_DIR}/config.json"
echo "Override env: ${ENV_FILE}"
echo "Config newt: ${NEWT_ENV_FILE}"
echo "Pagine: http://<IP_RASPBERRY>/ (control) e /config"

if [[ "${NEED_REBOOT}" -eq 1 ]]; then
  echo "Nota: cmdline seriale aggiornato. Esegui un reboot per applicare completamente la modifica."
fi
