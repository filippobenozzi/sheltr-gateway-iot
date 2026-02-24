#!/usr/bin/env python3
"""AlgoDomo lightweight web app over serial /dev/ttyS0."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import select
import socket
import subprocess
import termios
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

FRAME_START = 0x49
FRAME_END = 0x46
FRAME_LEN = 14

RELAY_COMMANDS = {
    1: 0x51,
    2: 0x52,
    3: 0x53,
    4: 0x54,
    5: 0x65,
    6: 0x66,
    7: 0x67,
    8: 0x68,
}

LIGHT_ACTIONS = {
    "on": 0x41,
    "off": 0x53,
    "pulse": 0x50,
    "toggle": 0x55,
    "toggle_no_ack": 0x54,
}

SHUTTER_ACTIONS = {
    "up": 0x55,
    "down": 0x44,
    "stop": 0x53,
}

DIMMER_COMMAND = 0x5B
DIMMER_SET_KEY = 0x53
DIMMER_MIN_LEVEL = 0
DIMMER_MAX_LEVEL = 9

ALLOWED_KINDS = {"light", "shutter", "thermostat", "dimmer"}
MAX_CHANNEL_BY_KIND = {
    "light": 8,
    "shutter": 4,
    "thermostat": 8,
    "dimmer": 1,
}

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"
DEFAULT_CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_STATE_PATH = DATA_DIR / "state.json"
CONFIG_PATH = Path(os.environ.get("ALGODOMO_CONFIG", str(DEFAULT_CONFIG_PATH)))
STATE_PATH = Path(os.environ.get("ALGODOMO_STATE", str(DEFAULT_STATE_PATH)))
NEWT_ENV_PATH = Path(os.environ.get("ALGODOMO_NEWT_ENV", "/etc/algodomoiot/newt.env"))
MQTT_ENV_PATH = Path(os.environ.get("ALGODOMO_MQTT_ENV", "/etc/algodomoiot/mqtt.env"))
ADMIN_CONTROL_SCRIPT = os.environ.get("ALGODOMO_ADMIN_SCRIPT", "/usr/local/lib/algodomoiot-admin/admin_control.sh")

BAUD_MAP = {
    1200: termios.B1200,
    2400: termios.B2400,
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}

LOCK = threading.Lock()
SERIAL_LOCK = threading.Lock()
CONFIG: dict[str, Any] = {}
STATE: dict[str, Any] = {}


def make_board(board_id: str, name: str, address: int, kind: str, start: int, end: int) -> dict[str, Any]:
    channels = []
    for channel in range(start, end + 1):
        channels.append(
            {
                "channel": channel,
                "name": default_channel_name(kind, channel),
                "room": "Senza stanza",
            }
        )
    return {
        "id": board_id,
        "name": name,
        "address": address,
        "kind": kind,
        "channelStart": start,
        "channelEnd": end,
        "channels": channels,
    }


def default_channel_name(kind: str, channel: int) -> str:
    if kind == "light":
        return f"Luce {channel}"
    if kind == "shutter":
        return f"Tapparella {channel}"
    if kind == "dimmer":
        return f"Dimmer {channel}"
    return f"Termostato {channel}"


def default_config() -> dict[str, Any]:
    return {
        "serial": {
            "port": "/dev/ttyS0",
            "baudrate": 9600,
            "timeoutMs": 1200,
        },
        "displayName": "Controllo Casa",
        "apiToken": "cambia-questo-token",
        "newt": {
            "enabled": False,
            "id": "",
            "secret": "",
            "endpoint": "https://app.pangolin.net",
        },
        "mqtt": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 1883,
            "username": "",
            "password": "",
            "clientId": "algodomoiot",
            "baseTopic": "algodomoiot",
            "discoveryPrefix": "homeassistant",
            "keepalive": 60,
            "pollIntervalSec": 30,
            "qos": 0,
            "retain": True,
        },
        "network": {
            "mode": "ethernet",
            "wifi": {
                "ssid": "",
                "password": "",
            },
        },
        "boards": [
            make_board("luci-1", "Scheda Luci", 1, "light", 1, 8),
            make_board("tapparelle-1", "Scheda Tapparelle", 2, "shutter", 1, 4),
            make_board("termostati-1", "Scheda Termostati", 3, "thermostat", 1, 1),
        ],
    }


def default_state() -> dict[str, Any]:
    return {
        "boards": {},
        "lights": {},
        "shutters": {},
        "thermostats": {},
        "dimmers": {},
        "updatedAt": 0,
    }


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, payload: Any) -> None:
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(fallback)


def write_text_atomic(path: Path, payload: str) -> None:
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def bootstrap() -> None:
    global CONFIG, STATE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        write_json_atomic(CONFIG_PATH, default_config())
    if not STATE_PATH.exists():
        write_json_atomic(STATE_PATH, default_state())

    with LOCK:
        CONFIG = normalize_config(read_json(CONFIG_PATH, default_config()))
        STATE = normalize_state(read_json(STATE_PATH, default_state()))

    try:
        cfg = get_config()
        sync_newt_env(cfg)
        sync_newt_runtime_state(cfg)
        sync_mqtt_env(cfg)
        sync_mqtt_runtime_state(cfg)
    except Exception as exc:  # noqa: BLE001
        print("[warn] impossibile inizializzare runtime esterni:", exc)


def get_config() -> dict[str, Any]:
    with LOCK:
        return copy.deepcopy(CONFIG)


def set_config(new_config: dict[str, Any]) -> dict[str, Any]:
    global CONFIG
    previous = get_config()
    normalized = normalize_config(new_config)
    with LOCK:
        CONFIG = normalized
    write_json_atomic(CONFIG_PATH, normalized)
    try:
        sync_newt_env(normalized)
        sync_newt_runtime_state(normalized, previous)
        sync_mqtt_env(normalized)
        sync_mqtt_runtime_state(normalized, previous)
    except Exception as exc:  # noqa: BLE001
        print("[warn] impossibile aggiornare runtime esterni:", exc)
    return copy.deepcopy(normalized)


def get_state() -> dict[str, Any]:
    with LOCK:
        return copy.deepcopy(STATE)


def update_state(mutator) -> dict[str, Any]:
    global STATE
    with LOCK:
        tmp = copy.deepcopy(STATE)
    mutator(tmp)
    normalized = normalize_state(tmp)
    with LOCK:
        STATE = normalized
    write_json_atomic(STATE_PATH, normalized)
    return copy.deepcopy(normalized)


def to_number(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        txt = value.strip().lower()
        if not txt:
            return fallback
        try:
            if txt.startswith("0x"):
                return float(int(txt, 16))
            return float(txt)
        except Exception:
            return fallback
    return fallback


def clamp_int(value: float, min_value: int, max_value: int) -> int:
    if not math.isfinite(value):
        return min_value
    return max(min_value, min(max_value, int(value)))


def to_byte(value: Any, fallback: int) -> int:
    return clamp_int(to_number(value, fallback), 0, 255)


def to_port(value: Any, fallback: int) -> int:
    return clamp_int(to_number(value, fallback), 1, 65535)


def to_timeout(value: Any, fallback: int) -> int:
    return clamp_int(to_number(value, fallback), 100, 20000)


def to_address(value: Any, fallback: int) -> int:
    number = to_number(value, float(fallback))
    if not math.isfinite(number):
        return fallback
    integer = int(number)
    if integer < 0 or integer > 254:
        return fallback
    return integer


def to_float(value: Any, fallback: float) -> float:
    number = to_number(value, fallback)
    if not math.isfinite(number):
        return fallback
    return float(number)


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def normalize_text(value: Any, fallback: str) -> str:
    txt = str(value if value is not None else "").strip()
    return txt or fallback


def normalize_topic(value: Any, fallback: str) -> str:
    topic = normalize_text(value, fallback).strip("/")
    return topic or fallback


def normalize_id(value: Any, fallback: str) -> str:
    raw = normalize_text(value, fallback).lower()
    cleaned = []
    for ch in raw:
        if ch.isalnum() or ch in {"_", "-"}:
            cleaned.append(ch)
        else:
            cleaned.append("-")
    out = "".join(cleaned)
    while "--" in out:
        out = out.replace("--", "-")
    out = out.strip("-")
    return out or fallback


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_board(board_any: Any, index: int) -> dict[str, Any]:
    board = board_any if isinstance(board_any, dict) else {}
    board_id = normalize_id(board.get("id"), f"board-{index}")
    name = normalize_text(board.get("name"), f"Scheda {index}")
    address = to_address(board.get("address"), index)

    kind_raw = normalize_text(board.get("kind"), "light").lower()
    kind = kind_raw if kind_raw in ALLOWED_KINDS else "light"
    max_channel = MAX_CHANNEL_BY_KIND[kind]

    start = clamp_int(to_number(board.get("channelStart"), 1), 1, max_channel)
    end = clamp_int(to_number(board.get("channelEnd"), max_channel), 1, max_channel)
    if end < start:
        end = start

    provided_names: dict[int, str] = {}
    provided_rooms: dict[int, str] = {}
    for channel_any in as_list(board.get("channels")):
        entry = channel_any if isinstance(channel_any, dict) else {}
        channel_num = clamp_int(to_number(entry.get("channel"), -1), 1, max_channel)
        if channel_num < start or channel_num > end:
            continue
        provided_names[channel_num] = normalize_text(entry.get("name"), default_channel_name(kind, channel_num))
        provided_rooms[channel_num] = normalize_text(entry.get("room"), "Senza stanza")

    channels = []
    for channel_num in range(start, end + 1):
        channels.append(
            {
                "channel": channel_num,
                "name": provided_names.get(channel_num, default_channel_name(kind, channel_num)),
                "room": provided_rooms.get(channel_num, "Senza stanza"),
            }
        )

    return {
        "id": board_id,
        "name": name,
        "address": address,
        "kind": kind,
        "channelStart": start,
        "channelEnd": end,
        "channels": channels,
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    defaults = default_config()
    if not isinstance(raw, dict):
        raw = {}

    serial_raw = as_dict(raw.get("serial"))
    newt_raw = as_dict(raw.get("newt"))
    mqtt_raw = as_dict(raw.get("mqtt"))
    network_raw = as_dict(raw.get("network"))
    wifi_raw = as_dict(network_raw.get("wifi"))
    boards_raw = as_list(raw.get("boards"))

    boards = []
    for idx, board_any in enumerate(boards_raw, start=1):
        boards.append(normalize_board(board_any, idx))

    if not boards:
        boards = copy.deepcopy(defaults["boards"])

    return {
        "serial": {
            "port": normalize_text(serial_raw.get("port"), defaults["serial"]["port"]),
            "baudrate": clamp_int(to_number(serial_raw.get("baudrate"), defaults["serial"]["baudrate"]), 1200, 115200),
            "timeoutMs": to_timeout(serial_raw.get("timeoutMs"), defaults["serial"]["timeoutMs"]),
        },
        "displayName": normalize_text(raw.get("displayName"), defaults["displayName"]),
        "apiToken": normalize_text(raw.get("apiToken"), defaults["apiToken"]),
        "newt": {
            "enabled": bool_value(newt_raw.get("enabled")),
            "id": normalize_text(newt_raw.get("id"), ""),
            "secret": normalize_text(newt_raw.get("secret"), ""),
            "endpoint": normalize_text(newt_raw.get("endpoint"), defaults["newt"]["endpoint"]),
        },
        "mqtt": {
            "enabled": bool_value(mqtt_raw.get("enabled")),
            "host": normalize_text(mqtt_raw.get("host"), defaults["mqtt"]["host"]),
            "port": to_port(mqtt_raw.get("port"), defaults["mqtt"]["port"]),
            "username": normalize_text(mqtt_raw.get("username"), ""),
            "password": normalize_text(mqtt_raw.get("password"), ""),
            "clientId": normalize_text(mqtt_raw.get("clientId"), defaults["mqtt"]["clientId"]),
            "baseTopic": normalize_topic(mqtt_raw.get("baseTopic"), defaults["mqtt"]["baseTopic"]),
            "discoveryPrefix": normalize_topic(mqtt_raw.get("discoveryPrefix"), defaults["mqtt"]["discoveryPrefix"]),
            "keepalive": clamp_int(to_number(mqtt_raw.get("keepalive"), defaults["mqtt"]["keepalive"]), 10, 86400),
            "pollIntervalSec": clamp_int(
                to_number(mqtt_raw.get("pollIntervalSec"), defaults["mqtt"]["pollIntervalSec"]), 2, 3600
            ),
            "qos": clamp_int(to_number(mqtt_raw.get("qos"), defaults["mqtt"]["qos"]), 0, 2),
            "retain": bool_value(mqtt_raw.get("retain")),
        },
        "network": {
            "mode": "wifi" if normalize_text(network_raw.get("mode"), "ethernet").lower() == "wifi" else "ethernet",
            "wifi": {
                "ssid": normalize_text(wifi_raw.get("ssid"), ""),
                "password": normalize_text(wifi_raw.get("password"), ""),
            },
        },
        "boards": boards,
    }


def normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "boards": raw.get("boards") if isinstance(raw.get("boards"), dict) else {},
        "lights": raw.get("lights") if isinstance(raw.get("lights"), dict) else {},
        "shutters": raw.get("shutters") if isinstance(raw.get("shutters"), dict) else {},
        "thermostats": raw.get("thermostats") if isinstance(raw.get("thermostats"), dict) else {},
        "dimmers": raw.get("dimmers") if isinstance(raw.get("dimmers"), dict) else {},
        "updatedAt": int(to_number(raw.get("updatedAt"), 0)),
    }


def env_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def sync_newt_env(cfg: dict[str, Any]) -> None:
    newt = as_dict(cfg.get("newt"))
    endpoint = normalize_text(newt.get("endpoint"), "https://app.pangolin.net")
    lines = [
        "# File auto-generato da AlgoDomo. Modificare da /config",
        f'NEWT_ENABLED={"1" if bool_value(newt.get("enabled")) else "0"}',
        f'NEWT_ID="{env_escape(normalize_text(newt.get("id"), ""))}"',
        f'NEWT_SECRET="{env_escape(normalize_text(newt.get("secret"), ""))}"',
        f'PANGOLIN_ENDPOINT="{env_escape(endpoint)}"',
        f'NEWT_ENDPOINT="{env_escape(endpoint)}"',
    ]
    write_text_atomic(NEWT_ENV_PATH, "\n".join(lines) + "\n")


def newt_runtime_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    newt = as_dict(cfg.get("newt"))
    return {
        "enabled": bool_value(newt.get("enabled")),
        "id": normalize_text(newt.get("id"), ""),
        "secret": normalize_text(newt.get("secret"), ""),
        "endpoint": normalize_text(newt.get("endpoint"), ""),
    }


def newt_should_run(cfg: dict[str, Any]) -> bool:
    payload = newt_runtime_payload(cfg)
    return bool(payload["enabled"] and payload["id"] and payload["secret"] and payload["endpoint"])


def sync_newt_runtime_state(current_cfg: dict[str, Any], previous_cfg: dict[str, Any] | None = None) -> None:
    should_run = newt_should_run(current_cfg)
    status = service_status("newt.service")
    is_active = status in {"active", "activating", "reloading"}

    newt_changed = False
    if previous_cfg is not None:
        newt_changed = newt_runtime_payload(previous_cfg) != newt_runtime_payload(current_cfg)

    if should_run:
        if newt_changed or not is_active:
            try:
                run_admin_action("restart-newt")
            except Exception as exc:  # noqa: BLE001
                print("[warn] impossibile riavviare newt:", exc)
        return

    if is_active:
        try:
            run_admin_action("stop-newt")
        except Exception as exc:  # noqa: BLE001
            print("[warn] impossibile fermare newt:", exc)


def sync_mqtt_env(cfg: dict[str, Any]) -> None:
    mqtt_cfg = as_dict(cfg.get("mqtt"))
    host = normalize_text(mqtt_cfg.get("host"), "127.0.0.1")
    port = to_port(mqtt_cfg.get("port"), 1883)
    username = normalize_text(mqtt_cfg.get("username"), "")
    password = normalize_text(mqtt_cfg.get("password"), "")
    client_id = normalize_text(mqtt_cfg.get("clientId"), "algodomoiot")
    base_topic = normalize_topic(mqtt_cfg.get("baseTopic"), "algodomoiot")
    discovery_prefix = normalize_topic(mqtt_cfg.get("discoveryPrefix"), "homeassistant")
    keepalive = clamp_int(to_number(mqtt_cfg.get("keepalive"), 60), 10, 86400)
    poll_interval = clamp_int(to_number(mqtt_cfg.get("pollIntervalSec"), 30), 2, 3600)
    qos = clamp_int(to_number(mqtt_cfg.get("qos"), 0), 0, 2)
    retain = "1" if bool_value(mqtt_cfg.get("retain")) else "0"
    token = normalize_text(cfg.get("apiToken"), "")
    lines = [
        "# File auto-generato da AlgoDomo. Modificare da /config",
        f'MQTT_ENABLED={"1" if bool_value(mqtt_cfg.get("enabled")) else "0"}',
        f'MQTT_HOST="{env_escape(host)}"',
        f"MQTT_PORT={port}",
        f'MQTT_USERNAME="{env_escape(username)}"',
        f'MQTT_PASSWORD="{env_escape(password)}"',
        f'MQTT_CLIENT_ID="{env_escape(client_id)}"',
        f'MQTT_BASE_TOPIC="{env_escape(base_topic)}"',
        f'MQTT_DISCOVERY_PREFIX="{env_escape(discovery_prefix)}"',
        f"MQTT_KEEPALIVE={keepalive}",
        f"MQTT_POLL_INTERVAL={poll_interval}",
        f"MQTT_QOS={qos}",
        f"MQTT_RETAIN={retain}",
        'ALGODOMO_HTTP_BASE="http://127.0.0.1"',
        f'ALGODOMO_TOKEN="{env_escape(token)}"',
    ]
    write_text_atomic(MQTT_ENV_PATH, "\n".join(lines) + "\n")


def mqtt_runtime_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    mqtt_cfg = as_dict(cfg.get("mqtt"))
    boards = as_list(cfg.get("boards"))
    boards_hash = hashlib.sha1(json.dumps(boards, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    return {
        "enabled": bool_value(mqtt_cfg.get("enabled")),
        "host": normalize_text(mqtt_cfg.get("host"), ""),
        "port": to_port(mqtt_cfg.get("port"), 1883),
        "username": normalize_text(mqtt_cfg.get("username"), ""),
        "password": normalize_text(mqtt_cfg.get("password"), ""),
        "clientId": normalize_text(mqtt_cfg.get("clientId"), ""),
        "baseTopic": normalize_topic(mqtt_cfg.get("baseTopic"), "algodomoiot"),
        "discoveryPrefix": normalize_topic(mqtt_cfg.get("discoveryPrefix"), "homeassistant"),
        "keepalive": clamp_int(to_number(mqtt_cfg.get("keepalive"), 60), 10, 86400),
        "pollIntervalSec": clamp_int(to_number(mqtt_cfg.get("pollIntervalSec"), 30), 2, 3600),
        "qos": clamp_int(to_number(mqtt_cfg.get("qos"), 0), 0, 2),
        "retain": bool_value(mqtt_cfg.get("retain")),
        "apiToken": normalize_text(cfg.get("apiToken"), ""),
        "boardsHash": boards_hash,
    }


def mqtt_should_run(cfg: dict[str, Any]) -> bool:
    payload = mqtt_runtime_payload(cfg)
    return bool(payload["enabled"] and payload["host"] and payload["port"] and payload["baseTopic"] and payload["apiToken"])


def sync_mqtt_runtime_state(current_cfg: dict[str, Any], previous_cfg: dict[str, Any] | None = None) -> None:
    should_run = mqtt_should_run(current_cfg)
    status = service_status("algodomoiot-mqtt.service")
    is_active = status in {"active", "activating", "reloading"}

    mqtt_changed = False
    if previous_cfg is not None:
        mqtt_changed = mqtt_runtime_payload(previous_cfg) != mqtt_runtime_payload(current_cfg)

    if should_run:
        if mqtt_changed or not is_active:
            try:
                run_admin_action("restart-mqtt")
            except Exception as exc:  # noqa: BLE001
                print("[warn] impossibile riavviare mqtt:", exc)
        return

    if is_active:
        try:
            run_admin_action("stop-mqtt")
        except Exception as exc:  # noqa: BLE001
            print("[warn] impossibile fermare mqtt:", exc)


def run_cmd(args: list[str], timeout_s: int = 20) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_s),
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc), "cmd": args}
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "cmd": args,
    }


def list_ipv4_by_interface() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    out = run_cmd(["ip", "-o", "-4", "addr", "show"])
    if not out["ok"]:
        return result
    for line in out["stdout"].splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        cidr = parts[3]
        ip = cidr.split("/", 1)[0].strip()
        if not ip:
            continue
        result.setdefault(iface, []).append(ip)
    return result


def interface_type(name: str) -> str:
    if Path(f"/sys/class/net/{name}/wireless").exists():
        return "wifi"
    if name.startswith("eth"):
        return "ethernet"
    return "other"


def read_text(path: Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return fallback


def service_status(unit: str) -> str:
    status = run_cmd(["systemctl", "is-active", unit], timeout_s=6)
    if status["ok"] and status["stdout"]:
        return normalize_text(status["stdout"], "unknown")
    if status["stdout"]:
        return normalize_text(status["stdout"], "unknown")
    return "unknown"


def system_info() -> dict[str, Any]:
    ipv4 = list_ipv4_by_interface()
    interfaces = []

    for iface_path in sorted(Path("/sys/class/net").glob("*")):
        name = iface_path.name
        if name == "lo":
            continue
        interfaces.append(
            {
                "name": name,
                "type": interface_type(name),
                "state": read_text(iface_path / "operstate", "unknown"),
                "ips": ipv4.get(name, []),
            }
        )

    ips = []
    for item in interfaces:
        for ip in item.get("ips", []):
            if ip not in ips:
                ips.append(ip)

    return {
        "hostname": socket.gethostname(),
        "ips": ips,
        "interfaces": interfaces,
        "services": {
            "app": service_status("algodomoiot.service"),
            "newt": service_status("newt.service"),
            "mqtt": service_status("algodomoiot-mqtt.service"),
        },
    }


def run_admin_action(action: str, args: list[str] | None = None) -> dict[str, Any]:
    script = normalize_text(ADMIN_CONTROL_SCRIPT, "")
    if not script:
        raise RuntimeError("Script amministrativo non configurato")
    cmd = ["sudo", "-n", script, action]
    if args:
        cmd.extend(args)
    result = run_cmd(cmd, timeout_s=40)
    if not result["ok"]:
        reason = result["stderr"] or result["stdout"] or "comando fallito"
        raise RuntimeError(reason)
    return result


def to_hex(byte: int) -> str:
    return f"0x{to_byte(byte, 0):02x}"


def entity_id(board_id: str, channel: int) -> str:
    return f"{board_id}-c{channel}"


def iter_entities(cfg: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    out = []
    for board in cfg.get("boards", []):
        board_kind = board.get("kind")
        if kind and board_kind != kind:
            continue
        address = to_address(board.get("address"), -1)
        for channel in board.get("channels", []):
            channel_num = clamp_int(to_number(channel.get("channel"), 1), 1, MAX_CHANNEL_BY_KIND.get(board_kind, 8))
            out.append(
                {
                    "id": entity_id(str(board.get("id")), channel_num),
                    "kind": board_kind,
                    "boardId": board.get("id"),
                    "boardName": board.get("name"),
                    "address": address,
                    "channel": channel_num,
                    "name": normalize_text(channel.get("name"), default_channel_name(board_kind or "light", channel_num)),
                    "room": normalize_text(channel.get("room"), "Senza stanza"),
                }
            )
    return out


def find_entity(cfg: dict[str, Any], kind: str, item_id: str, address_raw: str, channel_raw: str) -> dict[str, Any] | None:
    entities = iter_entities(cfg, kind)
    if item_id:
        for item in entities:
            if item.get("id") == item_id:
                return item
        return None

    address = to_address(address_raw, -1)
    channel = clamp_int(to_number(channel_raw, -1), 1, MAX_CHANNEL_BY_KIND.get(kind, 8))
    if address < 0:
        return None

    by_address = [item for item in entities if item.get("address") == address]
    if channel >= 1:
        for item in by_address:
            if item.get("channel") == channel:
                return item
        return None

    if len(by_address) == 1:
        return by_address[0]
    return by_address[0] if by_address else None


def build_frame(address: int, command: int, g_bytes: list[int]) -> bytes:
    packet = bytearray([0] * FRAME_LEN)
    packet[0] = FRAME_START
    packet[1] = to_address(address, 1)
    packet[2] = to_byte(command, 0x40)
    for idx in range(10):
        packet[3 + idx] = to_byte(g_bytes[idx] if idx < len(g_bytes) else 0, 0)
    packet[13] = FRAME_END
    return bytes(packet)


def extract_first_frame(buffer: bytes) -> bytes | None:
    if len(buffer) < FRAME_LEN:
        return None
    for idx in range(0, len(buffer) - FRAME_LEN + 1):
        if buffer[idx] != FRAME_START:
            continue
        if buffer[idx + FRAME_LEN - 1] == FRAME_END:
            return buffer[idx : idx + FRAME_LEN]
    return None


def parse_frame(frame: bytes) -> dict[str, Any]:
    return {
        "start": frame[0],
        "address": frame[1],
        "command": frame[2],
        "g": [frame[3 + idx] for idx in range(10)],
        "end": frame[13],
        "hex": " ".join(to_hex(byte) for byte in frame),
    }


def configure_serial_port(fd: int, baudrate: int) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0

    speed = BAUD_MAP.get(baudrate, termios.B9600)
    if hasattr(termios, "cfsetispeed"):
        termios.cfsetispeed(attrs, speed)
        termios.cfsetospeed(attrs, speed)
    else:
        attrs[4] = speed
        attrs[5] = speed

    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)


def send_raw(payload: bytes, expect_frame: bool = True, expected_bytes: int = 1) -> Any:
    cfg = get_config()
    serial_cfg = cfg.get("serial", {})
    port = normalize_text(serial_cfg.get("port"), "/dev/ttyS0")
    baudrate = clamp_int(to_number(serial_cfg.get("baudrate"), 9600), 1200, 115200)
    timeout_s = to_timeout(serial_cfg.get("timeoutMs"), 1200) / 1000.0

    received = b""
    deadline = time.monotonic() + timeout_s

    with SERIAL_LOCK:
        fd = -1
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_SYNC)
            configure_serial_port(fd, baudrate)
            os.write(fd, payload)
            termios.tcdrain(fd)

            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    continue

                chunk = os.read(fd, 1024)
                if not chunk:
                    continue

                received += chunk

                if expect_frame:
                    frame = extract_first_frame(received)
                    if frame is not None:
                        return parse_frame(frame)
                else:
                    min_bytes = max(1, int(expected_bytes))
                    if len(received) >= min_bytes:
                        return received[:min_bytes]

        except OSError as exc:
            raise RuntimeError(f"Errore seriale su {port}: {exc}") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    if expect_frame:
        frame = extract_first_frame(received)
        if frame is not None:
            return parse_frame(frame)
        raise RuntimeError("Risposta protocollo non valida")

    min_bytes = max(1, int(expected_bytes))
    if len(received) >= min_bytes:
        return received[:min_bytes]
    raise RuntimeError("Nessuna risposta ricevuta")


def send_frame(address: int, command: int, g_bytes: list[int]) -> dict[str, Any]:
    packet = build_frame(address, command, g_bytes)
    result = send_raw(packet, expect_frame=True)
    assert isinstance(result, dict)
    return result


def decode_polling_frame(frame: dict[str, Any]) -> dict[str, Any]:
    g = frame.get("g", [0] * 10)
    type_and_release = to_byte(g[0], 0)
    output_mask = to_byte(g[1], 0)
    input_mask = to_byte(g[2], 0)
    dimmer_level = clamp_int(to_number(g[3], 0), DIMMER_MIN_LEVEL, DIMMER_MAX_LEVEL)
    sign = -1 if g[6] == 0x2D else 1
    temp_i = to_byte(g[4], 0)
    temp_d = to_byte(g[5], 0)

    return {
        "boardType": type_and_release & 0x0F,
        "release": (type_and_release >> 4) & 0x0F,
        "outputMask": output_mask,
        "inputMask": input_mask,
        "dimmerLevel": dimmer_level,
        "temperature": sign * (temp_i + temp_d / 10),
        "powerKw": to_byte(g[7], 0) / 10,
        "setpoint": to_byte(g[8], 0),
    }


def poll_board(address: int) -> dict[str, Any]:
    frame = send_frame(address, 0x40, [])
    poll = decode_polling_frame(frame)
    now = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("boards", {})[str(address)] = {
            "address": address,
            "poll": poll,
            "frameHex": frame.get("hex"),
            "updatedAt": now,
        }
        state["updatedAt"] = now

    update_state(mutator)
    return poll


def split_temperature(value: float) -> tuple[int, int]:
    rounded = round(abs(value), 1)
    int_part = int(rounded)
    dec_part = int(round((rounded - int_part) * 10))
    return clamp_int(int_part, 0, 99), clamp_int(dec_part, 0, 9)


def parse_thermostat_power(value: str) -> bool:
    v = normalize_text(value, "").lower()
    if v in {"on", "1", "true", "yes", "acceso", "attivo"}:
        return True
    if v in {"off", "0", "false", "no", "spento"}:
        return False
    raise ValueError("power non valido: usa on/off")


def parse_thermostat_mode(value: str) -> str:
    v = normalize_text(value, "").lower()
    if v in {"winter", "inverno", "heat"}:
        return "winter"
    if v in {"summer", "estate", "cool"}:
        return "summer"
    raise ValueError("mode non valido: usa winter/summer")


def parse_dimmer_level(value: str) -> int:
    if not value.strip():
        raise ValueError("level non valido: usa 0..9")
    number = to_number(value, float("nan"))
    if not math.isfinite(number):
        raise ValueError("level non valido: usa 0..9")
    return clamp_int(number, DIMMER_MIN_LEVEL, DIMMER_MAX_LEVEL)


def parse_dimmer_action(value: str) -> str:
    action = normalize_text(value, "").lower()
    if action in {"on", "off", "toggle"}:
        return action
    if action:
        raise ValueError("action non valida: usa on/off/toggle")
    return ""


def infer_light_state(channel: int, poll: dict[str, Any] | None, fallback: Any, action: str | None) -> bool | None:
    if isinstance(poll, dict):
        bit = 1 << (channel - 1)
        return (to_byte(poll.get("outputMask"), 0) & bit) != 0
    if action == "on":
        return True
    if action == "off":
        return False
    if action == "toggle" and isinstance(fallback, bool):
        return not fallback
    return fallback if isinstance(fallback, bool) else None


def infer_thermostat_active(channel: int, poll: dict[str, Any] | None, fallback: Any) -> bool:
    # Da protocollo polling esteso 0x40: G2 = stato uscite a bit.
    # Per il termostato consideriamo "attivo" quando il bit uscita del canale e' ON.
    if isinstance(poll, dict):
        bit = 1 << (channel - 1)
        return (to_byte(poll.get("outputMask"), 0) & bit) != 0
    return fallback if isinstance(fallback, bool) else True


def collect_addresses(cfg: dict[str, Any]) -> list[int]:
    values = set()
    for board in cfg.get("boards", []):
        values.add(to_address(board.get("address"), -1))
    return sorted(item for item in values if 0 <= item <= 254)


def token_valid(token: str) -> bool:
    expected = normalize_text(get_config().get("apiToken"), "")
    return bool(expected) and token == expected


def query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def build_status(refresh: bool) -> dict[str, Any]:
    cfg = get_config()

    refresh_errors: list[dict[str, Any]] = []
    if refresh:
        for address in collect_addresses(cfg):
            try:
                poll_board(address)
            except Exception as exc:
                message = normalize_text(str(exc), "Errore polling")
                # Alcune schede/firmware non rispondono al polling esteso 0x40:
                # non bloccare UI e mantieni ultimo stato noto.
                if "Risposta protocollo non valida" in message:
                    continue
                refresh_errors.append({"address": address, "error": message})

    snapshot = get_state()
    now = int(time.time() * 1000)

    boards_out = []
    rooms_map: dict[str, dict[str, Any]] = {}
    new_light_state: dict[str, dict[str, Any]] = {}
    new_dimmer_state: dict[str, dict[str, Any]] = {}
    new_thermostat_state: dict[str, dict[str, Any]] = {}

    def room_bucket(name: str) -> dict[str, Any]:
        key = normalize_text(name, "Senza stanza")
        if key not in rooms_map:
            rooms_map[key] = {
                "name": key,
                "dimmers": [],
                "lights": [],
                "shutters": [],
                "thermostats": [],
            }
        return rooms_map[key]

    for board in cfg.get("boards", []):
        address = to_address(board.get("address"), -1)
        poll = snapshot.get("boards", {}).get(str(address), {}).get("poll")

        payload_board = {
            "id": board.get("id"),
            "name": board.get("name"),
            "address": address,
            "kind": board.get("kind"),
            "channels": [],
        }

        for channel in board.get("channels", []):
            max_channel = MAX_CHANNEL_BY_KIND.get(board.get("kind"), 8)
            ch = clamp_int(to_number(channel.get("channel"), 1), 1, max_channel)
            ch_name = normalize_text(channel.get("name"), default_channel_name(board.get("kind"), ch))
            ch_room = normalize_text(channel.get("room"), "Senza stanza")
            item_id = entity_id(str(board.get("id")), ch)

            if board.get("kind") == "light":
                prev = snapshot.get("lights", {}).get(item_id, {})
                fallback = prev.get("isOn") if isinstance(prev, dict) else None
                is_on = infer_light_state(ch, poll if isinstance(poll, dict) else None, fallback, None)
                payload_board["channels"].append(
                    {
                        "id": item_id,
                        "channel": ch,
                        "name": ch_name,
                        "room": ch_room,
                        "isOn": is_on,
                    }
                )
                new_light_state[item_id] = {"isOn": is_on, "updatedAt": now}
                room_bucket(ch_room)["lights"].append(
                    {
                        "id": item_id,
                        "name": ch_name,
                        "room": ch_room,
                        "boardId": board.get("id"),
                        "boardName": board.get("name"),
                        "address": address,
                        "channel": ch,
                        "isOn": is_on,
                    }
                )

            elif board.get("kind") == "dimmer":
                prev = snapshot.get("dimmers", {}).get(item_id, {})
                prev_level = clamp_int(
                    to_number(prev.get("level") if isinstance(prev, dict) else 0, 0),
                    DIMMER_MIN_LEVEL,
                    DIMMER_MAX_LEVEL,
                )
                poll_level: int | None = None
                if isinstance(poll, dict):
                    poll_level = clamp_int(
                        to_number(poll.get("dimmerLevel"), prev_level),
                        DIMMER_MIN_LEVEL,
                        DIMMER_MAX_LEVEL,
                    )
                level = poll_level if poll_level is not None else prev_level
                is_on = level > 0
                last_on = clamp_int(
                    to_number(prev.get("lastOnLevel") if isinstance(prev, dict) else DIMMER_MAX_LEVEL, DIMMER_MAX_LEVEL),
                    1,
                    DIMMER_MAX_LEVEL,
                )
                if is_on:
                    last_on = level

                payload_board["channels"].append(
                    {
                        "id": item_id,
                        "channel": ch,
                        "name": ch_name,
                        "room": ch_room,
                        "level": level,
                        "isOn": is_on,
                    }
                )
                new_dimmer_state[item_id] = {
                    "level": level,
                    "isOn": is_on,
                    "lastOnLevel": last_on,
                    "updatedAt": now,
                }
                room_bucket(ch_room)["dimmers"].append(
                    {
                        "id": item_id,
                        "name": ch_name,
                        "room": ch_room,
                        "boardId": board.get("id"),
                        "boardName": board.get("name"),
                        "address": address,
                        "channel": ch,
                        "level": level,
                        "isOn": is_on,
                    }
                )

            elif board.get("kind") == "shutter":
                prev = snapshot.get("shutters", {}).get(item_id, {})
                action = prev.get("action") if isinstance(prev, dict) else "unknown"
                payload_board["channels"].append(
                    {
                        "id": item_id,
                        "channel": ch,
                        "name": ch_name,
                        "room": ch_room,
                        "action": action or "unknown",
                    }
                )
                room_bucket(ch_room)["shutters"].append(
                    {
                        "id": item_id,
                        "name": ch_name,
                        "room": ch_room,
                        "boardId": board.get("id"),
                        "boardName": board.get("name"),
                        "address": address,
                        "channel": ch,
                        "action": action or "unknown",
                    }
                )

            else:  # thermostat
                prev = snapshot.get("thermostats", {}).get(item_id, {})
                setpoint = prev.get("setpoint") if isinstance(prev, dict) else None
                if not isinstance(setpoint, (int, float)):
                    setpoint = None
                mode = prev.get("mode") if isinstance(prev, dict) else None
                if mode not in {"winter", "summer"}:
                    mode = "winter"
                is_on = prev.get("isOn") if isinstance(prev, dict) else None
                if not isinstance(is_on, bool):
                    is_on = True
                poll_setpoint: int | None = None
                if isinstance(poll, dict):
                    raw_sp = to_number(poll.get("setpoint"), float("nan"))
                    if math.isfinite(raw_sp):
                        poll_setpoint = clamp_int(raw_sp, 0, 99)
                if poll_setpoint is not None:
                    setpoint = float(poll_setpoint)
                    is_on = poll_setpoint > 0
                is_active = infer_thermostat_active(
                    ch,
                    poll if isinstance(poll, dict) else None,
                    prev.get("isActive") if isinstance(prev, dict) else None,
                )
                payload_board["channels"].append(
                    {
                        "id": item_id,
                        "channel": ch,
                        "name": ch_name,
                        "room": ch_room,
                        "temperature": poll.get("temperature") if isinstance(poll, dict) else None,
                        "setpoint": setpoint,
                        "mode": mode,
                        "isOn": is_on,
                        "isActive": is_active,
                        "boardSetpoint": poll_setpoint,
                    }
                )
                new_thermostat_state[item_id] = {
                    "setpoint": setpoint,
                    "mode": mode,
                    "isOn": is_on,
                    "isActive": is_active,
                    "updatedAt": now,
                }
                room_bucket(ch_room)["thermostats"].append(
                    {
                        "id": item_id,
                        "name": ch_name,
                        "room": ch_room,
                        "boardId": board.get("id"),
                        "boardName": board.get("name"),
                        "address": address,
                        "channel": ch,
                        "temperature": poll.get("temperature") if isinstance(poll, dict) else None,
                        "setpoint": setpoint,
                        "mode": mode,
                        "isOn": is_on,
                        "isActive": is_active,
                        "boardSetpoint": poll_setpoint,
                    }
                )

        boards_out.append(payload_board)

    def mutator(state: dict[str, Any]) -> None:
        lights = state.setdefault("lights", {})
        for key, value in new_light_state.items():
            lights[key] = value
        dimmers = state.setdefault("dimmers", {})
        for key, value in new_dimmer_state.items():
            dimmers[key] = value
        thermostats = state.setdefault("thermostats", {})
        for key, value in new_thermostat_state.items():
            thermostats[key] = value
        state["updatedAt"] = now

    update_state(mutator)

    rooms_out = sorted(rooms_map.values(), key=lambda item: item["name"].lower())

    return {
        "updatedAt": now,
        "refreshErrors": refresh_errors,
        "rooms": rooms_out,
        "boards": boards_out,
    }


def api_light(query: dict[str, list[str]]) -> dict[str, Any]:
    action = query_value(query, "action").strip().lower()
    code = LIGHT_ACTIONS.get(action)
    if code is None:
        raise ValueError("action non valida")

    cfg = get_config()
    entity = find_entity(
        cfg,
        "light",
        query_value(query, "id"),
        query_value(query, "address"),
        query_value(query, "channel"),
    )
    if entity is None:
        raise LookupError("Luce non trovata")

    relay_cmd = RELAY_COMMANDS.get(entity["channel"])
    if relay_cmd is None:
        raise ValueError("channel non valido per luce")

    frame = send_frame(entity["address"], relay_cmd, [code])

    poll = None
    try:
        poll = poll_board(entity["address"])
    except Exception:
        poll = None

    snapshot = get_state()
    prev = snapshot.get("lights", {}).get(entity["id"], {})
    fallback = prev.get("isOn") if isinstance(prev, dict) else None
    is_on = infer_light_state(entity["channel"], poll, fallback, action)
    now = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("lights", {})[entity["id"]] = {"isOn": is_on, "updatedAt": now}
        state["updatedAt"] = now

    update_state(mutator)

    return {"ok": True, "entity": entity, "action": action, "frame": frame}


def api_dimmer(query: dict[str, list[str]]) -> dict[str, Any]:
    cfg = get_config()
    entity = find_entity(
        cfg,
        "dimmer",
        query_value(query, "id"),
        query_value(query, "address"),
        query_value(query, "channel"),
    )
    if entity is None:
        raise LookupError("Dimmer non trovato")

    level_raw = query_value(query, "level")
    action = parse_dimmer_action(query_value(query, "action"))

    snapshot = get_state()
    prev = snapshot.get("dimmers", {}).get(entity["id"], {})
    prev_level = clamp_int(
        to_number(prev.get("level") if isinstance(prev, dict) else 0, 0),
        DIMMER_MIN_LEVEL,
        DIMMER_MAX_LEVEL,
    )
    last_on = clamp_int(
        to_number(prev.get("lastOnLevel") if isinstance(prev, dict) else DIMMER_MAX_LEVEL, DIMMER_MAX_LEVEL),
        1,
        DIMMER_MAX_LEVEL,
    )

    if level_raw.strip():
        target_level = parse_dimmer_level(level_raw)
        resolved_action = "set"
    else:
        if not action:
            raise ValueError("serve almeno un parametro: level o action")
        if action == "off":
            target_level = DIMMER_MIN_LEVEL
        elif action == "on":
            target_level = last_on if prev_level == 0 else prev_level
        else:  # toggle
            target_level = DIMMER_MIN_LEVEL if prev_level > 0 else last_on
        resolved_action = action

    frame = send_frame(entity["address"], DIMMER_COMMAND, [DIMMER_SET_KEY, target_level])

    poll = None
    try:
        poll = poll_board(entity["address"])
    except Exception:
        poll = None

    final_level = target_level
    if isinstance(poll, dict):
        final_level = clamp_int(
            to_number(poll.get("dimmerLevel"), target_level),
            DIMMER_MIN_LEVEL,
            DIMMER_MAX_LEVEL,
        )

    now = int(time.time() * 1000)
    new_last_on = last_on
    if final_level > 0:
        new_last_on = final_level

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("dimmers", {})[entity["id"]] = {
            "level": final_level,
            "isOn": final_level > 0,
            "lastOnLevel": new_last_on,
            "updatedAt": now,
        }
        state["updatedAt"] = now

    update_state(mutator)

    return {
        "ok": True,
        "entity": entity,
        "action": resolved_action,
        "level": final_level,
        "frame": frame,
    }


def api_shutter(query: dict[str, list[str]]) -> dict[str, Any]:
    action = query_value(query, "action").strip().lower()
    code = SHUTTER_ACTIONS.get(action)
    if code is None:
        raise ValueError("action non valida")

    cfg = get_config()
    entity = find_entity(
        cfg,
        "shutter",
        query_value(query, "id"),
        query_value(query, "address"),
        query_value(query, "channel"),
    )
    if entity is None:
        raise LookupError("Tapparella non trovata")

    frame = send_frame(entity["address"], 0x5C, [entity["channel"], code])
    now = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("shutters", {})[entity["id"]] = {"action": action, "updatedAt": now}
        state["updatedAt"] = now

    update_state(mutator)

    return {"ok": True, "entity": entity, "action": action, "frame": frame}


def api_thermostat(query: dict[str, list[str]]) -> dict[str, Any]:
    cfg = get_config()
    entity = find_entity(
        cfg,
        "thermostat",
        query_value(query, "id"),
        query_value(query, "address"),
        query_value(query, "channel"),
    )
    if entity is None:
        raise LookupError("Termostato non trovato")

    set_raw = query_value(query, "set")
    mode_raw = query_value(query, "mode")
    power_raw = query_value(query, "power")

    requested_setpoint: float | None = None
    if set_raw.strip():
        s = to_float(set_raw, float("nan"))
        if not math.isfinite(s):
            raise ValueError("set non valido")
        requested_setpoint = s

    requested_mode: str | None = None
    if mode_raw.strip():
        requested_mode = parse_thermostat_mode(mode_raw)

    requested_power: bool | None = None
    if power_raw.strip():
        requested_power = parse_thermostat_power(power_raw)

    if requested_setpoint is None and requested_mode is None and requested_power is None:
        raise ValueError("serve almeno un parametro: set, power o mode")

    snapshot = get_state()
    prev = snapshot.get("thermostats", {}).get(entity["id"], {})

    next_setpoint = prev.get("setpoint") if isinstance(prev, dict) else None
    if not isinstance(next_setpoint, (int, float)):
        next_setpoint = 21.0

    next_mode = prev.get("mode") if isinstance(prev, dict) else None
    if next_mode not in {"winter", "summer"}:
        next_mode = "winter"

    next_power = prev.get("isOn") if isinstance(prev, dict) else None
    if not isinstance(next_power, bool):
        next_power = True

    frames: list[dict[str, Any]] = []

    if requested_mode is not None:
        mode_byte = 1 if requested_mode == "summer" else 0
        mode_frame = send_frame(entity["address"], 0x6B, [mode_byte])
        frames.append({"type": "mode", "frame": mode_frame})
        next_mode = requested_mode

    if requested_setpoint is not None:
        next_setpoint = requested_setpoint

    if requested_power is False:
        off_frame = send_frame(entity["address"], 0x5A, [0, 0])
        frames.append({"type": "power_off", "frame": off_frame})
        next_power = False
    else:
        if requested_setpoint is not None:
            i, d = split_temperature(next_setpoint)
            set_frame = send_frame(entity["address"], 0x5A, [i, d])
            frames.append({"type": "setpoint", "frame": set_frame})
            next_power = True
        elif requested_power is True:
            i, d = split_temperature(next_setpoint)
            on_frame = send_frame(entity["address"], 0x5A, [i, d])
            frames.append({"type": "power_on", "frame": on_frame})
            next_power = True

    now = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        prev_active = prev.get("isActive") if isinstance(prev, dict) else None
        if not isinstance(prev_active, bool):
            prev_active = next_power
        state.setdefault("thermostats", {})[entity["id"]] = {
            "setpoint": next_setpoint,
            "mode": next_mode,
            "isOn": next_power,
            "isActive": prev_active,
            "updatedAt": now,
        }
        state["updatedAt"] = now

    update_state(mutator)

    try:
        poll_board(entity["address"])
    except Exception:
        pass

    first_frame = frames[0]["frame"] if frames else None
    return {
        "ok": True,
        "entity": entity,
        "setpoint": next_setpoint,
        "mode": next_mode,
        "isOn": next_power,
        "frame": first_frame,
        "frames": frames,
    }


def api_poll(query: dict[str, list[str]]) -> dict[str, Any]:
    address = to_address(query_value(query, "address"), -1)
    if address < 0:
        raise ValueError("address mancante")
    return {"ok": True, "poll": poll_board(address)}


def api_program_address(query: dict[str, list[str]]) -> dict[str, Any]:
    address = to_address(query_value(query, "address"), -1)
    if address < 0:
        raise ValueError("address mancante")

    response = send_raw(bytes([address]), expect_frame=False, expected_bytes=1)
    if not isinstance(response, (bytes, bytearray)) or len(response) < 1:
        raise RuntimeError("Nessun ACK ricevuto")

    ack = int(response[0])
    return {
        "ok": True,
        "programmedAddress": address,
        "ack": ack,
        "ackHex": to_hex(ack),
    }


def api_system_info() -> dict[str, Any]:
    cfg = get_config()
    return {
        "ok": True,
        "system": system_info(),
        "networkConfig": as_dict(cfg.get("network")),
        "newtConfig": as_dict(cfg.get("newt")),
        "mqttConfig": as_dict(cfg.get("mqtt")),
    }


def api_admin_restart(query: dict[str, list[str]]) -> dict[str, Any]:
    service = normalize_text(query_value(query, "service"), "").lower()
    if service in {"app", "algodomoiot", "algodomoiot.service"}:
        action = "restart-app"
        label = "algodomoiot.service"
    elif service in {"newt", "newt.service"}:
        cfg = get_config()
        newt = as_dict(cfg.get("newt"))
        enabled = bool_value(newt.get("enabled"))
        newt_id = normalize_text(newt.get("id"), "")
        secret = normalize_text(newt.get("secret"), "")
        endpoint = normalize_text(newt.get("endpoint"), "")
        if not enabled or not newt_id or not secret or not endpoint:
            raise ValueError("newt non configurato: abilita NEWT e compila ID/SECRET/ENDPOINT in /config")
        action = "restart-newt"
        label = "newt.service"
    elif service in {"mqtt", "algodomoiot-mqtt", "algodomoiot-mqtt.service"}:
        cfg = get_config()
        mqtt_cfg = as_dict(cfg.get("mqtt"))
        enabled = bool_value(mqtt_cfg.get("enabled"))
        host = normalize_text(mqtt_cfg.get("host"), "")
        base_topic = normalize_topic(mqtt_cfg.get("baseTopic"), "algodomoiot")
        token = normalize_text(cfg.get("apiToken"), "")
        if not enabled or not host or not base_topic or not token:
            raise ValueError("mqtt non configurato: abilita MQTT e compila host/base topic/token in /config")
        action = "restart-mqtt"
        label = "algodomoiot-mqtt.service"
    else:
        raise ValueError("service non valido: usa app, newt o mqtt")

    run_admin_action(action)
    return {
        "ok": True,
        "service": label,
        "message": f"Restart richiesto per {label}",
    }


def api_admin_apply_network() -> dict[str, Any]:
    cfg = get_config()
    network = as_dict(cfg.get("network"))
    mode = "wifi" if normalize_text(network.get("mode"), "ethernet").lower() == "wifi" else "ethernet"
    wifi = as_dict(network.get("wifi"))
    ssid = normalize_text(wifi.get("ssid"), "")
    password = normalize_text(wifi.get("password"), "")

    run_admin_action("apply-network", [mode, ssid, password])
    return {
        "ok": True,
        "mode": mode,
        "system": system_info(),
        "message": f"Configurazione rete applicata ({mode})",
    }


class AlgoHandler(BaseHTTPRequestHandler):
    server_version = "AlgoDomoPython/2.0"

    def do_GET(self) -> None:  # noqa: N802
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request()

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[http]", self.address_string(), fmt % args)

    def _handle_request(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            query = parse_qs(parsed.query, keep_blank_values=True)

            if path == "/":
                self._serve_file(PUBLIC_DIR / "control.html", "text/html; charset=utf-8")
                return

            if self.command == "GET" and path == "/config":
                self._serve_file(PUBLIC_DIR / "config.html", "text/html; charset=utf-8")
                return

            if self.command == "GET" and path == "/control":
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if self.command == "GET" and path == "/manifest.webmanifest":
                self._serve_file(
                    PUBLIC_DIR / "manifest.webmanifest",
                    "application/manifest+json; charset=utf-8",
                    "public, max-age=86400",
                )
                return

            if self.command == "GET" and path == "/sw.js":
                self._serve_file(
                    PUBLIC_DIR / "sw.js",
                    "application/javascript; charset=utf-8",
                    "no-cache, max-age=0, must-revalidate",
                )
                return

            if self.command == "GET" and path == "/icon.svg":
                self._serve_file(
                    PUBLIC_DIR / "icon.svg",
                    "image/svg+xml; charset=utf-8",
                    "public, max-age=31536000, immutable",
                )
                return

            if self.command == "GET" and path == "/health":
                self._json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return

            if path == "/api/config" and self.command == "GET":
                self._json(HTTPStatus.OK, get_config())
                return

            if path == "/api/config" and self.command == "POST":
                payload = self._read_json_body(get_config())
                cfg = set_config(payload)
                self._json(HTTPStatus.OK, {"ok": True, "config": cfg})
                return

            if path.startswith("/api/"):
                token = query_value(query, "token")
                if not token_valid(token):
                    self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Token non valido"})
                    return

                if self.command != "GET":
                    self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "Solo GET consentito"})
                    return

                if path == "/api/status":
                    refresh = bool_value(query_value(query, "refresh"))
                    self._json(HTTPStatus.OK, {"ok": True, **build_status(refresh)})
                    return

                if path == "/api/system/info":
                    self._json(HTTPStatus.OK, api_system_info())
                    return

                if path == "/api/cmd/light":
                    self._json(HTTPStatus.OK, api_light(query))
                    return

                if path == "/api/cmd/dimmer":
                    self._json(HTTPStatus.OK, api_dimmer(query))
                    return

                if path == "/api/cmd/shutter":
                    self._json(HTTPStatus.OK, api_shutter(query))
                    return

                if path == "/api/cmd/thermostat":
                    self._json(HTTPStatus.OK, api_thermostat(query))
                    return

                if path == "/api/cmd/poll":
                    self._json(HTTPStatus.OK, api_poll(query))
                    return

                if path == "/api/cmd/program-address":
                    self._json(HTTPStatus.OK, api_program_address(query))
                    return

                if path == "/api/admin/restart":
                    self._json(HTTPStatus.OK, api_admin_restart(query))
                    return

                if path == "/api/admin/apply-network":
                    self._json(HTTPStatus.OK, api_admin_apply_network())
                    return

                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint non trovato"})
                return

            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except LookupError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            print("[error]", exc)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def _read_json_body(self, default: Any) -> Any:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return default
        if length > 512 * 1024:
            raise ValueError("Payload troppo grande")

        raw = self.rfile.read(length)
        text = raw.decode("utf-8", errors="strict").strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except Exception as exc:
            raise ValueError("JSON non valido") from exc

    def _serve_file(
        self,
        file_path: Path,
        content_type: str,
        cache_control: str = "no-cache, max-age=0, must-revalidate",
    ) -> None:
        if not file_path.exists() or not file_path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "File non trovato"})
            return

        data = file_path.read_bytes()
        etag = '"' + hashlib.sha1(data, usedforsecurity=False).hexdigest()[:16] + '"'  # noqa: S324
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("Cache-Control", cache_control)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, target: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        raw = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run() -> None:
    bootstrap()
    host = os.environ.get("HOST", "0.0.0.0")
    port = to_port(os.environ.get("PORT", "80"), 80)
    server = ThreadingHTTPServer((host, port), AlgoHandler)
    print(f"AlgoDomo Python in ascolto su http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
