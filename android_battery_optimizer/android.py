import re
from dataclasses import dataclass
from typing import Dict, List, Sequence


@dataclass
class DeviceInfo:
    serial: str
    brand: str
    model: str
    android_release: str
    sdk_int: int
    fingerprint: str

def parse_adb_devices(output: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of devices attached") or stripped.startswith("*"):
            continue
        if "\t" in stripped:
            serial, rest = stripped.split("\t", 1)
            serial = serial.strip()
            rest = rest.strip()
            if serial and rest:
                status = rest.split()[0]
                devices.append({"serial": serial, "status": status})
        else:
            parts = stripped.split()
            if len(parts) >= 2:
                devices.append({"serial": parts[0], "status": parts[1]})
    return devices

def resolve_package_choice(query: str, packages: Sequence[str]) -> List[str]:
    normalized = query.strip()
    if not normalized:
        return []
    if "." in normalized:
        return [pkg for pkg in packages if pkg == normalized]
    lowered = normalized.lower()
    return [pkg for pkg in packages if lowered in pkg.lower()]


def parse_builtin_refresh_rates(output: str) -> List[float]:
    for line in output.splitlines():
        if "Built-in Screen" not in line or "supportedRefreshRates" not in line:
            continue
        match = re.search(r"supportedRefreshRates \[([^]]+)\]", line)
        if not match:
            continue
        try:
            return sorted(
                float(value.strip())
                for value in match.group(1).split(",")
            )
        except ValueError:
            return []
    return []
