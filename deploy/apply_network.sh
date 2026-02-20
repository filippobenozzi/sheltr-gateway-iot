#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-}"
SSID_RAW="${2:-}"
PASS_RAW="${3:-}"
WPA_FILE="/etc/wpa_supplicant/wpa_supplicant.conf"

esc(){
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

if [[ "${MODE}" != "ethernet" && "${MODE}" != "wifi" ]]; then
  echo "Modo non valido: usa ethernet o wifi" >&2
  exit 1
fi

if [[ "${MODE}" == "wifi" ]]; then
  if [[ -z "${SSID_RAW}" ]]; then
    echo "SSID mancante" >&2
    exit 1
  fi

  SSID="$(esc "${SSID_RAW}")"
  PASS="$(esc "${PASS_RAW}")"

  if [[ -f "${WPA_FILE}" ]]; then
    cp "${WPA_FILE}" "${WPA_FILE}.bak.$(date +%Y%m%d%H%M%S)"
  fi

  cat > "${WPA_FILE}" <<EOT
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=IT

network={
    ssid="${SSID}"
    psk="${PASS}"
    key_mgmt=WPA-PSK
}
EOT

  chmod 600 "${WPA_FILE}" || true
  command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi || true
  ip link show wlan0 >/dev/null 2>&1 && ip link set wlan0 up || true
  command -v wpa_cli >/dev/null 2>&1 && wpa_cli -i wlan0 reconfigure || true
else
  ip link show wlan0 >/dev/null 2>&1 && ip link set wlan0 down || true
  ip link show eth0 >/dev/null 2>&1 && ip link set eth0 up || true
fi

systemctl restart dhcpcd.service >/dev/null 2>&1 || true
systemctl restart networking.service >/dev/null 2>&1 || true

echo "Configurazione rete applicata (${MODE})"
