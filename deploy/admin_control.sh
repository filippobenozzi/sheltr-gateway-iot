#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"

case "${ACTION}" in
  restart-app)
    systemctl restart --no-block algodomoiot.service
    echo "Restart richiesto: algodomoiot.service"
    ;;
  restart-newt)
    systemctl restart --no-block newt.service
    echo "Restart richiesto: newt.service"
    ;;
  restart-mqtt)
    systemctl restart --no-block algodomoiot-mqtt.service
    echo "Restart richiesto: algodomoiot-mqtt.service"
    ;;
  restart-all)
    systemctl restart --no-block algodomoiot.service || true
    systemctl restart --no-block newt.service || true
    systemctl restart --no-block algodomoiot-mqtt.service || true
    echo "Restart richiesto: algodomoiot.service,newt.service,algodomoiot-mqtt.service"
    ;;
  stop-newt)
    systemctl stop --no-block newt.service
    echo "Stop richiesto: newt.service"
    ;;
  stop-mqtt)
    systemctl stop --no-block algodomoiot-mqtt.service
    echo "Stop richiesto: algodomoiot-mqtt.service"
    ;;
  apply-network)
    MODE="${2:-}"
    SSID="${3:-}"
    PASS="${4:-}"
    IP_MODE="${5:-dhcp}"
    IP_ADDR="${6:-}"
    IP_PREFIX="${7:-24}"
    IP_GATEWAY="${8:-}"
    /usr/local/lib/algodomoiot-admin/apply_network.sh "${MODE}" "${SSID}" "${PASS}" "${IP_MODE}" "${IP_ADDR}" "${IP_PREFIX}" "${IP_GATEWAY}"
    ;;
  *)
    echo "Azione non valida" >&2
    exit 1
    ;;
esac
