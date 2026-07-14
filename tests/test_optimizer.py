import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from android_battery_optimizer.adb import AdbClient, CommandError, CommandResult, SubprocessRunner
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI
from android_battery_optimizer.recorder import StateRecorder, SnapshotError, VerificationError
from android_battery_optimizer.state import StateStore
from android_battery_optimizer.android import parse_adb_devices, resolve_package_choice


class OptimizerTests(unittest.TestCase):
    def make_app_and_cli(self, state_dir, user_inputs=None, verify=False, capture_output=False):
        outputs = []
        input_values = list(user_inputs or [])

        def fake_input(prompt):
            if not input_values:
                raise AssertionError(f"Unexpected prompt: {prompt}")
            return input_values.pop(0)

        def emit(message):
            if capture_output:
                outputs.append(message)

        runner = SubprocessRunner()
        client = AdbClient(runner=runner, output=emit)
        app = BatteryOptimizerApp(client=client, state_dir=state_dir)
        app.recorder.verify = verify
        cli = BatteryOptimizerCLI(
            app=app,
            output=emit,
            input_fn=fake_input,
        )
        return app, cli, outputs

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_state_is_scoped_by_serial(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Serial 1
            app1, _, _ = self.make_app_and_cli(tmp_path)
            app1.client.serial = "serial-1"
            app1.rebind_device()
            with app1.recorder.transaction():
                app1.recorder.put_setting("global", "test", "1")

            state_file_1 = tmp_path / "devices" / "serial-1" / "state.json"
            self.assertTrue(state_file_1.exists())

            # Serial 2
            app2, _, _ = self.make_app_and_cli(tmp_path)
            app2.client.serial = "serial-2"
            app2.rebind_device()
            self.assertFalse(app2.store.has_entries())

            with app2.recorder.transaction():
                app2.recorder.put_setting("global", "test", "2")

            state_file_2 = tmp_path / "devices" / "serial-2" / "state.json"
            self.assertTrue(state_file_2.exists())

            # Sanitize test
            app3, _, _ = self.make_app_and_cli(tmp_path)
            app3.client.serial = "serial:3/path"
            app3.rebind_device()
            state_file_3 = tmp_path / "devices" / "serial_3_path" / "state.json"
            with app3.recorder.transaction():
                app3.recorder.put_setting("global", "test", "3")
            self.assertTrue(state_file_3.exists())

    def test_whitelist_is_scoped_by_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            app1, _, _ = self.make_app_and_cli(tmp_path)
            app1.client.serial = "serial-1"
            app1.rebind_device()
            app1.save_whitelist(["com.example.alpha"])

            app2, _, _ = self.make_app_and_cli(tmp_path)
            app2.client.serial = "serial-2"
            app2.rebind_device()

            whitelist_1 = tmp_path / "devices" / "serial-1" / "whitelist.txt"
            whitelist_2 = tmp_path / "devices" / "serial-2" / "whitelist.txt"
            legacy_whitelist = tmp_path / "whitelist.txt"

            self.assertTrue(whitelist_1.exists())
            self.assertFalse(legacy_whitelist.exists())
            self.assertFalse(whitelist_2.exists())
            self.assertEqual(app1.load_whitelist(), ["com.example.alpha"])
            self.assertEqual(app2.load_whitelist(), [])

    def test_rebind_device_changes_whitelist_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app, _, _ = self.make_app_and_cli(tmp_path)

            app.client.serial = "serial:one"
            first_path = app.whitelist_path
            app.client.serial = "serial/two"
            app.rebind_device()
            second_path = app.whitelist_path

            self.assertEqual(first_path, tmp_path / "devices" / "serial_one" / "whitelist.txt")
            self.assertEqual(second_path, tmp_path / "devices" / "serial_two" / "whitelist.txt")
            self.assertNotEqual(first_path, second_path)

    def test_old_global_whitelist_is_migrated_for_selected_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            legacy_whitelist = tmp_path / "whitelist.txt"
            legacy_whitelist.write_text("com.example.legacy\n", encoding="utf-8")

            outputs = []
            client = AdbClient(runner=SubprocessRunner(), serial="serial-1", output=outputs.append)
            app = BatteryOptimizerApp(client=client, state_dir=tmp_path)

            self.assertEqual(app.load_whitelist(), ["com.example.legacy"])

            migrated_whitelist = tmp_path / "devices" / "serial-1" / "whitelist.txt"
            self.assertTrue(migrated_whitelist.exists())
            self.assertEqual(
                migrated_whitelist.read_text(encoding="utf-8"),
                legacy_whitelist.read_text(encoding="utf-8"),
            )
            self.assertTrue(any("Migrated legacy whitelist.txt" in message for message in outputs))

            app.load_whitelist()
            migration_messages = [message for message in outputs if "Migrated legacy whitelist.txt" in message]
            self.assertEqual(len(migration_messages), 1)

    def test_whitelist_add_for_device_a_does_not_affect_device_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            app_a, _, _ = self.make_app_and_cli(tmp_path)
            app_a.client.serial = "device-a"
            app_a.rebind_device()
            app_a.save_whitelist(["com.example.alpha"])

            app_b, _, _ = self.make_app_and_cli(tmp_path)
            app_b.client.serial = "device-b"
            app_b.rebind_device()
            app_b.save_whitelist(["com.example.beta"])

            self.assertEqual(app_a.load_whitelist(), ["com.example.alpha"])
            self.assertEqual(app_b.load_whitelist(), ["com.example.beta"])
            self.assertEqual((tmp_path / "devices" / "device-a" / "whitelist.txt").read_text(encoding="utf-8"), "com.example.alpha\n")
            self.assertEqual((tmp_path / "devices" / "device-b" / "whitelist.txt").read_text(encoding="utf-8"), "com.example.beta\n")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_restrict_apps_uses_selected_device_whitelist(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "cmd appops help" in cmd:
                return MagicMock(returncode=0, stdout="help", stderr="")
            if "getprop ro.build.version.sdk" in cmd:
                return MagicMock(returncode=0, stdout="30\n", stderr="")
            if "am get-standby-bucket" in cmd:
                return MagicMock(returncode=0, stdout="10\n", stderr="")
            if "pm list packages -3" in cmd:
                return MagicMock(returncode=0, stdout="package:com.example.chat\npackage:com.example.music\n", stderr="")
            if "pm list packages" in cmd and "-3" not in cmd:
                return MagicMock(returncode=0, stdout="package:com.example.chat\npackage:com.example.music\n", stderr="")
            if "dumpsys appops" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "Package com.example.chat:\n"
                        "  RUN_ANY_IN_BACKGROUND: allow\n"
                        "Package com.example.music:\n"
                        "  RUN_ANY_IN_BACKGROUND: allow\n"
                    ),
                    stderr="",
                )
            if "dumpsys usagestats" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="package=com.example.chat bucket=active\npackage=com.example.music bucket=active\n",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            app_a, _, _ = self.make_app_and_cli(tmp_path)
            app_a.client.serial = "device-a"
            app_a.rebind_device()
            app_a.save_whitelist(["com.example.chat"])

            app_b, _, _ = self.make_app_and_cli(tmp_path)
            app_b.client.serial = "device-b"
            app_b.rebind_device()

            app_b.restrict_background_apps(level="ignore")

            self.assertEqual(app_b.load_whitelist(), [])
            self.assertEqual(app_a.load_whitelist(), ["com.example.chat"])

            batched_scripts = [call.kwargs.get("input") for call in mock_run.call_args_list if call.kwargs.get("input")]
            self.assertTrue(any("com.example.chat" in script for script in batched_scripts))
            self.assertTrue(any("com.example.music" in script for script in batched_scripts))
            self.assertFalse(
                any(
                    "com.example.chat" in script and "RUN_ANY_IN_BACKGROUND" not in script
                    for script in batched_scripts
                )
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_dry_run_does_not_create_state_file(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.client.dry_run = True
            app.rebind_device()

            with app.recorder.transaction():
                app.recorder.put_setting("global", "test", "1")

            self.assertFalse((Path(tmp) / "devices").exists())

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_restore_refuses_device_mismatch(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "getprop" in cmd:
                if "serial-1" in self.current_serial:
                    return MagicMock(returncode=0, stdout="val1\n", stderr="")
                return MagicMock(returncode=0, stdout="val2\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))

            self.current_serial = "serial-1"
            app.client.serial = "serial-1"
            app.rebind_device()

            with app.recorder.transaction():
                app.recorder.put_setting("global", "test", "1")

            # Switch serial
            self.current_serial = "serial-2"
            app.client.serial = "serial-2"
            # Manually point store back to serial-1 to simulate loading it
            app.store.path = Path(tmp) / "devices" / "serial-1" / "state.json"
            app.store.data = app.store._load()

            with self.assertRaises(ValueError) as cm:
                app.revert_saved_state()
            self.assertIn("Device serial mismatch", str(cm.exception))

            # Ensure no restore ADB command was run (except getprop)
            for call in mock_run.call_args_list:
                args = call[0][0]
                if "settings" in args and "put" in args:
                    self.fail("ADB restore command run on mismatched device")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_corrupt_state_file_is_quarantined(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            serial_dir = tmp_path / "devices" / "serial-1"
            serial_dir.mkdir(parents=True)
            state_file = serial_dir / "state.json"
            with state_file.open("w") as f:
                f.write("{invalid json")

            app, _, _ = self.make_app_and_cli(tmp_path)
            app.client.serial = "serial-1"
            app.rebind_device()

            self.assertEqual(app.store.data["settings"], {})
            self.assertTrue(any(f.name.startswith("state.json.corrupt.") for f in serial_dir.iterdir()))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_state_save_is_atomic(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.rebind_device()

            app.store.save()
            state_file = Path(tmp) / "devices" / "serial-1" / "state.json"
            self.assertTrue(state_file.exists())
            self.assertFalse(state_file.with_suffix(".tmp").exists())

            with state_file.open("r") as f:
                data = json.load(f)
                self.assertEqual(data["version"], 2)

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_state_save_falls_back_to_minimal_metadata_when_getprop_times_out(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "getprop" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout") or 0)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app, _, _ = self.make_app_and_cli(tmp_path)
            app.client.serial = "serial-1"
            app.rebind_device()
            app.store.data["settings"] = {
                "global/test": {"namespace": "global", "key": "test", "value": "1"}
            }

            app.store.save()

            state_file = tmp_path / "devices" / "serial-1" / "state.json"
            with state_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)

            self.assertEqual(
                data["device"],
                {
                    "serial": "serial-1",
                    "brand": "",
                    "model": "",
                    "android_release": "",
                    "sdk": "",
                    "fingerprint": "",
                },
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_state_save_falls_back_to_minimal_metadata_when_getprop_command_fails(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "getprop" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="property lookup failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app, _, _ = self.make_app_and_cli(tmp_path)
            app.client.serial = "serial-1"
            app.rebind_device()
            app.store.data["settings"] = {
                "global/test": {"namespace": "global", "key": "test", "value": "1"}
            }

            app.store.save()

            state_file = tmp_path / "devices" / "serial-1" / "state.json"
            with state_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)

            self.assertEqual(
                data["device"],
                {
                    "serial": "serial-1",
                    "brand": "",
                    "model": "",
                    "android_release": "",
                    "sdk": "",
                    "fingerprint": "",
                },
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_state_save_still_raises_filesystem_write_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app, _, _ = self.make_app_and_cli(tmp_path)
            app.client.serial = "serial-1"
            app.rebind_device()
            app.store.data["settings"] = {
                "global/test": {"namespace": "global", "key": "test", "value": "1"}
            }

            with patch("android_battery_optimizer.state.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    app.store.save()

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_restore_serial_mismatch_still_works_with_minimal_metadata(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "getprop" in cmd:
                if "-s serial-1" in cmd:
                    return MagicMock(returncode=1, stdout="", stderr="property lookup failed")
                return MagicMock(returncode=0, stdout="value\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            app1, _, _ = self.make_app_and_cli(tmp_path)
            app1.client.serial = "serial-1"
            app1.rebind_device()
            app1.store.data["settings"] = {
                "global/test": {"namespace": "global", "key": "test", "value": "1"}
            }
            app1.store.save()

            state_file = tmp_path / "devices" / "serial-1" / "state.json"

            app2, _, _ = self.make_app_and_cli(tmp_path)
            app2.client.serial = "serial-2"
            app2.rebind_device()
            app2.store.path = state_file
            app2.store.data = app2.store._load()

            with self.assertRaises(ValueError) as cm:
                app2.revert_saved_state()

            self.assertIn("Device serial mismatch", str(cm.exception))

    def test_parse_adb_devices(self):
        devices = parse_adb_devices(
            "List of devices attached\n"
            "serial-1\tdevice\n"
            "serial-2\tunauthorized\n"
            "adb-RFCT8025BKP-6rXWfv (2)._adb-tls-connect._tcp\tdevice\n"
            "* daemon not running; starting now at tcp:5037\n"
            "emulator-5554 device\n"
        )
        self.assertEqual(
            devices,
            [
                {"serial": "serial-1", "status": "device"},
                {"serial": "serial-2", "status": "unauthorized"},
                {"serial": "adb-RFCT8025BKP-6rXWfv (2)._adb-tls-connect._tcp", "status": "device"},
                {"serial": "emulator-5554", "status": "device"},
            ],
        )

    def test_resolve_package_choice_partial_match(self):
        packages = ["com.example.chat", "com.example.music", "org.sample"]
        self.assertEqual(
            resolve_package_choice("music", packages),
            ["com.example.music"],
        )

    @patch("android_battery_optimizer.adb.shutil.which")
    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_missing_adb_is_reported(self, mock_run, mock_which):
        mock_which.return_value = None
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, outputs = self.make_app_and_cli(Path(tmp), capture_output=True)
            self.assertFalse(cli.check_environment())
            self.assertIn("ADB was not found in PATH.", outputs[0])

    @patch("android_battery_optimizer.adb.shutil.which")
    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_no_devices_is_reported(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        mock_run.return_value = MagicMock(returncode=0, stdout="List of devices attached\n\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, outputs = self.make_app_and_cli(Path(tmp), capture_output=True)
            self.assertFalse(cli.check_environment())
            self.assertIn("No ADB devices detected.", outputs[0])

    @patch("android_battery_optimizer.adb.shutil.which")
    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_multiple_devices_requires_selection(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\nserial-1\tdevice\nserial-2\tdevice\n",
            stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, _ = self.make_app_and_cli(Path(tmp), user_inputs=["2"])
            self.assertTrue(cli.check_environment())
            self.assertEqual(cli.client.serial, "serial-2")

    @patch("android_battery_optimizer.adb.shutil.which")
    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_unauthorized_device_is_blocked(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\nserial-1\tunauthorized\n",
            stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, outputs = self.make_app_and_cli(Path(tmp), capture_output=True)
            self.assertFalse(cli.check_environment())
            self.assertIn("No authorized online device is available.", outputs[-1])

    @patch("android_battery_optimizer.adb.shutil.which")
    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_experimental_confirmation_blocks_mutation(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "devices" in cmd:
                return MagicMock(returncode=0, stdout="List of devices attached\nserial-1\tdevice\n", stderr="")
            if "getprop ro.product.brand" in cmd:
                return MagicMock(returncode=0, stdout="google\n", stderr="")
            if "getprop ro.product.model" in cmd:
                return MagicMock(returncode=0, stdout="Pixel\n", stderr="")
            if "getprop ro.build.version.release" in cmd:
                return MagicMock(returncode=0, stdout="14\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, cli, outputs = self.make_app_and_cli(
                Path(tmp), user_inputs=["3", "n", "11"], capture_output=True
            )
            cli.client.serial = "serial-1"

            with patch.object(app, 'apply_experimental_optimizations') as mock_apply:
                cli.run()
                mock_apply.assert_not_called()

            self.assertIn("Skipped experimental optimizations.", outputs)

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_snapshot_restore_for_unset_setting_uses_delete(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with app.recorder.transaction():
                app.recorder.put_setting("global", "wifi_scan_throttle_enabled", "1")

            messages = app.revert_saved_state()
            self.assertTrue(any("Restored setting global/wifi_scan_throttle_enabled" in m for m in messages))

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "delete", "global", "wifi_scan_throttle_enabled"],
                capture_output=True,
                text=True,
                input=None,
                timeout=30
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_package_state_restore_covers_appops_bucket_and_enabled_state(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "dumpsys appops" in cmd:
                return MagicMock(returncode=0, stdout="  Package com.example.app:\n    RUN_ANY_IN_BACKGROUND: allow\n", stderr="")
            if "dumpsys usagestats" in cmd:
                return MagicMock(returncode=0, stdout="package=com.example.app u=0 bucket=active reason=...\n", stderr="")
            if "list packages --user 0 -d" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "list packages --user 0 -e" in cmd:
                return MagicMock(returncode=0, stdout="package:com.example.app\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with app.recorder.transaction():
                app.recorder.prefetch_package_states()
                app.recorder.set_appop("com.example.app", "RUN_ANY_IN_BACKGROUND", "ignore")
                app.recorder.set_standby_bucket("com.example.app", "rare")
                app.recorder.set_package_enabled("com.example.app", enabled=False)

            app.revert_saved_state()

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "cmd", "appops", "set", "com.example.app", "RUN_ANY_IN_BACKGROUND", "allow"],
                capture_output=True, text=True, input=None, timeout=30
            )
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "am", "set-standby-bucket", "com.example.app", "active"],
                capture_output=True, text=True, input=None, timeout=30
            )
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "pm", "enable", "--user", "0", "com.example.app"],
                capture_output=True, text=True, input=None, timeout=30
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_prefetch_package_states_uses_user_0_for_enabled_and_disabled_lists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            app.recorder.prefetch_package_states()

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "pm", "list", "packages", "--user", "0", "-d"],
                capture_output=True,
                text=True,
                input=None,
                timeout=30,
            )
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "pm", "list", "packages", "--user", "0", "-e"],
                capture_output=True,
                text=True,
                input=None,
                timeout=30,
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_set_package_enabled_still_mutates_user_0(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.recorder._prefetch_package_enabled_success = True
            app.recorder._package_enabled_cache["com.example.app"] = True

            app.recorder.set_package_enabled("com.example.app", enabled=False, verify=False)

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "pm", "disable-user", "--user", "0", "com.example.app"],
                capture_output=True,
                text=True,
                input=None,
                timeout=30,
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_restore_package_enabled_still_mutates_user_0(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "pm list packages --user 0 -d com.example.app" in cmd:
                return MagicMock(returncode=0, stdout="package:com.example.app\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.store.data["packages"] = {
                "com.example.app": {
                    "enabled": False,
                    "appops": {},
                    "standby_bucket": None,
                }
            }

            app.recorder.restore()

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "pm", "disable-user", "--user", "0", "com.example.app"],
                capture_output=True,
                text=True,
                input=None,
                timeout=30,
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_appop_default_rollback_uses_set_default_not_reset(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            app.recorder._perform_rollback(
                {
                    "type": "appop",
                    "package": "com.example.app",
                    "op": "RUN_ANY_IN_BACKGROUND",
                    "prior_value": "default",
                }
            )

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "cmd", "appops", "set", "com.example.app", "RUN_ANY_IN_BACKGROUND", "default"],
                capture_output=True, text=True, input=None, timeout=30
            )
            self.assertFalse(
                any("cmd appops reset" in " ".join(call.args[0]) for call in mock_run.call_args_list)
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_restore_default_appop_uses_set_default_not_reset(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.store.data["packages"] = {
                "com.example.app": {
                    "enabled": None,
                    "appops": {"RUN_ANY_IN_BACKGROUND": "default"},
                    "standby_bucket": None,
                }
            }
            mock_run.side_effect = lambda args, **kwargs: MagicMock(
                returncode=0,
                stdout="No operations.",
                stderr="",
            ) if args[:5] == ["adb", "-s", "serial-1", "shell", "cmd"] and args[5:8] == ["appops", "get", "com.example.app"] else MagicMock(returncode=0, stdout="", stderr="")

            messages = app.recorder.restore()

            self.assertTrue(any("Restored com.example.app appop RUN_ANY_IN_BACKGROUND" in m for m in messages))
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "cmd", "appops", "set", "com.example.app", "RUN_ANY_IN_BACKGROUND", "default"],
                capture_output=True, text=True, input=None, timeout=30
            )
            self.assertFalse(
                any("cmd appops reset" in " ".join(call.args[0]) for call in mock_run.call_args_list)
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_verify_appop_never_calls_cmd_appops_reset_with_op_argument(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            app.recorder._perform_rollback(
                {
                    "type": "appop",
                    "package": "com.example.app",
                    "op": "RUN_ANY_IN_BACKGROUND",
                    "prior_value": "default",
                }
            )
            app.store.data["packages"] = {
                "com.example.app": {
                    "enabled": None,
                    "appops": {"RUN_ANY_IN_BACKGROUND": "default"},
                    "standby_bucket": None,
                }
            }
            mock_run.side_effect = lambda args, **kwargs: MagicMock(
                returncode=0,
                stdout="No operations.",
                stderr="",
            ) if args[:5] == ["adb", "-s", "serial-1", "shell", "cmd"] and args[5:8] == ["appops", "get", "com.example.app"] else MagicMock(returncode=0, stdout="", stderr="")
            app.recorder.restore()

            for call in mock_run.call_args_list:
                self.assertNotIn("cmd appops reset", " ".join(call.args[0]))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_restore_reports_failures(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="window_animation_scale=1.0\n", stderr="")
            if "settings put global window_animation_scale 1.0" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="permission denied")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, outputs = self.make_app_and_cli(Path(tmp), capture_output=True)
            app.client.serial = "serial-1"
            app.rebind_device()

            with app.recorder.transaction():
                app.recorder.put_setting("global", "window_animation_scale", "0.5")

            messages = app.revert_saved_state()
            self.assertTrue(any("Failed to restore setting global/window_animation_scale" in m for m in messages))
            self.assertTrue((Path(tmp) / "devices" / "serial-1" / "state.json").exists())
            self.assertTrue(any("Partial state corruption" in out for out in outputs))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_validate_package_blocks_unknown_package(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="package:com.example.safe\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            with self.assertRaises(ValueError):
                app.validate_package("com.bad.actor;rm -rf /")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_get_packages_failure_raises_command_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="permission denied")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with self.assertRaises(CommandError) as cm:
                app.get_packages()

            self.assertIn("Failed to list packages", str(cm.exception))
            self.assertIn("permission denied", str(cm.exception))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_get_packages_success_parses_packages(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="package:com.example.b\npackage:com.example.a\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            self.assertEqual(app.get_packages(), ["com.example.a", "com.example.b"])

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_validate_package_propagates_package_list_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="device offline")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with self.assertRaises(CommandError):
                app.validate_package("com.example.app")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_partial_rollback_on_batch_failure(self, mock_run):
        def side_effect(args, **kwargs):
            input_data = kwargs.get('input')
            if input_data and "SUCCESS_0" in input_data:
                # First command succeeds, second fails
                return MagicMock(returncode=1, stdout="SUCCESS_0\n", stderr="simulated failure")
            if "settings list global" in " ".join(args):
                return MagicMock(returncode=0, stdout="some_setting=old_value\nother_setting=old_value\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            try:
                with app.recorder.transaction():
                    app.recorder.put_setting("global", "some_setting", "new_value")
                    app.recorder.put_setting("global", "other_setting", "new_value")
            except CommandError:
                pass

            # Both some_setting (index 0) and other_setting (index 1) should be reverted
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "put", "global", "some_setting", "old_value"],
                capture_output=True, text=True, input=None, timeout=30
            )
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "put", "global", "other_setting", "old_value"],
                capture_output=True, text=True, input=None, timeout=30
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_no_rollback_if_not_dispatched(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.rebind_device()

            with self.assertRaises(RuntimeError) as cm:
                with app.recorder.transaction():
                    app.recorder.put_setting("global", "some_setting", "value")
                    raise RuntimeError("Pre-dispatch error")

            self.assertIs(type(cm.exception), RuntimeError)
            self.assertEqual(str(cm.exception), "Pre-dispatch error")

            # Revert should NOT be called because batch_dispatched was False
            for call in mock_run.call_args_list:
                args = call[0][0]
                cmd_str = " ".join(args)
                self.assertFalse("settings put" in cmd_str and "old_value" in cmd_str)

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_pre_dispatch_error_does_not_persist_state(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.rebind_device()

            with self.assertRaises(RuntimeError) as cm:
                with app.recorder.transaction():
                    app.recorder.put_setting("global", "test_setting", "new_val")
                    raise RuntimeError("Fail before dispatch")

            self.assertIs(type(cm.exception), RuntimeError)
            self.assertEqual(str(cm.exception), "Fail before dispatch")

            state_file = Path(tmp) / "devices" / "serial-1" / "state.json"
            if state_file.exists():
                with state_file.open() as f:
                    data = json.load(f)
                    self.assertEqual(data["settings"], {})

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_partial_batch_failure_rolls_back_successes_and_does_not_keep_reverted_entries(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="setting0=old0\nsetting1=old1\n", stderr="")
            input_data = kwargs.get('input')
            if input_data and "SUCCESS_0" in input_data:
                # Command 0 success, Command 1 fail
                return MagicMock(returncode=1, stdout="SUCCESS_0\n", stderr="fail")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.rebind_device()

            try:
                with app.recorder.transaction():
                    app.recorder.put_setting("global", "setting0", "new0")
                    app.recorder.put_setting("global", "setting1", "new1")
            except CommandError:
                pass

            # setting0 should be rolled back to old0
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "put", "global", "setting0", "old0"],
                capture_output=True, text=True, input=None, timeout=30
            )

            # state file should be clean (setting0 was reverted, setting1 never ran successfully)
            state_file = Path(tmp) / "devices" / "serial-1" / "state.json"
            if state_file.exists():
                with state_file.open() as f:
                    data = json.load(f)
                    self.assertEqual(data["settings"], {})

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_partial_batch_failure_keeps_unresolved_state_if_rollback_fails(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="setting0=old0\n", stderr="")
            input_data = kwargs.get('input')
            if input_data and "SUCCESS_0" in input_data:
                return MagicMock(returncode=1, stdout="SUCCESS_0\n", stderr="fail")
            if "settings put global setting0 old0" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="rollback failed")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, outputs = self.make_app_and_cli(Path(tmp), capture_output=True)
            app.client.serial = "serial-1"
            app.rebind_device()

            try:
                with app.recorder.transaction():
                    app.recorder.put_setting("global", "setting0", "new0")
                    app.recorder.put_setting("global", "setting1", "new1")
            except CommandError:
                pass

            # Rollback was attempted
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "put", "global", "setting0", "old0"],
                capture_output=True, text=True, input=None, timeout=30
            )

            # Since rollback failed, state.json should contain setting0 but NOT setting1
            state_file = Path(tmp) / "devices" / "serial-1" / "state.json"
            self.assertTrue(state_file.exists())
            with state_file.open() as f:
                data = json.load(f)
                self.assertIn("global/setting0", data["settings"])
                self.assertEqual(data["settings"]["global/setting0"]["value"], "old0")
                self.assertNotIn("global/setting1", data["settings"])

            self.assertTrue(any("Partial state corruption" in out for out in outputs))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_successful_transaction_persists_state(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="setting0=old0\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            app.rebind_device()

            with app.recorder.transaction():
                app.recorder.put_setting("global", "setting0", "new0")

            state_file = Path(tmp) / "devices" / "serial-1" / "state.json"
            self.assertTrue(state_file.exists())
            with state_file.open() as f:
                data = json.load(f)
                self.assertIn("global/setting0", data["settings"])
                self.assertEqual(data["settings"]["global/setting0"]["value"], "old0")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_subprocess_runner_timeout_raises_command_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["sleep", "10"], timeout=1.0, output=b"partial stdout", stderr=b"partial stderr"
        )
        runner = SubprocessRunner()
        with self.assertRaises(CommandError) as cm:
            runner.run(["sleep", "10"], timeout=1.0)

        self.assertIn("Command timed out after 1.0s: sleep 10", str(cm.exception))
        result = cm.exception.result
        if result is None:
            self.fail("Expected timeout command error to include a CommandResult")
        self.assertEqual(result.stdout, "partial stdout")
        self.assertEqual(result.stderr, "partial stderr")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_adb_shell_uses_default_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runner = SubprocessRunner()
        client = AdbClient(runner=runner, serial="test-device", output=lambda _: None)
        client.shell(["settings", "list", "global"], mutate=True)

        mock_run.assert_called_with(
            ["adb", "-s", "test-device", "shell", "settings", "list", "global"],
            capture_output=True,
            text=True,
            input=None,
            timeout=30
        )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_bg_dexopt_uses_long_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "test-device"
            app.run_bg_dexopt()

            mock_run.assert_called_with(
                ["adb", "-s", "test-device", "shell", "cmd", "package", "bg-dexopt-job"],
                capture_output=True,
                text=True,
                input=None,
                timeout=300
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_whitelisted_apps_are_not_mutated(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "pm list packages -3" in cmd:
                return MagicMock(returncode=0, stdout="package:com.example.chat\npackage:com.example.music\n", stderr="")
            if "pm list packages" in cmd and "-3" not in cmd:
                return MagicMock(returncode=0, stdout="package:com.example.chat\npackage:com.example.music\n", stderr="")
            if "dumpsys appops" in cmd:
                return MagicMock(returncode=0, stdout="Package com.example.music:\n  RUN_ANY_IN_BACKGROUND: allow\n", stderr="")
            if "dumpsys usagestats" in cmd:
                return MagicMock(returncode=0, stdout="package=com.example.music bucket=active\n", stderr="")
            if "ro.build.version.sdk" in cmd:
                return MagicMock(returncode=0, stdout="30\n", stderr="")
            if "cmd appops help" in cmd:
                return MagicMock(returncode=0, stdout="help", stderr="")
            if "am set-standby-bucket" in cmd or "am get-standby-bucket" in cmd:
                return MagicMock(returncode=0, stdout="10\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            # Setup whitelist
            app.save_whitelist(["com.example.chat"])

            # Run restriction
            skipped = app.restrict_background_apps(level="ignore")

            # Assert com.example.chat appears in returned skipped list
            self.assertIn("com.example.chat", skipped["skipped_whitelisted"])

            # Check all calls for mutations of com.example.chat
            for call in mock_run.call_args_list:
                # Check command line args
                args = call[0][0]
                cmd_str = " ".join(args)
                if "com.example.chat" in cmd_str:
                    if "appops" in cmd_str or "set-standby-bucket" in cmd_str:
                        self.fail(f"Whitelisted app com.example.chat was mutated in command line: {cmd_str}")

                # Check input_data (since it's batched)
                input_data = call[1].get('input', '')
                if input_data and "com.example.chat" in input_data:
                    if "appops" in input_data or "set-standby-bucket" in input_data:
                        self.fail(f"Whitelisted app com.example.chat was mutated in batched input: {input_data}")

            # Assert com.example.music is mutated
            mutation_found = False
            for call in mock_run.call_args_list:
                input_data = call[1].get('input', '')
                if input_data and "com.example.music" in input_data:
                    if "appops" in input_data and "RUN_ANY_IN_BACKGROUND" in input_data:
                        mutation_found = True
                        break
            self.assertTrue(mutation_found, "com.example.music was not mutated")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_unknown_package_enabled_state_blocks_mutation(self, mock_run):
        # Simulate package NOT present in enabled/disabled lists
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with self.assertRaises(SnapshotError) as cm:
                with app.recorder.transaction():
                    app.recorder.prefetch_package_states()
                    app.recorder.set_package_enabled("com.unknown.pkg", enabled=False)

            self.assertIn("Could not determine enabled state for package: com.unknown.pkg", str(cm.exception))

            # Verify no mutation (pm disable) was sent to ADB
            for call in mock_run.call_args_list:
                args = call[0][0]
                self.assertFalse("pm" in args and "disable" in args)

            # Verify no state persisted
            self.assertFalse(app.store.has_entries())

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_appop_snapshot_failure_blocks_mutation(self, mock_run):
        # Simulate appops command failure
        def side_effect(args, **kwargs):
            if "dumpsys appops" in " ".join(args):
                return MagicMock(returncode=1, stdout="", stderr="error")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with self.assertRaises(SnapshotError) as cm:
                with app.recorder.transaction():
                    app.recorder.prefetch_package_states()
                    app.recorder.set_appop("com.example.app", "RUN_ANY_IN_BACKGROUND", "ignore")

            self.assertIn("Failed to parse appop output", str(cm.exception))

            # Verify no mutation
            for call in mock_run.call_args_list:
                args = call[0][0]
                self.assertFalse("appops" in args and "set" in args)

            # Verify no state persisted
            self.assertFalse(app.store.has_entries())

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_standby_bucket_snapshot_failure_blocks_mutation(self, mock_run):
        # Simulate usagestats output missing the package
        mock_run.return_value = MagicMock(returncode=0, stdout="some other output\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with self.assertRaises(SnapshotError) as cm:
                with app.recorder.transaction():
                    app.recorder.prefetch_package_states()
                    app.recorder.set_standby_bucket("com.missing.pkg", "rare")

            self.assertIn("Failed to read standby bucket for package com.missing.pkg", str(cm.exception))

            # Verify no mutation
            for call in mock_run.call_args_list:
                args = call[0][0]
                self.assertFalse("set-standby-bucket" in args)

            # Verify no state persisted
            self.assertFalse(app.store.has_entries())

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_put_setting_verifies_readback_success(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="some_key=old_val\n", stderr="")
            if "settings get global some_key" in cmd:
                return MagicMock(returncode=0, stdout="new_val\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp), verify=True)
            app.client.serial = "serial-1"

            # Should not raise any error
            app.recorder.put_setting("global", "some_key", "new_val")

            # Verify readback was called
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "get", "global", "some_key"],
                capture_output=True, text=True, input=None, timeout=30
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_put_setting_verification_failure_rolls_back(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="some_key=old_val\n", stderr="")
            if "settings get global some_key" in cmd:
                return MagicMock(returncode=0, stdout="old_val\n", stderr="") # Still old
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp), verify=True)
            app.client.serial = "serial-1"

            with self.assertRaises(VerificationError):
                app.recorder.put_setting("global", "some_key", "new_val")

            # Verify rollback was attempted
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "put", "global", "some_key", "old_val"],
                capture_output=True, text=True, input=None, timeout=30
            )

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_device_config_verification_failure_reports_error(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "device_config list namespace" in cmd:
                return MagicMock(returncode=0, stdout="key=old\n", stderr="")
            if "device_config get namespace key" in cmd:
                return MagicMock(returncode=0, stdout="old\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp), verify=True)
            app.client.serial = "serial-1"

            with self.assertRaises(VerificationError):
                app.recorder.put_device_config("namespace", "key", "new")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_delete_setting_verifies_absence(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="some_key=val\n", stderr="")
            if "settings get global some_key" in cmd:
                return MagicMock(returncode=0, stdout="null\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp), verify=True)
            app.client.serial = "serial-1"

            # Should not raise error because "null" is normalized to None
            app.recorder.delete_setting("global", "some_key")

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_batched_transaction_verifies_all_entries_after_success(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="s1=old1\ns2=old2\n", stderr="")
            if "settings get global s1" in cmd:
                return MagicMock(returncode=0, stdout="v1\n", stderr="")
            if "settings get global s2" in cmd:
                return MagicMock(returncode=0, stdout="v2\n", stderr="")
            input_data = kwargs.get('input')
            if input_data and "SUCCESS_0" in input_data:
                return MagicMock(returncode=0, stdout="SUCCESS_0\nSUCCESS_1\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp), verify=True)
            app.client.serial = "serial-1"

            with app.recorder.transaction():
                app.recorder.put_setting("global", "s1", "v1")
                app.recorder.put_setting("global", "s2", "v2")

            # Verify both were read back
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "get", "global", "s1"],
                capture_output=True, text=True, input=None, timeout=30
            )
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "get", "global", "s2"],
                capture_output=True, text=True, input=None, timeout=30
            )


    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_safe_optimizations_refuse_without_device_config_support(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "device_config list" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            with self.assertRaises(ValueError) as cm:
                app.apply_documented_safe_optimizations()
            self.assertIn("Device does not support `device_config`", str(cm.exception))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_restrict_background_apps_refuses_without_appops_support(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "appops" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            return MagicMock(returncode=0, stdout="30\n", stderr="") # sdk 30
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            with self.assertRaises(ValueError) as cm:
                app.restrict_background_apps()
            self.assertIn("Device does not support `appops`", str(cm.exception))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_experimental_optimizations_refuse_when_sdk_too_old(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "ro.build.version.sdk" in cmd:
                return MagicMock(returncode=0, stdout="25\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            with self.assertRaises(ValueError) as cm:
                app.apply_experimental_optimizations()
            self.assertIn("Device SDK 25 is too old", str(cm.exception))

    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_samsung_optimizations_still_require_samsung_brand(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "ro.product.brand" in cmd:
                return MagicMock(returncode=0, stdout="google\n", stderr="")
            return MagicMock(returncode=0, stdout="30\n", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            with self.assertRaises(ValueError) as cm:
                app.apply_samsung_experimental_optimizations()
            self.assertIn("Connected device is not Samsung", str(cm.exception))

    def test_optimizer_wrapper_exposes_main(self):
        import optimizer
        self.assertTrue(callable(optimizer.main))
        self.assertTrue(callable(optimizer.AdbClient))


if __name__ == "__main__":
    unittest.main()
