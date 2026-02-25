#!/usr/bin/env bash
set -euo pipefail

NEWT_ENV_FILE="${SHELTR_NEWT_ENV:-${ALGODOMO_NEWT_ENV:-/etc/sheltr/newt.env}}"
STATE_DIR="/run/sheltr"
OFFLINE_MARKER="${STATE_DIR}/newt.offline"
LAST_RESTART_MARKER="${STATE_DIR}/newt.last_restart"
ERROR_WINDOW_SEC="${NEWT_WATCHDOG_ERROR_WINDOW_SEC:-120}"
RESTART_COOLDOWN_SEC="${NEWT_WATCHDOG_RESTART_COOLDOWN_SEC:-90}"
ERROR_MIN_HITS="${NEWT_WATCHDOG_ERROR_MIN_HITS:-1}"
RESTART_GRACE_SEC="${NEWT_WATCHDOG_RESTART_GRACE_SEC:-10}"

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

now_epoch() {
  date +%s
}

mark_restart() {
  now_epoch > "${LAST_RESTART_MARKER}"
}

last_restart_epoch() {
  if [[ -f "${LAST_RESTART_MARKER}" ]]; then
    cat "${LAST_RESTART_MARKER}" 2>/dev/null || echo 0
    return 0
  fi
  echo 0
}

can_restart() {
  local now last
  now="$(now_epoch)"
  last=0
  if [[ -f "${LAST_RESTART_MARKER}" ]]; then
    last="$(cat "${LAST_RESTART_MARKER}" 2>/dev/null || echo 0)"
  fi
  [[ $((now - last)) -ge "${RESTART_COOLDOWN_SEC}" ]]
}

restart_newt() {
  local reason="$1"
  if can_restart; then
    mark_restart
    systemctl restart newt.service
    echo "watchdog: ${reason}, riavvio newt.service"
  else
    echo "watchdog: ${reason}, cooldown riavvio attivo"
  fi
}

log_since_epoch() {
  local now base_window last_restart after_restart since
  now="$(now_epoch)"
  base_window=$((now - ERROR_WINDOW_SEC))
  last_restart="$(last_restart_epoch)"
  since="${base_window}"
  if [[ "${last_restart}" -gt 0 ]]; then
    after_restart=$((last_restart + RESTART_GRACE_SEC))
    if [[ "${after_restart}" -gt "${since}" ]]; then
      since="${after_restart}"
    fi
  fi
  if [[ "${since}" -lt 0 ]]; then
    since=0
  fi
  echo "${since}"
}

recent_connection_error_hits() {
  local since_epoch
  if ! command -v journalctl >/dev/null 2>&1; then
    echo 0
    return 0
  fi
  since_epoch="$(log_since_epoch)"
  journalctl -u newt.service --since "@${since_epoch}" --no-pager 2>/dev/null \
    | grep -Eic 'failed to connect|failed to get token|failed to report peer bandwidth.*not connected|periodic ping failed|failed to connect to websocket|no route to host|ping failed:.*i/o timeout|failed to read icmp packet' || true
}

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
  restart_newt "rete ripristinata"
  exit 0
fi

ERROR_HITS="$(recent_connection_error_hits)"
if [[ "${ERROR_HITS}" -ge "${ERROR_MIN_HITS}" ]]; then
  restart_newt "errori connessione recenti (${ERROR_HITS})"
  exit 0
fi

if ! systemctl is-active --quiet newt.service; then
  restart_newt "newt.service non attivo"
  exit 0
fi

echo "watchdog: OK"
