import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock
import json
from io import BytesIO

from android_battery_optimizer.adb import AdbClient, CommandRunner, CommandResult
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.android import parse_battery_dumpsys
from android_battery_optimizer.webgui import WebApi, GuiRequestHandler


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


class DummyServer:
    def __init__(self, web_api):
        self.web_api = web_api


class TestWebGui(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        self.client = AdbClient(self.runner, serial="test_device", output=lambda x: None)
        self.app = BatteryOptimizerApp(self.client, self.test_dir)
        self.app.recorder.verify = False
        self.token = "test_token_12345"
        self.web_api = WebApi(self.app, self.token)
        self.setup_default_responses()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def setup_default_responses(self):
        self.runner.responses[
            "adb -s test_device shell getprop ro.product.brand"
        ] = CommandResult(0, "Google", "")
        self.runner.responses[
            "adb -s test_device shell getprop ro.build.version.sdk"
        ] = CommandResult(0, "30", "")
        self.runner.responses[
            "adb -s test_device shell pm list packages"
        ] = CommandResult(0, "package:com.example.app\npackage:com.critical.launcher", "")
        self.runner.responses[
            "adb -s test_device shell pm list packages -3"
        ] = CommandResult(0, "package:com.example.app\npackage:com.critical.launcher", "")
        self.runner.responses[
            "adb -s test_device shell pm list packages --user 0 -d"
        ] = CommandResult(0, "", "")
        self.runner.responses[
            "adb -s test_device shell pm list packages --user 0 -e"
        ] = CommandResult(0, "package:com.example.app\npackage:com.critical.launcher", "")
        self.runner.responses[
            "adb -s test_device shell dumpsys appops"
        ] = CommandResult(0, "Package com.example.app:\n  RUN_ANY_IN_BACKGROUND: allow", "")
        self.runner.responses[
            "adb -s test_device shell dumpsys usagestats"
        ] = CommandResult(0, "package=com.example.app bucket=10", "")
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
            "adb -s test_device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND"
        ] = CommandResult(0, "allow", "")
        self.runner.responses[
            "adb -s test_device shell cmd appops get com.example.app WAKE_LOCK"
        ] = CommandResult(0, "default", "")

    def _make_handler(self, method, path, headers, body=b""):
        handler = GuiRequestHandler.__new__(GuiRequestHandler)
        handler.request = MagicMock()
        handler.client_address = ("127.0.0.1", 12345)
        handler.server = DummyServer(self.web_api)
        handler.headers = headers
        handler.path = path
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()

        responses = []
        def send_response(code, message=None):
            responses.append(code)
        handler.send_response = send_response
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        return handler, responses

    def test_401_missing_wrong_token(self):
        headers = {"Host": "127.0.0.1"}
        handler, codes = self._make_handler("GET", "/?token=wrong", headers)
        handler.do_GET()
        self.assertEqual(codes[0], 401)

        headers = {"Host": "127.0.0.1", "X-ABO-Token": "wrong"}
        handler, codes = self._make_handler("GET", "/api/status", headers)
        handler.do_GET()
        self.assertEqual(codes[0], 401)

        headers = {"Host": "127.0.0.1", "X-ABO-Token": "wrong"}
        handler, codes = self._make_handler("POST", "/api/smart-restrict/apply", headers)
        handler.do_POST()
        self.assertEqual(codes[0], 401)

    def test_404_unknown_route(self):
        code, res = self.web_api.dispatch("GET", "/api/unknown", {})
        self.assertEqual(code, 404)

    def test_405_get_on_mutating_route(self):
        code, res = self.web_api.dispatch("GET", "/api/smart-restrict/apply", {})
        self.assertEqual(code, 405)

    def test_api_status_shape(self):
        self.client.dry_run = True
        code, res = self.web_api.dispatch("GET", "/api/status", {})
        self.assertEqual(code, 200)
        body = res["result"]
        self.assertTrue(res["ok"])
        self.assertEqual(body["serial"], "test_device")
        self.assertTrue(body["dry_run"])
        self.assertIn("Google", body["device_info"])
        self.assertIn("battery_status", body)

    def test_confirm_gating(self):
        code, res = self.web_api.dispatch("POST", "/api/smart-restrict/apply", {})
        self.assertEqual(code, 400)
        self.assertEqual(res["error"], "Confirmation required.")

        code2, res2 = self.web_api.dispatch(
            "POST", "/api/smart-restrict/apply", {"confirm": True}
        )
        self.assertEqual(code2, 200)

    def test_value_error_to_400(self):
        code, res = self.web_api.dispatch(
            "POST", "/api/apply/samsung-experimental", {"confirm": True}
        )
        self.assertEqual(code, 400)
        self.assertIn("is not Samsung", res["error"])

    def test_busy_lock(self):
        self.web_api._busy.acquire()
        try:
            code, res = self.web_api.dispatch("GET", "/api/status", {})
            self.assertEqual(code, 409)
            self.assertEqual(res["error"], "Another operation is in progress.")
        finally:
            self.web_api._busy.release()

    def test_preview_mutating_commands(self):
        self.runner.commands.clear()
        code, res = self.web_api.dispatch("POST", "/api/smart-restrict/preview", {})
        self.assertEqual(code, 200)
        self.assertIn("would_restrict", res["result"])
        for cmd in self.runner.commands:
            self.assertNotIn("cmd appops set", cmd)
            self.assertNotIn("am set-standby-bucket", cmd)

    def test_whitelist_add_remove(self):
        code, res = self.web_api.dispatch("GET", "/api/whitelist", {})
        self.assertEqual(code, 200)
        self.assertEqual(res["result"]["whitelist"], [])

        code2, res2 = self.web_api.dispatch(
            "POST", "/api/whitelist", {"action": "add", "package": "com.example.app"}
        )
        self.assertEqual(code2, 200)
        self.assertTrue(res2["result"]["changed"])

        code3, res3 = self.web_api.dispatch("GET", "/api/whitelist", {})
        self.assertEqual(code3, 200)
        self.assertEqual(res3["result"]["whitelist"], ["com.example.app"])

        code4, res4 = self.web_api.dispatch(
            "POST", "/api/whitelist", {"action": "remove", "package": "com.example.app"}
        )
        self.assertEqual(code4, 200)
        self.assertTrue(res4["result"]["changed"])

        code5, res5 = self.web_api.dispatch("GET", "/api/whitelist", {})
        self.assertEqual(code5, 200)
        self.assertEqual(res5["result"]["whitelist"], [])

    def test_dry_run_message_capture(self):
        self.client.dry_run = True
        code, res = self.web_api.dispatch("POST", "/api/apply/safe", {})
        self.assertEqual(code, 200)
        found_dry = any("[dry-run]" in m for m in res["messages"])
        self.assertTrue(found_dry)

    def test_client_output_restored(self):
        old_output = self.client.output
        self.web_api.dispatch("POST", "/api/apply/samsung-experimental", {"confirm": True})
        self.assertEqual(self.client.output, old_output)

    def test_parse_battery_dumpsys(self):
        raw = (
            "Current Battery Service State:\n"
            "  AC powered: false\n"
            "  USB powered: true\n"
            "  Wireless powered: false\n"
            "  Max charging current: 500000\n"
            "  Charge counter: 3100000\n"
            "  status: 2\n"
            "  health: 2\n"
            "  present: true\n"
            "  level: 85\n"
            "  scale: 100\n"
            "  temperature: 290\n"
        )
        res = parse_battery_dumpsys(raw)
        self.assertEqual(res["level"], 85)
        self.assertEqual(res["status"], 2)
        self.assertEqual(res["health"], 2)
        self.assertEqual(res["temperature"], 29.0)
        self.assertTrue(res["plugged"])
        self.assertEqual(res["charge_counter"], 3100000)

    def test_preview_smart_restrict(self):
        res = self.app.preview_smart_restrict(aggressive=False)
        self.assertIn("would_restrict", res)
        self.assertIn("skipped", res)
        self.assertIn("kept", res)

    def test_restrict_apps_preview_read_only(self):
        self.app.add_to_whitelist("com.critical.launcher")
        before = len(self.runner.commands)
        status, res = self.web_api.dispatch(
            "POST", "/api/restrict-apps/preview", {"level": "ignore"}
        )
        self.assertEqual(status, 200)
        result = res["result"]
        self.assertEqual(result["level"], "ignore")
        self.assertEqual(result["would_change"], ["com.example.app"])
        self.assertEqual(result["kept_whitelisted"], ["com.critical.launcher"])
        for cmd in self.runner.commands[before:]:
            self.assertNotIn("appops set", cmd)
            self.assertNotIn("set-standby-bucket", cmd)

    def test_samsung_features_endpoint(self):
        status, res = self.web_api.dispatch("GET", "/api/samsung-features", {})
        self.assertEqual(status, 200)
        features = res["result"]["features"]
        keys = [f["key"] for f in features]
        self.assertIn("vibration", keys)
        self.assertIn("aod", keys)
        vibration = next(f for f in features if f["key"] == "vibration")
        self.assertFalse(vibration["enabled_by_default"])
        aod = next(f for f in features if f["key"] == "aod")
        self.assertTrue(aod["enabled_by_default"])

    def test_samsung_apply_respects_exclude(self):
        self.runner.responses[
            "adb -s test_device shell getprop ro.product.brand"
        ] = CommandResult(0, "samsung", "")
        before = len(self.runner.commands)
        status, res = self.web_api.dispatch(
            "POST",
            "/api/apply/samsung-experimental",
            {"confirm": True, "exclude": ["vibration", "aod"]},
        )
        self.assertEqual(status, 200)
        new_cmds = "\n".join(self.runner.commands[before:])
        self.assertNotIn("vibration_on", new_cmds)
        self.assertNotIn("aod_mode", new_cmds)
        self.assertIn("master_motion", new_cmds)

    def test_samsung_apply_unknown_exclude_key(self):
        self.runner.responses[
            "adb -s test_device shell getprop ro.product.brand"
        ] = CommandResult(0, "samsung", "")
        status, res = self.web_api.dispatch(
            "POST",
            "/api/apply/samsung-experimental",
            {"confirm": True, "exclude": ["nonsense"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("Unknown Samsung feature keys", res["error"])

    def test_samsung_apply_all_excluded(self):
        from android_battery_optimizer.app import SAMSUNG_FEATURES
        self.runner.responses[
            "adb -s test_device shell getprop ro.product.brand"
        ] = CommandResult(0, "samsung", "")
        status, res = self.web_api.dispatch(
            "POST",
            "/api/apply/samsung-experimental",
            {"confirm": True, "exclude": list(SAMSUNG_FEATURES)},
        )
        self.assertEqual(status, 400)
        self.assertIn("nothing to apply", res["error"])


if __name__ == "__main__":
    unittest.main()
