#!/usr/bin/env python3
"""AlgoDomo MQTT bridge for Home Assistant (no rooms, channel names as CHx)."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import paho.mqtt.client as mqtt
except Exception as exc:  # noqa: BLE001
    mqtt = None
    MQTT_IMPORT_ERROR = exc
else:
    MQTT_IMPORT_ERROR = None

LOGGER = logging.getLogger("algodomoiot-mqtt")


def bool_env(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def int_env(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(str(raw).strip())
    except Exception:
        value = default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def text_env(name: str, default: str = "") -> str:
    value = str(os.environ.get(name, default)).strip()
    return value or default


def slugify(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return out or "board"


def as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return fallback


def clamp(value: int, min_value: int, max_value: int) -> int:
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


class AlgoDomoMqttBridge:
    def __init__(self) -> None:
        self.enabled = bool_env("MQTT_ENABLED", False)
        self.host = text_env("MQTT_HOST", "127.0.0.1")
        self.port = int_env("MQTT_PORT", 1883, 1, 65535)
        self.username = text_env("MQTT_USERNAME", "")
        self.password = text_env("MQTT_PASSWORD", "")
        self.client_id = text_env("MQTT_CLIENT_ID", "algodomoiot")
        self.base_topic = text_env("MQTT_BASE_TOPIC", "algodomoiot").strip("/")
        self.discovery_prefix = text_env("MQTT_DISCOVERY_PREFIX", "homeassistant").strip("/")
        self.keepalive = int_env("MQTT_KEEPALIVE", 60, 10, 86400)
        self.poll_interval = int_env("MQTT_POLL_INTERVAL", 30, 2, 3600)
        self.qos = int_env("MQTT_QOS", 0, 0, 2)
        self.retain = bool_env("MQTT_RETAIN", True)
        self.http_base = text_env("ALGODOMO_HTTP_BASE", "http://127.0.0.1").rstrip("/")
        self.api_token = text_env("ALGODOMO_TOKEN", "")

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._boards: list[dict[str, Any]] = []
        self._boards_by_slug: dict[str, dict[str, Any]] = {}
        if mqtt is None:
            if self.enabled:
                raise RuntimeError(f"Modulo paho-mqtt non disponibile: {MQTT_IMPORT_ERROR}")
            self._mqtt = None
            return
        self._mqtt = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)
        if self.username:
            self._mqtt.username_pw_set(self.username, self.password)
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_disconnect = self._on_disconnect
        self._mqtt.on_message = self._on_message
        self._mqtt.will_set(f"{self.base_topic}/bridge/status", "offline", qos=self.qos, retain=True)

    def _http_json(self, path: str, timeout: int = 10) -> dict[str, Any]:
        req = Request(f"{self.http_base}{path}", headers={"Accept": "application/json"})  # noqa: S310
        with urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8", errors="ignore")
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}

    def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        q = dict(params)
        q["token"] = self.api_token
        query = urlencode(q, doseq=False)
        return self._http_json(f"{path}?{query}")

    def _load_boards(self) -> None:
        cfg = self._http_json("/api/config")
        boards_raw = cfg.get("boards")
        if not isinstance(boards_raw, list):
            boards_raw = []
        boards: list[dict[str, Any]] = []
        by_slug: dict[str, dict[str, Any]] = {}
        for raw in boards_raw:
            if not isinstance(raw, dict):
                continue
            board_id = str(raw.get("id", "")).strip()
            board_name = str(raw.get("name") or board_id or "Scheda").strip()
            if not board_id:
                board_id = slugify(board_name)
            kind = str(raw.get("kind", "")).strip().lower()
            address = as_int(raw.get("address"), -1)
            if kind not in {"light", "shutter", "thermostat", "dimmer"}:
                continue
            if address < 0:
                continue
            channels: list[int] = []
            for ch_raw in raw.get("channels", []) if isinstance(raw.get("channels"), list) else []:
                if not isinstance(ch_raw, dict):
                    continue
                num = as_int(ch_raw.get("channel"), -1)
                if num >= 1:
                    channels.append(num)
            if not channels:
                start = as_int(raw.get("channelStart"), 1)
                end = as_int(raw.get("channelEnd"), start)
                if end < start:
                    end = start
                channels = list(range(max(1, start), max(1, end) + 1))
            channels = sorted(set(channels))
            slug = slugify(board_id)
            board = {
                "id": board_id,
                "slug": slug,
                "name": board_name,
                "kind": kind,
                "address": address,
                "channels": channels,
            }
            boards.append(board)
            by_slug[slug] = board
        with self._lock:
            self._boards = boards
            self._boards_by_slug = by_slug

    def _topic_prefix(self, board: dict[str, Any]) -> str:
        return f"{self.base_topic}/{board['slug']}"

    def _availability_topic(self, board: dict[str, Any]) -> str:
        return f"{self._topic_prefix(board)}/availability"

    def _publish(self, topic: str, payload: Any, retain: bool | None = None) -> None:
        if isinstance(payload, dict):
            raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        else:
            raw = str(payload)
        hold = self.retain if retain is None else retain
        self._mqtt.publish(topic, raw, qos=self.qos, retain=hold)

    def _device_payload(self, board: dict[str, Any]) -> dict[str, Any]:
        return {
            "identifiers": [f"algodomoiot_{board['slug']}"],
            "name": board["name"],
            "manufacturer": "AlgoDomo",
            "model": f"board-{board['kind']}",
        }

    def _publish_discovery(self) -> None:
        with self._lock:
            boards = list(self._boards)
        for board in boards:
            device = self._device_payload(board)
            availability = self._availability_topic(board)
            topic_prefix = self._topic_prefix(board)
            for channel in board["channels"]:
                suffix = f"algodomoiot_{board['slug']}_ch{channel}"
                name = f"{board['name']} CH{channel}"
                if board["kind"] == "light":
                    self._publish(
                        f"{self.discovery_prefix}/switch/{suffix}/config",
                        {
                            "name": name,
                            "unique_id": suffix,
                            "command_topic": f"{topic_prefix}/ch{channel}/set",
                            "state_topic": f"{topic_prefix}/ch{channel}/state",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )
                elif board["kind"] == "shutter":
                    self._publish(
                        f"{self.discovery_prefix}/cover/{suffix}/config",
                        {
                            "name": name,
                            "unique_id": suffix,
                            "command_topic": f"{topic_prefix}/ch{channel}/set",
                            "state_topic": f"{topic_prefix}/ch{channel}/state",
                            "payload_open": "OPEN",
                            "payload_close": "CLOSE",
                            "payload_stop": "STOP",
                            "state_open": "OPEN",
                            "state_opening": "OPENING",
                            "state_closed": "CLOSED",
                            "state_closing": "CLOSING",
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )
                elif board["kind"] == "dimmer":
                    self._publish(
                        f"{self.discovery_prefix}/light/{suffix}/config",
                        {
                            "name": name,
                            "unique_id": suffix,
                            "command_topic": f"{topic_prefix}/ch{channel}/set",
                            "state_topic": f"{topic_prefix}/ch{channel}/state",
                            "brightness_command_topic": f"{topic_prefix}/ch{channel}/brightness/set",
                            "brightness_state_topic": f"{topic_prefix}/ch{channel}/brightness/state",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )
                else:
                    self._publish(
                        f"{self.discovery_prefix}/sensor/{suffix}_temperature/config",
                        {
                            "name": f"{name} Temp",
                            "unique_id": f"{suffix}_temperature",
                            "state_topic": f"{topic_prefix}/ch{channel}/temperature/state",
                            "unit_of_measurement": "C",
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )
                    self._publish(
                        f"{self.discovery_prefix}/number/{suffix}_setpoint/config",
                        {
                            "name": f"{name} Set",
                            "unique_id": f"{suffix}_setpoint",
                            "command_topic": f"{topic_prefix}/ch{channel}/setpoint/set",
                            "state_topic": f"{topic_prefix}/ch{channel}/setpoint/state",
                            "min": 5,
                            "max": 30,
                            "step": 0.5,
                            "mode": "box",
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )
                    self._publish(
                        f"{self.discovery_prefix}/select/{suffix}_mode/config",
                        {
                            "name": f"{name} Mode",
                            "unique_id": f"{suffix}_mode",
                            "command_topic": f"{topic_prefix}/ch{channel}/mode/set",
                            "state_topic": f"{topic_prefix}/ch{channel}/mode/state",
                            "options": ["WINTER", "SUMMER"],
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )
                    self._publish(
                        f"{self.discovery_prefix}/switch/{suffix}_power/config",
                        {
                            "name": f"{name} Power",
                            "unique_id": f"{suffix}_power",
                            "command_topic": f"{topic_prefix}/ch{channel}/power/set",
                            "state_topic": f"{topic_prefix}/ch{channel}/power/state",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )
                    self._publish(
                        f"{self.discovery_prefix}/binary_sensor/{suffix}_active/config",
                        {
                            "name": f"{name} Active",
                            "unique_id": f"{suffix}_active",
                            "state_topic": f"{topic_prefix}/ch{channel}/active/state",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "availability_topic": availability,
                            "device": device,
                        },
                        retain=True,
                    )

    def _publish_board_states(self, board_state: dict[str, Any], failed_addresses: set[int]) -> None:
        board_id = str(board_state.get("id", "")).strip()
        with self._lock:
            board = next((item for item in self._boards if item["id"] == board_id), None)
        if board is None:
            return
        topic_prefix = self._topic_prefix(board)
        is_online = int(board.get("address", -1)) not in failed_addresses
        self._publish(self._availability_topic(board), "online" if is_online else "offline", retain=True)
        channels = board_state.get("channels")
        if not isinstance(channels, list):
            return
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            ch = as_int(channel.get("channel"), -1)
            if ch < 1:
                continue
            if board["kind"] == "light":
                self._publish(f"{topic_prefix}/ch{ch}/state", "ON" if channel.get("isOn") else "OFF", retain=True)
            elif board["kind"] == "shutter":
                action = str(channel.get("action", "stop")).lower()
                state = "STOP"
                if action == "up":
                    state = "OPENING"
                elif action == "down":
                    state = "CLOSING"
                self._publish(f"{topic_prefix}/ch{ch}/state", state, retain=True)
            elif board["kind"] == "dimmer":
                level = clamp(as_int(channel.get("level"), 0), 0, 9)
                brightness = clamp(round(level * 255 / 9), 0, 255)
                self._publish(f"{topic_prefix}/ch{ch}/state", "ON" if level > 0 else "OFF", retain=True)
                self._publish(f"{topic_prefix}/ch{ch}/brightness/state", brightness, retain=True)
            elif board["kind"] == "thermostat":
                temp = channel.get("temperature")
                setpoint = channel.get("setpoint")
                mode = str(channel.get("mode", "winter")).upper()
                is_on = bool(channel.get("isOn"))
                is_active = bool(channel.get("isActive")) if channel.get("isActive") is not None else is_on
                if isinstance(temp, (int, float)):
                    self._publish(f"{topic_prefix}/ch{ch}/temperature/state", round(float(temp), 1), retain=True)
                if isinstance(setpoint, (int, float)):
                    self._publish(f"{topic_prefix}/ch{ch}/setpoint/state", round(float(setpoint), 1), retain=True)
                self._publish(f"{topic_prefix}/ch{ch}/mode/state", "SUMMER" if mode == "SUMMER" else "WINTER", retain=True)
                self._publish(f"{topic_prefix}/ch{ch}/power/state", "ON" if is_on else "OFF", retain=True)
                self._publish(f"{topic_prefix}/ch{ch}/active/state", "ON" if is_active else "OFF", retain=True)

    def publish_status(self, refresh: bool) -> None:
        try:
            status = self._api_get("/api/status", {"refresh": 1 if refresh else 0})
        except Exception as exc:
            LOGGER.warning("Polling stato fallito: %s", exc)
            return
        refresh_errors = status.get("refreshErrors")
        failed_addresses: set[int] = set()
        if isinstance(refresh_errors, list):
            for item in refresh_errors:
                if isinstance(item, dict):
                    failed_addresses.add(as_int(item.get("address"), -1))
        boards = status.get("boards")
        if not isinstance(boards, list):
            return
        for board_state in boards:
            if isinstance(board_state, dict):
                self._publish_board_states(board_state, failed_addresses)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
        raw_rc = getattr(reason_code, "value", reason_code)
        rc = as_int(raw_rc, -1)
        if rc != 0 and str(reason_code).strip().lower() in {"success", "0"}:
            rc = 0
        if rc != 0:
            LOGGER.error("Connessione MQTT fallita: rc=%s", rc)
            return
        LOGGER.info("MQTT connesso a %s:%d", self.host, self.port)
        self._publish(f"{self.base_topic}/bridge/status", "online", retain=True)
        self._mqtt.subscribe(f"{self.base_topic}/poll_all/set", qos=self.qos)
        self._mqtt.subscribe(f"{self.base_topic}/+/ch+/set", qos=self.qos)
        self._mqtt.subscribe(f"{self.base_topic}/+/ch+/brightness/set", qos=self.qos)
        self._mqtt.subscribe(f"{self.base_topic}/+/ch+/setpoint/set", qos=self.qos)
        self._mqtt.subscribe(f"{self.base_topic}/+/ch+/mode/set", qos=self.qos)
        self._mqtt.subscribe(f"{self.base_topic}/+/ch+/power/set", qos=self.qos)
        self._publish_discovery()
        self.publish_status(refresh=True)

    def _on_disconnect(self, client, userdata, reason_code, properties=None):  # noqa: ANN001
        LOGGER.warning("MQTT disconnesso: rc=%s", reason_code)

    def _send_command(self, board: dict[str, Any], channel: int, tail: str, payload: str) -> None:
        entity_id = f"{board['id']}-c{channel}"
        text = payload.strip().upper()
        if board["kind"] == "light" and tail == "set":
            action = "toggle" if text == "TOGGLE" else ("on" if text in {"ON", "1", "TRUE"} else "off")
            self._api_get("/api/cmd/light", {"id": entity_id, "action": action})
            return
        if board["kind"] == "shutter" and tail == "set":
            if text in {"OPEN", "UP", "SU"}:
                action = "up"
            elif text in {"CLOSE", "DOWN", "GIU"}:
                action = "down"
            else:
                action = "stop"
            self._api_get("/api/cmd/shutter", {"id": entity_id, "action": action})
            return
        if board["kind"] == "dimmer":
            if tail == "brightness/set":
                raw_value = as_int(text, 0)
                if raw_value <= 9:
                    level = raw_value
                elif raw_value <= 100:
                    level = round(raw_value * 9 / 100)
                else:
                    level = round(raw_value * 9 / 255)
                self._api_get("/api/cmd/dimmer", {"id": entity_id, "level": clamp(level, 0, 9)})
                return
            if tail == "set":
                if text in {"ON", "1", "TRUE", "OFF", "0", "FALSE", "TOGGLE"}:
                    action = text.lower()
                    self._api_get("/api/cmd/dimmer", {"id": entity_id, "action": action})
                    return
                value = as_int(text, -1)
                if value >= 0:
                    self._api_get("/api/cmd/dimmer", {"id": entity_id, "level": clamp(value, 0, 9)})
            return
        if board["kind"] == "thermostat":
            if tail == "setpoint/set":
                self._api_get("/api/cmd/thermostat", {"id": entity_id, "set": payload.strip()})
                return
            if tail == "mode/set":
                mode = "summer" if text in {"SUMMER", "COOL", "ESTATE"} else "winter"
                self._api_get("/api/cmd/thermostat", {"id": entity_id, "mode": mode})
                return
            if tail == "power/set":
                power = "on" if text in {"ON", "1", "TRUE"} else "off"
                self._api_get("/api/cmd/thermostat", {"id": entity_id, "power": power})

    def _on_message(self, client, userdata, msg):  # noqa: ANN001
        topic = str(getattr(msg, "topic", "") or "")
        payload = (getattr(msg, "payload", b"") or b"").decode("utf-8", errors="ignore").strip()
        if topic == f"{self.base_topic}/poll_all/set":
            self.publish_status(refresh=True)
            return
        prefix = f"{self.base_topic}/"
        if not topic.startswith(prefix):
            return
        tail = topic[len(prefix) :]
        match = re.match(r"^([^/]+)/ch([0-9]+)/(set|brightness/set|setpoint/set|mode/set|power/set)$", tail)
        if not match:
            return
        slug = match.group(1)
        channel = as_int(match.group(2), -1)
        cmd_tail = match.group(3)
        with self._lock:
            board = self._boards_by_slug.get(slug)
        if board is None or channel < 1:
            return
        try:
            self._send_command(board, channel, cmd_tail, payload)
            self.publish_status(refresh=False)
        except Exception as exc:
            LOGGER.warning("Comando MQTT fallito topic=%s: %s", topic, exc)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.publish_status(refresh=True)

    def run(self) -> int:
        if not self.enabled:
            LOGGER.info("MQTT disabilitato (MQTT_ENABLED=0)")
            return 0
        if not self.api_token:
            LOGGER.error("Token API mancante (ALGODOMO_TOKEN)")
            return 2
        self._load_boards()
        self._mqtt.connect_async(self.host, self.port, keepalive=self.keepalive)
        self._mqtt.loop_start()
        poll_thread = threading.Thread(target=self._poll_loop, name="mqtt-poll", daemon=True)
        poll_thread.start()
        while not self._stop.is_set():
            time.sleep(0.25)
        try:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        except Exception:
            pass
        return 0

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(asctime)s %(message)s")
    bridge = AlgoDomoMqttBridge()

    def _stop_handler(signum, frame):  # noqa: ANN001
        LOGGER.info("Ricevuto segnale %s, stop MQTT bridge", signum)
        bridge.stop()

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
