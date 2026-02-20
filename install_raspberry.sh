#!/usr/bin/env bash
set -euo pipefail

APP_NAME="algodomoiot"
APP_USER="${APP_USER:-algodomoiot}"
APP_GROUP="${APP_GROUP:-$APP_USER}"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="/etc/default/${APP_NAME}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Esegui come root: sudo ./install_raspberry.sh"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi

if getent group dialout >/dev/null 2>&1; then
  usermod -a -G dialout "${APP_USER}" || true
fi

mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}"

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

install -m 644 "${INSTALL_DIR}/deploy/algodomoiot.service" "${SERVICE_FILE}"

chown -R "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}" "${CONFIG_DIR}"
chmod 750 "${INSTALL_DIR}" "${CONFIG_DIR}"
chmod 640 "${CONFIG_DIR}/config.json" "${CONFIG_DIR}/state.json"
chmod 644 "${ENV_FILE}" "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable --now "${APP_NAME}.service"
systemctl restart "${APP_NAME}.service"

echo
systemctl --no-pager --full status "${APP_NAME}.service" || true

echo
echo "Installazione completata."
echo "Config: ${CONFIG_DIR}/config.json"
echo "Override env: ${ENV_FILE}"
echo "Pagine: http://<IP_RASPBERRY>:8080/config e /control"
