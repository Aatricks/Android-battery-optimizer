import re
from typing import Any, Dict, List, Optional

from .adb import AdbClient
from .snapshot import parse_usagestats
from .verification import parse_appop_output, parse_deviceidle_whitelist


def parse_alarm_wakeups(output: str) -> Dict[str, int]:
    pkg_wakeups = {}
    if not output:
        return pkg_wakeups
    # Match wakeups, alarms: <uid-ish>:<package>
    # e.g., "  6994 wakeups, 6994 alarms: u0a274:com.google.android.gms"
    pattern = re.compile(
        r"(\d+)\s+wakeups,\s+\d+\s+alarms:\s*[a-zA-Z0-9_-]+:([a-zA-Z0-9_.-]+)"
    )
    for line in output.splitlines():
        match = pattern.search(line)
        if match:
            wakeups = int(match.group(1))
            package = match.group(2).strip()
            pkg_wakeups[package] = pkg_wakeups.get(package, 0) + wakeups
    return pkg_wakeups


def parse_wakelock_ms(output: str) -> Dict[str, int]:
    uid_to_pkgs = {}
    pkg_wakelocks = {}
    if not output:
        return pkg_wakelocks

    lines = [
        line.strip().split(",")
        for line in output.splitlines()
        if line.strip()
    ]

    # First pass: UID -> package mappings
    # e.g., "9,0,i,uid,1000,com.samsung.android.provider.filterprovider"
    for fields in lines:
        if len(fields) >= 6 and fields[3] == "uid":
            uid = fields[4]
            pkg = fields[5]
            uid_to_pkgs.setdefault(uid, set()).add(pkg)

    # Second pass: sum partial wakelock times
    for fields in lines:
        if len(fields) >= 4 and fields[3] == "wl":
            uid = fields[1]
            if uid in uid_to_pkgs:
                try:
                    p_idx = fields.index("p")
                    if p_idx > 0:
                        partial_ms = int(fields[p_idx - 1])
                        for pkg in uid_to_pkgs[uid]:
                            pkg_wakelocks[pkg] = (
                                pkg_wakelocks.get(pkg, 0) + partial_ms
                            )
                except (ValueError, IndexError):
                    continue

    return pkg_wakelocks


def parse_registered_jobs(
    output: str, pkg: str, has_package_signal_fn
) -> int:
    count = 0
    if not output:
        return 0
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("JOB") and has_package_signal_fn(pkg, stripped):
            count += 1
    return count


class Diagnoser:
    def __init__(self, client: AdbClient):
        self.client = client
        self.warnings: List[str] = []

    def run(self, third_party_only: bool = True) -> Dict[str, Any]:
        device = self.client.get_device_metadata_with_fallback()

        packages = self._get_packages(third_party=third_party_only)

        # D5: Batterystats --checkin and alarm use LONG_TIMEOUT_SECONDS
        dumpsys_outputs = {
            "batterystats": self._safe_dumpsys(
                ["batterystats", "--checkin"],
                timeout=self.client.LONG_TIMEOUT_SECONDS,
            ),
            "usagestats": self._safe_dumpsys(["usagestats"]),
            "alarm": self._safe_dumpsys(
                ["alarm"], timeout=self.client.LONG_TIMEOUT_SECONDS
            ),
            "jobscheduler": self._safe_dumpsys(["jobscheduler"]),
        }

        # D5: usagestats standby bucket bulk prefetch
        prefetch_buckets = {}
        if dumpsys_outputs["usagestats"]:
            try:
                prefetch_buckets = parse_usagestats(
                    dumpsys_outputs["usagestats"]
                )
            except Exception as exc:
                self.warnings.append(f"Failed to parse usagestats: {exc}")

        # D5: doze-whitelist report
        doze_whitelist_user = []
        try:
            out = self.client.shell_text(
                ["cmd", "deviceidle", "whitelist"], check=True
            )
            doze_whitelist_user = sorted(
                list(parse_deviceidle_whitelist(out))
            )
        except Exception as exc:
            self.warnings.append(
                f"Failed to query deviceidle whitelist: {exc}"
            )

        # Parse measurements
        alarm_map = (
            parse_alarm_wakeups(dumpsys_outputs["alarm"])
            if dumpsys_outputs["alarm"]
            else None
        )
        wakelock_map = (
            parse_wakelock_ms(dumpsys_outputs["batterystats"])
            if dumpsys_outputs["batterystats"]
            else None
        )

        results = []
        for pkg in packages:
            # Standby bucket with fallback
            bucket = None
            if prefetch_buckets:
                bucket = prefetch_buckets.get(pkg)
            if bucket is None:
                bucket = self._get_standby_bucket(pkg)

            appops = self._get_appops(pkg)

            # Build signals dict
            alarm_wakeups = (
                alarm_map.get(pkg, 0) if alarm_map is not None else None
            )
            wakelock_partial_ms = (
                wakelock_map.get(pkg, 0) if wakelock_map is not None else None
            )
            jobs_registered = (
                parse_registered_jobs(
                    dumpsys_outputs["jobscheduler"],
                    pkg,
                    self._has_package_signal,
                )
                if dumpsys_outputs["jobscheduler"]
                else None
            )

            # Parsing last_used (remains unchanged)
            last_used = self._parse_last_used(
                pkg, dumpsys_outputs["usagestats"]
            )

            signals = {
                "alarm_wakeups": alarm_wakeups,
                "wakelock_partial_ms": wakelock_partial_ms,
                "jobs_registered": jobs_registered,
                "last_used": last_used,
            }

            rec, reason = self._recommend(bucket, appops, signals)

            results.append(
                {
                    "package": pkg,
                    "standby_bucket": bucket,
                    "run_any_in_background": appops,
                    "signals": signals,
                    "recommendation": rec,
                    "reason": reason,
                }
            )

        return {
            "device": device,
            "warnings": self.warnings,
            "packages": results,
            "doze_whitelist_user": doze_whitelist_user,
        }

    def _safe_dumpsys(
        self, args: List[str], timeout: Optional[float] = None
    ) -> str:
        try:
            return self.client.shell_text(
                ["dumpsys"] + args, check=True, timeout=timeout
            )
        except Exception as exc:
            self.warnings.append(f"dumpsys {' '.join(args)} failed: {exc}")
            return ""

    def _get_packages(self, third_party: bool) -> List[str]:
        args = ["pm", "list", "packages"]
        if third_party:
            args.append("-3")
        try:
            out = self.client.shell_text(args, check=True)
            return [
                line.split(":", 1)[1].strip()
                for line in out.splitlines()
                if ":" in line
            ]
        except Exception as exc:
            self.warnings.append(f"Failed to list packages: {exc}")
            return []

    def _get_standby_bucket(self, pkg: str) -> Optional[str]:
        try:
            out = self.client.shell_text(
                ["am", "get-standby-bucket", pkg], check=True
            )
            out = out.strip()
            if "unknown" in out.lower() or "error" in out.lower():
                return None
            return out
        except Exception:
            return None

    def _get_appops(self, pkg: str) -> Optional[str]:
        try:
            out = self.client.shell_text(
                ["cmd", "appops", "get", pkg, "RUN_ANY_IN_BACKGROUND"],
                check=True,
            )
            return parse_appop_output(out)
        except Exception:
            return None

    def _has_package_signal(self, pkg: str, dumpsys_output: str) -> bool:
        if not dumpsys_output:
            return False
        pattern = re.compile(
            rf"(?:^|[^a-zA-Z0-9_.])({re.escape(pkg)})(?:[^a-zA-Z0-9_.]|$)"
        )
        return bool(pattern.search(dumpsys_output))

    def _parse_last_used(
        self, pkg: str, usagestats_output: str
    ) -> Dict[str, Any]:
        last_used = {"raw": None, "epoch_ms": None, "parsed": False}
        if usagestats_output:
            for line in usagestats_output.splitlines():
                if (
                    self._has_package_signal(pkg, line)
                    and "lastTimeUsed" in line
                ):
                    raw_val = None
                    m = re.search(r'lastTimeUsed="([^"]+)"', line)
                    if m:
                        raw_val = m.group(1)
                    elif "=" in line:
                        parts = line.split()
                        for p in parts:
                            if p.startswith("lastTimeUsed="):
                                raw_val = p.split("=")[1].strip('"')
                                break

                    if raw_val is not None:
                        last_used["raw"] = raw_val
                        try:
                            last_used["epoch_ms"] = int(raw_val)
                            last_used["parsed"] = True
                        except ValueError:
                            pass
                    break
        return last_used

    def _recommend(
        self,
        bucket: Optional[str],
        appops: Optional[str],
        signals: Dict[str, Any],
    ) -> tuple[str, str]:
        alarm_wakeups = signals.get("alarm_wakeups")
        wakelock_ms = signals.get("wakelock_partial_ms")
        jobs = signals.get("jobs_registered")

        # Threshold checks: treat None as 0
        w_alarm = alarm_wakeups if alarm_wakeups is not None else 0
        w_wake = wakelock_ms if wakelock_ms is not None else 0
        w_jobs = jobs if jobs is not None else 0

        if w_alarm >= 1000 or w_wake >= 3600000:
            reasons = []
            if w_alarm >= 1000:
                reasons.append(f"{w_alarm} alarm wakeups since charge")
            if w_wake >= 3600000:
                reasons.append(
                    f"{w_wake} ms partial wakelock time since charge"
                )
            return "aggressive_restrict", ", ".join(reasons)

        if w_alarm >= 100 or w_wake >= 600000 or w_jobs >= 100:
            reasons = []
            if w_alarm >= 100:
                reasons.append(f"{w_alarm} alarm wakeups since charge")
            if w_wake >= 600000:
                reasons.append(
                    f"{w_wake} ms partial wakelock time since charge"
                )
            if w_jobs >= 100:
                reasons.append(f"{w_jobs} registered jobs")
            return "restrict", ", ".join(reasons)

        return "keep", "Minimal background activity detected"
