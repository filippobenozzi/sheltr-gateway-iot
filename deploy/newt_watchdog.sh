#!/usr/bin/env bash
set -euo pipefail

NEWT_ENV_FILE="${ALGODOMO_NEWT_ENV:-/etc/algodomoiot/newt.env}"
STATE_DIR="/run/algodomoiot"
OFFLINE_MARKER="${STATE_DIR}/newt.offline"

mkdir -p "${STATE_DIR}"

NEWT_ENABLED="0"
NEWT_ID=""
NEWT_SECRET=""
PANGOLIN_ENDPOINT=""
NEWT_ENDPOINT=""

if [[ -f "${NEWT_ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${NEWT_ENV_FILE}"
  set +a
fi

ENDPOINT="${PANGOLIN_ENDPOINT:-${NEWT_ENDPOINT:-}}"

if [[ "${NEWT_ENABLED:-0}" != "1" || -z "${NEWT_ID:-}" || -z "${NEWT_SECRET:-}" || -z "${ENDPOINT:-}" ]]; then
  if systemctl is-active --quiet newt.service; then
    systemctl stop newt.service
    echo "watchdog: newt disabilitato/non configurato, stop servizio"
  fi
  rm -f "${OFFLINE_MARKER}" >/dev/null 2>&1 || true
  exit 0
fi

if ! ip route get 1.1.1.1 >/dev/null 2>&1 && ! ip route get 8.8.8.8 >/dev/null 2>&1; then
  touch "${OFFLINE_MARKER}"
  echo "watchdog: rete non disponibile, attendo ripristino"
  exit 0
fi

if [[ -f "${OFFLINE_MARKER}" ]]; then
  rm -f "${OFFLINE_MARKER}" >/dev/null 2>&1 || true
  systemctl restart newt.service
  echo "watchdog: rete ripristinata, riavvio newt.service"
  exit 0
fi

if ! systemctl is-active --quiet newt.service; then
  systemctl restart newt.service
  echo "watchdog: newt.service non attivo, riavvio eseguito"
  exit 0
fi

echo "watchdog: OK"
