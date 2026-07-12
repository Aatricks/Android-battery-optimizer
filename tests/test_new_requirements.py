import unittest
import tempfile
import shutil
import argparse
from unittest.mock import MagicMock, patch
from pathlib import Path
import time
from android_battery_optimizer.adb import AdbClient, CommandRunner, CommandResult
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI
from android_battery_optimizer.diagnose import Diagnoser
from android_battery_optimizer.verification import VerificationError

class FakeRunner(CommandRunner):
    def __init__(self):
        self.commands = []
        self.responses = {}

    def run(self, args, input_data=None, timeout=None):
        cmd_str = " ".join(map(str, args))
        if input_data:
            for line in input_data.splitlines():
                if line.strip():
                    self.commands.append(line.strip())
        self.commands.append(cmd_str)
        if cmd_str in self.responses:
            res = self.responses[cmd_str]
            if isinstance(res, Exception):
                raise res
            return res
        return CommandResult(0, "", "")

    def which(self, name):
        return "/usr/bin/" + name

class TestNewRequirements(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        self.client = AdbClient(self.runner, serial="test_device", output=lambda x: None)
        self.app = BatteryOptimizerApp(self.client, self.test_dir)
        self.app.recorder.verify = False
        
        self.cli_outputs = []
        def capture_output(msg):
            self.cli_outputs.append(msg)
        self.cli = BatteryOptimizerCLI(self.app, output=capture_output)

        self.setup_default_responses()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def setup_default_responses(self):
        self.runner.responses["adb -s test_device shell getprop ro.product.brand"] = CommandResult(0, "Google", "")
        self.runner.responses["adb -s test_device shell getprop ro.build.version.sdk"] = CommandResult(0, "30", "")
        
        self.runner.responses["adb -s test_device shell pm list packages"] = CommandResult(0, "package:com.example.app\npackage:com.example.recent\npackage:com.example.kept", "")
        self.runner.responses["adb -s test_device shell pm list packages -3"] = CommandResult(0, "package:com.example.app\npackage:com.example.recent\npackage:com.example.kept", "")
        
        self.runner.responses["adb -s test_device shell pm list packages --user 0 -d"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell pm list packages --user 0 -e"] = CommandResult(0, "package:com.example.app\npackage:com.example.recent\npackage:com.example.kept", "")
        
        # com.example.app is aggressive restrict, com.example.recent is restrict, com.example.kept is keep
        self.runner.responses["adb devices"] = CommandResult(0, "List of devices attached\ntest_device\tdevice\n", "")
        self.runner.responses["adb -s test_device shell dumpsys appops"] = CommandResult(0, 
            "Package com.example.app:\n  RUN_ANY_IN_BACKGROUND: allow\n"
            "Package com.example.recent:\n  RUN_ANY_IN_BACKGROUND: allow\n"
            "Package com.example.kept:\n  RUN_ANY_IN_BACKGROUND: allow", "")
        self.runner.responses["adb -s test_device shell cmd appops help"] = CommandResult(0, "help", "")
        self.runner.responses["adb -s test_device shell am get-standby-bucket android"] = CommandResult(0, "10", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.app WAKE_LOCK"] = CommandResult(0, "default", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.recent"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.recent RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.recent WAKE_LOCK"] = CommandResult(0, "default", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.kept"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.kept RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.kept WAKE_LOCK"] = CommandResult(0, "default", "")

        alarm_out = (
            "    +1m20s000ms running, 1200 wakeups, 1200 alarms: u0a101:com.example.app\n"
            "    +2m30s000ms running, 150 wakeups, 150 alarms: u0a102:com.example.recent\n"
        )
        self.runner.responses["adb -s test_device shell dumpsys alarm"] = CommandResult(0, alarm_out, "")
        self.runner.responses["adb -s test_device shell dumpsys jobscheduler"] = CommandResult(0, "JOB u0a101:com.example.app\n", "")
        bs_out = (
            "9,0,i,uid,10001,com.example.app\n"
            "9,10001,l,wua,alarm,1200\n"
            "9,10001,l,wl,mywakelock,0,f,0,3700000,p,1\n"
            "9,0,i,uid,10002,com.example.recent\n"
            "9,10002,l,wua,alarm,150\n"
            "9,10002,l,wl,mywakelock,0,f,0,700000,p,1\n"
        )
        self.runner.responses["adb -s test_device shell dumpsys batterystats --checkin"] = CommandResult(0, bs_out, "")
        self.runner.responses["adb -s test_device shell dumpsys batterystats --charged"] = CommandResult(
            0, "  Time on battery: 2h 0m 0s 0ms (100.0%) realtime\n", ""
        )
        self.runner.responses["adb -s test_device shell cmd deviceidle whitelist"] = CommandResult(0, "", "")
        
        now_ms = int(time.time() * 1000)
        recent_ms = now_ms - (2 * 86400 * 1000) # 2 days ago
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0, 
            f"package=com.example.recent lastTimeUsed=\"{recent_ms}\" bucket=10\n"
            "package=com.example.app lastTimeUsed=\"unparsed_string\" bucket=10\n"
            "package=com.example.kept bucket=10", "")
            
        self.runner.responses["adb -s test_device shell cmd package resolve-activity -a android.intent.action.MAIN -c android.intent.category.HOME"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell telecom get-default-dialer"] = CommandResult(0, "", "")

    def test_smart_restrict_returns_applied_skipped_kept_warnings(self):
        # Trigger some warning
        self.runner.responses["adb -s test_device shell cmd deviceidle whitelist"] = Exception("failed")
        
        res = self.app.smart_restrict(aggressive=False)
        
        self.assertIn("applied", res)
        self.assertIn("skipped", res)
        self.assertIn("kept", res)
        self.assertIn("warnings", res)
        
        self.assertTrue(any("Failed to query deviceidle whitelist" in w for w in res["warnings"]))
        
        applied_pkgs = [r["package"] for r in res["applied"]]
        self.assertIn("com.example.app", applied_pkgs)
        self.assertIn("com.example.recent", applied_pkgs)
        
        kept_pkgs = [r["package"] for r in res["kept"]]
        self.assertIn("com.example.kept", kept_pkgs)

    def test_smart_restrict_cli_prints_applied_packages(self):
        args = argparse.Namespace(command="smart-restrict", yes=True, dry_run=False, aggressive=False, min_last_used_days=None)
        self.cli.run_command(args)
        
        out = "\n".join(self.cli_outputs)
        self.assertIn("Restricted: 2", out)
        self.assertIn("Kept: 1", out)
        self.assertIn("com.example.app -> RUN_ANY_IN_BACKGROUND=ignore, bucket=rare", out)

    def test_smart_restrict_cli_prints_diagnostics_warnings(self):
        self.runner.responses["adb -s test_device shell cmd deviceidle whitelist"] = Exception("failed")
        args = argparse.Namespace(command="smart-restrict", yes=True, dry_run=False, aggressive=False, min_last_used_days=None)
        self.cli.run_command(args)
        
        out = "\n".join(self.cli_outputs)
        self.assertIn("Warnings:", out)
        self.assertIn("Failed to query deviceidle whitelist", out)

    def test_min_last_used_days_skips_recently_used_package_with_reason(self):
        res = self.app.smart_restrict(aggressive=False, min_last_used_days=7)
        
        skipped = {s["package"]: s["reason"] for s in res["skipped"]}
        self.assertIn("com.example.recent", skipped)
        self.assertEqual(skipped["com.example.recent"], "recently_used")
        
        applied_pkgs = [r["package"] for r in res["applied"]]
        self.assertNotIn("com.example.recent", applied_pkgs)

    def test_min_last_used_days_skips_unknown_last_used_with_reason(self):
        res = self.app.smart_restrict(aggressive=False, min_last_used_days=7)
        
        skipped = {s["package"]: s["reason"] for s in res["skipped"]}
        self.assertIn("com.example.app", skipped)
        self.assertEqual(skipped["com.example.app"], "last_used_unknown")

    def test_diagnose_last_used_parses_epoch_ms(self):
        diag = Diagnoser(self.client)
        res = diag.run(third_party_only=True)
        
        for pkg in res["packages"]:
            if pkg["package"] == "com.example.recent":
                last_used = pkg["signals"]["last_used"]
                self.assertTrue(last_used["parsed"])
                self.assertIsNotNone(last_used["epoch_ms"])

    def test_diagnose_last_used_reports_unparsed_human_time(self):
        diag = Diagnoser(self.client)
        res = diag.run(third_party_only=True)
        
        for pkg in res["packages"]:
            if pkg["package"] == "com.example.app":
                last_used = pkg["signals"]["last_used"]
                self.assertFalse(last_used["parsed"])
                self.assertEqual(last_used["raw"], "unparsed_string")
                self.assertIsNone(last_used["epoch_ms"])

    def test_diagnose_reuses_appops_parser_for_colon_format(self):
        self.runner.responses["adb -s test_device shell cmd appops get com.example.kept RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "RUN_ANY_IN_BACKGROUND: mode=ignore; time=+1m", "")
        diag = Diagnoser(self.client)
        res = diag.run(third_party_only=True)
        
        for pkg in res["packages"]:
            if pkg["package"] == "com.example.kept":
                self.assertEqual(pkg["run_any_in_background"], "ignore")

    def test_smart_restrict_dry_run_reports_would_apply_but_does_not_write_state(self):
        args = argparse.Namespace(command="smart-restrict", yes=False, dry_run=True, aggressive=False, min_last_used_days=None)
        
        self.cli.client.dry_run = True
        self.cli.run_command(args)
        
        out = "\n".join(self.cli_outputs)
        self.assertIn("Would restrict (dry-run):", out)
        self.assertIn("com.example.app ->", out)
        self.assertNotIn("Smart restrict applied successfully.", out)
        
        state_file = self.test_dir / "devices" / "test_device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_item5_restrict_background_apps_non_restorable_skips(self):
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0,
            "package=com.example.app u=0 bucket=5\n"
            "package=com.example.recent u=0 bucket=10", ""
        )
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "5\n", "")
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.recent"] = CommandResult(0, "10\n", "")
        
        res = self.app.restrict_background_apps(level="ignore")
        self.assertEqual(res["skipped_non_restorable"], ["com.example.app"])
        self.assertEqual(res["skipped_whitelisted"], [])

    def test_item6_restrict_background_apps_levels(self):
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0,
            "package=com.example.app u=0 bucket=10", ""
        )
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "10\n", "")
        
        self.runner.commands.clear()
        self.app.restrict_background_apps(level="allow")
        self.assertTrue(any("am set-standby-bucket com.example.app active" in cmd for cmd in self.runner.commands))

        self.runner.commands.clear()
        self.app.restrict_background_apps(level="ignore")
        self.assertTrue(any("am set-standby-bucket com.example.app rare" in cmd for cmd in self.runner.commands))

        self.runner.commands.clear()
        self.app.restrict_background_apps(level="deny")
        self.assertTrue(any("am set-standby-bucket com.example.app rare" in cmd for cmd in self.runner.commands))

    def test_item7_standby_bucket_fallback(self):
        self.app.recorder._prefetch_standby_bucket_success = True
        self.app.recorder._standby_bucket_cache = {}
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "5\n", "")
        
        val = self.app.recorder._get_standby_bucket("com.example.app")
        self.assertEqual(val, "5")
        self.assertEqual(self.app.recorder._standby_bucket_cache["com.example.app"], "5")

        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.invalid"] = CommandResult(0, "invalid output\n", "")
        from android_battery_optimizer.snapshot import SnapshotError
        with self.assertRaises(SnapshotError):
            self.app.recorder._get_standby_bucket("com.example.invalid")

    def test_item8_appops_prefetch_and_fallback(self):
        from android_battery_optimizer.snapshot import prefetch_package_states
        
        dumpsys_output = (
            "    Package com.google.android.youtube:\n"
            "      RUN_ANY_IN_BACKGROUND (ignore): \n"
            "    Op RUN_ANY_IN_BACKGROUND:\n"
            "      #0: ModeCallback{4bac5bb watchinguid=-1 flags=0x0 op=RUN_ANY_IN_BACKGROUND from uid=1000 pid=1345}\n"
        )
        self.runner.responses["adb -s test_device shell dumpsys appops"] = CommandResult(0, dumpsys_output, "")
        
        _, _, _, appops_cache, _, _ = prefetch_package_states(self.client)
        
        self.assertIn("com.google.android.youtube", appops_cache)
        self.assertEqual(appops_cache["com.google.android.youtube"].get("RUN_ANY_IN_BACKGROUND"), "ignore")
        
        self.assertNotIn("Op", appops_cache)
        self.assertNotIn("#0", appops_cache)

        self.app.recorder._prefetch_appops_success = True
        self.app.recorder._appops_cache = {}
        self.runner.responses["adb -s test_device shell cmd appops get com.example.fallback RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "RUN_ANY_IN_BACKGROUND: ignore\n", "")
        
        val = self.app.recorder._get_appop("com.example.fallback", "RUN_ANY_IN_BACKGROUND")
        self.assertEqual(val, "ignore")

    def test_item10_timeout_rollback_all(self):
        self.app.recorder.verify = True
        self.runner.responses["adb -s test_device shell settings list global"] = CommandResult(0, "some_key=old_val\nother_key=old_val\n", "")
        
        from android_battery_optimizer.adb import CommandError
        self.runner.responses["adb -s test_device shell"] = CommandError(
            "adb shell failed",
            result=CommandResult(1, "", "timeout")
        )
        self.runner.responses["adb -s test_device shell settings put global some_key old_val"] = CommandError(
            "put failed",
            result=CommandResult(1, "", "error")
        )
        self.runner.responses["adb -s test_device shell settings put global other_key old_val"] = CommandResult(0, "", "")

        with self.assertRaises(CommandError):
            with self.app.recorder.transaction():
                self.app.recorder.put_setting("global", "some_key", "new_val")
                self.app.recorder.put_setting("global", "other_key", "new_val")
                
        self.assertIn("adb -s test_device shell settings put global other_key old_val", self.runner.commands)
        self.assertIn("adb -s test_device shell settings put global some_key old_val", self.runner.commands)
        
        self.assertIn("global/some_key", self.app.store.data["settings"])
        self.assertNotIn("global/other_key", self.app.store.data["settings"])

    def test_item11_whitelist_add_validation(self):
        args = argparse.Namespace(command="whitelist", wl_command="add", package="com.example.missing")
        exit_code = self.cli.run_command(args)
        
        self.assertEqual(exit_code, 1)
        out = "\n".join(self.cli_outputs)
        self.assertIn("Error: Package `com.example.missing` is not installed on the connected device.", out)
        
        self.cli_outputs.clear()
        args = argparse.Namespace(command="whitelist", wl_command="add", package="com.example.app")
        exit_code = self.cli.run_command(args)
        
        self.assertEqual(exit_code, 0)
        self.assertIn("com.example.app", self.app.load_whitelist())
        out = "\n".join(self.cli_outputs)
        self.assertIn("Added com.example.app to whitelist.", out)

    def test_prop_caching(self):
        self.runner.commands.clear()
        self.client._device_info_cache.clear()
        
        props_out = "Google\nPixel\n11\n30\nfingerprint\n"
        self.runner.responses["adb -s test_device shell getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.fingerprint"] = CommandResult(0, props_out, "")
        
        info1 = self.client.get_device_info_struct()
        info2 = self.client.get_device_info_struct()
        
        shell_cmds = [c for c in self.runner.commands if "getprop" in c]
        self.assertEqual(len(shell_cmds), 1)
        self.assertEqual(info1.brand, "Google")
        self.assertEqual(info2.brand, "Google")
        
        self.client.serial = "new_device"
        self.runner.responses["adb -s new_device shell getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.fingerprint"] = CommandResult(0, props_out, "")
        
        info3 = self.client.get_device_info_struct()
        shell_cmds_new = [c for c in self.runner.commands if "getprop" in c]
        self.assertEqual(len(shell_cmds_new), 2)

    def test_absence_of_refresh_rate_and_low_power(self):
        self.runner.commands.clear()
        self.runner.responses["adb -s test_device shell getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.fingerprint"] = CommandResult(
            0, "Samsung\nSM-S901B\n12\n31\nfingerprint\n", ""
        )
        self.runner.responses["adb -s test_device shell cmd netpolicy get restrict-background"] = CommandResult(0, "disabled", "")
        self.runner.responses["adb -s test_device shell cmd netpolicy list restrict-background-whitelist"] = CommandResult(0, "Restrict background whitelisted UIDs:", "")
        
        self.app.apply_experimental_optimizations()
        self.app.apply_samsung_experimental_optimizations()
        
        applied_commands = " ".join(self.runner.commands)
        self.assertNotIn("refresh_rate", applied_commands)
        self.assertNotIn("peak_refresh_rate", applied_commands)
        self.assertNotIn("min_refresh_rate", applied_commands)
        self.assertNotIn("low_power", applied_commands)

    def test_parse_builtin_display_refresh_rates(self):
        from android_battery_optimizer.android import parse_builtin_refresh_rates

        output = (
            'DisplayDeviceInfo{"Built-in Screen": supportedRefreshRates '
            "[120.00001, 96.0, 60.0, 10.0]}\n"
            'DisplayDeviceInfo{"External Screen": supportedRefreshRates '
            "[144.0, 60.0]}"
        )

        self.assertEqual(
            parse_builtin_refresh_rates(output),
            [10.0, 60.0, 96.0, 120.00001],
        )

    def test_120hz_endurance_refuses_device_without_120hz(self):
        with (
            patch.object(self.app, "_display_supports_120hz", return_value=False),
            patch.object(self.app, "apply_experimental_optimizations") as apply_profile,
        ):
            with self.assertRaisesRegex(ValueError, "120 Hz"):
                self.app.apply_120hz_endurance_profile()

        apply_profile.assert_not_called()

    def test_120hz_endurance_restores_if_120hz_is_lost(self):
        with (
            patch.object(
                self.app,
                "_display_supports_120hz",
                side_effect=[True, False],
            ),
            patch.object(self.app, "apply_experimental_optimizations") as apply_profile,
            patch.object(self.app, "revert_saved_state", return_value=[]) as restore,
        ):
            with self.assertRaises(VerificationError):
                self.app.apply_120hz_endurance_profile()

        apply_profile.assert_called_once()
        restore.assert_called_once()

    def test_120hz_endurance_keeps_refresh_settings_untouched(self):
        with patch.object(
            self.app,
            "_display_supports_120hz",
            side_effect=[True, True],
        ):
            self.app.apply_120hz_endurance_profile()

        applied_commands = " ".join(self.runner.commands)
        self.assertNotIn("peak_refresh_rate", applied_commands)
        self.assertNotIn("min_refresh_rate", applied_commands)

    def test_doze_profile_and_battery_saver_constants(self):
        self.runner.commands.clear()
        self.runner.responses["adb -s test_device shell getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.fingerprint"] = CommandResult(
            0, "Google\nPixel\n11\n30\nfingerprint\n", ""
        )
        self.runner.responses["adb -s test_device shell cmd netpolicy get restrict-background"] = CommandResult(0, "disabled", "")
        self.runner.responses["adb -s test_device shell cmd netpolicy list restrict-background-whitelist"] = CommandResult(0, "Restrict background whitelisted UIDs:", "")

        self.app.apply_experimental_optimizations()
        
        config_cmds = [c for c in self.runner.commands if "device_config put device_idle" in c]
        self.assertTrue(any("inactive_to 300000" in cmd for cmd in config_cmds))
        self.assertTrue(any("light_after_inactive_to 60000" in cmd for cmd in config_cmds))
        
        saver_cmds = [c for c in self.runner.commands if "battery_saver_constants" in c]
        self.assertTrue(any("adjust_brightness_factor=0.5" in cmd for cmd in saver_cmds))
        self.assertTrue(any("gps_mode=2" in cmd for cmd in saver_cmds))
        self.assertTrue(any("firewall_disabled=false" in cmd for cmd in saver_cmds))

    def test_netpolicy_skip_on_unparseable(self):
        self.runner.commands.clear()
        self.runner.responses["adb -s test_device shell getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.fingerprint"] = CommandResult(
            0, "Google\nPixel\n11\n30\nfingerprint\n", ""
        )
        self.runner.responses["adb -s test_device shell cmd netpolicy get restrict-background"] = CommandResult(1, "error", "error")
        
        self.app.apply_experimental_optimizations()
        self.assertFalse(any("netpolicy set" in c for c in self.runner.commands))

    def test_hibernation_sdk_gate(self):
        self.runner.responses["adb -s test_device shell getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.fingerprint"] = CommandResult(
            0, "Google\nPixel\n11\n30\nfingerprint\n", ""
        )
        self.app.recorder.verify = False
        self.runner.commands.clear()
        self.app.smart_restrict(min_last_used_days=7)
        self.assertFalse(any("app_hibernation" in c for c in self.runner.commands))
        
        self.client._device_info_cache.clear()
        self.runner.responses["adb -s test_device shell getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.fingerprint"] = CommandResult(
            0, "Google\nPixel\n12\n31\nfingerprint\n", ""
        )
        now_ms = int(time.time() * 1000)
        ten_days_ago_ms = now_ms - (10 * 86400 * 1000)
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0, 
            f"package=com.example.recent lastTimeUsed=\"{ten_days_ago_ms}\" bucket=10\n"
            "package=com.example.app lastTimeUsed=\"unparsed_string\" bucket=10\n"
            "package=com.example.kept bucket=10", "")
        self.runner.commands.clear()
        self.runner.responses["adb -s test_device shell cmd app_hibernation get-state com.example.recent"] = CommandResult(0, "false", "")
        self.app.smart_restrict(min_last_used_days=7)
        self.assertTrue(any("app_hibernation set-state com.example.recent true" in c for c in self.runner.commands))

    def test_wakelock_appop_aggressive_only(self):
        self.runner.commands.clear()
        self.app.smart_restrict(aggressive=False)
        self.assertFalse(any("appops set com.example.app WAKE_LOCK ignore" in c for c in self.runner.commands))
        
        self.runner.commands.clear()
        self.app.smart_restrict(aggressive=True)
        self.assertTrue(any("appops set com.example.app WAKE_LOCK ignore" in c for c in self.runner.commands))

    def test_parsers_against_fixtures(self):
        from android_battery_optimizer.diagnose import (
            parse_alarm_wakeups,
            parse_wakelock_ms,
            parse_registered_jobs,
        )
        
        alarm_fixture = (
            "    +8m50s210ms running, 0 wakeups, 25432 alarms: 1000:android\n"
            "    +5m48s224ms running, 6994 wakeups, 6994 alarms: u0a274:com.google.android.gms\n"
        )
        alarms = parse_alarm_wakeups(alarm_fixture)
        self.assertEqual(alarms.get("com.google.android.gms"), 6994)
        self.assertEqual(alarms.get("android"), 0)
        
        bs_fixture = (
            "9,0,i,uid,1000,com.samsung.android.provider.filterprovider\n"
            "9,10100,l,wl,mywakelock,0,f,0,3600000,p,1\n"
        )
        bs_fixture_with_uid = bs_fixture + "9,0,i,uid,10100,com.example.pkg\n"
        wls = parse_wakelock_ms(bs_fixture_with_uid)
        self.assertEqual(wls.get("com.example.pkg"), 3600)
        
        jobs_fixture = (
            "  JOB companion:1000/1: dc16cd7 @companion@android/com.android.server.companion.association.InactiveAssociationsRemovalService\n"
        )
        fn = lambda pkg, line: pkg in line
        jobs = parse_registered_jobs(jobs_fixture, "android", fn)
        self.assertEqual(jobs, 1)

    def test_recommendation_thresholds(self):
        diag = Diagnoser(self.client)
        
        sufficient = {"observation_ms": 3600000}

        rec, _ = diag._recommend("active", "allow", {"alarm_wakeups": 1000, "wakelock_partial_ms": 0, "jobs_registered": 0, **sufficient})
        self.assertEqual(rec, "aggressive_restrict")
        
        rec, _ = diag._recommend("active", "allow", {"alarm_wakeups": 0, "wakelock_partial_ms": 3600000, "jobs_registered": 0, **sufficient})
        self.assertEqual(rec, "aggressive_restrict")
        
        rec, _ = diag._recommend("active", "allow", {"alarm_wakeups": 100, "wakelock_partial_ms": 0, "jobs_registered": 0, **sufficient})
        self.assertEqual(rec, "restrict")
        
        rec, _ = diag._recommend("active", "allow", {"alarm_wakeups": 0, "wakelock_partial_ms": 600000, "jobs_registered": 0, **sufficient})
        self.assertEqual(rec, "restrict")
        
        rec, _ = diag._recommend("active", "allow", {"alarm_wakeups": 0, "wakelock_partial_ms": 0, "jobs_registered": 100, **sufficient})
        self.assertEqual(rec, "restrict")
        
        rec, _ = diag._recommend("active", "allow", {"alarm_wakeups": 99, "wakelock_partial_ms": 599999, "jobs_registered": 99, **sufficient})
        self.assertEqual(rec, "keep")

    def test_batched_verification_scenarios(self):
        from android_battery_optimizer.verification import verify_entries_batched
        from android_battery_optimizer.adb import CommandError
        from android_battery_optimizer.verification import VerificationError
        
        entries = [
            {"type": "setting", "namespace": "global", "key": "k1", "new_value": "v1"},
        ]
        
        self.runner.responses["adb -s test_device shell"] = CommandResult(0, "===V_0===\nv1\n", "")
        verify_entries_batched(self.client, entries)
        
        self.runner.responses["adb -s test_device shell"] = CommandResult(0, "===V_0===\nwrong\n", "")
        with self.assertRaises(VerificationError):
            verify_entries_batched(self.client, entries)
            
        self.runner.responses["adb -s test_device shell"] = CommandResult(0, "", "")
        with self.assertRaises(CommandError):
            verify_entries_batched(self.client, entries)

    def test_new_ledger_types_roundtrip(self):
        self.runner.responses["adb -s test_device shell pm list packages -U com.example.app"] = CommandResult(0, "package:com.example.app uid:10010\n", "")
        self.runner.responses["adb -s test_device shell cmd netpolicy get restrict-background"] = CommandResult(0, "disabled", "")
        self.runner.responses["adb -s test_device shell cmd netpolicy list restrict-background-whitelist"] = CommandResult(0, "Restrict background whitelisted UIDs:", "")
        self.runner.responses["adb -s test_device shell cmd deviceidle whitelist"] = CommandResult(0, "user,com.example.app,10010\n", "")
        self.runner.responses["adb -s test_device shell cmd app_hibernation get-state com.example.app"] = CommandResult(0, "false", "")
        
        self.app.recorder.verify = False
        
        with self.app.recorder.transaction():
            self.app.recorder.set_netpolicy_restrict_background(True)
            self.app.recorder.add_netpolicy_whitelist("com.example.app", "10010")
            self.app.recorder.remove_deviceidle_whitelist("com.example.app")
            self.app.recorder.set_app_hibernation("com.example.app", True)
            
        self.assertIn("netpolicy", self.app.store.data)
        self.assertIn("netpolicy_whitelist", self.app.store.data)
        self.assertIn("deviceidle_whitelist", self.app.store.data)
        self.assertIn("hibernation", self.app.store.data)
        
        self.app.store.rebind()
        self.assertIn("netpolicy", self.app.store.data)
        
        self.runner.commands.clear()
        self.runner.responses["adb -s test_device shell cmd netpolicy get restrict-background"] = CommandResult(0, "enabled", "")
        self.runner.responses["adb -s test_device shell cmd netpolicy list restrict-background-whitelist"] = CommandResult(0, "Restrict background whitelisted UIDs: 10010", "")
        self.runner.responses["adb -s test_device shell cmd deviceidle whitelist"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell cmd app_hibernation get-state com.example.app"] = CommandResult(0, "true", "")
        
        self.app.revert_saved_state()
        
        rollback_cmds = " ".join(self.runner.commands)
        self.assertIn("netpolicy set restrict-background false", rollback_cmds)
        self.assertIn("netpolicy remove restrict-background-whitelist 10010", rollback_cmds)
        self.assertIn("deviceidle whitelist +com.example.app", rollback_cmds)
        self.assertIn("app_hibernation set-state com.example.app false", rollback_cmds)

SECURITY_EXCEPTION_OUTPUT = (
    "Exception occurred while executing 'put':\n"
    "java.lang.SecurityException: Permission denial for flag "
    "'activity_manager/bg_auto_restrict_abusive_apps'; allowlist permission "
    "granted, but must add flag to the allowlist\n"
)


class TestBatch3DeviceConfigAllowlist(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        self.outputs = []
        self.client = AdbClient(
            self.runner, serial="test_device", output=self.outputs.append
        )
        self.app = BatteryOptimizerApp(self.client, self.test_dir)
        self.app.recorder.verify = False
        self.runner.responses[
            "adb -s test_device shell getprop ro.build.version.sdk"
        ] = CommandResult(0, "34", "")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_probe_true_when_flag_set_and_put_succeeds(self):
        self.runner.responses[
            "adb -s test_device shell device_config get device_idle inactive_to"
        ] = CommandResult(0, "500000\n", "")
        self.assertTrue(
            self.client.supports_device_config_write(
                "device_idle", "inactive_to", "300000"
            )
        )
        self.assertIn(
            "adb -s test_device shell device_config put device_idle inactive_to 500000",
            self.runner.commands,
        )
        self.assertNotIn(
            "adb -s test_device shell device_config delete device_idle inactive_to",
            self.runner.commands,
        )

    def test_probe_true_when_flag_unset_put_delete_succeed(self):
        self.runner.responses[
            "adb -s test_device shell device_config get device_idle inactive_to"
        ] = CommandResult(0, "null\n", "")
        self.assertTrue(
            self.client.supports_device_config_write(
                "device_idle", "inactive_to", "300000"
            )
        )
        self.assertIn(
            "adb -s test_device shell device_config put device_idle inactive_to 300000",
            self.runner.commands,
        )
        self.assertIn(
            "adb -s test_device shell device_config delete device_idle inactive_to",
            self.runner.commands,
        )

    def test_probe_false_on_denied_write(self):
        self.runner.responses[
            "adb -s test_device shell device_config get device_idle inactive_to"
        ] = CommandResult(0, "null\n", "")
        self.runner.responses[
            "adb -s test_device shell device_config put device_idle inactive_to 300000"
        ] = CommandResult(255, SECURITY_EXCEPTION_OUTPUT, "")
        self.assertFalse(
            self.client.supports_device_config_write(
                "device_idle", "inactive_to", "300000"
            )
        )

    def test_probe_false_on_security_exception_with_zero_exit(self):
        self.runner.responses[
            "adb -s test_device shell device_config get device_idle inactive_to"
        ] = CommandResult(0, "null\n", "")
        self.runner.responses[
            "adb -s test_device shell device_config put device_idle inactive_to 300000"
        ] = CommandResult(0, SECURITY_EXCEPTION_OUTPUT, "")
        self.assertFalse(
            self.client.supports_device_config_write(
                "device_idle", "inactive_to", "300000"
            )
        )

    def test_probe_true_in_dry_run_without_commands(self):
        self.client.dry_run = True
        self.assertTrue(
            self.client.supports_device_config_write(
                "device_idle", "inactive_to", "300000"
            )
        )
        device_config_cmds = [
            c for c in self.runner.commands if "device_config" in c
        ]
        self.assertEqual(device_config_cmds, [])

    def test_apply_safe_clean_error_when_blocked(self):
        self.runner.responses[
            "adb -s test_device shell device_config put activity_manager bg_auto_restrict_abusive_apps 1"
        ] = CommandResult(255, SECURITY_EXCEPTION_OUTPUT, "")
        with self.assertRaises(ValueError) as ctx:
            self.app.apply_documented_safe_optimizations()
        self.assertIn("blocks shell device_config writes", str(ctx.exception))

    def test_apply_experimental_skips_doze_when_blocked(self):
        self.runner.responses[
            "adb -s test_device shell device_config put device_idle inactive_to 300000"
        ] = CommandResult(255, SECURITY_EXCEPTION_OUTPUT, "")
        self.runner.responses[
            "adb -s test_device shell cmd netpolicy get restrict-background"
        ] = CommandResult(0, "Restrict background status: disabled\n", "")
        self.app.apply_experimental_optimizations()
        joined = "\n".join(self.runner.commands)
        self.assertNotIn("device_config put device_idle light_after_inactive_to", joined)
        self.assertNotIn("device_config put activity_manager", joined)
        self.assertTrue(
            any(
                c.startswith("settings put global window_animation_scale 0.5")
                for c in self.runner.commands
            )
        )
        legacy_puts = [
            c for c in self.runner.commands
            if c.startswith("settings put global device_idle_constants ")
        ]
        self.assertEqual(len(legacy_puts), 1)
        self.assertIn("inactive_to=300000", legacy_puts[0])
        self.assertIn("light_after_inactive_to=60000", legacy_puts[0])
        self.assertIn("max_idle_to=21600000", legacy_puts[0])
        self.assertTrue(
            any("legacy device_idle_constants" in o for o in self.outputs)
        )

    def test_apply_experimental_includes_doze_when_writable(self):
        self.runner.responses[
            "adb -s test_device shell device_config get device_idle inactive_to"
        ] = CommandResult(0, "null\n", "")
        self.runner.responses[
            "adb -s test_device shell cmd netpolicy get restrict-background"
        ] = CommandResult(0, "Restrict background status: disabled\n", "")
        self.app.apply_experimental_optimizations()
        self.assertTrue(
            any(
                c.startswith("device_config put device_idle inactive_to 300000")
                for c in self.runner.commands
            )
        )
        self.assertTrue(
            any(
                c.startswith(
                    "device_config put activity_manager bg_auto_restrict_abusive_apps 1"
                )
                for c in self.runner.commands
            )
        )

    def test_restore_recovers_when_write_blocked_but_value_matches(self):
        self.app.store.data["settings"]["global/test_key"] = {
            "namespace": "global",
            "key": "test_key",
            "value": "42",
        }
        self.app.store.data["device_config"]["activity_manager/foo"] = {
            "namespace": "activity_manager",
            "key": "foo",
            "value": None,
        }
        self.runner.responses[
            "adb -s test_device shell settings put global test_key 42"
        ] = CommandResult(255, "", "denied")
        self.runner.responses[
            "adb -s test_device shell settings get global test_key"
        ] = CommandResult(0, "42\n", "")
        self.runner.responses[
            "adb -s test_device shell device_config delete activity_manager foo"
        ] = CommandResult(255, SECURITY_EXCEPTION_OUTPUT, "")
        self.runner.responses[
            "adb -s test_device shell device_config get activity_manager foo"
        ] = CommandResult(0, "null\n", "")
        messages = self.app.revert_saved_state()
        self.assertTrue(
            any(
                "Already at saved value: setting global/test_key" in m
                for m in messages
            )
        )
        self.assertTrue(
            any(
                "Already at saved value: device_config activity_manager/foo" in m
                for m in messages
            )
        )
        self.assertFalse(self.app.store.has_entries())

    def test_restore_still_fails_when_write_blocked_and_value_differs(self):
        self.app.store.data["settings"]["global/test_key"] = {
            "namespace": "global",
            "key": "test_key",
            "value": "42",
        }
        self.runner.responses[
            "adb -s test_device shell settings put global test_key 42"
        ] = CommandResult(255, "", "denied")
        self.runner.responses[
            "adb -s test_device shell settings get global test_key"
        ] = CommandResult(0, "7\n", "")
        messages = self.app.revert_saved_state()
        self.assertTrue(
            any(
                "Failed to restore setting global/test_key" in m
                for m in messages
            )
        )
        self.assertTrue(self.app.store.has_entries())
