import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from .adb import AdbClient, CommandError
from .recorder import StateRecorder
from .state import StateStore

WHITELIST_FILE = "whitelist.txt"

class BatteryOptimizerApp:
    def __init__(self, client: AdbClient, state_dir: Path) -> None:
        self.client = client
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._whitelist_migration_announced = False
        self.store = StateStore(self.state_dir, client)
        self.recorder = StateRecorder(client, self.store)

    def rebind_device(self) -> None:
        self.store.rebind()

    @property
    def whitelist_path(self) -> Path:
        serial = self.client.serial or "unknown-device"
        safe_serial = StateStore.sanitize_serial(serial)
        return self.state_dir / "devices" / safe_serial / WHITELIST_FILE

    @property
    def legacy_whitelist_path(self) -> Path:
        return self.state_dir / WHITELIST_FILE

    def _migrate_legacy_whitelist_if_needed(self) -> None:
        current_path = self.whitelist_path
        if current_path.exists():
            return

        legacy_path = self.legacy_whitelist_path
        if not legacy_path.exists():
            return

        current_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, current_path)
        if not self._whitelist_migration_announced:
            self.client.output(
                f"Migrated legacy whitelist.txt to {current_path}."
            )
            self._whitelist_migration_announced = True

    def load_whitelist(self) -> List[str]:
        self._migrate_legacy_whitelist_if_needed()
        if not self.whitelist_path.exists():
            return []
        with self.whitelist_path.open("r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def save_whitelist(self, packages: Sequence[str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_whitelist_if_needed()
        self.whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        with self.whitelist_path.open("w", encoding="utf-8") as handle:
            for package in packages:
                handle.write(f"{package}\n")


    def get_device_info(self) -> str:
        info = self.client.get_device_info_struct()
        return f"{info.brand} {info.model} (Android {info.android_release})".strip()

    def get_packages(self, third_party: bool = True, user_id: Optional[str] = None) -> List[str]:
        args: List[object] = ["pm", "list", "packages"]
        if user_id is not None:
            args.extend(["--user", user_id])
        if third_party:
            args.append("-3")
        result = self.client.shell(args, check=False)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            details = stderr or stdout or "unknown error"
            raise CommandError(
                f"Failed to list packages with `{' '.join(str(arg) for arg in args)}`: {details}",
                result=result,
            )

        output = result.stdout.strip()
        packages = []
        for line in output.splitlines():
            if ":" in line:
                packages.append(line.split(":", 1)[1].strip())
        return sorted(packages)

    def get_installed_packages_set(self, user_id: Optional[str] = None) -> Set[str]:
        return set(self.get_packages(third_party=False, user_id=user_id))

    def validate_package(self, package: str) -> None:
        if package not in self.get_installed_packages_set():
            raise ValueError(f"Package `{package}` is not installed on the connected device.")

    def apply_documented_safe_optimizations(self) -> None:
        if not self.client.supports_device_config():
            raise ValueError("Device does not support `device_config` command. Optimization aborted.")
        if not self.client.supports_device_config_write(
            "activity_manager", "bg_auto_restrict_abusive_apps", "1"
        ):
            raise ValueError(
                "This Android build blocks shell device_config writes "
                "(Android 14+ flag allowlist). Safe optimizations are "
                "unavailable on this device."
            )

        with self.recorder.transaction():
            self._apply_safe_flags()

    def _apply_safe_flags(self) -> None:
        self.recorder.put_device_config("activity_manager", "bg_auto_restrict_abusive_apps", 1)
        self.recorder.put_device_config(
            "activity_manager",
            "bg_current_drain_auto_restrict_abusive_apps_enabled",
            1,
        )

    def apply_experimental_optimizations(self) -> None:
        info = self.client.get_device_info_struct()
        # SDK 26 (Android 8.0) is a conservative minimum for device_config
        # and stable settings behavior
        if info.sdk_int < 26:
            raise ValueError(
                f"Device SDK {info.sdk_int} is too old for experimental "
                "optimizations (min SDK 26 required)."
            )

        if not self.client.supports_device_config():
            raise ValueError(
                "Device does not support `device_config` command. "
                "Experimental optimization aborted."
            )

        for namespace in ("global", "system", "secure"):
            if not self.client.supports_settings_namespace(namespace):
                raise ValueError(
                    f"Device does not support `settings` namespace "
                    f"`{namespace}`. Experimental optimization aborted."
                )

        device_config_writable = self.client.supports_device_config_write(
            "device_idle", "inactive_to", "300000"
        )
        if not device_config_writable:
            self.client.output(
                "Warning: device_config writes are blocked on this build; "
                "applying Doze tuning via the legacy device_idle_constants "
                "setting and skipping abusive-app flags."
            )

        with self.recorder.transaction():
            doze_settings = {
                # light doze: slightly faster than stock, no rapid cycling
                "light_after_inactive_to": "60000",  # 1 min
                "light_idle_to": "300000",  # 5 min
                "light_idle_factor": "2",
                "light_max_idle_to": "1800000",  # 30 min
                # deep doze: enter fast, stay long
                "inactive_to": "300000",  # 5 min (stock 30 min)
                "idle_after_inactive_to": "0",
                "sensing_to": "0",
                "locating_to": "0",
                "motion_inactive_to": "0",
                "idle_pending_to": "60000",  # 1 min maintenance
                "max_idle_pending_to": "120000",
                "idle_pending_factor": "2",
                "idle_to": "3600000",  # 1 h first deep idle
                "idle_factor": "2",
                "max_idle_to": "21600000",  # 6 h max
            }
            if device_config_writable:
                for key, value in doze_settings.items():
                    self.recorder.put_device_config("device_idle", key, value)
            else:
                # Android 14+ allowlist blocks shell device_config writes, but
                # DeviceIdleController still honors the pre-Android-10 global
                # setting (verified live on Samsung SM-S901B / Android 16).
                legacy_constants = ",".join(
                    f"{key}={value}" for key, value in doze_settings.items()
                )
                self.recorder.put_setting(
                    "global", "device_idle_constants", legacy_constants
                )

            settings_to_apply = [
                ("global", "window_animation_scale", "0.5"),
                ("global", "transition_animation_scale", "0.5"),
                ("global", "animator_duration_scale", "0.5"),
                ("global", "ble_scan_always_enabled", "0"),
                ("system", "nearby_scanning_enabled", "0"),
                ("global", "wifi_scan_throttle_enabled", "1"),
                ("global", "mobile_data_always_on", "0"),
                ("global", "cached_apps_freezer", "enabled"),
                ("global", "adaptive_battery_management_enabled", "1"),
                ("global", "wifi_scan_always_enabled", "0"),
                ("secure", "doze_always_on", "0"),
                ("secure", "ui_night_mode", "2"),
                ("system", "screen_off_timeout", "30000"),
            ]
            for namespace, key, value in settings_to_apply:
                self.recorder.put_setting(namespace, key, value)

            constants = (
                "advertise_is_enabled=true,"
                "datasaver_disabled=false,"
                "enable_night_mode=true,"
                "launch_boost_disabled=true,"
                "vibration_disabled=true,"
                "animation_disabled=true,"
                "soundtrigger_disabled=true,"
                "fullbackup_deferred=true,"
                "keyvaluebackup_deferred=true,"
                "firewall_disabled=false,"
                "gps_mode=2,"
                "adjust_brightness_disabled=false,"
                "adjust_brightness_factor=0.5,"
                "force_all_apps_standby=true,"
                "force_background_check=true,"
                "optional_sensors_disabled=true,"
                "aod_disabled=false,"
                "quick_doze_enabled=true"
            )
            self.recorder.put_setting("global", "battery_saver_constants", constants)
            if device_config_writable:
                self._apply_safe_flags()

            # Data Saver exemptions + toggle (exemptions first)
            try:
                # Probe netpolicy support
                res = self.client.shell(
                    ["cmd", "netpolicy", "get", "restrict-background"]
                )
                from .verification import parse_netpolicy_toggle
                parse_netpolicy_toggle(res.stdout)
                netpolicy_supported = True
            except Exception as exc:
                self.client.output(
                    f"Warning: Data Saver optimizations skipped because "
                    f"restrict-background status query failed: {exc}"
                )
                netpolicy_supported = False

            if netpolicy_supported:
                whitelist = self.load_whitelist()
                for pkg in whitelist:
                    try:
                        self.recorder.add_netpolicy_whitelist(pkg)
                    except Exception as exc:
                        self.client.output(
                            f"Warning: Could not add whitelisted package "
                            f"{pkg} to restrict-background whitelist: {exc}"
                        )
                self.recorder.set_netpolicy_restrict_background(True)

    def apply_samsung_experimental_optimizations(self) -> None:
        info = self.client.get_device_info_struct()
        if info.brand.lower() != "samsung":
            raise ValueError("Connected device is not Samsung.")

        for namespace in ("system", "global", "secure"):
            if not self.client.supports_settings_namespace(namespace):
                raise ValueError(
                    f"Device does not support `settings` namespace "
                    f"`{namespace}`. Samsung optimization aborted."
                )

        with self.recorder.transaction():
            samsung_settings = {
                "system": {
                    "master_motion": "0",
                    "motion_engine": "0",
                    "air_motion_engine": "0",
                    "air_motion_wake_up": "0",
                    "mcf_continuity": "0",
                    "intelligent_sleep_mode": "0",
                    "nearby_scanning_enabled": "0",
                    "nearby_scanning_permission_allowed": "0",
                    "aod_mode": "0",
                },
                "global": {
                    "ram_expand_size": "0",
                    "enhanced_processing": "0",
                },
                "secure": {
                    "vibration_on": "0",
                    "adaptive_sleep": "0",
                    "game_auto_temperature_control": "0",
                    "game_bixby_block": "1",
                },
            }
            for namespace, values in samsung_settings.items():
                for key, value in values.items():
                    self.recorder.put_setting(namespace, key, value)

    def restrict_background_apps(self, level: str = "ignore") -> Dict[str, List[str]]:
        if not self.client.supports_appops():
            raise ValueError("Device does not support `appops` command via `cmd`. Background restriction aborted.")
        if not self.client.supports_standby_bucket():
            raise ValueError("Device does not support `am set-standby-bucket`. Background restriction aborted.")

        from .operations import normalize_restorable_bucket
        from .snapshot import SnapshotError

        whitelist = set(self.load_whitelist())
        packages = self.get_packages(third_party=True)
        skipped_whitelisted = []
        skipped_non_restorable = []

        with self.recorder.transaction():
            self.recorder.prefetch_package_states()
            for package in packages:
                if package in whitelist:
                    skipped_whitelisted.append(package)
                    continue
                try:
                    prior_bucket = self.recorder._get_standby_bucket(package)
                    normalize_restorable_bucket(prior_bucket)
                except (SnapshotError, ValueError):
                    skipped_non_restorable.append(package)
                    continue
                self.recorder.set_appop(package, "RUN_ANY_IN_BACKGROUND", level)
                bucket = "active" if level == "allow" else "rare"
                self.recorder.set_standby_bucket(package, bucket)
        return {
            "skipped_whitelisted": skipped_whitelisted,
            "skipped_non_restorable": skipped_non_restorable
        }

    def run_bg_dexopt(self) -> None:
        self.client.shell(
            ["cmd", "package", "bg-dexopt-job"],
            mutate=True,
            timeout=self.client.LONG_TIMEOUT_SECONDS,
        )

    def revert_saved_state(self) -> List[str]:
        if not self.store.has_entries():
            return []
        return self.recorder.restore()

    def diagnose(self, third_party_only: bool = True) -> Dict[str, Any]:
        from .diagnose import Diagnoser
        return Diagnoser(self.client).run(third_party_only=third_party_only)

    def _get_critical_packages(self) -> Set[str]:
        critical = set()
        # Common prefixes
        installed = self.get_installed_packages_set()
        for pkg in installed:
            if pkg.startswith(("com.android.", "com.google.android.", "android")):
                critical.add(pkg)

        commands = {
            "launcher": ["cmd", "package", "resolve-activity", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
            "dialer": ["telecom", "get-default-dialer"],
            "sms": ["settings", "get", "secure", "sms_default_application"],
            "ime": ["settings", "get", "secure", "default_input_method"],
            "a11y": ["settings", "get", "secure", "enabled_accessibility_services"],
            "vpn": ["settings", "get", "secure", "always_on_vpn_app"],
            "companion": ["cmd", "companiondevice", "list"],
        }

        for name, cmd in commands.items():
            try:
                out = self.client.shell_text(cmd, check=False)
                if out and out != "null" and "Error" not in out:
                    if name == "launcher" and "packageName=" in out:
                        for line in out.splitlines():
                            if "packageName=" in line:
                                critical.add(line.split("=")[1].strip())
                    elif name == "ime":
                        critical.add(out.split("/")[0])
                    elif name == "a11y":
                        for service in out.split(":"):
                            if service:
                                critical.add(service.split("/")[0])
                    elif name == "companion":
                        for line in out.splitlines():
                            if "Package:" in line:
                                critical.add(line.split("Package:")[1].strip())
                    else:
                        critical.add(out.strip())
            except Exception:
                pass

        return critical

    def smart_restrict(
        self,
        aggressive: bool = False,
        min_last_used_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.client.supports_appops():
            raise ValueError(
                "Device does not support `appops` command via `cmd`."
            )
        if not self.client.supports_standby_bucket():
            raise ValueError("Device does not support `am set-standby-bucket`.")

        whitelist = set(self.load_whitelist())
        critical = self._get_critical_packages()

        applied = []
        skipped = []
        kept = []

        report = self.diagnose(third_party_only=True)
        warnings = report.get("warnings", [])

        if any("Failed to list packages" in w for w in warnings):
            raise ValueError("Diagnose could not list packages.")

        if not report["packages"]:
            installed = self.get_packages(third_party=True)
            if installed:
                raise ValueError(
                    "Diagnostic report yielded no packages but third-party "
                    "packages exist."
                )

        import time

        from .operations import normalize_restorable_bucket
        from .snapshot import SnapshotError

        current_time_ms = time.time() * 1000
        info = self.client.get_device_info_struct()

        user_whitelist = set()
        if aggressive:
            try:
                res = self.client.shell(["cmd", "deviceidle", "whitelist"])
                from .verification import parse_deviceidle_whitelist
                user_whitelist = parse_deviceidle_whitelist(res.stdout)
            except Exception:
                pass

        with self.recorder.transaction():
            self.recorder.prefetch_package_states()
            for pkg_info in report["packages"]:
                pkg = pkg_info["package"]
                if pkg in whitelist:
                    skipped.append({"package": pkg, "reason": "whitelisted"})
                    continue
                if pkg in critical:
                    skipped.append({"package": pkg, "reason": "critical"})
                    continue

                if min_last_used_days is not None:
                    last_used = (
                        pkg_info.get("signals", {})
                        .get("last_used", {})
                    )
                    if last_used.get("parsed"):
                        last_used_ms = float(last_used["epoch_ms"])
                        time_diff = current_time_ms - last_used_ms
                        if time_diff < (min_last_used_days * 86400 * 1000):
                            skipped.append(
                                {"package": pkg, "reason": "recently_used"}
                            )
                            continue
                    else:
                        skipped.append(
                            {"package": pkg, "reason": "last_used_unknown"}
                        )
                        continue

                rec = pkg_info["recommendation"]
                reason = pkg_info.get("reason", "")

                if rec == "keep":
                    kept.append({"package": pkg, "reason": reason})
                    continue

                try:
                    prior_bucket = self.recorder._get_standby_bucket(pkg)
                except SnapshotError:
                    skipped.append(
                        {"package": pkg, "reason": "standby_bucket_unreadable"}
                    )
                    continue

                try:
                    normalize_restorable_bucket(prior_bucket)
                except ValueError:
                    skipped.append(
                        {
                            "package": pkg,
                            "reason": "non_restorable_standby_bucket",
                        }
                    )
                    continue

                if aggressive and rec == "aggressive_restrict":
                    self.recorder.set_appop(
                        pkg, "RUN_ANY_IN_BACKGROUND", "ignore"
                    )
                    self.recorder.set_appop(pkg, "WAKE_LOCK", "ignore")
                    self.recorder.set_standby_bucket(pkg, "restricted")
                    if pkg in user_whitelist:
                        self.recorder.remove_deviceidle_whitelist(pkg)

                    if min_last_used_days is not None and info.sdk_int >= 31:
                        try:
                            self.recorder.set_app_hibernation(pkg, True)
                        except Exception:
                            skipped.append(
                                {
                                    "package": pkg,
                                    "reason": "hibernation_state_unreadable",
                                }
                            )

                    applied.append(
                        {
                            "package": pkg,
                            "appop": "ignore",
                            "wake_lock": "ignore",
                            "bucket": "restricted",
                            "reason": reason,
                        }
                    )
                elif not aggressive and rec in (
                    "restrict",
                    "aggressive_restrict",
                ):
                    self.recorder.set_appop(
                        pkg, "RUN_ANY_IN_BACKGROUND", "ignore"
                    )
                    self.recorder.set_standby_bucket(pkg, "rare")

                    if min_last_used_days is not None and info.sdk_int >= 31:
                        try:
                            self.recorder.set_app_hibernation(pkg, True)
                        except Exception:
                            skipped.append(
                                {
                                    "package": pkg,
                                    "reason": "hibernation_state_unreadable",
                                }
                            )

                    applied.append(
                        {
                            "package": pkg,
                            "appop": "ignore",
                            "bucket": "rare",
                            "reason": reason,
                        }
                    )
                else:
                    skipped.append(
                        {
                            "package": pkg,
                            "reason": "unsupported_recommendation",
                        }
                    )

        return {
            "applied": applied,
            "skipped": skipped,
            "kept": kept,
            "warnings": warnings
        }
