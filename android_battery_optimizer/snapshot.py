import re
from typing import Dict, Tuple

from .adb import AdbClient, CommandError

PACKAGE_USER_ID = "0"

class SnapshotError(RuntimeError):
    pass

def read_settings_namespace(client: AdbClient, namespace: str) -> Dict[str, str]:
    result = client.shell(["settings", "list", namespace], check=False)
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise SnapshotError(f"Failed to list settings in {namespace}: {err}")

    cache = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cache[k.strip()] = v.strip()
    return cache

def read_device_config_namespace(client: AdbClient, namespace: str) -> Dict[str, str]:
    result = client.shell(["device_config", "list", namespace], check=False)
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise SnapshotError(f"Failed to list device_config in {namespace}: {err}")

    cache = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cache[k.strip()] = v.strip()
    return cache

def parse_usagestats(output: str) -> Dict[str, str]:
    cache = {}
    for line in output.splitlines():
        if "package=" in line and "bucket=" in line:
            pkg_match = re.search(r"package=([a-zA-Z0-9_\.]+)", line)
            bucket_match = re.search(r"bucket=(\d+|[a-zA-Z_]+)", line)
            if pkg_match and bucket_match:
                cache[pkg_match.group(1)] = bucket_match.group(1)
    return cache

def prefetch_package_states(client: AdbClient) -> Tuple[bool, Dict[str, bool], bool, Dict[str, Dict[str, str]], bool, Dict[str, str]]:
    package_enabled_cache: Dict[str, bool] = {}
    appops_cache: Dict[str, Dict[str, str]] = {}
    standby_bucket_cache: Dict[str, str] = {}

    prefetch_package_enabled_success = False
    try:
        disabled = client.shell_text([
            "pm", "list", "packages", "--user", PACKAGE_USER_ID, "-d"
        ])
        enabled = client.shell_text([
            "pm", "list", "packages", "--user", PACKAGE_USER_ID, "-e"
        ])
        for line in disabled.splitlines():
            if ":" in line:
                package_enabled_cache[line.split(":", 1)[1].strip()] = False
        for line in enabled.splitlines():
            if ":" in line:
                package_enabled_cache[line.split(":", 1)[1].strip()] = True
        prefetch_package_enabled_success = True
    except CommandError:
        pass

    prefetch_appops_success = False
    try:
        appops = client.shell_text(["dumpsys", "appops"])
        current_pkg = None
        for line in appops.splitlines():
            stripped = line.strip()
            if stripped.startswith("Package "):
                pkg_match = re.search(r"Package\s+([a-zA-Z0-9_\.]+):", line)
                if pkg_match:
                    current_pkg = pkg_match.group(1)
                    appops_cache.setdefault(current_pkg, {})
                else:
                    current_pkg = None
                continue
            elif stripped.startswith("Op ") or stripped.startswith("Uid "):
                current_pkg = None
                continue

            if current_pkg:
                op_match = re.search(r"\s+([A-Z_a-z0-9]+):\s*([a-zA-Z0-9_]+)", line)
                if op_match:
                    appops_cache.setdefault(current_pkg, {})[op_match.group(1)] = op_match.group(2)
                    continue
                op_match = re.search(r"\s+([A-Z_a-z0-9]+)\s+\(([a-zA-Z0-9_]+)\):", line)
                if op_match:
                    appops_cache.setdefault(current_pkg, {})[op_match.group(1)] = op_match.group(2)
                    continue
        prefetch_appops_success = True
    except CommandError:
        pass

    prefetch_standby_bucket_success = False
    try:
        usagestats = client.shell_text(["dumpsys", "usagestats"])
        standby_bucket_cache = parse_usagestats(usagestats)
        prefetch_standby_bucket_success = True
    except CommandError:
        pass

    return (
        prefetch_package_enabled_success, package_enabled_cache,
        prefetch_appops_success, appops_cache,
        prefetch_standby_bucket_success, standby_bucket_cache
    )
