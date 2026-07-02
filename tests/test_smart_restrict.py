import unittest
import tempfile
import shutil
import argparse
from pathlib import Path
from android_battery_optimizer.adb import AdbClient, CommandRunner, CommandResult, CommandError
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI

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

class TestSmartRestrict(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        self.client = AdbClient(self.runner, serial="test_device", output=lambda x: None)
        self.app = BatteryOptimizerApp(self.client, self.test_dir)
        self.app.recorder.verify = False
        self.cli = BatteryOptimizerCLI(self.app, output=lambda x: None)

        self.setup_default_responses()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def setup_default_responses(self):
        # Base info
        self.runner.responses["adb -s test_device shell getprop ro.product.brand"] = CommandResult(0, "Google", "")
        self.runner.responses["adb -s test_device shell getprop ro.build.version.sdk"] = CommandResult(0, "30", "")
        
        # Package list
        self.runner.responses["adb -s test_device shell pm list packages"] = CommandResult(0, "package:com.example.app\npackage:com.example.aggressive\npackage:com.critical.launcher", "")
        self.runner.responses["adb -s test_device shell pm list packages -3"] = CommandResult(0, "package:com.example.app\npackage:com.example.aggressive\npackage:com.critical.launcher", "")
        
        # Prefetch dependencies
        self.runner.responses["adb -s test_device shell pm list packages --user 0 -d"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell pm list packages --user 0 -e"] = CommandResult(0, "package:com.example.app\npackage:com.example.aggressive\npackage:com.critical.launcher", "")
        self.runner.responses["adb -s test_device shell dumpsys appops"] = CommandResult(0, "Package com.example.app:\n  RUN_ANY_IN_BACKGROUND: allow\nPackage com.example.aggressive:\n  RUN_ANY_IN_BACKGROUND: allow\nPackage com.critical.launcher:\n  RUN_ANY_IN_BACKGROUND: allow", "")
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0, "package=com.example.app bucket=10\npackage=com.example.aggressive bucket=10\npackage=com.critical.launcher bucket=10", "")
        
        # Supports checks
        self.runner.responses["adb -s test_device shell cmd appops help"] = CommandResult(0, "help", "")
        self.runner.responses["adb -s test_device shell am get-standby-bucket android"] = CommandResult(0, "10", "")
        
        # Diagnostics dependencies
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.app WAKE_LOCK"] = CommandResult(0, "default", "")
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.aggressive"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.aggressive RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.aggressive WAKE_LOCK"] = CommandResult(0, "default", "")
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.critical.launcher"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.critical.launcher RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.critical.launcher WAKE_LOCK"] = CommandResult(0, "default", "")
        
        # Make `com.example.app` restrict and `com.example.aggressive` aggressive_restrict
        alarm_out = (
            "    +1m20s000ms running, 150 wakeups, 150 alarms: u0a123:com.example.app\n"
            "    +2m30s000ms running, 1200 wakeups, 1200 alarms: u0a274:com.example.aggressive\n"
        )
        self.runner.responses["adb -s test_device shell dumpsys alarm"] = CommandResult(0, alarm_out, "")
        
        # Make `com.example.aggressive` aggressive_restrict using jobs and checkin
        job_out = "JOB u0a274:com.example.aggressive\n"
        self.runner.responses["adb -s test_device shell dumpsys jobscheduler"] = CommandResult(0, job_out, "")
        
        bs_out = (
            "9,0,i,uid,10001,com.example.app\n"
            "9,10001,l,wl,mywakelock,0,f,0,700000,p,1\n"
            "9,0,i,uid,10002,com.example.aggressive\n"
            "9,10002,l,wl,mywakelock,0,f,0,3700000,p,1\n"
        )
        self.runner.responses["adb -s test_device shell dumpsys batterystats --checkin"] = CommandResult(0, bs_out, "")
        self.runner.responses["adb -s test_device shell cmd deviceidle whitelist"] = CommandResult(0, "", "")
        
        # Critical apps setup
        self.runner.responses["adb -s test_device shell cmd package resolve-activity -a android.intent.action.MAIN -c android.intent.category.HOME"] = CommandResult(0, "packageName=com.critical.launcher", "")
        self.runner.responses["adb -s test_device shell telecom get-default-dialer"] = CommandResult(0, "", "")

    def test_skips_whitelist(self):
        self.app.save_whitelist(["com.example.app"])
        result = self.app.smart_restrict(aggressive=False)
        skipped = [s["package"] for s in result.get("skipped", [])]
        self.assertIn("com.example.app", skipped)
        
        # Verify appops wasn't called for it
        for cmd in self.runner.commands:
            if "appops set com.example.app" in cmd:
                self.fail("Whitelisted app was modified")

    def test_skips_critical_packages(self):
        result = self.app.smart_restrict(aggressive=False)
        skipped = [s["package"] for s in result["skipped"]]
        self.assertIn("com.critical.launcher", skipped)
        
        for cmd in self.runner.commands:
            if "appops set com.critical.launcher" in cmd:
                self.fail("Critical app was modified")

    def test_balanced_mode(self):
        self.app.smart_restrict(aggressive=False)
        
        app_restricted = False
        agg_restricted = False
        
        for cmd in self.runner.commands:
            if "am set-standby-bucket com.example.app rare" in cmd:
                app_restricted = True
            if "am set-standby-bucket com.example.aggressive rare" in cmd:
                agg_restricted = True
                
        if not app_restricted or not agg_restricted:
            print(f"FAILED BALANCED MODE. Commands: {self.runner.commands}")
            
        self.assertTrue(app_restricted)
        self.assertTrue(agg_restricted)

    def test_aggressive_mode(self):
        self.app.smart_restrict(aggressive=True)
        
        agg_restricted = False
        
        for cmd in self.runner.commands:
            if "am set-standby-bucket com.example.aggressive restricted" in cmd:
                agg_restricted = True
                
        self.assertTrue(agg_restricted)

    def test_dry_run_does_not_write_state(self):
        client = AdbClient(self.runner, serial="test_device", dry_run=True, output=lambda x: None)
        app = BatteryOptimizerApp(client, self.test_dir)
        
        app.smart_restrict(aggressive=False)
        
        state_file = self.test_dir / "devices" / "test_device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_mutations_are_snapshotted(self):
        self.app.smart_restrict(aggressive=False)
        
        state_file = self.test_dir / "devices" / "test_device" / "state.json"
        self.assertTrue(state_file.exists())
        
        has_snapshot = self.app.store.has_entries()
        self.assertTrue(has_snapshot)

    def test_refuses_without_yes(self):
        args = argparse.Namespace(command="smart-restrict", yes=False, dry_run=False, aggressive=False, min_last_used_days=None)
        res = self.cli.run_command(args)
        self.assertEqual(res, 1)
