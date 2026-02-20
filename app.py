#!/usr/bin/env python3
"""AlgoDomo lightweight web app over serial /dev/ttyS0."""

from __future__ import annotations

import copy
import json
import math
import os
import select
import socket
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

ALLOWED_KINDS = {"light", "shutter", "thermostat"}
MAX_CHANNEL_BY_KIND = {
    "light": 8,
    "shutter": 4,
    "thermostat": 8,
}

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"
DEFAULT_CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_STATE_PATH = DATA_DIR / "state.json"
CONFIG_PATH = Path(os.environ.get("ALGODOMO_CONFIG", str(DEFAULT_CONFIG_PATH)))
STATE_PATH = Path(os.environ.get("ALGODOMO_STATE", str(DEFAULT_STATE_PATH)))

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
    return f"Termostato {channel}"


def default_config() -> dict[str, Any]:
    return {
        "serial": {
            "port": "/dev/ttyS0",
            "baudrate": 9600,
            "timeoutMs": 1200,
        },
        "apiToken": "cambia-questo-token",
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

    with LOCK:
        CONFIG = normalize_config(read_json(CONFIG_PATH, default_config()))
        STATE = normalize_state(read_json(STATE_PATH, default_state()))


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

    serial_raw = raw.get("serial") if isinstance(raw.get("serial"), dict) else {}
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
        "apiToken": normalize_text(raw.get("apiToken"), defaults["apiToken"]),
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
        "updatedAt": int(to_number(raw.get("updatedAt"), 0)),
    }


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
    channel = clamp_int(to_number(channel_raw, -1), 1, 8)
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
    sign = -1 if g[6] == 0x2D else 1
    temp_i = to_byte(g[4], 0)
    temp_d = to_byte(g[5], 0)

    return {
        "boardType": type_and_release & 0x0F,
        "release": (type_and_release >> 4) & 0x0F,
        "outputMask": output_mask,
        "inputMask": input_mask,
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
                refresh_errors.append({"address": address, "error": str(exc)})

    snapshot = get_state()
    now = int(time.time() * 1000)

    boards_out = []
    rooms_map: dict[str, dict[str, Any]] = {}
    new_light_state: dict[str, dict[str, Any]] = {}

    def room_bucket(name: str) -> dict[str, Any]:
        key = normalize_text(name, "Senza stanza")
        if key not in rooms_map:
            rooms_map[key] = {
                "name": key,
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
            ch = clamp_int(to_number(channel.get("channel"), 1), 1, 8)
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
                payload_board["channels"].append(
                    {
                        "id": item_id,
                        "channel": ch,
                        "name": ch_name,
                        "room": ch_room,
                        "temperature": poll.get("temperature") if isinstance(poll, dict) else None,
                        "setpoint": setpoint,
                        "boardSetpoint": poll.get("setpoint") if isinstance(poll, dict) else None,
                    }
                )
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
                        "boardSetpoint": poll.get("setpoint") if isinstance(poll, dict) else None,
                    }
                )

        boards_out.append(payload_board)

    def mutator(state: dict[str, Any]) -> None:
        lights = state.setdefault("lights", {})
        for key, value in new_light_state.items():
            lights[key] = value
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
    setpoint = to_float(query_value(query, "set"), float("nan"))
    if not math.isfinite(setpoint):
        raise ValueError("set mancante o non valido")

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

    i, d = split_temperature(setpoint)
    frame = send_frame(entity["address"], 0x5A, [i, d])
    now = int(time.time() * 1000)

    def mutator(state: dict[str, Any]) -> None:
        state.setdefault("thermostats", {})[entity["id"]] = {
            "setpoint": setpoint,
            "updatedAt": now,
        }
        state["updatedAt"] = now

    update_state(mutator)

    try:
        poll_board(entity["address"])
    except Exception:
        pass

    return {"ok": True, "entity": entity, "setpoint": setpoint, "frame": frame}


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
                    self._json(HTTPStatus.OK, api_poll(query))
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
