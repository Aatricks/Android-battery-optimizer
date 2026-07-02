# Android Battery Optimizer

This is not a guarantee of better battery life. Android already includes its own battery-management systems, and some device-specific tweaks can reduce battery drain on one phone while breaking notifications, sync, companion devices, or app reliability on another.

## Safety Model

- **Per-Device Isolation:** State is stored per ADB serial under the user state directory (`~/.local/state/android-battery-optimizer/devices/<serial>/`).
- **Device Verification:** The `revert` command refuses to run if the connected device does not match the saved snapshot (checked via serial and build fingerprint).
- **Dry-Run Mode:** Running with `--dry-run` prints planned mutations and does not write any rollback state.
- **Atomic Writes:** State updates are atomic, using temporary files and `fsync` to prevent corruption during power loss or crashes.
- **Fail-Safe Restoration:** If restoration of a specific setting fails, the tool logs the error and preserves the state for future retry.
- **Partial Compatibility:** Restore may still fail if Android or the OEM refuses specific commands (e.g., due to permission changes after an OS update).

## Known Limitations

- **Compatibility:** Android/OEM compatibility varies significantly. Features like `device_config` require Android 8.0+, and App Standby Buckets require Android 9.0+.
- **device_config write allowlist:** Android 14+ (and some OEM builds) block shell `device_config` writes via a build-time flag allowlist. On such devices `apply-experimental` applies Doze tuning through the legacy `settings global device_idle_constants` pathway instead (still honored by DeviceIdleController), the abusive-app flags are skipped, and `apply-safe` reports a clear error.
- **Silent Ignorance:** Some settings may be accepted by ADB but silently ignored or overridden by the OS or vendor-specific power management.
- **No Guarantees:** Battery gains are highly dependent on usage patterns and are not guaranteed.
- **App Behavior:** Experimental optimizations may degrade notifications, background sync, location accuracy, or overall app behavior.
- **Connection Requirements:** Root is not required, but an authorized ADB connection is mandatory.

## Recommended Workflow

1. **Status Check:** Run `status` to confirm the device is detected and check for existing rollback state.
2. **Dry Run:** Run your planned command with `--dry-run` to see exactly what ADB commands will be executed.
3. **Safe Start:** Apply safe optimizations first (`apply-safe`).
4. **Observation:** Test the device for at least 24 hours to ensure no critical apps are broken.
5. **Experimental Path:** Only then consider experimental optimizations, and always keep important apps (email, chat, music) in the **whitelist** before using `restrict-apps`.
6. **Revert:** Use `revert` immediately if you experience unexpected behavior or degraded performance.

## Requirements

- [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) installed and `adb` available in `PATH`
- USB debugging enabled on the device
- An authorized ADB connection
- Python 3

## Installation

You can install the tool as a package:

```bash
python3 -m pip install -e .
```

After installation, the `android-battery-optimizer` command will be available:

```bash
android-battery-optimizer --help
```

## Development

Install the local package with developer tools:

```bash
python3 -m pip install -e ".[dev]"
```

Run the test suite without writing bytecode files:

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -v
```

Run the linters and type checker:

```bash
ruff check .
mypy android_battery_optimizer
```

## Usage

### Interactive Menu
Run without a command to start the interactive menu:
```bash
python3 optimizer.py
# or
android-battery-optimizer
```

### Non-Interactive CLI
The tool supports direct subcommands for automation:

| Command | Description |
|---------|-------------|
| `status` | Checks ADB environment and device info |
| `diagnose` | Run battery diagnostics (alarms, partial wakelocks, jobs) and list apps bypassing Doze |
| `smart-restrict` | Intelligently restrict apps (appop `RUN_ANY_IN_BACKGROUND` and Standby Bucket rare/restricted). Aggressive mode prunes Doze-whitelist and disables `WAKE_LOCK` appop. Hibernates long-inactive apps (SDK >= 31) |
| `apply-safe` | Applies documented safe optimizations |
| `apply-experimental --yes` | Applies experimental optimizations (accelerated Doze constants, force dark mode, screen timeout, disabled AOD/wifi-scan, Netpolicy Data Saver/exemptions; does not write `low_power` or touch refresh-rate) |
| `apply-samsung-experimental --yes` | Applies Samsung optimizations (AOD off; preserves GOS; never touches refresh-rate) |
| `restrict-apps --level {ignore,deny,allow} --yes` | Restrict background apps |
| `revert` | Restores saved state for the selected device |
| `doctor-state` | Check saved state for non-restorable standby bucket entries |
| `whitelist list` | List whitelisted apps |
| `whitelist add <package>` | Add app to whitelist |
| `whitelist remove <package>` | Remove app from whitelist |

### Global Flags
- `--serial <id>`: Target a specific device serial.
- `--dry-run`: Show what would happen without making changes.
- `--state-dir <path>`: Use a custom directory for state and whitelist.

### Smart Restrict Examples
`smart-restrict` is measurement-driven and conservative. It uses `diagnose` to identify background activity and only restricts apps that exceed activity thresholds or are explicitly targeted.
- `optimizer.py smart-restrict --dry-run`
- `optimizer.py smart-restrict --yes`
- `optimizer.py smart-restrict --aggressive --yes` (additionally disables `WAKE_LOCK` appops and removes restricted apps from the user Doze whitelist)
- `optimizer.py smart-restrict --min-last-used-days 14 --yes` (restricts apps unused for 14 days; on Android 12+ / SDK 31+, also applies App Hibernation)

### Diagnose Examples
`diagnose` scans apps and gathers quantitative metrics since last charge (alarm wakeups, partial-wakelock ms, registered jobs) to recommend restriction levels. It also lists user-whitelisted apps that bypass Doze.
- `optimizer.py diagnose`
- `optimizer.py diagnose --output report.json`
- `optimizer.py diagnose --all-packages`

## Troubleshooting

- **ADB not found:** Ensure Android Platform Tools are installed and `adb` is in your system PATH.
- **Unauthorized device:** Check your phone for the "Allow USB debugging?" prompt and select "Always allow".
- **Multiple devices:** Use `--serial <serial>` to target a specific device, or select from the list in interactive mode.
- **Restore refused:** The tool prevents restoring state to the wrong device. If you've reinstalled your OS, the fingerprint might have changed; verify manually.
- **Corrupt state file:** If a state file is corrupted, it is quarantined (renamed to `.corrupt.<timestamp>`) and a fresh state is started.
- **Command timeout:** Some commands (like `bg-dexopt-job`) take a long time. The tool uses extended timeouts for these, but poor cables or USB hubs can still cause failures.

## Data Location

Mutable files are stored under:
`~/.local/state/android-battery-optimizer/`

- `devices/<serial>/whitelist.txt`: List of packages to exclude from restrictions for the specific device.
- `devices/<serial>/state.json`: Rollback snapshot for that specific device.

## Whitelist Behavior

If you use `Restrict 3rd Party Apps`, apps in the whitelist are skipped.
Use the whitelist for:
- Messaging and Email apps (to ensure notifications arrive)
- Music or Podcast apps (to prevent playback cut-off)
- Companion device apps (Smartwatches, Galaxy Wearable, etc.)

## References

- [Optimize for Doze and App Standby](https://developer.android.com/training/monitoring-device-state/doze-standby)
- [App Standby Buckets](https://developer.android.com/topic/performance/appstandby)
- [AOSP app background trackers](https://source.android.com/docs/core/power/trackers)
- [Android dumpsys battery diagnostics](https://developer.android.com/tools/dumpsys)
