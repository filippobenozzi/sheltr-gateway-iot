#!/usr/bin/env bash
set -euo pipefail

APP_NAME="sheltr"
APP_SERVICE="${APP_NAME}.service"
NEWT_SERVICE="newt.service"
MQTT_SERVICE="sheltr-mqtt.service"
WATCHDOG_SERVICE="newt-watchdog.service"
WATCHDOG_TIMER="newt-watchdog.timer"
LEGACY_APP_SERVICE="algodomoiot.service"
LEGACY_MQTT_SERVICE="algodomoiot-mqtt.service"
SYSTEMD_DIR="/etc/systemd/system"
ADMIN_DIR="/usr/local/lib/sheltr-admin"
NEWT_ENV_FILE="/etc/${APP_NAME}/newt.env"
MQTT_ENV_FILE="/etc/${APP_NAME}/mqtt.env"
DEPLOY_DIR="${SHELTR_DEPLOY_DIR:-${ALGODOMO_DEPLOY_DIR:-/opt/${APP_NAME}/deploy}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${DEPLOY_DIR}/sheltr.service" ]]; then
  if [[ -f "${SCRIPT_DIR}/sheltr.service" ]]; then
    DEPLOY_DIR="${SCRIPT_DIR}"
  elif [[ -f "${SCRIPT_DIR}/../deploy/sheltr.service" ]]; then
    DEPLOY_DIR="$(cd "${SCRIPT_DIR}/../deploy" && pwd)"
  fi
fi

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Esegui come root: sudo $0 <enable-all|disable-all|status>"
    exit 1
  fi
}

ensure_deploy_files() {
  local required=(
    "sheltr.service"
    "newt.service"
    "sheltr-mqtt.service"
    "newt-watchdog.service"
    "newt-watchdog.timer"
    "admin_control.sh"
    "apply_network.sh"
    "newt_watchdog.sh"
    "mqtt.env"
  )
  local item
  for item in "${required[@]}"; do
    if [[ ! -f "${DEPLOY_DIR}/${item}" ]]; then
      echo "File mancante: ${DEPLOY_DIR}/${item}" >&2
      exit 1
    fi
  done
}

is_newt_configured() {
  [[ -f "${NEWT_ENV_FILE}" ]] || return 1
  grep -q '^NEWT_ENABLED=1' "${NEWT_ENV_FILE}" \
    && grep -q '^NEWT_ID="[^"]\+"' "${NEWT_ENV_FILE}" \
    && grep -q '^NEWT_SECRET="[^"]\+"' "${NEWT_ENV_FILE}" \
    && (grep -q '^PANGOLIN_ENDPOINT="[^"]\+"' "${NEWT_ENV_FILE}" || grep -q '^NEWT_ENDPOINT="[^"]\+"' "${NEWT_ENV_FILE}")
}

install_runtime_files() {
  ensure_deploy_files
  mkdir -p "${ADMIN_DIR}"
  install -m 644 "${DEPLOY_DIR}/sheltr.service" "${SYSTEMD_DIR}/${APP_SERVICE}"
  install -m 644 "${DEPLOY_DIR}/newt.service" "${SYSTEMD_DIR}/${NEWT_SERVICE}"
  install -m 644 "${DEPLOY_DIR}/sheltr-mqtt.service" "${SYSTEMD_DIR}/${MQTT_SERVICE}"
  install -m 644 "${DEPLOY_DIR}/newt-watchdog.service" "${SYSTEMD_DIR}/${WATCHDOG_SERVICE}"
  install -m 644 "${DEPLOY_DIR}/newt-watchdog.timer" "${SYSTEMD_DIR}/${WATCHDOG_TIMER}"
  install -m 750 "${DEPLOY_DIR}/admin_control.sh" "${ADMIN_DIR}/admin_control.sh"
  install -m 750 "${DEPLOY_DIR}/apply_network.sh" "${ADMIN_DIR}/apply_network.sh"
  install -m 750 "${DEPLOY_DIR}/newt_watchdog.sh" "${ADMIN_DIR}/newt_watchdog.sh"
  chown root:root "${ADMIN_DIR}/admin_control.sh" "${ADMIN_DIR}/apply_network.sh" "${ADMIN_DIR}/newt_watchdog.sh"
}

is_mqtt_configured() {
  [[ -f "${MQTT_ENV_FILE}" ]] || return 1
  grep -q '^MQTT_ENABLED=1' "${MQTT_ENV_FILE}" \
    && grep -q '^MQTT_HOST="[^"]\+"' "${MQTT_ENV_FILE}" \
    && grep -q '^MQTT_BASE_TOPIC="[^"]\+"' "${MQTT_ENV_FILE}" \
    && (grep -q '^SHELTR_TOKEN="[^"]\+"' "${MQTT_ENV_FILE}" || grep -q '^ALGODOMO_TOKEN="[^"]\+"' "${MQTT_ENV_FILE}")
}

lock_serial_for_app() {
  local unit
  for unit in serial-getty@ttyS0.service serial-getty@serial0.service; do
    systemctl disable --now "${unit}" >/dev/null 2>&1 || true
    systemctl mask "${unit}" >/dev/null 2>&1 || true
  done
  if command -v fuser >/dev/null 2>&1; then
    fuser -k /dev/ttyS0 >/dev/null 2>&1 || true
    [[ -e /dev/serial0 ]] && fuser -k /dev/serial0 >/dev/null 2>&1 || true
  fi
}

print_status() {
  systemctl --no-pager --full status "${APP_SERVICE}" || true
  echo
  systemctl --no-pager --full status "${NEWT_SERVICE}" || true
  echo
  systemctl --no-pager --full status "${MQTT_SERVICE}" || true
  echo
  systemctl --no-pager --full status "${WATCHDOG_TIMER}" || true
}

enable_all() {
  install_runtime_files
  lock_serial_for_app
  systemctl disable --now "${LEGACY_APP_SERVICE}" >/dev/null 2>&1 || true
  systemctl disable --now "${LEGACY_MQTT_SERVICE}" >/dev/null 2>&1 || true
  rm -f "${SYSTEMD_DIR}/${LEGACY_APP_SERVICE}" "${SYSTEMD_DIR}/${LEGACY_MQTT_SERVICE}"
  systemctl daemon-reload
  systemctl enable --now "${APP_SERVICE}"
  systemctl enable "${NEWT_SERVICE}"
  systemctl enable "${MQTT_SERVICE}"
  systemctl enable --now "${WATCHDOG_TIMER}"
  if is_newt_configured; then
    systemctl restart "${NEWT_SERVICE}" || true
  else
    systemctl stop "${NEWT_SERVICE}" >/dev/null 2>&1 || true
  fi
  if is_mqtt_configured; then
    systemctl restart "${MQTT_SERVICE}" || true
  else
    systemctl stop "${MQTT_SERVICE}" >/dev/null 2>&1 || true
  fi
  echo "Attivazione completata."
  print_status
}

disable_all() {
  systemctl disable --now "${WATCHDOG_TIMER}" >/dev/null 2>&1 || true
  systemctl stop "${WATCHDOG_SERVICE}" >/dev/null 2>&1 || true
  systemctl disable --now "${MQTT_SERVICE}" >/dev/null 2>&1 || true
  systemctl disable --now "${NEWT_SERVICE}" >/dev/null 2>&1 || true
  systemctl disable --now "${APP_SERVICE}" >/dev/null 2>&1 || true
  systemctl disable --now "${LEGACY_MQTT_SERVICE}" >/dev/null 2>&1 || true
  systemctl disable --now "${LEGACY_APP_SERVICE}" >/dev/null 2>&1 || true
  echo "Disattivazione completata (app/newt/mqtt/watchdog)."
  print_status
}

usage() {
  cat <<'EOF'
Uso:
  sudo ./deploy/stack_control.sh enable-all
  sudo ./deploy/stack_control.sh disable-all
  sudo ./deploy/stack_control.sh status

Comandi:
  enable-all   Installa/aggiorna unit e script, abilita e avvia app + watchdog (+newt/mqtt se configurati)
  disable-all  Disabilita e ferma in blocco app + newt + mqtt + watchdog
  status       Mostra stato attuale dei servizi
EOF
}

CMD="${1:-}"

case "${CMD}" in
  enable-all)
    need_root
    enable_all
    ;;
  disable-all)
    need_root
    disable_all
    ;;
  status)
    need_root
    print_status
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "Comando non valido: ${CMD}" >&2
    usage
    exit 1
    ;;
esac
