import re
from typing import List, Optional, cast

from .adb import AdbClient, CommandError
from .ledger import AnyLedgerEntry
from .operations import STANDBY_BUCKET_MAP

PACKAGE_USER_ID = "0"

class VerificationError(RuntimeError):
    pass

def normalize_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    if stripped in {"", "null", "None", "undefined"}:
        return None
    return stripped

def parse_appop_output(output: str) -> str:
    normalized_output = output.strip()
    if not normalized_output:
        raise VerificationError(f"Verification failed for appop: could not parse command output: {output}")

    if normalized_output in {"No operations.", "No overrides."}:
        return "default"

    for line in normalized_output.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if candidate in {"No operations.", "No overrides."}:
            return "default"
        match = re.match(
            r"^(?:(?:[A-Z_a-z0-9]+)\s*:\s*)?(?:mode\s*[:=]\s*)?(?P<value>[A-Za-z0-9_]+)(?:\s*;.*)?$",
            candidate,
        )
        if match:
            return match.group("value").lower()

    raise VerificationError(
        f"Verification failed for appop: could not parse command output: {output}"
    )

def compare_setting_value(actual_stdout: str, expected_value: Optional[str]) -> bool:
    return normalize_value(actual_stdout) == normalize_value(expected_value)


def compare_device_config_value(actual_stdout: str, expected_value: Optional[str]) -> bool:
    return normalize_value(actual_stdout) == normalize_value(expected_value)


def compare_appop_value(actual_stdout: str, expected_value: str) -> bool:
    actual = parse_appop_output(actual_stdout)
    expected = normalize_value(expected_value)
    if expected is not None:
        expected = expected.lower()
    if expected == "default":
        return actual in {"default", "no operations.", "no overrides."}
    return actual == expected


def compare_standby_bucket_value(actual_stdout: str, expected_bucket: str) -> bool:
    actual = actual_stdout.strip()
    expected_code = STANDBY_BUCKET_MAP.get(
        expected_bucket.lower(), expected_bucket
    )
    return actual == expected_code


def compare_package_enabled_value(
    actual_stdout: str, package: str, expected_enabled: bool
) -> bool:
    found = False
    for line in actual_stdout.strip().splitlines():
        if line.strip() == f"package:{package}":
            found = True
            break
    return found


def compare_netpolicy_restrict_background(
    actual_stdout: str, expected_value: bool
) -> bool:
    try:
        return parse_netpolicy_toggle(actual_stdout) == expected_value
    except Exception:
        return False


def compare_netpolicy_whitelist(
    actual_stdout: str, uid: str, expected_member: bool
) -> bool:
    try:
        return (uid in parse_netpolicy_whitelist(actual_stdout)) == expected_member
    except Exception:
        return False


def compare_deviceidle_whitelist(
    actual_stdout: str, package: str, expected_member: bool
) -> bool:
    try:
        return (
            package in parse_deviceidle_whitelist(actual_stdout)
        ) == expected_member
    except Exception:
        return False


def compare_app_hibernation(actual_stdout: str, expected_value: bool) -> bool:
    try:
        return parse_hibernation_state(actual_stdout) == expected_value
    except Exception:
        return False


def verify_setting(client: AdbClient, namespace: str, key: str, expected_value: Optional[str]) -> None:
    if client.dry_run:
        return
    result = client.shell(["settings", "get", namespace, key], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for setting {namespace}/{key}: "
            f"read command failed with exit code {result.returncode}"
        )
    if not compare_setting_value(result.stdout, expected_value):
        actual = normalize_value(result.stdout)
        expected = normalize_value(expected_value)
        raise VerificationError(
            f"Verification failed for setting {namespace}/{key}: "
            f"expected {expected}, got {actual}"
        )


def verify_device_config(client: AdbClient, namespace: str, key: str, expected_value: Optional[str]) -> None:
    if client.dry_run:
        return
    result = client.shell(["device_config", "get", namespace, key], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for device_config {namespace}/{key}: "
            f"read command failed with exit code {result.returncode}"
        )
    if not compare_device_config_value(result.stdout, expected_value):
        actual = normalize_value(result.stdout)
        expected = normalize_value(expected_value)
        raise VerificationError(
            f"Verification failed for device_config {namespace}/{key}: "
            f"expected {expected}, got {actual}"
        )


def verify_appop(client: AdbClient, package: str, op: str, expected_value: str) -> None:
    if client.dry_run:
        return
    result = client.shell(["cmd", "appops", "get", package, op], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for appop {op} for package {package}: "
            f"read command failed with exit code {result.returncode}"
        )
    if not compare_appop_value(result.stdout, expected_value):
        actual = parse_appop_output(result.stdout)
        raise VerificationError(
            f"Verification failed for appop {op} for package {package}: "
            f"expected {expected_value}, got {actual}"
        )


def verify_standby_bucket(client: AdbClient, package: str, expected_bucket: str) -> None:
    if client.dry_run:
        return
    result = client.shell(["am", "get-standby-bucket", package], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for standby bucket for package {package}: "
            f"read command failed with exit code {result.returncode}"
        )
    if not compare_standby_bucket_value(result.stdout, expected_bucket):
        actual = result.stdout.strip()
        expected_code = STANDBY_BUCKET_MAP.get(expected_bucket.lower(), expected_bucket)
        raise VerificationError(
            f"Verification failed for standby bucket for package {package}: "
            f"expected {expected_bucket} ({expected_code}), got {actual}"
        )


def verify_package_enabled(client: AdbClient, package: str, expected_enabled: bool) -> None:
    if client.dry_run:
        return
    result = client.shell(
        [
            "pm",
            "list",
            "packages",
            "--user",
            PACKAGE_USER_ID,
            "-e" if expected_enabled else "-d",
            package,
        ],
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for package {package} enabled state: "
            f"package-enabled verification readback failed with exit code {result.returncode}"
        )
    if not compare_package_enabled_value(result.stdout, package, expected_enabled):
        actual = "enabled" if not expected_enabled else "disabled/missing"
        raise VerificationError(
            f"Verification failed for package {package} enabled state: "
            f"expected {expected_enabled}, but package is {actual}"
        )


def parse_netpolicy_toggle(stdout: str) -> bool:
    stdout_strip = stdout.strip()
    if "enabled" in stdout_strip:
        return True
    elif "disabled" in stdout_strip:
        return False
    raise ValueError(f"Unparseable netpolicy status: {stdout}")


def parse_netpolicy_whitelist(stdout: str) -> set[str]:
    stdout_strip = stdout.strip()
    prefix = "Restrict background whitelisted UIDs:"
    if not stdout_strip.startswith(prefix):
        raise ValueError(f"Unparseable netpolicy whitelist: {stdout}")
    uids_str = stdout_strip[len(prefix):].strip()
    if not uids_str:
        return set()
    return set(uids_str.split())


def parse_deviceidle_whitelist(stdout: str) -> set[str]:
    packages = set()
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("user,"):
            parts = line.split(",")
            if len(parts) >= 2:
                packages.add(parts[1])
    return packages


def parse_hibernation_state(stdout: str) -> bool:
    val = stdout.strip().lower()
    if val == "true":
        return True
    elif val == "false":
        return False
    raise ValueError(f"Unparseable hibernation state: {stdout}")


def verify_netpolicy_restrict_background(
    client: AdbClient, expected_value: bool
) -> None:
    if client.dry_run:
        return
    result = client.shell(
        ["cmd", "netpolicy", "get", "restrict-background"],
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for netpolicy restrict-background: "
            f"read command failed with exit code {result.returncode}"
        )
    if not compare_netpolicy_restrict_background(result.stdout, expected_value):
        try:
            actual = parse_netpolicy_toggle(result.stdout)
        except Exception:
            actual = result.stdout.strip()
        raise VerificationError(
            f"Verification failed for netpolicy restrict-background: "
            f"expected {expected_value}, got {actual}"
        )


def verify_netpolicy_whitelist(
    client: AdbClient, package: str, uid: str, expected_member: bool
) -> None:
    if client.dry_run:
        return
    result = client.shell(
        ["cmd", "netpolicy", "list", "restrict-background-whitelist"],
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for netpolicy whitelist for {package} "
            f"(uid {uid}): read command failed with exit code "
            f"{result.returncode}"
        )
    if not compare_netpolicy_whitelist(result.stdout, uid, expected_member):
        raise VerificationError(
            f"Verification failed for netpolicy whitelist for {package} "
            f"(uid {uid}): expected {expected_member}"
        )


def verify_deviceidle_whitelist(
    client: AdbClient, package: str, expected_member: bool
) -> None:
    if client.dry_run:
        return
    result = client.shell(["cmd", "deviceidle", "whitelist"], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for deviceidle whitelist for {package}: "
            f"read command failed with exit code {result.returncode}"
        )
    if not compare_deviceidle_whitelist(result.stdout, package, expected_member):
        raise VerificationError(
            f"Verification failed for deviceidle whitelist for {package}: "
            f"expected {expected_member}"
        )


def verify_app_hibernation(
    client: AdbClient, package: str, expected_value: bool
) -> None:
    if client.dry_run:
        return
    result = client.shell(
        ["cmd", "app_hibernation", "get-state", package],
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for hibernation state for package {package}: "
            f"read command failed with exit code {result.returncode}"
        )
    if not compare_app_hibernation(result.stdout, expected_value):
        try:
            actual = parse_hibernation_state(result.stdout)
        except Exception:
            actual = result.stdout.strip()
        raise VerificationError(
            f"Verification failed for hibernation state for package {package}: "
            f"expected {expected_value}, got {actual}"
        )


def verify_entries_batched(client: AdbClient, entries: List[AnyLedgerEntry]) -> None:
    if client.dry_run or not entries:
        return

    lines = []
    for i, entry in enumerate(entries):
        lines.append(f"echo \"===V_{i}===\"")
        type_ = entry["type"]
        if type_ == "setting":
            cmd = ["settings", "get", entry["namespace"], entry["key"]]
        elif type_ == "device_config":
            cmd = ["device_config", "get", entry["namespace"], entry["key"]]
        elif type_ == "appop":
            cmd = ["cmd", "appops", "get", entry["package"], entry["op"]]
        elif type_ == "standby_bucket":
            cmd = ["am", "get-standby-bucket", entry["package"]]
        elif type_ == "package_enabled":
            expected_enabled = bool(entry.get("new_value"))
            cmd = [
                "pm",
                "list",
                "packages",
                "--user",
                PACKAGE_USER_ID,
                "-e" if expected_enabled else "-d",
                entry["package"],
            ]
        elif type_ == "netpolicy_restrict_background":
            cmd = ["cmd", "netpolicy", "get", "restrict-background"]
        elif type_ == "netpolicy_whitelist":
            cmd = ["cmd", "netpolicy", "list", "restrict-background-whitelist"]
        elif type_ == "deviceidle_whitelist":
            cmd = ["cmd", "deviceidle", "whitelist"]
        elif type_ == "app_hibernation":
            cmd = ["cmd", "app_hibernation", "get-state", entry["package"]]
        else:
            raise VerificationError(f"Unknown ledger entry type: {type_}")

        formatted = client._format(client._stringify(cmd))
        lines.append(formatted)

    script = "\n".join(lines)
    result = client.shell([], mutate=False, input_data=script, check=True)

    # Check if the output has any markers. If not (e.g. mocked default empty response),
    # raise CommandError to trigger sequential fallback.
    if not any(f"===V_{i}===" in result.stdout for i in range(len(entries))):
        raise CommandError(
            "Batched verification output missing markers",
            result=result,
        )

    parts = result.stdout.split("===V_")
    sections = {}
    for part in parts[1:]:
        if not part:
            continue
        lines_part = part.splitlines()
        if not lines_part:
            continue
        marker_line = lines_part[0].strip()
        if marker_line.endswith("==="):
            idx_str = marker_line[:-3].strip()
            try:
                idx = int(idx_str)
                content = "\n".join(lines_part[1:])
                sections[idx] = content
            except ValueError:
                continue

    for i, entry in enumerate(entries):
        content = sections.get(i, "")
        type_ = entry["type"]
        new_val = entry.get("new_value")

        success = False
        try:
            if type_ == "setting":
                success = compare_setting_value(content, cast(Optional[str], new_val))
            elif type_ == "device_config":
                success = compare_device_config_value(content, cast(Optional[str], new_val))
            elif type_ == "appop":
                success = compare_appop_value(content, cast(str, new_val))
            elif type_ == "standby_bucket":
                success = compare_standby_bucket_value(content, cast(str, new_val))
            elif type_ == "package_enabled":
                success = compare_package_enabled_value(
                    content, entry["package"], bool(new_val)
                )
            elif type_ == "netpolicy_restrict_background":
                success = compare_netpolicy_restrict_background(
                    content, bool(new_val)
                )
            elif type_ == "netpolicy_whitelist":
                success = compare_netpolicy_whitelist(
                    content, entry["uid"], bool(new_val)
                )
            elif type_ == "deviceidle_whitelist":
                success = compare_deviceidle_whitelist(
                    content, entry["package"], bool(new_val)
                )
            elif type_ == "app_hibernation":
                success = compare_app_hibernation(content, bool(new_val))
        except Exception as exc:
            raise VerificationError(
                f"Verification failed for entry {entry}: {exc}"
            ) from exc

        if not success:
            raise VerificationError(
                f"Verification failed for entry {entry}: value mismatch"
            )
