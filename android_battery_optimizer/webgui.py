import http.server
import importlib.resources
import json
import secrets
import threading
import urllib.parse
import webbrowser
from typing import Callable, Tuple

from .adb import CommandError
from .app import BatteryOptimizerApp
from .recorder import SnapshotError, VerificationError


class DaemonThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def _load_html() -> str:
    return importlib.resources.files(__package__).joinpath("webui.html").read_text(encoding="utf-8")


class WebApi:
    def __init__(self, app: BatteryOptimizerApp, token: str) -> None:
        self.app = app
        self.token = token
        self._busy = threading.Lock()
        self.routes = {
            "/api/status": ("GET", self.handle_status),
            "/api/diagnose": ("GET", self.handle_diagnose),
            "/api/smart-restrict/preview": ("POST", self.handle_smart_restrict_preview),
            "/api/smart-restrict/apply": ("POST", self.handle_smart_restrict_apply),
            "/api/apply/safe": ("POST", self.handle_apply_safe),
            "/api/apply/experimental": ("POST", self.handle_apply_experimental),
            "/api/apply/samsung-experimental": ("POST", self.handle_apply_samsung_experimental),
            "/api/samsung-features": ("GET", self.handle_samsung_features),
            "/api/apply/120hz-endurance": ("POST", self.handle_apply_120hz_endurance),
            "/api/restrict-apps/preview": ("POST", self.handle_restrict_apps_preview),
            "/api/restrict-apps": ("POST", self.handle_restrict_apps),
            "/api/revert": ("POST", self.handle_revert),
            "/api/whitelist": (("GET", "POST"), self.handle_whitelist),
            "/api/packages": ("GET", self.handle_packages),
            "/api/doctor-state": ("GET", self.handle_doctor_state),
        }

    def dispatch(self, method: str, path: str, payload: dict) -> Tuple[int, dict]:
        parsed_path = urllib.parse.urlparse(path)
        path_str = parsed_path.path
        query_params = urllib.parse.parse_qs(parsed_path.query)

        if path_str not in self.routes:
            return 404, {"error": "Not Found"}

        allowed, handler = self.routes[path_str]
        if isinstance(allowed, str):
            allowed = (allowed,)
        if method not in allowed:
            return 405, {"error": "Method Not Allowed"}

        if not self._busy.acquire(blocking=False):
            return 409, {"error": "Another operation is in progress."}

        messages = []
        old_output = self.app.client.output
        self.app.client.output = messages.append
        try:
            status_code, result = handler(method, path_str, query_params, payload)
            if status_code < 400:
                return status_code, {"ok": True, "result": result, "messages": messages}
            if isinstance(result, dict):
                result["messages"] = messages
            return status_code, result
        except (CommandError, ValueError, SnapshotError, VerificationError) as exc:
            return 400, {"error": str(exc), "messages": messages}
        except Exception as exc:
            return 500, {"error": f"Internal Error: {exc}", "messages": messages}
        finally:
            self.app.client.output = old_output
            self._busy.release()

    def handle_status(self, method, path, query, payload):
        info = self.app.client.get_device_info_struct()
        device_info_str = f"{info.brand} {info.model} (Android {info.android_release})".strip()
        return 200, {
            "serial": self.app.client.serial,
            "device_info": device_info_str,
            "battery_status": self.app.get_battery_status(),
            "rollback_exists": self.app.store.has_entries(),
            "dry_run": self.app.client.dry_run
        }

    def handle_diagnose(self, method, path, query, payload):
        third_party = True
        if "all" in query:
            val = query["all"][0]
            if val == "1":
                third_party = False
        res = self.app.diagnose(third_party_only=third_party)
        return 200, res

    def handle_smart_restrict_preview(self, method, path, query, payload):
        aggressive = payload.get("aggressive", False)
        min_days = payload.get("min_last_used_days")
        if min_days is not None:
            try:
                min_days = int(min_days)
            except ValueError:
                return 400, {"error": "Invalid min_last_used_days"}
        res = self.app.preview_smart_restrict(
            aggressive=aggressive, min_last_used_days=min_days
        )
        return 200, res

    def handle_smart_restrict_apply(self, method, path, query, payload):
        if not payload.get("confirm"):
            return 400, {"error": "Confirmation required."}
        aggressive = payload.get("aggressive", False)
        min_days = payload.get("min_last_used_days")
        if min_days is not None:
            try:
                min_days = int(min_days)
            except ValueError:
                return 400, {"error": "Invalid min_last_used_days"}
        res = self.app.smart_restrict(aggressive=aggressive, min_last_used_days=min_days)
        return 200, res

    def handle_apply_safe(self, method, path, query, payload):
        self.app.apply_documented_safe_optimizations()
        return 200, {"ok": True}

    def handle_apply_experimental(self, method, path, query, payload):
        if not payload.get("confirm"):
            return 400, {"error": "Confirmation required."}
        self.app.apply_experimental_optimizations()
        return 200, {"ok": True}

    def handle_apply_samsung_experimental(self, method, path, query, payload):
        if not payload.get("confirm"):
            return 400, {"error": "Confirmation required."}
        info = self.app.client.get_device_info_struct()
        device_info_str = f"{info.brand} {info.model} (Android {info.android_release})".strip()
        if "samsung" not in device_info_str.lower():
            raise ValueError("Connected device is not Samsung.")
        exclude = payload.get("exclude") or []
        if not isinstance(exclude, list) or not all(isinstance(k, str) for k in exclude):
            return 400, {"error": "exclude must be a list of feature keys."}
        self.app.apply_samsung_experimental_optimizations(exclude=exclude)
        return 200, {"ok": True}

    def handle_samsung_features(self, method, path, query, payload):
        from .app import SAMSUNG_FEATURES
        features = [
            {"key": key, "label": label, "enabled_by_default": default}
            for key, (label, default, _settings) in SAMSUNG_FEATURES.items()
        ]
        return 200, {"features": features}

    def handle_apply_120hz_endurance(self, method, path, query, payload):
        if not payload.get("confirm"):
            return 400, {"error": "Confirmation required."}
        self.app.apply_120hz_endurance_profile()
        return 200, {"ok": True}

    def handle_restrict_apps_preview(self, method, path, query, payload):
        level = payload.get("level", "ignore")
        res = self.app.preview_restrict_background_apps(level=level)
        return 200, res

    def handle_restrict_apps(self, method, path, query, payload):
        if not payload.get("confirm"):
            return 400, {"error": "Confirmation required."}
        level = payload.get("level", "ignore")
        res = self.app.restrict_background_apps(level=level)
        return 200, res

    def handle_revert(self, method, path, query, payload):
        res = self.app.revert_saved_state()
        return 200, {"restored": res}

    def handle_whitelist(self, method, path, query, payload):
        if method == "POST":
            action = payload.get("action")
            pkg = payload.get("package")
            if not pkg:
                return 400, {"error": "package parameter is required."}
            if action == "add":
                changed = self.app.add_to_whitelist(pkg)
                return 200, {"changed": changed}
            elif action == "remove":
                changed = self.app.remove_from_whitelist(pkg)
                return 200, {"changed": changed}
            else:
                return 400, {"error": "Invalid action, must be 'add' or 'remove'."}
        else:
            return 200, {"whitelist": self.app.load_whitelist()}

    def handle_packages(self, method, path, query, payload):
        res = self.app.get_packages(third_party=True)
        return 200, {"packages": res}

    def handle_doctor_state(self, method, path, query, payload):
        count = 0
        non_restorable = []
        packages = self.app.store.data.get("packages", {})
        from .operations import is_restorable_bucket
        for package, item in packages.items():
            bucket = item.get("standby_bucket")
            if bucket is not None and not is_restorable_bucket(bucket):
                non_restorable.append({"package": package, "bucket": bucket})
                count += 1
        return 200, {"count": count, "non_restorable": non_restorable}


class GuiRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send_response_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = self.headers.get("Host", "")
        host_name = host.split(":")[0] if ":" in host else host
        if host_name.lower() not in ("127.0.0.1", "localhost"):
            self.send_error(400, "Bad Request: Invalid Host header")
            return

        parsed = urllib.parse.urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            query = urllib.parse.parse_qs(parsed.query)
            token_val = query.get("token", [""])[0]
            if not secrets.compare_digest(token_val, self.server.web_api.token):
                body = b"Unauthorized: Missing or invalid token."
                self.send_response(401)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            try:
                html = _load_html()
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, f"Error loading UI: {e}")
            return

        if parsed.path.startswith("/api/"):
            header_token = self.headers.get("X-ABO-Token", "")
            if not secrets.compare_digest(header_token, self.server.web_api.token):
                self._send_response_json(401, {"error": "Unauthorized"})
                return

            status, result = self.server.web_api.dispatch("GET", self.path, {})
            self._send_response_json(status, result)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        host = self.headers.get("Host", "")
        host_name = host.split(":")[0] if ":" in host else host
        if host_name.lower() not in ("127.0.0.1", "localhost"):
            self.send_error(400, "Bad Request: Invalid Host header")
            return

        parsed = urllib.parse.urlparse(self.path)

        if parsed.path.startswith("/api/"):
            header_token = self.headers.get("X-ABO-Token", "")
            if not secrets.compare_digest(header_token, self.server.web_api.token):
                self._send_response_json(401, {"error": "Unauthorized"})
                return

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 65536:
                self.send_error(413, "Payload Too Large")
                return
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return

            status, result = self.server.web_api.dispatch("POST", self.path, payload)
            self._send_response_json(status, result)
            return

        self.send_error(404, "Not Found")


def serve(
    app: BatteryOptimizerApp,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    output: Callable[[str], None] = print,
) -> int:
    token = secrets.token_urlsafe(32)
    web_api = WebApi(app, token)

    class CustomThreadingHTTPServer(DaemonThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            self.web_api = web_api
            super().__init__(*args, **kwargs)

    try:
        server = CustomThreadingHTTPServer((host, port), GuiRequestHandler)
    except Exception as exc:
        output(f"Failed to start GUI server: {exc}")
        return 1

    actual_port = server.server_port
    url = f"http://127.0.0.1:{actual_port}/?token={token}"
    output(f"Web GUI server running at: {url}")

    if open_browser:
        try:
            threading.Thread(
                target=lambda: webbrowser.open(url), daemon=True
            ).start()
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        output("\nShutting down Web GUI server...")
    finally:
        server.server_close()
    return 0
