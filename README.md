# Android Battery Optimizer

A Python CLI and local web dashboard that tunes battery-related settings on an Android phone over ADB. No root required.

This is not a guarantee of better battery life. Android already has its own battery management, and a tweak that helps one phone can break notifications, sync, companion devices, or app reliability on another.

## Safety model

Every change the tool makes is snapshotted first and can be reverted:

- State lives per device, under `~/.local/state/android-battery-optimizer/devices/<serial>/`.
- `revert` refuses to run if the connected device does not match the saved snapshot. It checks both the serial and the build fingerprint.
- `--dry-run` prints the planned mutations and writes no rollback state.
- State files are written atomically (temp file, then `fsync`), so a crash or power loss will not corrupt them.
- If restoring one setting fails, the tool logs the error and keeps that entry so you can retry later.
- A restore can still fail when Android or the OEM refuses a command, for example after an OS update changes permissions.

## Known limitations

- Compatibility varies a lot between Android versions and OEMs. `device_config` needs Android 8.0 or later; App Standby Buckets need 9.0 or later.
- Android 14 and some OEM builds block shell `device_config` writes through a build-time allowlist. On those devices `apply-experimental` applies its Doze tuning through the legacy `settings global device_idle_constants` setting instead (DeviceIdleController still honors it) and skips the abusive-app flags, while `apply-safe` reports a clear error.
- ADB sometimes accepts a setting that the OS or the vendor's power manager then ignores or overrides.
- Battery gains depend on how you use the phone.
- Experimental optimizations can degrade notifications, background sync, location accuracy, or app behavior.
- Root is not required, but you need an authorized ADB connection.

## Recommended workflow

1. Run `status` to confirm the device is detected and check for existing rollback state.
2. Run your planned command with `--dry-run` to see the exact ADB commands it would execute.
3. Apply the safe optimizations first (`apply-safe`).
4. Use the phone for at least 24 hours and watch for broken apps.
5. Only then consider the experimental optimizations, and put important apps (email, chat, music) in the whitelist before using `restrict-apps`.
6. Run `revert` as soon as anything misbehaves.

## Requirements

- [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) installed and `adb` available in `PATH`
- USB debugging enabled on the device
- An authorized ADB connection
- Python 3

## Installation

Install the tool as a package:

```bash
python3 -m pip install -e .
```

This gives you the `android-battery-optimizer` command:

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

### Interactive menu

Run without a command to start the interactive menu:

```bash
python3 optimizer.py
# or
android-battery-optimizer
```

### Non-interactive CLI

Each action is also a direct subcommand, which is easier to script:

| Command | Description |
|---------|-------------|
| `status` | Checks ADB environment and device info |
| `diagnose` | Run battery diagnostics (alarms, partial wakelocks, jobs) and list apps bypassing Doze |
| `smart-restrict` | Intelligently restrict apps (appop `RUN_ANY_IN_BACKGROUND` and Standby Bucket rare/restricted). Aggressive mode prunes Doze-whitelist and disables `WAKE_LOCK` appop. Hibernates long-inactive apps (SDK >= 31) |
| `apply-safe` | Applies documented safe optimizations |
| `apply-experimental --yes` | Applies experimental optimizations (accelerated Doze constants, force dark mode, screen timeout, disabled AOD/wifi-scan, Netpolicy Data Saver/exemptions; does not write `low_power` or touch refresh-rate) |
| `apply-samsung-experimental --yes` | Applies Samsung optimizations (AOD off; preserves GOS; never touches refresh-rate). The web GUI lets you untick individual features |
| `apply-120hz-endurance --yes` | Applies the reversible experimental profile only when the built-in display supports 120 Hz, then verifies 120 Hz remains available |
| `restrict-apps --level {ignore,deny,allow} --yes` | Restrict background apps |
| `revert` | Restores saved state for the selected device |
| `doctor-state` | Check saved state for non-restorable standby bucket entries |
| `whitelist list` | List whitelisted apps |
| `whitelist add <package>` | Add app to whitelist |
| `whitelist remove <package>` | Remove app from whitelist |
| `gui [--port N] [--no-browser]` | Start the local web dashboard (see below) |

### Global flags

- `--serial <id>`: Target a specific device serial.
- `--dry-run`: Show what would happen without making changes.
- `--state-dir <path>`: Use a custom directory for state and whitelist.

### Web GUI

```bash
android-battery-optimizer gui              # opens your browser automatically
android-battery-optimizer gui --no-browser --port 8765
android-battery-optimizer --dry-run gui    # shows a DRY RUN badge; nothing is written
```

`gui` starts a dashboard on `127.0.0.1`. The printed URL contains a random session token; requests without it get a 401. The dashboard covers the same operations as the CLI:

- Device and battery status, plus whether a rollback snapshot exists.
- The four optimization profiles. Experimental ones ask for confirmation first. The Samsung profile has a "Customize features" panel where you can untick individual features; vibration starts unticked because most people want to keep it.
- Diagnostics in a table you can sort and filter. Standby buckets appear by name (active, rare, restricted) instead of their numeric codes.
- Smart Restrict, with a preview you have to run before the apply button unlocks.
- Restrict All Third-Party Apps tells you how many apps it will touch, and how many whitelisted apps it will skip, before you confirm.
- Whitelist management with a search box over installed packages.
- An activity log with the warnings and `[dry-run]` lines from each action.

The server runs one operation at a time; a second request while one is in progress gets HTTP 409. It only listens on localhost. Stop it with `Ctrl-C`.

In a terminal, the CLI uses colors and aligned tables when stdout is a TTY. Set `NO_COLOR=1` to turn that off, or `FORCE_COLOR=1` to force it on.

### Smart restrict examples

`smart-restrict` is measurement-driven and conservative. It uses `diagnose` to find background activity and only restricts apps that go over the activity thresholds or that you target explicitly.

- `optimizer.py smart-restrict --dry-run`
- `optimizer.py smart-restrict --yes`
- `optimizer.py smart-restrict --aggressive --yes` (also disables `WAKE_LOCK` appops and removes restricted apps from the user Doze whitelist)
- `optimizer.py smart-restrict --min-last-used-days 14 --yes` (restricts apps unused for 14 days; on Android 12+ / SDK 31+, also applies App Hibernation)

### Diagnose examples

`diagnose` scans apps and gathers metrics since the last charge (alarm wakeups, partial-wakelock milliseconds, registered jobs) to recommend a restriction level for each. It also lists user-whitelisted apps that bypass Doze.

- `optimizer.py diagnose`
- `optimizer.py diagnose --output report.json`
- `optimizer.py diagnose --all-packages`

## Troubleshooting

- **ADB not found:** Install Android Platform Tools and make sure `adb` is in your PATH.
- **Unauthorized device:** Check the phone for the "Allow USB debugging?" prompt and select "Always allow".
- **Multiple devices:** Use `--serial <serial>` to pick one, or select from the list in interactive mode.
- **Restore refused:** The tool will not restore state to the wrong device. If you reinstalled the OS, the fingerprint may have changed; verify manually.
- **Corrupt state file:** A corrupted state file is renamed to `.corrupt.<timestamp>` and a fresh one is started.
- **Command timeout:** Some commands (like `bg-dexopt-job`) take a long time. The tool uses extended timeouts for them, but a bad cable or USB hub can still cause failures.

## Data location

Mutable files live under `~/.local/state/android-battery-optimizer/`:

- `devices/<serial>/whitelist.txt`: packages to exclude from restrictions on that device.
- `devices/<serial>/state.json`: the rollback snapshot for that device.

## Whitelist behavior

`restrict-apps` and `smart-restrict` skip whitelisted apps. Good candidates:

- Messaging and email apps, so notifications keep arriving
- Music or podcast apps, so playback does not cut off
- Companion device apps (smartwatches, Galaxy Wearable, and similar)

## References

- [Optimize for Doze and App Standby](https://developer.android.com/training/monitoring-device-state/doze-standby)
- [App Standby Buckets](https://developer.android.com/topic/performance/appstandby)
- [AOSP app background trackers](https://source.android.com/docs/core/power/trackers)
- [Android dumpsys battery diagnostics](https://developer.android.com/tools/dumpsys)
