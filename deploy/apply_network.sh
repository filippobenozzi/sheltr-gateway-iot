#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-}"
SSID_RAW="${2:-}"
PASS_RAW="${3:-}"
IP_MODE_RAW="${4:-dhcp}"
IP_ADDR_RAW="${5:-}"
IP_PREFIX_RAW="${6:-24}"
IP_GW_RAW="${7:-}"
WPA_FILE="/etc/wpa_supplicant/wpa_supplicant.conf"

esc() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

iface_exists() {
  ip link show "$1" >/dev/null 2>&1
}

detect_wifi_iface() {
  local iface_path iface_name
  for iface_path in /sys/class/net/*; do
    iface_name="$(basename "${iface_path}")"
    [[ "${iface_name}" == "lo" ]] && continue
    [[ -d "${iface_path}/wireless" ]] && { printf '%s' "${iface_name}"; return 0; }
  done
  return 1
}

detect_eth_iface() {
  local iface_path iface_name
  for iface_path in /sys/class/net/*; do
    iface_name="$(basename "${iface_path}")"
    [[ "${iface_name}" == "lo" ]] && continue
    [[ -d "${iface_path}/wireless" ]] && continue
    case "${iface_name}" in
      eth*|en*)
        printf '%s' "${iface_name}"
        return 0
        ;;
    esac
  done
  for iface_path in /sys/class/net/*; do
    iface_name="$(basename "${iface_path}")"
    [[ "${iface_name}" == "lo" ]] && continue
    [[ -d "${iface_path}/wireless" ]] && continue
    printf '%s' "${iface_name}"
    return 0
  done
  return 1
}

nm_active() {
  systemctl is-active --quiet NetworkManager.service
}

restart_if_present() {
  local unit="$1"
  systemctl cat "${unit}" >/dev/null 2>&1 && systemctl restart "${unit}" >/dev/null 2>&1 || true
}

wait_ipv4() {
  local iface="$1" timeout="${2:-20}" i
  for ((i = 0; i < timeout; i++)); do
    if ip -o -4 addr show dev "${iface}" scope global | grep -q 'inet '; then
      return 0
    fi
    sleep 1
  done
  return 1
}

is_ipv4() {
  local ip="$1" o1 o2 o3 o4
  [[ "${ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  IFS=. read -r o1 o2 o3 o4 <<< "${ip}"
  for oct in "${o1}" "${o2}" "${o3}" "${o4}"; do
    [[ "${oct}" =~ ^[0-9]+$ ]] || return 1
    ((oct >= 0 && oct <= 255)) || return 1
  done
  return 0
}

apply_static_ipv4() {
  local iface="$1"
  [[ -z "${iface}" ]] && return 1
  iface_exists "${iface}" && ip link set "${iface}" up >/dev/null 2>&1 || true
  ip addr flush dev "${iface}" scope global >/dev/null 2>&1 || true
  ip addr add "${IP_ADDR_RAW}/${IP_PREFIX}" dev "${iface}" >/dev/null
  if [[ -n "${IP_GW_RAW}" ]]; then
    ip route replace default via "${IP_GW_RAW}" dev "${iface}" >/dev/null 2>&1 || true
  fi
}

nm_set_ipv4() {
  local conn="$1"
  if [[ "${IP_MODE}" == "static" ]]; then
    nmcli connection modify "${conn}" ipv4.method manual ipv4.addresses "${IP_ADDR_RAW}/${IP_PREFIX}" >/dev/null
    if [[ -n "${IP_GW_RAW}" ]]; then
      nmcli connection modify "${conn}" ipv4.gateway "${IP_GW_RAW}" >/dev/null
    else
      nmcli connection modify "${conn}" -ipv4.gateway >/dev/null 2>&1 || true
      nmcli connection modify "${conn}" ipv4.gateway "" >/dev/null 2>&1 || true
    fi
  else
    nmcli connection modify "${conn}" ipv4.method auto >/dev/null
    nmcli connection modify "${conn}" -ipv4.addresses >/dev/null 2>&1 || true
    nmcli connection modify "${conn}" -ipv4.gateway >/dev/null 2>&1 || true
    nmcli connection modify "${conn}" ipv4.addresses "" >/dev/null 2>&1 || true
    nmcli connection modify "${conn}" ipv4.gateway "" >/dev/null 2>&1 || true
  fi
}

write_wpa_files() {
  local ssid_esc pass_esc iface_file
  ssid_esc="$(esc "${SSID_RAW}")"
  pass_esc="$(esc "${PASS_RAW}")"

  [[ -f "${WPA_FILE}" ]] && cp "${WPA_FILE}" "${WPA_FILE}.bak.$(date +%Y%m%d%H%M%S)"

  if [[ -n "${pass_esc}" ]]; then
    cat > "${WPA_FILE}" <<EOT
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=IT

network={
    ssid="${ssid_esc}"
    psk="${pass_esc}"
    key_mgmt=WPA-PSK
}
EOT
  else
    cat > "${WPA_FILE}" <<EOT
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=IT

network={
    ssid="${ssid_esc}"
    key_mgmt=NONE
}
EOT
  fi

  chmod 600 "${WPA_FILE}" || true

  if [[ -n "${WLAN_IF}" ]]; then
    iface_file="/etc/wpa_supplicant/wpa_supplicant-${WLAN_IF}.conf"
    cp "${WPA_FILE}" "${iface_file}" || true
    chmod 600 "${iface_file}" || true
  fi
}

disconnect_eth() {
  local iface="$1"
  [[ -z "${iface}" ]] && return 0
  nm_active && has_cmd nmcli && nmcli device disconnect "${iface}" >/dev/null 2>&1 || true
  iface_exists "${iface}" && ip link set "${iface}" down >/dev/null 2>&1 || true
}

disconnect_wifi() {
  local iface="$1"
  [[ -z "${iface}" ]] && return 0
  nm_active && has_cmd nmcli && nmcli device disconnect "${iface}" >/dev/null 2>&1 || true
  iface_exists "${iface}" && ip link set "${iface}" down >/dev/null 2>&1 || true
}

connect_eth() {
  local iface="$1"
  [[ -z "${iface}" ]] && return 1
  iface_exists "${iface}" && ip link set "${iface}" up >/dev/null 2>&1 || true

  if nm_active && has_cmd nmcli; then
    nmcli device set "${iface}" managed yes >/dev/null 2>&1 || true
    nmcli connection delete algodomoiot-ethernet >/dev/null 2>&1 || true
    nmcli connection delete sheltr-ethernet >/dev/null 2>&1 || true
    nmcli connection add type ethernet ifname "${iface}" con-name sheltr-ethernet >/dev/null
    nm_set_ipv4 sheltr-ethernet
    nmcli connection modify sheltr-ethernet connection.autoconnect yes connection.autoconnect-priority 100 ipv6.method auto >/dev/null
    nmcli connection up sheltr-ethernet >/dev/null
    return 0
  fi

  if [[ "${IP_MODE}" == "static" ]]; then
    apply_static_ipv4 "${iface}"
    return 0
  fi

  restart_if_present dhcpcd.service
  restart_if_present networking.service
  has_cmd dhclient && dhclient -r "${iface}" >/dev/null 2>&1 || true
  has_cmd dhclient && dhclient "${iface}" >/dev/null 2>&1 || true
  return 0
}

connect_wifi_nm() {
  local iface="$1"
  nmcli radio wifi on >/dev/null 2>&1 || true
  nmcli device set "${iface}" managed yes >/dev/null 2>&1 || true
  nmcli connection delete algodomoiot-wifi >/dev/null 2>&1 || true
  nmcli connection delete sheltr-wifi >/dev/null 2>&1 || true
  nmcli connection add type wifi ifname "${iface}" con-name sheltr-wifi ssid "${SSID_RAW}" >/dev/null
  if [[ -n "${PASS_RAW}" ]]; then
    nmcli connection modify sheltr-wifi wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${PASS_RAW}" >/dev/null
  else
    nmcli connection modify sheltr-wifi wifi-sec.key-mgmt none >/dev/null
  fi
  nm_set_ipv4 sheltr-wifi
  nmcli connection modify sheltr-wifi connection.autoconnect yes connection.autoconnect-priority 100 ipv6.method auto >/dev/null
  nmcli connection up sheltr-wifi >/dev/null
}

connect_wifi_wpa() {
  local iface="$1"
  write_wpa_files
  has_cmd rfkill && rfkill unblock wifi || true
  iface_exists "${iface}" && ip link set "${iface}" up >/dev/null 2>&1 || true
  restart_if_present "wpa_supplicant@${iface}.service"
  restart_if_present wpa_supplicant.service
  has_cmd wpa_cli && wpa_cli -i "${iface}" reconfigure >/dev/null 2>&1 || true
  if [[ "${IP_MODE}" == "static" ]]; then
    apply_static_ipv4 "${iface}"
  else
    restart_if_present dhcpcd.service
    restart_if_present networking.service
    has_cmd dhclient && dhclient -r "${iface}" >/dev/null 2>&1 || true
    has_cmd dhclient && dhclient "${iface}" >/dev/null 2>&1 || true
  fi
}

if [[ "${MODE}" != "ethernet" && "${MODE}" != "wifi" ]]; then
  echo "Modo non valido: usa ethernet o wifi" >&2
  exit 1
fi

IP_MODE="dhcp"
if [[ "${IP_MODE_RAW,,}" == "static" ]]; then
  IP_MODE="static"
fi

IP_PREFIX=24
if [[ "${IP_PREFIX_RAW}" =~ ^[0-9]+$ ]]; then
  IP_PREFIX="${IP_PREFIX_RAW}"
fi
if ((IP_PREFIX < 1)); then
  IP_PREFIX=1
elif ((IP_PREFIX > 32)); then
  IP_PREFIX=32
fi

if [[ "${IP_MODE}" == "static" ]]; then
  if [[ -z "${IP_ADDR_RAW}" ]]; then
    echo "IP statico selezionato ma indirizzo mancante" >&2
    exit 1
  fi
  if ! is_ipv4 "${IP_ADDR_RAW}"; then
    echo "Indirizzo IP non valido: ${IP_ADDR_RAW}" >&2
    exit 1
  fi
  if [[ -n "${IP_GW_RAW}" ]] && ! is_ipv4 "${IP_GW_RAW}"; then
    echo "Gateway non valido: ${IP_GW_RAW}" >&2
    exit 1
  fi
fi

WLAN_IF="${SHELTR_WIFI_IFACE:-${ALGODOMO_WIFI_IFACE:-$(detect_wifi_iface || true)}}"
ETH_IF="${SHELTR_ETH_IFACE:-${ALGODOMO_ETH_IFACE:-$(detect_eth_iface || true)}}"

if [[ "${MODE}" == "wifi" ]]; then
  if [[ -z "${SSID_RAW}" ]]; then
    echo "SSID mancante" >&2
    exit 1
  fi
  if [[ -z "${WLAN_IF}" ]]; then
    echo "Interfaccia WiFi non trovata" >&2
    exit 1
  fi

  if nm_active && has_cmd nmcli; then
    connect_wifi_nm "${WLAN_IF}"
  else
    connect_wifi_wpa "${WLAN_IF}"
  fi

  disconnect_eth "${ETH_IF}"

  if ! wait_ipv4 "${WLAN_IF}" 25; then
    connect_eth "${ETH_IF}" >/dev/null 2>&1 || true
    echo "WiFi configurato ma nessun IPv4 su ${WLAN_IF}" >&2
    exit 1
  fi

  WIFI_IP="$(ip -o -4 addr show dev "${WLAN_IF}" scope global | awk '{print $4}' | head -n1 | cut -d/ -f1)"
  echo "Configurazione rete applicata (wifi su ${WLAN_IF}, ${IP_MODE}, IP ${WIFI_IP:-n/a})"
  exit 0
fi

if [[ -z "${ETH_IF}" ]]; then
  echo "Interfaccia Ethernet non trovata" >&2
  exit 1
fi

disconnect_wifi "${WLAN_IF}"
connect_eth "${ETH_IF}"

if ! wait_ipv4 "${ETH_IF}" 25; then
  echo "Ethernet configurata ma nessun IPv4 su ${ETH_IF}" >&2
  exit 1
fi

ETH_IP="$(ip -o -4 addr show dev "${ETH_IF}" scope global | awk '{print $4}' | head -n1 | cut -d/ -f1)"
echo "Configurazione rete applicata (ethernet su ${ETH_IF}, ${IP_MODE}, IP ${ETH_IP:-n/a})"
