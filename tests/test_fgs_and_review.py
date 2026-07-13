import argparse
import shutil
import tempfile
import unittest
from pathlib import Path

from android_battery_optimizer.adb import AdbClient, CommandResult, CommandRunner
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI
from android_battery_optimizer.diagnose import Diagnoser, parse_fgs_ms


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


class TestFgsAndReview(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        self.client = AdbClient(
            self.runner, serial="test_device", output=lambda x: None
        )
        self.app = BatteryOptimizerApp(self.client, self.test_dir)
        self.app.recorder.verify = False
        self.cli_outputs = []
        self.cli = BatteryOptimizerCLI(
            self.app, output=self.cli_outputs.append
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def setup_default_responses(self):
        self.runner.responses["adb devices"] = CommandResult(
            0, "List of devices attached\ntest_device\tdevice\n", ""
        )
        self.runner.responses[
            "adb -s test_device shell getprop ro.product.brand"
        ] = CommandResult(0, "Google", "")
        self.runner.responses[
            "adb -s test_device shell getprop ro.build.version.sdk"
        ] = CommandResult(0, "30", "")
        self.runner.responses[
            "adb -s test_device shell pm list packages"
        ] = CommandResult(
            0, "package:com.example.app\npackage:com.example.lowact", ""
        )
        self.runner.responses[
            "adb -s test_device shell pm list packages -3"
        ] = CommandResult(
            0, "package:com.example.app\npackage:com.example.lowact", ""
        )

        self.runner.responses[
            "adb -s test_device shell pm list packages --user 0 -d"
        ] = CommandResult(0, "", "")
        self.runner.responses[
            "adb -s test_device shell pm list packages --user 0 -e"
        ] = CommandResult(
            0, "package:com.example.app\npackage:com.example.lowact", ""
        )
        self.runner.responses[
            "adb -s test_device shell dumpsys appops"
        ] = CommandResult(
            0,
            "Package com.example.app:\n"
            "  RUN_ANY_IN_BACKGROUND: allow\n"
            "Package com.example.lowact:\n"
            "  RUN_ANY_IN_BACKGROUND: allow",
            "",
        )
        self.runner.responses[
            "adb -s test_device shell cmd appops help"
        ] = CommandResult(0, "help", "")
        self.runner.responses[
            "adb -s test_device shell am get-standby-bucket android"
        ] = CommandResult(0, "10", "")

        self.runner.responses[
            "adb -s test_device shell am get-standby-bucket com.example.app"
        ] = CommandResult(0, "active", "")
        self.runner.responses[
            "adb -s test_device shell cmd appops get com.example.app "
            "RUN_ANY_IN_BACKGROUND"
        ] = CommandResult(0, "allow", "")
        self.runner.responses[
            "adb -s test_device shell cmd appops get com.example.app WAKE_LOCK"
        ] = CommandResult(0, "default", "")

        self.runner.responses[
            "adb -s test_device shell am get-standby-bucket com.example.lowact"
        ] = CommandResult(0, "active", "")
        self.runner.responses[
            "adb -s test_device shell cmd appops get com.example.lowact "
            "RUN_ANY_IN_BACKGROUND"
        ] = CommandResult(0, "allow", "")
        self.runner.responses[
            "adb -s test_device shell cmd appops get com.example.lowact "
            "WAKE_LOCK"
        ] = CommandResult(0, "default", "")

        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --charged"
        ] = CommandResult(
            0, "  Time on battery: 2h 0m 0s 0ms (100.0%) realtime\n", ""
        )
        self.runner.responses[
            "adb -s test_device shell cmd deviceidle whitelist"
        ] = CommandResult(0, "", "")
        self.runner.responses[
            "adb -s test_device shell dumpsys usagestats"
        ] = CommandResult(0, "", "")
        self.runner.responses[
            "adb -s test_device shell dumpsys jobscheduler"
        ] = CommandResult(0, "", "")
        self.runner.responses[
            "adb -s test_device shell cmd package resolve-activity "
            "-a android.intent.action.MAIN -c android.intent.category.HOME"
        ] = CommandResult(0, "", "")
        self.runner.responses[
            "adb -s test_device shell telecom get-default-dialer"
        ] = CommandResult(0, "", "")

    def test_successful_fgs_parsing(self):
        output = (
            "9,0,i,uid,10100,com.example.app\n"
            "9,10100,l,fgs,600000,5\n"
        )
        res = parse_fgs_ms(output)
        self.assertEqual(res, {"com.example.app": 600000})

    def test_omission_for_shared_uid(self):
        output = (
            "9,0,i,uid,10100,com.example.one\n"
            "9,0,i,uid,10100,com.example.two\n"
            "9,10100,l,fgs,600000,5\n"
        )
        res = parse_fgs_ms(output)
        self.assertEqual(res, {})

    def test_run_report_signal_plumbing(self):
        self.setup_default_responses()
        bs_out = (
            "9,0,i,uid,10100,com.example.app\n"
            "9,10100,l,fgs,350000,3\n"
        )
        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --checkin"
        ] = CommandResult(0, bs_out, "")

        diagnoser = Diagnoser(self.client)
        report = diagnoser.run(third_party_only=True)

        pkg_report = next(
            p for p in report["packages"] if p["package"] == "com.example.app"
        )
        self.assertEqual(
            pkg_report["signals"]["foreground_service_ms"], 350000
        )

    def test_ten_minute_review_threshold_and_behavior(self):
        self.setup_default_responses()
        bs_out = (
            "9,0,i,uid,10100,com.example.app\n"
            "9,10100,l,wua,alarm,1500\n"
            "9,10100,l,fgs,600000,5\n"
            "9,0,i,uid,10101,com.example.lowact\n"
            "9,10101,l,fgs,660000,2\n"
        )
        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --checkin"
        ] = CommandResult(0, bs_out, "")

        diagnoser = Diagnoser(self.client)
        report = diagnoser.run(third_party_only=True)

        pkg_app = next(
            p for p in report["packages"] if p["package"] == "com.example.app"
        )
        self.assertEqual(pkg_app["recommendation"], "review")
        self.assertIn("High foreground service usage", pkg_app["reason"])
        self.assertIn("manual review recommended", pkg_app["reason"])

        pkg_lowact = next(
            p
            for p in report["packages"]
            if p["package"] == "com.example.lowact"
        )
        self.assertEqual(pkg_lowact["recommendation"], "keep")
        self.assertEqual(
            pkg_lowact["reason"], "Minimal background activity detected"
        )

    def test_smart_restrict_no_writes_for_review_in_both_modes(self):
        self.setup_default_responses()
        bs_out = (
            "9,0,i,uid,10100,com.example.app\n"
            "9,10100,l,wua,alarm,1500\n"
            "9,10100,l,fgs,600000,5\n"
        )
        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --checkin"
        ] = CommandResult(0, bs_out, "")

        self.runner.commands.clear()
        res_balanced = self.app.smart_restrict(aggressive=False)

        kept_pkgs = [k["package"] for k in res_balanced["kept"]]
        self.assertIn("com.example.app", kept_pkgs)

        for cmd in self.runner.commands:
            self.assertNotIn("appops set com.example.app", cmd)
            self.assertNotIn("am set-standby-bucket com.example.app", cmd)

        self.runner.commands.clear()
        res_aggressive = self.app.smart_restrict(aggressive=True)

        kept_pkgs_agg = [k["package"] for k in res_aggressive["kept"]]
        self.assertIn("com.example.app", kept_pkgs_agg)

        for cmd in self.runner.commands:
            self.assertNotIn("appops set com.example.app", cmd)
            self.assertNotIn("am set-standby-bucket com.example.app", cmd)

    def test_cli_summary_output(self):
        self.setup_default_responses()

        summary_out = (
            "  Time on battery: 1h 30m 0s 0ms (100.0%) realtime\n"
            "  Time on battery screen off: 0h 45m 0s 0ms (50.0%) realtime\n"
            "    Amount discharged while screen on: 10\n"
            "    Amount discharged while screen off: 4\n"
        )
        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --charged"
        ] = CommandResult(0, summary_out, "")

        args = argparse.Namespace(
            command="diagnose", third_party_only=True, output=None
        )
        self.cli.run_command(args)

        out = "\n".join(self.cli_outputs)

        self.assertIn("Battery Summary:", out)
        self.assertIn("Observation duration: 1.50 hours", out)
        self.assertIn("Screen-off duration: 0.75 hours", out)
        self.assertIn("Screen-on drain: 10%", out)
        self.assertIn("Screen-off drain: 4%", out)
        self.assertIn("Diagnosis Summary:", out)

        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --charged"
        ] = CommandResult(0, "", "")
        self.cli_outputs.clear()

        self.cli.run_command(args)
        out_missing = "\n".join(self.cli_outputs)
        self.assertIn("Observation duration: unavailable", out_missing)
        self.assertIn("Screen-off duration: unavailable", out_missing)
        self.assertIn("Screen-on drain: unavailable", out_missing)
        self.assertIn("Screen-off drain: unavailable", out_missing)
