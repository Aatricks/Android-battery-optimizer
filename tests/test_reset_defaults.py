import shutil
import tempfile
import unittest
from pathlib import Path

from android_battery_optimizer.adb import AdbClient, CommandResult
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI, parse_args
from tests.test_smart_restrict import FakeRunner

P = "adb -s dev shell "


class ResetDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        r = self.runner.responses
        r[P + "pm list packages"] = CommandResult(0, "package:com.mail\npackage:com.ok", "")
        r[P + "dumpsys appops"] = CommandResult(
            0,
            "Package com.mail:\n  RUN_ANY_IN_BACKGROUND: ignore\n"
            "Package com.ok:\n  RUN_ANY_IN_BACKGROUND: allow",
            "",
        )
        r[P + "dumpsys usagestats"] = CommandResult(
            0, "package=com.mail u=0 bucket=40\npackage=com.ok u=0 bucket=10", ""
        )
        r[P + "settings list global"] = CommandResult(
            0, "device_idle_constants=inactive_to=300000\nlow_power=1", ""
        )
        r[P + "settings list system"] = CommandResult(0, "", "")
        r[P + "settings list secure"] = CommandResult(0, "", "")
        r[P + "cmd netpolicy get restrict-background"] = CommandResult(
            0, "Restrict background status: enabled", ""
        )
        client = AdbClient(self.runner, serial="dev", output=lambda _: None)
        self.app = BatteryOptimizerApp(client, self.dir)
        self.app.recorder.verify = False

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_reset_clears_tuning_restrictions_and_is_revertible(self):
        changes = self.app.reset_to_defaults()
        cmds = "\n".join(self.runner.commands)
        self.assertIn("settings delete global device_idle_constants", cmds)
        self.assertNotIn("settings delete global battery_saver_constants", cmds)
        self.assertNotIn("low_power", cmds)
        self.assertIn("cmd appops set com.mail RUN_ANY_IN_BACKGROUND default", cmds)
        self.assertNotIn("appops set com.ok", cmds)
        self.assertIn("am set-standby-bucket com.mail active", cmds)
        self.assertIn("cmd netpolicy set restrict-background false", cmds)
        self.assertEqual(len(changes), 4)

        # Prior values were snapshotted, so revert restores them.
        self.runner.commands.clear()
        self.app.revert_saved_state()
        cmds = "\n".join(self.runner.commands)
        self.assertIn("device_idle_constants inactive_to=300000", cmds)
        self.assertIn("cmd appops set com.mail RUN_ANY_IN_BACKGROUND ignore", cmds)
        self.assertIn("am set-standby-bucket com.mail 40", cmds)
        self.assertIn("cmd netpolicy set restrict-background true", cmds)

    def test_cli_requires_yes(self):
        out = []
        cli = BatteryOptimizerCLI(self.app, output=out.append)
        self.assertEqual(cli.run_command(parse_args(["reset-defaults"])), 1)
        self.assertNotIn("settings delete", "\n".join(self.runner.commands))


class RefusedBucketTests(ResetDefaultsTests):
    test_reset_clears_tuning_restrictions_and_is_revertible = None
    test_cli_requires_yes = None

    def test_refused_bucket_is_reported_and_rest_still_applies(self):
        from unittest.mock import patch

        from android_battery_optimizer.verification import VerificationError

        # Phone accepts the command but keeps the app restricted (Samsung).
        with patch.object(
            self.app.recorder, "set_standby_bucket", side_effect=VerificationError("x")
        ):
            changes = self.app.reset_to_defaults()
        self.assertTrue(any("com.mail: bucket rare kept" in c for c in changes))
        self.assertIn("settings delete global device_idle_constants", "\n".join(self.runner.commands))


class RestrictAppsProtectionTests(unittest.TestCase):
    def test_sms_role_holder_is_never_restricted(self):
        runner = FakeRunner()
        r = runner.responses
        pkgs = "package:org.thoughtcrime.securesms\npackage:com.game"
        r[P + "pm list packages"] = CommandResult(0, pkgs, "")
        r[P + "pm list packages -3"] = CommandResult(0, pkgs, "")
        r[P + "cmd role get-role-holders android.app.role.SMS"] = CommandResult(
            0, "org.thoughtcrime.securesms", ""
        )
        r[P + "settings get secure sms_default_application"] = CommandResult(0, "null", "")
        r[P + "getprop ro.build.version.sdk"] = CommandResult(0, "36", "")
        r[P + "am get-standby-bucket android"] = CommandResult(0, "5", "")
        r[P + "dumpsys appops"] = CommandResult(
            0,
            "Package org.thoughtcrime.securesms:\n  RUN_ANY_IN_BACKGROUND: allow\n"
            "Package com.game:\n  RUN_ANY_IN_BACKGROUND: allow",
            "",
        )
        r[P + "dumpsys usagestats"] = CommandResult(
            0, "package=org.thoughtcrime.securesms u=0 bucket=10\npackage=com.game u=0 bucket=10", ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = AdbClient(runner, serial="dev", output=lambda _: None)
            app = BatteryOptimizerApp(client, Path(tmp))
            app.recorder.verify = False
            res = app.restrict_background_apps(level="ignore")
            preview = app.preview_restrict_background_apps(level="ignore")
        cmds = "\n".join(runner.commands)
        self.assertIn("cmd appops set com.game RUN_ANY_IN_BACKGROUND ignore", cmds)
        self.assertNotIn("appops set org.thoughtcrime.securesms", cmds)
        self.assertEqual(res["skipped_protected"], ["org.thoughtcrime.securesms (sms_role)"])
        self.assertEqual(preview["would_change"], ["com.game"])


class ChargingBucketTests(unittest.TestCase):
    """While charging, `am get-standby-bucket` reports 5 for every app."""

    def test_verify_reads_usagestats_when_am_says_exempted(self):
        from android_battery_optimizer.verification import (
            VerificationError,
            verify_standby_bucket,
        )

        runner = FakeRunner()
        runner.responses[P + "am get-standby-bucket com.mail"] = CommandResult(0, "5", "")
        runner.responses[P + "dumpsys usagestats"] = CommandResult(
            0, "package=com.mail u=0 bucket=10 reason=u", ""
        )
        client = AdbClient(runner, serial="dev", output=lambda _: None)
        verify_standby_bucket(client, "com.mail", "active")
        with self.assertRaises(VerificationError):
            verify_standby_bucket(client, "com.mail", "rare")


if __name__ == "__main__":
    unittest.main()
