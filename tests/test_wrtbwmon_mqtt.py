import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest


MODULE_PATH = Path(__file__).parents[1] / "src" / "wrtbwmon_mqtt.py"
SPEC = importlib.util.spec_from_file_location("wrtbwmon_mqtt", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


HEADER = "#mac,ip,iface,speed_in,speed_out,in,out,total,first_date,last_date\n"


class MacTests(unittest.TestCase):
    def test_normalizes_mac(self):
        self.assertEqual(bridge.normalize_mac("AA:BB:CC:DD:EE:FF"), "aa:bb:cc:dd:ee:ff")

    def test_rejects_invalid_mac(self):
        with self.assertRaises(ValueError):
            bridge.normalize_mac("not-a-mac")
        with self.assertRaises(ValueError):
            bridge.normalize_mac(None)


class ConfigTests(unittest.TestCase):
    def test_loads_and_deduplicates_mac_list(self):
        values = {
            "wrtbwmon-mqtt.main.host": "mqtt.example.net",
            "wrtbwmon-mqtt.main.username": "bridge-user",
            "wrtbwmon-mqtt.main.password": "secret",
            "wrtbwmon-mqtt.main.mac": "02:00:00:00:00:01 02:00:00:00:00:01",
        }
        config = bridge.load_app_config(values.get)
        self.assertEqual(config.host, "mqtt.example.net")
        self.assertEqual(config.macs, ("02:00:00:00:00:01",))

    def test_reads_wrtbwmon_path_and_falls_back(self):
        self.assertEqual(
            bridge.load_usage_path(lambda key: "/var/run/traffic.csv"),
            Path("/var/run/traffic.csv"),
        )
        self.assertEqual(bridge.load_usage_path(lambda key: None), Path("/tmp/usage.db"))


class SnapshotTests(unittest.TestCase):
    def test_parses_and_aggregates_duplicate_mac_rows(self):
        text = HEADER + (
            "02:00:00:00:00:01,192.0.2.10,br-lan,10,20,100,200,300,1000,1100\n"
            "02:00:00:00:00:01,2001:db8::10,br-lan,2,3,40,50,90,900,1200\n"
        )
        record = bridge.parse_snapshot(text)["02:00:00:00:00:01"]
        self.assertEqual(record.speed_in, 12)
        self.assertEqual(record.speed_out, 23)
        self.assertEqual(record.bytes_in, 140)
        self.assertEqual(record.bytes_out, 250)
        self.assertEqual(record.first_date, 900)
        self.assertEqual(record.last_date, 1200)
        self.assertEqual(record.ip_addresses, {"192.0.2.10", "2001:db8::10"})

    def test_rejects_partial_or_empty_snapshots(self):
        with self.assertRaises(bridge.SnapshotError):
            bridge.parse_snapshot("")
        with self.assertRaises(bridge.SnapshotError):
            bridge.parse_snapshot(HEADER + "02:00:00:00:00:01,192.0.2.10\n")
        with self.assertRaises(bridge.SnapshotError):
            bridge.parse_snapshot(
                HEADER
                + "02:00:00:00:00:01,192.0.2.10,br-lan,1,2,3,4,invalid,5,6\n"
            )

    def test_converts_bytes_per_second_to_bits_per_second(self):
        text = HEADER + "02:00:00:00:00:01,192.0.2.10,br-lan,10,20,100,200,300,1,2\n"
        state = bridge.parse_snapshot(text)["02:00:00:00:00:01"].state(3)
        self.assertEqual(state["download_bps"], 80)
        self.assertEqual(state["upload_bps"], 160)

    def test_caches_complete_snapshot_before_mqtt_publication(self):
        first_mac = "02:00:00:00:00:01"
        second_mac = "02:00:00:00:00:02"
        text = HEADER + (
            f"{first_mac},192.0.2.10,br-lan,1,2,3,4,7,5,6\n"
            f"{second_mac},192.0.2.11,br-lan,7,8,9,10,19,11,12\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            usage_path = Path(directory) / "usage.db"
            usage_path.write_text(text, encoding="utf-8")

            instance = bridge.Bridge.__new__(bridge.Bridge)
            instance.config = bridge.AppConfig(
                "mqtt.example.net", "", "", (first_mac, second_mac)
            )
            instance.usage_path = usage_path
            instance.last_signature = None
            instance.last_good_snapshot = None
            instance.latest_states = {}
            instance.source_available = False
            instance.source_problem = None
            instance.connected_event = threading.Event()
            instance.connected_event.set()

            def fail_publish(topic, payload, *, retain):
                raise TimeoutError("simulated publication failure")

            instance._publish = fail_publish
            with self.assertRaises(TimeoutError):
                instance._process_snapshot(100.0)

            self.assertEqual(set(instance.latest_states), {first_mac, second_mac})
            self.assertTrue(instance.source_available)


class DiscoveryTests(unittest.TestCase):
    def test_device_discovery_has_four_stable_components(self):
        mac = "02:00:00:00:00:01"
        payload = bridge.discovery_payload(mac)
        self.assertEqual(
            bridge.discovery_topic(mac),
            "homeassistant/device/wrtbwmon_020000000001/config",
        )
        self.assertEqual(payload["device"]["identifiers"], ["wrtbwmon_020000000001"])
        self.assertEqual(len(payload["components"]), 4)
        self.assertEqual(payload["availability_mode"], "all")
        self.assertEqual(payload["origin"]["name"], "wrtbwmon-hass")

    def test_registry_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = bridge.PublishedRegistry(Path(directory) / "published.json")
            registry.save({"02:00:00:00:00:02", "02:00:00:00:00:01"})
            self.assertEqual(
                registry.load(),
                {"02:00:00:00:00:01", "02:00:00:00:00:02"},
            )
            data = json.loads(registry.path.read_text(encoding="utf-8"))
            self.assertEqual(data, sorted(data))

    def test_sync_removes_retained_topics_from_previous_run(self):
        class PublishInfo:
            rc = 0

            def wait_for_publish(self, timeout=None):
                return None

            def is_published(self):
                return True

        class Client:
            def __init__(self):
                self.messages = []

            def publish(self, topic, payload, qos, retain):
                self.messages.append((topic, payload, qos, retain))
                return PublishInfo()

        old_mac = "02:00:00:00:00:01"
        new_mac = "02:00:00:00:00:02"
        with tempfile.TemporaryDirectory() as directory:
            registry = bridge.PublishedRegistry(Path(directory) / "published.json")
            registry.save({old_mac})
            instance = bridge.Bridge.__new__(bridge.Bridge)
            instance.config = bridge.AppConfig("mqtt.example.net", "", "", (new_mac,))
            instance.registry = registry
            instance.client = Client()
            instance.connected_event = threading.Event()
            instance.connected_event.set()
            instance.latest_states = {}
            instance.source_available = False

            instance._sync_discovery()

            messages = instance.client.messages
            self.assertIn((bridge.discovery_topic(old_mac), "", 0, True), messages)
            self.assertIn((bridge.state_topic(old_mac), "", 0, True), messages)
            self.assertTrue(
                any(topic == bridge.discovery_topic(new_mac) and payload for topic, payload, _, _ in messages)
            )
            self.assertEqual(registry.load(), {new_mac})


if __name__ == "__main__":
    unittest.main()
