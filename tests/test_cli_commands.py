import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import json

from android_battery_optimizer.cli import main, BatteryOptimizerCLI, parse_args
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.adb import AdbClient, SubprocessRunner

class TestCLICommands(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_diagnose_subcommand(self):
        args = parse_args(["diagnose"])
        self.assertEqual(args.command, "diagnose")
        self.assertTrue(args.third_party_only)
        self.assertIsNone(args.output)

    def test_parse_doctor_state_subcommand(self):
        args = parse_args(["doctor-state"])
        self.assertEqual(args.command, "doctor-state")

    def test_parse_diagnose_output(self):
        args = parse_args(["diagnose", "--output", "report.json"])
        self.assertEqual(args.command, "diagnose")
        self.assertEqual(args.output, "report.json")

    def test_parse_diagnose_all_packages(self):
        args = parse_args(["diagnose", "--all-packages"])
        self.assertEqual(args.command, "diagnose")
        self.assertFalse(args.third_party_only)

    def test_parse_gui_subcommand(self):
        args = parse_args(["gui", "--port", "0", "--no-browser"])
        self.assertEqual(args.command, "gui")
        self.assertEqual(args.port, 0)
        self.assertTrue(args.no_browser)

    def test_parse_smart_restrict_subcommand(self):
        args = parse_args(["smart-restrict"])
        self.assertEqual(args.command, "smart-restrict")
        self.assertFalse(args.yes)
        self.assertFalse(args.aggressive)
        self.assertIsNone(args.min_last_used_days)

    def test_parse_smart_restrict_aggressive_yes(self):
        args = parse_args(["smart-restrict", "--aggressive", "--yes"])
        self.assertEqual(args.command, "smart-restrict")
        self.assertTrue(args.yes)
        self.assertTrue(args.aggressive)

    def test_parse_smart_restrict_min_last_used_days(self):
        args = parse_args(["smart-restrict", "--min-last-used-days", "14", "--yes"])
        self.assertEqual(args.command, "smart-restrict")
        self.assertTrue(args.yes)
        self.assertEqual(args.min_last_used_days, 14)

    def test_parse_smart_restrict_dry_run_after_subcommand(self):
        # Even if --dry-run is placed before or after, parse_args handles it correctly since it's a parent_parser arg
        args = parse_args(["smart-restrict", "--dry-run"])
        self.assertEqual(args.command, "smart-restrict")
        self.assertTrue(args.dry_run)

    def test_parse_120hz_endurance_subcommand(self):
        args = parse_args(["apply-120hz-endurance", "--yes"])
        self.assertEqual(args.command, "apply-120hz-endurance")
        self.assertTrue(args.yes)

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.apply_120hz_endurance_profile")
    def test_120hz_endurance_requires_yes(self, mock_apply, mock_check_env):
        mock_check_env.return_value = True
        outputs = []
        app = BatteryOptimizerApp(
            client=AdbClient(runner=SubprocessRunner(), output=lambda _: None),
            state_dir=self.state_dir,
        )
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)

        result = cli.run_command(parse_args(["apply-120hz-endurance"]))
        self.assertEqual(result, 1)
        mock_apply.assert_not_called()

        result = cli.run_command(
            parse_args(["apply-120hz-endurance", "--yes"])
        )
        self.assertEqual(result, 0)
        mock_apply.assert_called_once()

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.get_device_info")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.apply_experimental_optimizations")
    def test_apply_experimental_requires_yes_noninteractive(self, mock_apply, mock_get_info, mock_check_env):
        mock_check_env.return_value = True
        mock_get_info.return_value = "Google Pixel 6"

        outputs = []
        app = BatteryOptimizerApp(
            client=AdbClient(runner=SubprocessRunner(), output=lambda _: None),
            state_dir=self.state_dir,
        )
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)

        # Test without --yes
        args = parse_args(["apply-experimental"])
        result = cli.run_command(args)
        self.assertEqual(result, 1)
        self.assertIn("Error: --yes is required", outputs[0])
        mock_apply.assert_not_called()

        # Test with --yes
        outputs.clear()
        args = parse_args(["apply-experimental", "--yes"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        self.assertIn("Experimental optimizations applied", outputs[-1])
        mock_apply.assert_called_once()

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.apply_documented_safe_optimizations")
    def test_apply_safe_subcommand_calls_app_method(self, mock_apply, mock_check_env):
        mock_check_env.return_value = True
        outputs = []
        app = BatteryOptimizerApp(
            client=AdbClient(runner=SubprocessRunner(), output=lambda _: None),
            state_dir=self.state_dir,
        )
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)

        args = parse_args(["apply-safe"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        mock_apply.assert_called_once()
        self.assertIn("Applying documented safe optimizations", outputs[0])

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.revert_saved_state")
    def test_revert_subcommand_calls_restore(self, mock_revert, mock_check_env):
        mock_check_env.return_value = True
        mock_revert.return_value = ["Restored something"]
        outputs = []
        app = BatteryOptimizerApp(
            client=AdbClient(runner=SubprocessRunner(), output=lambda _: None),
            state_dir=self.state_dir,
        )
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)

        args = parse_args(["revert"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        mock_revert.assert_called_once()
        self.assertIn("Restoring saved state", outputs[0])
        self.assertIn("Restored something", outputs[1])

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.get_device_info")
    @patch("android_battery_optimizer.state.StateStore.has_entries")
    def test_status_prints_device_info(self, mock_has_entries, mock_get_info, mock_check_env):
        mock_check_env.return_value = True
        mock_get_info.return_value = "Samsung S21"
        mock_has_entries.return_value = True

        outputs = []
        client = AdbClient(runner=SubprocessRunner(), serial="test-device", output=lambda _: None)
        app = BatteryOptimizerApp(client=client, state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)

        args = parse_args(["status"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)

        output_str = "\n".join(outputs)
        self.assertIn("Selected device: test-device", output_str)
        self.assertIn("Device info: Samsung S21", output_str)
        self.assertIn("Rollback state exists: True", output_str)

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run")
    def test_default_without_subcommand_still_runs_interactive_menu(self, mock_run, mock_check_env):
        # We need to mock main to avoid full initialization if possible, or just mock BatteryOptimizerCLI.run
        with patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run") as mock_run:
            main([])
            mock_run.assert_called_once()

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_dry_run_subcommand_does_not_create_state(self, mock_run, mock_check_env):
        mock_check_env.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        outputs = []
        client = AdbClient(
            runner=SubprocessRunner(),
            serial="test-device",
            dry_run=True,
            output=lambda _: None,
        )
        app = BatteryOptimizerApp(client=client, state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)

        # Manually trigger something that would normally save state
        with app.recorder.transaction():
            app.recorder.put_setting("global", "test", "1")

        state_file = self.state_dir / "devices" / "test-device" / "state.json"
        self.assertFalse(state_file.exists())

        # Now test via cli command
        args = parse_args(["--dry-run", "apply-safe"])
        cli.client.dry_run = True # Ensure it's set
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        self.assertFalse(state_file.exists())

    @patch("android_battery_optimizer.app.BatteryOptimizerApp.apply_documented_safe_optimizations")
    @patch("android_battery_optimizer.adb.subprocess.run")
    @patch("android_battery_optimizer.adb.shutil.which")
    def test_cli_binds_single_device_before_apply_safe(self, mock_which, mock_run, mock_apply):
        mock_which.return_value = "/usr/bin/adb"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\ntest-device\tdevice\n",
            stderr="",
        )

        outputs = []
        client = AdbClient(runner=SubprocessRunner(), output=lambda _: None)
        app = BatteryOptimizerApp(client=client, state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)

        args = parse_args(["apply-safe"])
        result = cli.run_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(cli.client.serial, "test-device")
        mock_apply.assert_called_once()

if __name__ == "__main__":
    unittest.main()
