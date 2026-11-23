import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from android_battery_optimizer.adb import AdbClient
from android_battery_optimizer.state import StateStore


class StateStoreLoadHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _write_state(self, payload: object, serial: str = "serial-1") -> Path:
        serial_dir = self.state_dir / "devices" / serial
        serial_dir.mkdir(parents=True, exist_ok=True)
        state_path = serial_dir / "state.json"
        with state_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return state_path

    def _make_store(self, serial: str = "serial-1") -> StateStore:
        client = AdbClient(runner=MagicMock(), output=lambda _: None)
        client.serial = serial
        return StateStore(self.state_dir, client)

    def _corrupt_files(self, serial: str = "serial-1") -> list[Path]:
        serial_dir = self.state_dir / "devices" / serial
        return sorted(serial_dir.glob("state.json.corrupt.*"))

    def test_state_load_quarantines_list_root(self):
        state_path = self._write_state([])

        store = self._make_store()

        self.assertEqual(store.data, store._empty_state())
        self.assertFalse(state_path.exists())
        self.assertTrue(self._corrupt_files())

    def test_state_load_quarantines_missing_required_sections(self):
        state_path = self._write_state({"version": 2, "device": {}})

        store = self._make_store()

        self.assertEqual(store.data, store._empty_state())
        self.assertFalse(state_path.exists())
        self.assertTrue(self._corrupt_files())

    def test_state_load_quarantines_wrong_section_types(self):
        state_path = self._write_state(
            {
                "version": 2,
                "device": {},
                "settings": [],
                "device_config": {},
                "packages": {},
            }
        )

        store = self._make_store()

        self.assertEqual(store.data, store._empty_state())
        self.assertFalse(state_path.exists())
        self.assertTrue(self._corrupt_files())

    def test_state_load_accepts_valid_v2_state(self):
        payload = {
            "version": 2,
            "device": {
                "serial": "serial-1",
                "model": "Pixel",
            },
            "settings": {
                "global/window_animation_scale": {
                    "namespace": "global",
                    "key": "window_animation_scale",
                    "value": "0.5",
                }
            },
            "device_config": {
                "activity_manager/max_cached_processes": {
                    "namespace": "activity_manager",
                    "key": "max_cached_processes",
                    "value": None,
                }
            },
            "packages": {
                "com.example.app": {
                    "appops": {"RUN_ANY_IN_BACKGROUND": "allow"},
                    "standby_bucket": "active",
                    "enabled": False,
                }
            },
        }
        state_path = self._write_state(payload)

        store = self._make_store()

        expected = dict(payload)
        expected.update({
            "netpolicy": {},
            "netpolicy_whitelist": {},
            "deviceidle_whitelist": {},
            "hibernation": {},
        })
        self.assertEqual(store.data, expected)
        self.assertTrue(state_path.exists())
        self.assertFalse(self._corrupt_files())

    def test_state_load_repairs_or_quarantines_old_empty_state_explicitly(self):
        state_path = self._write_state({})

        store = self._make_store()

        self.assertEqual(store.data, store._empty_state())
        self.assertFalse(state_path.exists())
        self.assertTrue(self._corrupt_files())


if __name__ == "__main__":
    unittest.main()


class DeviceDirTests(unittest.TestCase):
    def test_usb_and_wireless_serials_share_one_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            usb = StateStore.device_dir(base, "RFCT8025BKP")
            for wireless in (
                "adb-RFCT8025BKP-6rXWfv._adb-tls-connect._tcp",
                "adb-RFCT8025BKP-6rXWfv (2)._adb-tls-connect._tcp",
            ):
                self.assertEqual(StateStore.device_dir(base, wireless), usb)
            self.assertEqual(
                StateStore.device_dir(base, "192.168.1.5:5555").name, "192.168.1.5_5555"
            )

    def test_legacy_transport_folder_still_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            serial = "adb-ABC123-xyz (2)._adb-tls-connect._tcp"
            legacy = base / "devices" / StateStore.sanitize_serial(serial)
            legacy.mkdir(parents=True)
            self.assertEqual(StateStore.device_dir(base, serial), legacy)
