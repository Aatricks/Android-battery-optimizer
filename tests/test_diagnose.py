import unittest
from unittest.mock import MagicMock
from android_battery_optimizer.adb import AdbClient, CommandRunner, CommandResult, CommandError
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.diagnose import (
    Diagnoser,
    parse_alarm_wakeups,
    parse_battery_summary,
    parse_duration_ms,
    parse_wakelock_ms,
)

class FakeRunner(CommandRunner):
    def __init__(self):
        self.commands = []
        self.responses = {}

    def run(self, args, input_data=None, timeout=None):
        cmd_str = " ".join(map(str, args))
        self.commands.append(cmd_str)
        if cmd_str in self.responses:
            res = self.responses[cmd_str]
            if isinstance(res, Exception):
                raise res
            return res
        return CommandResult(0, "", "")

    def which(self, name):
        return "/usr/bin/" + name

class TestDiagnose(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.client = AdbClient(self.runner, serial="test_device", output=lambda x: None)
        
        # Setup basic responses
        self.runner.responses["adb -s test_device shell getprop ro.product.brand"] = CommandResult(0, "Google", "")
        self.runner.responses["adb -s test_device shell pm list packages -3"] = CommandResult(0, "package:com.example.app\npackage:com.example.other", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "Uid mode: RUN_ANY_IN_BACKGROUND: allow", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.other"] = CommandResult(0, "frequent", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.other RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "Uid mode: RUN_ANY_IN_BACKGROUND: ignore", "")

        self.runner.responses["adb -s test_device shell dumpsys batterystats --charged"] = CommandResult(0, "com.example.app", "")
        self.runner.responses["adb -s test_device shell dumpsys deviceidle"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0, "lastTimeUsed=\"100\"", "")
        self.runner.responses["adb -s test_device shell dumpsys alarm"] = CommandResult(0, "com.example.app", "")
        self.runner.responses["adb -s test_device shell dumpsys jobscheduler"] = CommandResult(0, "com.example.app", "")

    def test_diagnose_does_not_mutate(self):
        diagnoser = Diagnoser(self.client)
        diagnoser.run(third_party_only=True)
        
        # Check no mutate commands
        for cmd in self.runner.commands:
            self.assertNotIn("put", cmd)
            self.assertNotIn("set", cmd)
            self.assertNotIn("disable", cmd)

    def test_diagnose_continues_on_failure(self):
        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --checkin"
        ] = CommandError("timeout", CommandResult(-1, "", ""))
        
        diagnoser = Diagnoser(self.client)
        report = diagnoser.run(third_party_only=True)
        
        # Should continue and complete
        self.assertEqual(len(report["packages"]), 2)
        
        # Emit warning
        self.assertTrue(any("batterystats --checkin failed" in w for w in report["warnings"]))

    def test_diagnose_emits_json_with_warnings(self):
        # Make one command fail
        self.runner.responses[
            "adb -s test_device shell dumpsys batterystats --checkin"
        ] = CommandError("timeout", CommandResult(-1, "", ""))
        
        diagnoser = Diagnoser(self.client)
        report = diagnoser.run(third_party_only=True)
        
        self.assertIn("device", report)
        self.assertIn("warnings", report)
        self.assertIn("packages", report)
        
        self.assertTrue(len(report["warnings"]) > 0)
        
        pkg = report["packages"][0]
        self.assertIn("recommendation", pkg)
        self.assertIn("signals", pkg)

    def test_diagnose_package_boundary_no_false_positive(self):
        diagnoser = Diagnoser(self.client)
        # Check that 'com.foo' does not match 'com.foobar' or 'com.foo.bar'
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com.foobar"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com.foo.bar"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "a.com.foo"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com_foo"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com.foo1"))
        
    def test_diagnose_exact_package_signal_detected(self):
        diagnoser = Diagnoser(self.client)
        # Check valid boundaries
        self.assertTrue(diagnoser._has_package_signal("com.foo", "com.foo"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", " com.foo "))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "\ncom.foo\n"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "uid:com.foo,"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "package=com.foo "))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "package:com.foo"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", '"com.foo"'))

    def test_wakelock_checkin_microseconds_are_converted_to_milliseconds(self):
        output = (
            "9,0,i,uid,10100,com.example.app\n"
            "9,10100,l,wl,example,0,f,0,3600000000,p,1\n"
        )

        self.assertEqual(parse_wakelock_ms(output), {"com.example.app": 3600000})

    def test_checkin_wakeup_alarms_are_mapped_to_package(self):
        output = (
            "9,0,i,uid,10100,com.example.app\n"
            '9,10100,l,wua,"alarm,tag",70\n'
            '9,10100,l,wua,"other",30\n'
        )

        self.assertEqual(parse_alarm_wakeups(output), {"com.example.app": 100})

    def test_shared_uid_wakelock_is_not_attributed_to_each_package(self):
        output = (
            "9,0,i,uid,10100,com.example.one\n"
            "9,0,i,uid,10100,com.example.two\n"
            "9,10100,l,wl,example,0,f,0,3600000000,p,1\n"
        )

        self.assertEqual(parse_wakelock_ms(output), {})

    def test_aggregated_wakelock_milliseconds_take_precedence(self):
        output = (
            "9,0,i,uid,10337,com.spotify.music\n"
            "9,10337,l,awl,4031305,3882471\n"
            "9,10337,l,wl,AudioMix,0,f,0,0,0,0,2268283,p,1\n"
        )

        self.assertEqual(
            parse_wakelock_ms(output),
            {"com.spotify.music": 4031305},
        )

    def test_duration_parser_accepts_multi_day_windows(self):
        self.assertEqual(
            parse_duration_ms("1d 2h 3m 4s 5ms"),
            93784005,
        )

    def test_parse_battery_summary_extracts_window_drain_and_components(self):
        output = (
            "  Time on battery: 2h 30m 0s 0ms (100.0%) realtime\n"
            "  Time on battery screen off: 1h 0m 0s 0ms (40.0%) realtime\n"
            "    Amount discharged while screen on: 12\n"
            "    Amount discharged while screen off: 3\n"
            "    Global\n"
            "    screen: 141 apps: 141\n"
            "    cpu: 233 apps: 231 duration: 1h 53m 50s 613ms\n"
            "    bluetooth: 10.6 apps: 10.4\n"
            "    mobile_radio: 7.87 apps: 2.54\n"
            "    sensors: 6.47 apps: 6.47\n"
            "    wifi: 2.30 apps: 2.13\n"
            "    wakelock: 6.46 apps: 6.46 duration: 13m 10s 424ms\n"
        )

        summary = parse_battery_summary(output)

        self.assertEqual(summary["observation_ms"], 9000000)
        self.assertEqual(summary["screen_off_ms"], 3600000)
        self.assertEqual(summary["screen_on_drain_percent"], 12)
        self.assertEqual(summary["screen_off_drain_percent"], 3)
        self.assertEqual(summary["power_mah"]["screen"], 141.0)
        self.assertEqual(summary["power_mah"]["cpu"], 233.0)
        self.assertEqual(summary["power_mah"]["bluetooth"], 10.6)

    def test_short_observation_window_never_recommends_restriction(self):
        diagnoser = Diagnoser(self.client)

        recommendation, reason = diagnoser._recommend(
            "active",
            "allow",
            {
                "alarm_wakeups": 5000,
                "wakelock_partial_ms": 7200000,
                "jobs_registered": 500,
                "observation_ms": 3599999,
            },
        )

        self.assertEqual(recommendation, "keep")
        self.assertIn("Insufficient observation window", reason)
