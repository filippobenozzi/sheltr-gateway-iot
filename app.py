#!/usr/bin/env python3
"""AlgoDomo lightweight web app (config + control + GET APIs)."""

from __future__ import annotations

import copy
import json
import math
import os
import socket
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

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"
DEFAULT_CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_STATE_PATH = DATA_DIR / "state.json"
CONFIG_PATH = Path(os.environ.get("ALGODOMO_CONFIG", str(DEFAULT_CONFIG_PATH)))
STATE_PATH = Path(os.environ.get("ALGODOMO_STATE", str(DEFAULT_STATE_PATH)))

LOCK = threading.Lock()
CONFIG: dict[str, Any] = {}
STATE: dict[str, Any] = {}


def shutter_input_preset(board_address: int, room: str) -> list[dict[str, Any]]:
    """Preset mapping richiesto dall'utente per 4 tapparelle su 8 ingressi.

    Mappatura implementata:
    - Tapparella 1: IN3 SU, IN1 GIU
    - Tapparella 2: IN4 SU, IN2 GIU
    - Tapparella 3: IN7 SU, IN5 GIU
    - Tapparella 4: IN8 SU, IN6 GIU
    """

    mapping = [
        (1, "Tapparella 1 GIU", 1, 0x44),
        (2, "Tapparella 2 GIU", 2, 0x44),
        (3, "Tapparella 1 SU", 1, 0x55),
        (4, "Tapparella 2 SU", 2, 0x55),
        (5, "Tapparella 3 GIU", 3, 0x44),
        (6, "Tapparella 4 GIU", 4, 0x44),
        (7, "Tapparella 3 SU", 3, 0x55),
        (8, "Tapparella 4 SU", 4, 0x55),
    ]

    out = []
    for idx, name, channel, action in mapping:
        out.append(
            {
                "index": idx,
                "name": name,
                "room": room,
                "enabled": True,
                "g2": 0x5C,
                "g3": channel,
                "g4": action,
                "targetAddress": board_address,
            }
        )
    return out


def default_config() -> dict[str, Any]:
    room = "Soggiorno"
    return {
        "gateway": {
            "host": "127.0.0.1",
            "port": 1470,
            "timeoutMs": 1200,
        },
        "apiToken": "cambia-questo-token",
        "boards": [
            {
                "id": "board-1",
                "name": "Scheda 1",
                "address": 1,
                "inputs": shutter_input_preset(1, room),
            }
        ],
        "entities": {
            "lights": [
                {
                    "id": "light-1",
                    "name": "Luce Soggiorno",
                    "room": room,
                    "address": 1,
                    "relay": 1,
                }
            ],
            "shutters": [
                {
                    "id": "shutter-1",
                    "name": "Tapparella 1",
                    "room": room,
                    "address": 1,
                    "channel": 1,
                },
                {
                    "id": "shutter-2",
                    "name": "Tapparella 2",
                    "room": room,
                    "address": 1,
                    "channel": 2,
                },
                {
                    "id": "shutter-3",
                    "name": "Tapparella 3",
                    "room": room,
                    "address": 1,
                    "channel": 3,
                },
                {
                    "id": "shutter-4",
                    "name": "Tapparella 4",
                    "room": room,
                    "address": 1,
                    "channel": 4,
                },
            ],
            "thermostats": [
                {
                    "id": "thermo-1",
                    "name": "Termostato Soggiorno",
                    "room": room,
                    "address": 1,
                    "setpoint": 21,
                }
            ],
        },
    }


def default_state() -> dict[str, Any]:
    return {
        "boards": {},
        "lights": {},
        "shutters": {},
        "thermostats": {},
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


def bootstrap() -> None:
    global CONFIG, STATE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        write_json_atomic(CONFIG_PATH, default_config())
    if not STATE_PATH.exists():
        write_json_atomic(STATE_PATH, default_state())

    config_raw = read_json(CONFIG_PATH, default_config())
    state_raw = read_json(STATE_PATH, default_state())

    with LOCK:
        CONFIG = normalize_config(config_raw)
        STATE = normalize_state(state_raw)


def get_config() -> dict[str, Any]:
    with LOCK:
        return copy.deepcopy(CONFIG)


def set_config(new_config: dict[str, Any]) -> dict[str, Any]:
    global CONFIG
    normalized = normalize_config(new_config)
    with LOCK:
        CONFIG = normalized
    write_json_atomic(CONFIG_PATH, normalized)
    return copy.deepcopy(normalized)


def get_state() -> dict[str, Any]:
    with LOCK:
        return copy.deepcopy(STATE)


def update_state(mutator) -> dict[str, Any]:
    global STATE
    with LOCK:
        data = copy.deepcopy(STATE)
    mutator(data)
    normalized = normalize_state(data)
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
    text = str(value if value is not None else "").strip()
    return text or fallback


def normalize_id(value: Any, fallback: str) -> str:
    raw = normalize_text(value, fallback).lower()
    chars = []
    for ch in raw:
        if ch.isalnum() or ch in {"_", "-"}:
            chars.append(ch)
        else:
            chars.append("-")
    out = "".join(chars)
    while "--" in out:
        out = out.replace("--", "-")
    out = out.strip("-")
    return out or fallback


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}

    defaults = default_config()
    gateway_raw = raw.get("gateway", {}) if isinstance(raw.get("gateway"), dict) else {}
    entities_raw = raw.get("entities", {}) if isinstance(raw.get("entities"), dict) else {}

    boards_out = []
    boards_raw = as_list(raw.get("boards"))
    for i, board_any in enumerate(boards_raw, start=1):
        board = board_any if isinstance(board_any, dict) else {}
        board_address = to_address(board.get("address"), i)
        inputs_out = []
        for j, input_any in enumerate(as_list(board.get("inputs")), start=1):
            inp = input_any if isinstance(input_any, dict) else {}
            idx = clamp_int(to_number(inp.get("index"), j), 1, 8)
            inputs_out.append(
                {
                    "index": idx,
                    "name": normalize_text(inp.get("name"), f"Ingresso {idx}"),
                    "room": normalize_text(inp.get("room"), "Senza stanza"),
                    "enabled": inp.get("enabled", True) is not False,
                    "g2": to_byte(inp.get("g2"), 0),
                    "g3": to_byte(inp.get("g3"), 0),
                    "g4": to_byte(inp.get("g4"), 0),
                    "targetAddress": to_address(inp.get("targetAddress"), board_address),
                }
            )

        inputs_out.sort(key=lambda item: item["index"])
        boards_out.append(
            {
                "id": normalize_id(board.get("id"), f"board-{i}"),
                "name": normalize_text(board.get("name"), f"Scheda {i}"),
                "address": board_address,
                "inputs": inputs_out,
            }
        )

    lights_out = []
    for i, light_any in enumerate(as_list(entities_raw.get("lights")), start=1):
        light = light_any if isinstance(light_any, dict) else {}
        lights_out.append(
            {
                "id": normalize_id(light.get("id"), f"light-{i}"),
                "name": normalize_text(light.get("name"), f"Luce {i}"),
                "room": normalize_text(light.get("room"), "Senza stanza"),
                "address": to_address(light.get("address"), 1),
                "relay": clamp_int(to_number(light.get("relay"), 1), 1, 8),
            }
        )

    shutters_out = []
    for i, shutter_any in enumerate(as_list(entities_raw.get("shutters")), start=1):
        shutter = shutter_any if isinstance(shutter_any, dict) else {}
        shutters_out.append(
            {
                "id": normalize_id(shutter.get("id"), f"shutter-{i}"),
                "name": normalize_text(shutter.get("name"), f"Tapparella {i}"),
                "room": normalize_text(shutter.get("room"), "Senza stanza"),
                "address": to_address(shutter.get("address"), 1),
                "channel": clamp_int(to_number(shutter.get("channel"), 1), 1, 4),
            }
        )

    thermostats_out = []
    for i, thermo_any in enumerate(as_list(entities_raw.get("thermostats")), start=1):
        thermo = thermo_any if isinstance(thermo_any, dict) else {}
        thermostats_out.append(
            {
                "id": normalize_id(thermo.get("id"), f"thermo-{i}"),
                "name": normalize_text(thermo.get("name"), f"Termostato {i}"),
                "room": normalize_text(thermo.get("room"), "Senza stanza"),
                "address": to_address(thermo.get("address"), 1),
                "setpoint": to_float(thermo.get("setpoint"), 21.0),
            }
        )

    return {
        "gateway": {
            "host": normalize_text(gateway_raw.get("host"), defaults["gateway"]["host"]),
            "port": to_port(gateway_raw.get("port"), defaults["gateway"]["port"]),
            "timeoutMs": to_timeout(gateway_raw.get("timeoutMs"), defaults["gateway"]["timeoutMs"]),
        },
        "apiToken": normalize_text(raw.get("apiToken"), defaults["apiToken"]),
        "boards": boards_out,
        "entities": {
            "lights": lights_out,
            "shutters": shutters_out,
            "thermostats": thermostats_out,
        },
    }


def normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}

    return {
        "boards": raw.get("boards") if isinstance(raw.get("boards"), dict) else {},
        "lights": raw.get("lights") if isinstance(raw.get("lights"), dict) else {},
        "shutters": raw.get("shutters") if isinstance(raw.get("shutters"), dict) else {},
        "thermostats": raw.get("thermostats") if isinstance(raw.get("thermostats"), dict) else {},
        "updatedAt": int(to_number(raw.get("updatedAt"), 0)),
    }


def to_hex(byte: int) -> str:
    return f"0x{to_byte(byte, 0):02x}"


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


def send_raw(payload: bytes, expect_frame: bool = True, expected_bytes: int = 1) -> Any:
    cfg = get_config()
    gateway = cfg.get("gateway", {})
    host = normalize_text(gateway.get("host"), "127.0.0.1")
    port = to_port(gateway.get("port"), 1470)
    timeout = to_timeout(gateway.get("timeoutMs"), 1200) / 1000.0

    received = b""
    end_at = time.monotonic() + timeout

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)

            while time.monotonic() < end_at:
                try:
                    chunk = sock.recv(1024)
                except socket.timeout:
                    break

                if not chunk:
                    break

                received += chunk

                if expect_frame:
                    frame = extract_first_frame(received)
                    if frame is not None:
                        return parse_frame(frame)
                else:
                    if len(received) >= max(1, expected_bytes):
                        return received[: max(1, expected_bytes)]

    except OSError as exc:
        raise RuntimeError(f"Errore comunicazione gateway: {exc}") from exc

    if expect_frame:
        frame = extract_first_frame(received)
        if frame is not None:
            return parse_frame(frame)
        raise RuntimeError("Risposta protocollo non valida")

    if len(received) >= max(1, expected_bytes):
        return received[: max(1, expected_bytes)]

    raise RuntimeError("Nessuna risposta ricevuta")


def send_frame(address: int, command: int, g_bytes: list[int]) -> dict[str, Any]:
    payload = build_frame(address, command, g_bytes)
    result = send_raw(payload, expect_frame=True)
    assert isinstance(result, dict)
    return result


def decode_bits(mask: int) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for idx in range(1, 9):
        out[str(idx)] = (mask & (1 << (idx - 1))) != 0
    return out


def decode_polling_frame(frame: dict[str, Any]) -> dict[str, Any]:
    g = frame.get("g", [0] * 10)
    type_and_release = to_byte(g[0], 0)
    output_mask = to_byte(g[1], 0)
    input_mask = to_byte(g[2], 0)
    sign = -1 if g[6] == 0x2D else 1
    temp_int = to_byte(g[4], 0)
    temp_dec = to_byte(g[5], 0)

    return {
        "boardType": type_and_release & 0x0F,
        "release": (type_and_release >> 4) & 0x0F,
        "outputMask": output_mask,
        "inputMask": input_mask,
        "outputs": decode_bits(output_mask),
        "inputs": decode_bits(input_mask),
        "dimmer": to_byte(g[3], 0),
        "temperature": sign * (temp_int + temp_dec / 10),
        "powerKw": to_byte(g[7], 0) / 10,
        "setpoint": to_byte(g[8], 0),
    }


def is_input_active(mask: int, index: int) -> bool | None:
    if index < 1 or index > 8:
        return None
    bit = 1 << (index - 1)
    return (mask & bit) == 0


def collect_addresses(cfg: dict[str, Any]) -> list[int]:
    addresses = set()
    for board in cfg.get("boards", []):
        addresses.add(to_address(board.get("address"), -1))
    for light in cfg.get("entities", {}).get("lights", []):
        addresses.add(to_address(light.get("address"), -1))
    for shutter in cfg.get("entities", {}).get("shutters", []):
        addresses.add(to_address(shutter.get("address"), -1))
    for thermo in cfg.get("entities", {}).get("thermostats", []):
        addresses.add(to_address(thermo.get("address"), -1))
    return sorted(addr for addr in addresses if 0 <= addr <= 254)


def split_temperature(value: float) -> tuple[int, int]:
    rounded = round(abs(value), 1)
    i = int(rounded)
    d = int(round((rounded - i) * 10))
    return clamp_int(i, 0, 99), clamp_int(d, 0, 9)


def poll_board(address: int) -> dict[str, Any]:
    response = send_frame(address, 0x40, [])
    poll = decode_polling_frame(response)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("boards", {})[str(address)] = {
            "address": address,
            "poll": poll,
            "updatedAt": int(time.time() * 1000),
            "frameHex": response.get("hex"),
        }
        state["updatedAt"] = int(time.time() * 1000)

    update_state(mutator)
    return poll


def infer_light_state(light: dict[str, Any], poll: dict[str, Any] | None, fallback: Any, action: str | None) -> bool | None:
    if poll is not None and "outputMask" in poll:
        relay = clamp_int(to_number(light.get("relay"), 1), 1, 8)
        bit = 1 << (relay - 1)
        return (to_byte(poll.get("outputMask"), 0) & bit) != 0

    if action == "on":
        return True
    if action == "off":
        return False
    if action == "toggle" and isinstance(fallback, bool):
        return not fallback

    return fallback if isinstance(fallback, bool) else None


def find_light(cfg: dict[str, Any], light_id: str, address_raw: str, relay_raw: str) -> dict[str, Any] | None:
    lights = cfg.get("entities", {}).get("lights", [])
    if light_id:
        for light in lights:
            if light.get("id") == light_id:
                return light
        return None

    address = to_address(address_raw, -1)
    relay = clamp_int(to_number(relay_raw, -1), 1, 8)
    if address < 0:
        return None

    for light in lights:
        if to_address(light.get("address"), -1) == address and clamp_int(to_number(light.get("relay"), -1), 1, 8) == relay:
            return light
    return None


def find_shutter(cfg: dict[str, Any], shutter_id: str, address_raw: str, channel_raw: str) -> dict[str, Any] | None:
    shutters = cfg.get("entities", {}).get("shutters", [])
    if shutter_id:
        for shutter in shutters:
            if shutter.get("id") == shutter_id:
                return shutter
        return None

    address = to_address(address_raw, -1)
    channel = clamp_int(to_number(channel_raw, -1), 1, 4)
    if address < 0:
        return None

    for shutter in shutters:
        if to_address(shutter.get("address"), -1) == address and clamp_int(to_number(shutter.get("channel"), -1), 1, 4) == channel:
            return shutter
    return None


def find_thermostat(cfg: dict[str, Any], thermo_id: str, address_raw: str) -> dict[str, Any] | None:
    thermos = cfg.get("entities", {}).get("thermostats", [])
    if thermo_id:
        for thermo in thermos:
            if thermo.get("id") == thermo_id:
                return thermo
        return None

    address = to_address(address_raw, -1)
    if address < 0:
        return None

    for thermo in thermos:
        if to_address(thermo.get("address"), -1) == address:
            return thermo
    return None


def build_status(refresh: bool) -> dict[str, Any]:
    cfg = get_config()
    snapshot = get_state()

    refresh_errors: list[dict[str, Any]] = []
    if refresh:
        for address in collect_addresses(cfg):
            try:
                poll_board(address)
            except Exception as exc:
                refresh_errors.append({"address": address, "error": str(exc)})

    snapshot = get_state()
    room_map: dict[str, dict[str, Any]] = {}

    def room_of(name: str) -> dict[str, Any]:
        key = (name or "Senza stanza").strip() or "Senza stanza"
        if key not in room_map:
            room_map[key] = {
                "name": key,
                "lights": [],
                "shutters": [],
                "thermostats": [],
                "inputs": [],
            }
        return room_map[key]

    for light in cfg.get("entities", {}).get("lights", []):
        address = to_address(light.get("address"), 1)
        board_state = snapshot.get("boards", {}).get(str(address), {})
        poll = board_state.get("poll") if isinstance(board_state, dict) else None
        prev = snapshot.get("lights", {}).get(light.get("id"), {})
        fallback = prev.get("isOn") if isinstance(prev, dict) else None
        is_on = infer_light_state(light, poll if isinstance(poll, dict) else None, fallback, None)

        room = room_of(normalize_text(light.get("room"), "Senza stanza"))
        room["lights"].append(
            {
                "id": light.get("id"),
                "name": light.get("name"),
                "room": light.get("room"),
                "address": address,
                "relay": clamp_int(to_number(light.get("relay"), 1), 1, 8),
                "isOn": is_on,
            }
        )

    for shutter in cfg.get("entities", {}).get("shutters", []):
        sh_state = snapshot.get("shutters", {}).get(shutter.get("id"), {})
        action = sh_state.get("action") if isinstance(sh_state, dict) else "unknown"
        room = room_of(normalize_text(shutter.get("room"), "Senza stanza"))
        room["shutters"].append(
            {
                "id": shutter.get("id"),
                "name": shutter.get("name"),
                "room": shutter.get("room"),
                "address": to_address(shutter.get("address"), 1),
                "channel": clamp_int(to_number(shutter.get("channel"), 1), 1, 4),
                "action": action or "unknown",
            }
        )

    for thermo in cfg.get("entities", {}).get("thermostats", []):
        address = to_address(thermo.get("address"), 1)
        board_state = snapshot.get("boards", {}).get(str(address), {})
        poll = board_state.get("poll") if isinstance(board_state, dict) else None
        t_state = snapshot.get("thermostats", {}).get(thermo.get("id"), {})
        setpoint = None
        if isinstance(t_state, dict) and isinstance(t_state.get("setpoint"), (int, float)):
            setpoint = float(t_state.get("setpoint"))
        elif isinstance(thermo.get("setpoint"), (int, float)):
            setpoint = float(thermo.get("setpoint"))

        room = room_of(normalize_text(thermo.get("room"), "Senza stanza"))
        room["thermostats"].append(
            {
                "id": thermo.get("id"),
                "name": thermo.get("name"),
                "room": thermo.get("room"),
                "address": address,
                "temperature": poll.get("temperature") if isinstance(poll, dict) else None,
                "setpoint": setpoint,
                "boardSetpoint": poll.get("setpoint") if isinstance(poll, dict) else None,
            }
        )

    for board in cfg.get("boards", []):
        board_address = to_address(board.get("address"), -1)
        poll = snapshot.get("boards", {}).get(str(board_address), {}).get("poll")
        input_mask = to_byte(poll.get("inputMask"), 0) if isinstance(poll, dict) else None

        for inp in board.get("inputs", []):
            idx = clamp_int(to_number(inp.get("index"), 1), 1, 8)
            active = is_input_active(input_mask, idx) if input_mask is not None else None
            room = room_of(normalize_text(inp.get("room"), "Senza stanza"))
            room["inputs"].append(
                {
                    "boardId": board.get("id"),
                    "boardAddress": board_address,
                    "index": idx,
                    "name": inp.get("name"),
                    "room": inp.get("room"),
                    "active": active,
                    "enabled": inp.get("enabled", True),
                    "g2": to_byte(inp.get("g2"), 0),
                    "g3": to_byte(inp.get("g3"), 0),
                    "g4": to_byte(inp.get("g4"), 0),
                    "targetAddress": to_address(inp.get("targetAddress"), board_address),
                }
            )

    now_ms = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        lights = state.setdefault("lights", {})
        for room in room_map.values():
            for light in room["lights"]:
                lights[str(light["id"])] = {
                    "isOn": light["isOn"],
                    "updatedAt": now_ms,
                }
        state["updatedAt"] = now_ms

    update_state(mutator)

    rooms = sorted(room_map.values(), key=lambda room: room["name"].lower())
    return {
        "updatedAt": now_ms,
        "refreshErrors": refresh_errors,
        "rooms": rooms,
    }


def token_valid(token: str) -> bool:
    expected = normalize_text(get_config().get("apiToken"), "")
    return bool(expected) and token == expected


def query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def api_light(query: dict[str, list[str]]) -> dict[str, Any]:
    action = query_value(query, "action").strip().lower()
    code = LIGHT_ACTIONS.get(action)
    if code is None:
        raise ValueError("action non valida")

    cfg = get_config()
    light = find_light(cfg, query_value(query, "id"), query_value(query, "address"), query_value(query, "relay"))
    if light is None:
        raise LookupError("Luce non trovata")

    relay = clamp_int(to_number(light.get("relay"), 1), 1, 8)
    command = RELAY_COMMANDS.get(relay)
    if command is None:
        raise ValueError("relay non valida")

    frame = send_frame(to_address(light.get("address"), 1), command, [code])

    poll = None
    try:
        poll = poll_board(to_address(light.get("address"), 1))
    except Exception:
        poll = None

    snapshot = get_state()
    prev = snapshot.get("lights", {}).get(light.get("id"), {})
    fallback = prev.get("isOn") if isinstance(prev, dict) else None
    is_on = infer_light_state(light, poll, fallback, action)

    now_ms = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("lights", {})[str(light.get("id"))] = {
            "isOn": is_on,
            "updatedAt": now_ms,
        }
        state["updatedAt"] = now_ms

    update_state(mutator)

    return {
        "ok": True,
        "entity": light,
        "action": action,
        "frame": frame,
    }


def api_shutter(query: dict[str, list[str]]) -> dict[str, Any]:
    action = query_value(query, "action").strip().lower()
    code = SHUTTER_ACTIONS.get(action)
    if code is None:
        raise ValueError("action non valida")

    cfg = get_config()
    shutter = find_shutter(
        cfg,
        query_value(query, "id"),
        query_value(query, "address"),
        query_value(query, "channel"),
    )
    if shutter is None:
        raise LookupError("Tapparella non trovata")

    frame = send_frame(
        to_address(shutter.get("address"), 1),
        0x5C,
        [clamp_int(to_number(shutter.get("channel"), 1), 1, 4), code],
    )

    now_ms = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("shutters", {})[str(shutter.get("id"))] = {
            "action": action,
            "updatedAt": now_ms,
        }
        state["updatedAt"] = now_ms

    update_state(mutator)

    return {
        "ok": True,
        "entity": shutter,
        "action": action,
        "frame": frame,
    }


def api_thermostat(query: dict[str, list[str]]) -> dict[str, Any]:
    setpoint = to_float(query_value(query, "set"), float("nan"))
    if not math.isfinite(setpoint):
        raise ValueError("set mancante o non valido")

    cfg = get_config()
    thermo = find_thermostat(cfg, query_value(query, "id"), query_value(query, "address"))
    if thermo is None:
        raise LookupError("Termostato non trovato")

    i, d = split_temperature(setpoint)
    frame = send_frame(to_address(thermo.get("address"), 1), 0x5A, [i, d])

    now_ms = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("thermostats", {})[str(thermo.get("id"))] = {
            "setpoint": setpoint,
            "updatedAt": now_ms,
        }
        state["updatedAt"] = now_ms

    update_state(mutator)

    try:
        poll_board(to_address(thermo.get("address"), 1))
    except Exception:
        pass

    return {
        "ok": True,
        "entity": thermo,
        "setpoint": setpoint,
        "frame": frame,
    }


def api_apply_inputs(query: dict[str, list[str]]) -> dict[str, Any]:
    cfg = get_config()
    board_filter = query_value(query, "board").strip()
    address_filter = to_address(query_value(query, "address"), -1)

    targets = []
    for board in cfg.get("boards", []):
        if board_filter and board.get("id") != board_filter:
            continue
        if address_filter >= 0 and to_address(board.get("address"), -1) != address_filter:
            continue
        targets.append(board)

    if not targets:
        raise LookupError("Nessuna scheda trovata")

    results: list[dict[str, Any]] = []
    for board in targets:
        board_address = to_address(board.get("address"), 1)
        for inp in board.get("inputs", []):
            if inp.get("enabled") is False:
                continue

            g_bytes = [
                clamp_int(to_number(inp.get("index"), 1), 1, 8),
                to_byte(inp.get("g2"), 0),
                to_byte(inp.get("g3"), 0),
                to_byte(inp.get("g4"), 0),
                to_address(inp.get("targetAddress"), board_address),
            ]

            try:
                frame = send_frame(board_address, 0x55, g_bytes)
                results.append(
                    {
                        "ok": True,
                        "boardId": board.get("id"),
                        "boardAddress": board_address,
                        "input": g_bytes[0],
                        "frame": frame,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "ok": False,
                        "boardId": board.get("id"),
                        "boardAddress": board_address,
                        "input": g_bytes[0],
                        "error": str(exc),
                    }
                )

    return {
        "ok": all(item.get("ok") for item in results),
        "results": results,
    }


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


class AlgoHandler(BaseHTTPRequestHandler):
    server_version = "AlgoDomoPython/1.0"

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
                self._redirect("/control")
                return

            if self.command == "GET" and path == "/config":
                self._serve_file(PUBLIC_DIR / "config.html", "text/html; charset=utf-8")
                return

            if self.command == "GET" and path == "/control":
                self._serve_file(PUBLIC_DIR / "control.html", "text/html; charset=utf-8")
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
                payload = self._read_json_body(default=get_config())
                config = set_config(payload)
                self._json(HTTPStatus.OK, {"ok": True, "config": config})
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

                if path == "/api/cmd/light":
                    self._json(HTTPStatus.OK, api_light(query))
                    return

                if path == "/api/cmd/shutter":
                    self._json(HTTPStatus.OK, api_shutter(query))
                    return

                if path == "/api/cmd/thermostat":
                    self._json(HTTPStatus.OK, api_thermostat(query))
                    return

                if path == "/api/cmd/poll":
                    address = to_address(query_value(query, "address"), -1)
                    if address < 0:
                        raise ValueError("address mancante")
                    poll = poll_board(address)
                    self._json(HTTPStatus.OK, {"ok": True, "poll": poll})
                    return

                if path == "/api/cmd/apply-inputs":
                    self._json(HTTPStatus.OK, api_apply_inputs(query))
                    return

                if path == "/api/cmd/program-address":
                    self._json(HTTPStatus.OK, api_program_address(query))
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

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists() or not file_path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "File non trovato"})
            return

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
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
    port = to_port(os.environ.get("PORT", "8080"), 8080)
    server = ThreadingHTTPServer((host, port), AlgoHandler)
    print(f"AlgoDomo Python in ascolto su http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
