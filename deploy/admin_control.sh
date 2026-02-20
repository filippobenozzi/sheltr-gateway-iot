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
  apply-network)
    MODE="${2:-}"
    SSID="${3:-}"
    PASS="${4:-}"
    /usr/local/lib/algodomoiot-admin/apply_network.sh "${MODE}" "${SSID}" "${PASS}"
    ;;
  *)
    echo "Azione non valida" >&2
    exit 1
    ;;
esac
