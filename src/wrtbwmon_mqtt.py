#!/usr/bin/env python3
"""Publish selected wrtbwmon traffic records to MQTT for Home Assistant."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

try:
    import paho.mqtt.client as mqtt
except ModuleNotFoundError:  # Allows parser and payload unit tests on development hosts.
    mqtt = None


VERSION = "0.2.0"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 60
POLL_INTERVAL_SECONDS = 5
PATH_REFRESH_SECONDS = 60
SOURCE_FRESHNESS_SECONDS = 180
DEFAULT_USAGE_PATH = "/tmp/usage.db"
REGISTRY_PATH = Path("/etc/wrtbwmon-mqtt/published.json")
TOPIC_PREFIX = "wrtbwmon"
GATEWAY_AVAILABILITY_TOPIC = f"{TOPIC_PREFIX}/bridge/availability"
SOURCE_AVAILABILITY_TOPIC = f"{TOPIC_PREFIX}/source/availability"
HA_STATUS_TOPIC = "homeassistant/status"

MAC_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
REQUIRED_COLUMNS = {
    "mac",
    "ip",
    "iface",
    "speed_in",
    "speed_out",
    "in",
    "out",
    "total",
    "first_date",
    "last_date",
}

LOG = logging.getLogger("wrtbwmon-mqtt")


class ConfigError(ValueError):
    """The UCI configuration cannot be used."""


class SnapshotError(ValueError):
    """A wrtbwmon snapshot is incomplete or malformed."""


@dataclass(frozen=True)
class AppConfig:
    host: str
    username: str
    password: str
    macs: tuple[str, ...]


@dataclass
class TrafficRecord:
    mac: str
    ip_addresses: set[str]
    interfaces: set[str]
    speed_in: int
    speed_out: int
    bytes_in: int
    bytes_out: int
    first_date: int
    last_date: int

    def merge(self, other: "TrafficRecord") -> None:
        """Merge rows for the same MAC without making IP part of identity."""
        self.ip_addresses.update(other.ip_addresses)
        self.interfaces.update(other.interfaces)
        self.speed_in += other.speed_in
        self.speed_out += other.speed_out
        self.bytes_in += other.bytes_in
        self.bytes_out += other.bytes_out
        if self.first_date == 0 or (other.first_date and other.first_date < self.first_date):
            self.first_date = other.first_date
        self.last_date = max(self.last_date, other.last_date)

    def state(self, snapshot_timestamp: int) -> dict[str, object]:
        return {
            "download_bps": self.speed_in * 8,
            "upload_bps": self.speed_out * 8,
            "download_bytes": self.bytes_in,
            "upload_bytes": self.bytes_out,
            "ip_addresses": sorted(self.ip_addresses),
            "interfaces": sorted(self.interfaces),
            "first_traffic_timestamp": self.first_date,
            "last_traffic_timestamp": self.last_date,
            "snapshot_timestamp": snapshot_timestamp,
        }


def normalize_mac(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("MAC address must be a string")
    value = value.strip().lower()
    if not MAC_PATTERN.fullmatch(value):
        raise ValueError(f"invalid MAC address: {value!r}")
    return value


def _uci_get(key: str) -> str | None:
    try:
        result = subprocess.run(
            ["/sbin/uci", "-q", "get", key],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("the UCI command is unavailable") from exc
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def load_app_config(getter: Callable[[str], str | None] = _uci_get) -> AppConfig:
    host = (getter("wrtbwmon-mqtt.main.host") or "").strip()
    username = getter("wrtbwmon-mqtt.main.username") or ""
    password = getter("wrtbwmon-mqtt.main.password") or ""
    raw_macs = (getter("wrtbwmon-mqtt.main.mac") or "").split()

    macs: list[str] = []
    seen: set[str] = set()
    for raw_mac in raw_macs:
        try:
            mac = normalize_mac(raw_mac)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if mac not in seen:
            macs.append(mac)
            seen.add(mac)

    return AppConfig(host=host, username=username, password=password, macs=tuple(macs))


def load_usage_path(getter: Callable[[str], str | None] = _uci_get) -> Path:
    configured = (getter("wrtbwmon.general.path") or "").strip()
    return Path(configured or DEFAULT_USAGE_PATH)


def parse_snapshot(text: str) -> dict[str, TrafficRecord]:
    """Parse one complete wrtbwmon CSV snapshot and aggregate duplicate MAC rows."""
    if not text.strip():
        raise SnapshotError("snapshot is empty")

    lines = text.splitlines()
    if not lines or not lines[0].startswith("#mac,"):
        raise SnapshotError("snapshot header must start with #mac")
    lines[0] = lines[0][1:]

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if reader.fieldnames is None or set(reader.fieldnames) != REQUIRED_COLUMNS:
        raise SnapshotError("snapshot has an unexpected column set")

    records: dict[str, TrafficRecord] = {}
    row_count = 0
    for line_number, row in enumerate(reader, start=2):
        row_count += 1
        if None in row or any(row.get(column) is None for column in REQUIRED_COLUMNS):
            raise SnapshotError(f"row {line_number} has an unexpected number of columns")
        try:
            mac = normalize_mac(row["mac"])
            numeric = {
                key: int(row[key])
                for key in (
                    "speed_in",
                    "speed_out",
                    "in",
                    "out",
                    "total",
                    "first_date",
                    "last_date",
                )
            }
        except (TypeError, ValueError) as exc:
            raise SnapshotError(f"row {line_number} contains invalid data") from exc
        if any(value < 0 for value in numeric.values()):
            raise SnapshotError(f"row {line_number} contains a negative counter")

        record = TrafficRecord(
            mac=mac,
            ip_addresses={row["ip"]} if row["ip"] else set(),
            interfaces={row["iface"]} if row["iface"] else set(),
            speed_in=numeric["speed_in"],
            speed_out=numeric["speed_out"],
            bytes_in=numeric["in"],
            bytes_out=numeric["out"],
            first_date=numeric["first_date"],
            last_date=numeric["last_date"],
        )
        if mac in records:
            records[mac].merge(record)
        else:
            records[mac] = record

    if row_count == 0:
        raise SnapshotError("snapshot contains no records")
    return records


def _signature(stat_result: os.stat_result) -> tuple[int, int, int]:
    return (stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)


def read_snapshot(path: Path) -> tuple[dict[str, TrafficRecord], tuple[int, int, int], float]:
    """Read only a snapshot whose metadata is stable for the complete read."""
    before = path.stat()
    text = path.read_text(encoding="utf-8")
    after = path.stat()
    if _signature(before) != _signature(after):
        raise SnapshotError("snapshot changed while it was being read")
    return parse_snapshot(text), _signature(after), after.st_mtime


def compact_mac(mac: str) -> str:
    return mac.replace(":", "")


def discovery_topic(mac: str) -> str:
    return f"homeassistant/device/wrtbwmon_{compact_mac(mac)}/config"


def state_topic(mac: str) -> str:
    return f"{TOPIC_PREFIX}/device/{compact_mac(mac)}/state"


def discovery_payload(mac: str) -> dict[str, object]:
    stable_id = f"wrtbwmon_{compact_mac(mac)}"
    return {
        "device": {
            "identifiers": [stable_id],
            "name": f"Network device {mac}",
            "manufacturer": "wrtbwmon",
            "model": "Network traffic statistics",
        },
        "origin": {"name": "wrtbwmon-hass", "sw_version": VERSION},
        "availability": [
            {"topic": GATEWAY_AVAILABILITY_TOPIC},
            {"topic": SOURCE_AVAILABILITY_TOPIC},
        ],
        "availability_mode": "all",
        "state_topic": state_topic(mac),
        "components": {
            f"{stable_id}_download_rate": {
                "platform": "sensor",
                "name": "Download rate",
                "unique_id": f"{stable_id}_download_rate",
                "device_class": "data_rate",
                "state_class": "measurement",
                "unit_of_measurement": "bit/s",
                "value_template": "{{ value_json.download_bps }}",
            },
            f"{stable_id}_upload_rate": {
                "platform": "sensor",
                "name": "Upload rate",
                "unique_id": f"{stable_id}_upload_rate",
                "device_class": "data_rate",
                "state_class": "measurement",
                "unit_of_measurement": "bit/s",
                "value_template": "{{ value_json.upload_bps }}",
            },
            f"{stable_id}_download_total": {
                "platform": "sensor",
                "name": "Downloaded",
                "unique_id": f"{stable_id}_download_total",
                "device_class": "data_size",
                "state_class": "total_increasing",
                "unit_of_measurement": "B",
                "value_template": "{{ value_json.download_bytes }}",
            },
            f"{stable_id}_upload_total": {
                "platform": "sensor",
                "name": "Uploaded",
                "unique_id": f"{stable_id}_upload_total",
                "device_class": "data_size",
                "state_class": "total_increasing",
                "unit_of_measurement": "B",
                "value_template": "{{ value_json.upload_bytes }}",
            },
        },
    }


class PublishedRegistry:
    def __init__(self, path: Path = REGISTRY_PATH) -> None:
        self.path = path

    def load(self) -> set[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            LOG.warning("Ignoring unreadable published-device registry: %s", exc)
            return set()
        if not isinstance(data, list):
            LOG.warning("Ignoring published-device registry with an invalid format")
            return set()
        result: set[str] = set()
        for item in data:
            try:
                result.add(normalize_mac(item))
            except (TypeError, ValueError):
                LOG.warning("Ignoring an invalid entry in the published-device registry")
        return result

    def save(self, macs: Iterable[str]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = json.dumps(sorted(set(macs)), separators=(",", ":")) + "\n"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _client_id() -> str:
    identity = socket.gethostname().encode("utf-8", errors="replace")
    suffix = hashlib.sha256(identity).hexdigest()[:12]
    return f"wrtbwmon-hass-{suffix}"


def create_mqtt_client(config: AppConfig, *, with_will: bool = True):
    if mqtt is None:
        raise RuntimeError("python3-paho-mqtt is required")
    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=_client_id(),
            protocol=mqtt.MQTTv311,
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=_client_id(), protocol=mqtt.MQTTv311)
    if config.username or config.password:
        client.username_pw_set(config.username, config.password)
    if with_will:
        client.will_set(GATEWAY_AVAILABILITY_TOPIC, "offline", qos=0, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def _wait_for_publish(info, timeout: float = 10) -> None:
    if mqtt is not None and info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish was rejected with code {info.rc}")
    info.wait_for_publish(timeout=timeout)
    if hasattr(info, "is_published") and not info.is_published():
        raise TimeoutError("MQTT publish timed out")


def _connection_succeeded(reason_code) -> bool:
    failure = getattr(reason_code, "is_failure", False)
    try:
        numeric_code = int(reason_code)
    except (TypeError, ValueError):
        numeric_code = 1 if failure else 0
    return not failure and numeric_code == 0


class Bridge:
    def __init__(self, config: AppConfig, registry: PublishedRegistry) -> None:
        self.config = config
        self.registry = registry
        self.stop_event = threading.Event()
        self.connected_event = threading.Event()
        self.resync_event = threading.Event()
        self.client = create_mqtt_client(config)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.latest_states: dict[str, str] = {}
        self.source_available = False
        self.last_good_snapshot: float | None = None
        self.last_signature: tuple[int, int, int] | None = None
        self.usage_path: Path | None = None
        self.source_problem: str | None = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if not _connection_succeeded(reason_code):
            LOG.error("MQTT connection was rejected: %s", reason_code)
            return
        LOG.info("Connected to the MQTT broker")
        self.connected_event.set()
        client.subscribe(HA_STATUS_TOPIC, qos=0)
        client.publish(GATEWAY_AVAILABILITY_TOPIC, "online", qos=0, retain=True)
        client.publish(
            SOURCE_AVAILABILITY_TOPIC,
            "online" if self.source_available else "offline",
            qos=0,
            retain=True,
        )
        self.resync_event.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code=None, properties=None) -> None:
        if self.connected_event.is_set():
            LOG.warning("Disconnected from the MQTT broker")
        self.connected_event.clear()

    def _on_message(self, client, userdata, message) -> None:
        if message.topic == HA_STATUS_TOPIC and message.payload.strip().lower() == b"online":
            self.resync_event.set()

    def _publish(self, topic: str, payload: str, *, retain: bool) -> None:
        if not self.connected_event.is_set():
            raise ConnectionError("MQTT is disconnected")
        _wait_for_publish(self.client.publish(topic, payload, qos=0, retain=retain))

    def _set_source_availability(self, available: bool, *, force: bool = False) -> None:
        if not force and available == self.source_available:
            return
        self.source_available = available
        if self.connected_event.is_set():
            self._publish(
                SOURCE_AVAILABILITY_TOPIC,
                "online" if available else "offline",
                retain=True,
            )

    def _sync_discovery(self) -> None:
        configured = set(self.config.macs)
        previously_published = self.registry.load()
        for mac in sorted(previously_published - configured):
            self._publish(discovery_topic(mac), "", retain=True)
            self._publish(state_topic(mac), "", retain=True)

        for mac in self.config.macs:
            payload = json.dumps(discovery_payload(mac), separators=(",", ":"), sort_keys=True)
            self._publish(discovery_topic(mac), payload, retain=True)
            cached_state = self.latest_states.get(mac)
            if cached_state is not None:
                self._publish(state_topic(mac), cached_state, retain=True)

        self.registry.save(configured)
        self._set_source_availability(self.source_available, force=True)
        LOG.info("Synchronized discovery for %d configured device(s)", len(configured))

    def _refresh_usage_path(self) -> None:
        path = load_usage_path()
        if path != self.usage_path:
            self.usage_path = path
            self.last_signature = None
            self.last_good_snapshot = None
            self.source_problem = None
            self._set_source_availability(False)
            LOG.info("Using wrtbwmon data file %s", path)

    def _report_source_problem(self, problem: str) -> None:
        if problem != self.source_problem:
            LOG.warning("%s", problem)
            self.source_problem = problem

    def _clear_source_problem(self) -> None:
        if self.source_problem is not None:
            LOG.info("The wrtbwmon data file is available again")
            self.source_problem = None

    def _process_snapshot(self, now: float) -> None:
        if self.usage_path is None:
            return
        try:
            current_stat = self.usage_path.stat()
        except OSError as exc:
            self._report_source_problem(f"Cannot access the wrtbwmon data file: {exc}")
            return
        self._clear_source_problem()
        current_signature = _signature(current_stat)
        if current_signature == self.last_signature:
            return

        self.last_signature = current_signature
        try:
            records, stable_signature, modified_at = read_snapshot(self.usage_path)
        except (OSError, UnicodeError, SnapshotError) as exc:
            LOG.warning("Skipping incomplete wrtbwmon snapshot: %s", exc)
            return
        self.last_signature = stable_signature

        age = max(0.0, time.time() - modified_at)
        if age > SOURCE_FRESHNESS_SECONDS:
            LOG.warning("Ignoring stale wrtbwmon snapshot")
            return

        snapshot_timestamp = int(modified_at)
        snapshot_states: dict[str, str] = {}
        for mac in self.config.macs:
            record = records.get(mac)
            if record is None:
                continue
            payload = json.dumps(record.state(snapshot_timestamp), separators=(",", ":"), sort_keys=True)
            snapshot_states[mac] = payload

        self.last_good_snapshot = now - age
        was_source_available = self.source_available
        self.source_available = True
        self.latest_states.update(snapshot_states)

        if self.connected_event.is_set():
            for mac, payload in snapshot_states.items():
                self._publish(state_topic(mac), payload, retain=True)
            if not was_source_available:
                self._set_source_availability(True, force=True)

        LOG.info("Processed a new snapshot for %d configured device(s)", len(snapshot_states))

    def run(self) -> None:
        self.client.connect_async(self.config.host, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.client.loop_start()
        next_path_refresh = 0.0
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_path_refresh:
                    try:
                        self._refresh_usage_path()
                    except ConfigError as exc:
                        LOG.warning("Cannot read the wrtbwmon data path: %s", exc)
                    next_path_refresh = now + PATH_REFRESH_SECONDS

                if self.connected_event.is_set() and self.resync_event.is_set():
                    self.resync_event.clear()
                    try:
                        self._sync_discovery()
                    except (ConnectionError, RuntimeError, TimeoutError) as exc:
                        LOG.warning("MQTT discovery synchronization failed: %s", exc)
                        self.resync_event.set()

                try:
                    self._process_snapshot(now)
                except (ConnectionError, RuntimeError, TimeoutError) as exc:
                    LOG.warning("MQTT state publication failed: %s", exc)
                    self.resync_event.set()

                if (
                    self.last_good_snapshot is not None
                    and now - self.last_good_snapshot > SOURCE_FRESHNESS_SECONDS
                ):
                    try:
                        self._set_source_availability(False)
                    except (ConnectionError, RuntimeError, TimeoutError) as exc:
                        LOG.warning("Cannot mark the data source unavailable: %s", exc)

                self.stop_event.wait(POLL_INTERVAL_SECONDS)
        finally:
            if self.connected_event.is_set():
                try:
                    self._publish(GATEWAY_AVAILABILITY_TOPIC, "offline", retain=True)
                except (ConnectionError, RuntimeError, TimeoutError):
                    pass
            self.client.disconnect()
            self.client.loop_stop()


def cleanup(config: AppConfig, registry: PublishedRegistry) -> int:
    """Remove retained discovery and state messages before package removal."""
    if not config.host:
        LOG.error("MQTT host is not configured; retained messages were not removed")
        return 1

    client = create_mqtt_client(config, with_will=False)
    connected = threading.Event()

    def on_connect(inner_client, userdata, flags, reason_code, properties=None):
        if _connection_succeeded(reason_code):
            connected.set()

    client.on_connect = on_connect
    try:
        client.connect(config.host, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        client.loop_start()
        if not connected.wait(10):
            raise TimeoutError("MQTT cleanup connection timed out")
        macs = registry.load() | set(config.macs)
        for mac in sorted(macs):
            _wait_for_publish(client.publish(discovery_topic(mac), "", qos=0, retain=True))
            _wait_for_publish(client.publish(state_topic(mac), "", qos=0, retain=True))
        _wait_for_publish(client.publish(GATEWAY_AVAILABILITY_TOPIC, "", qos=0, retain=True))
        _wait_for_publish(client.publish(SOURCE_AVAILABILITY_TOPIC, "", qos=0, retain=True))
        registry.save(set())
        LOG.info("Removed retained MQTT messages for %d device(s)", len(macs))
        return 0
    except (OSError, RuntimeError, TimeoutError) as exc:
        LOG.error("MQTT cleanup failed: %s", exc)
        return 1
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _wait_unconfigured() -> int:
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    LOG.warning("MQTT host is not configured; waiting for a configuration change")
    while not stop_event.wait(60):
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true", help="remove retained MQTT messages")
    parser.add_argument("--version", action="version", version=VERSION)
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config = load_app_config()
    except ConfigError as exc:
        LOG.error("Invalid configuration: %s", exc)
        return 2

    registry = PublishedRegistry()
    if arguments.cleanup:
        return cleanup(config, registry)
    if not config.host:
        return _wait_unconfigured()

    try:
        bridge = Bridge(config, registry)
    except RuntimeError as exc:
        LOG.error("Cannot start: %s", exc)
        return 2
    _install_signal_handlers(bridge.stop_event)
    try:
        bridge.run()
    except (OSError, ValueError) as exc:
        LOG.error("Bridge stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
