#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"

case "${ACTION}" in
  restart-app)
    systemctl restart --no-block sheltr.service
    echo "Restart richiesto: sheltr.service"
    ;;
  restart-newt)
    systemctl restart --no-block newt.service
    echo "Restart richiesto: newt.service"
    ;;
  restart-mqtt)
    systemctl restart --no-block sheltr-mqtt.service
    echo "Restart richiesto: sheltr-mqtt.service"
    ;;
  restart-all)
    systemctl restart --no-block sheltr.service || true
    systemctl restart --no-block newt.service || true
    systemctl restart --no-block sheltr-mqtt.service || true
    echo "Restart richiesto: sheltr.service,newt.service,sheltr-mqtt.service"
    ;;
  stop-newt)
    systemctl stop --no-block newt.service
    echo "Stop richiesto: newt.service"
    ;;
  stop-mqtt)
    systemctl stop --no-block sheltr-mqtt.service
    echo "Stop richiesto: sheltr-mqtt.service"
    ;;
  apply-network)
    MODE="${2:-}"
    SSID="${3:-}"
    PASS="${4:-}"
    IP_MODE="${5:-dhcp}"
    IP_ADDR="${6:-}"
    IP_PREFIX="${7:-24}"
    IP_GATEWAY="${8:-}"
    /usr/local/lib/sheltr-admin/apply_network.sh "${MODE}" "${SSID}" "${PASS}" "${IP_MODE}" "${IP_ADDR}" "${IP_PREFIX}" "${IP_GATEWAY}"
    ;;
  apply-rtc)
    ENABLED="${2:-0}"
    MODEL="${3:-ds3231}"
    BUS="${4:-1}"
    ADDRESS="${5:-0x68}"
    /usr/local/lib/sheltr-admin/rtc_control.sh apply "${ENABLED}" "${MODEL}" "${BUS}" "${ADDRESS}"
    ;;
  sync-rtc)
    MODE="${2:-from-rtc}"
    /usr/local/lib/sheltr-admin/rtc_control.sh sync "${MODE}"
    ;;
  rtc-read)
    DEV="${2:-}"
    /usr/local/lib/sheltr-admin/rtc_control.sh read "${DEV}"
    ;;
  *)
    echo "Azione non valida" >&2
    exit 1
    ;;
esac
