import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from .adb import AdbClient, CommandError, SubprocessRunner
from .android import parse_adb_devices, resolve_package_choice
from .app import BatteryOptimizerApp
from .recorder import SnapshotError, VerificationError

APP_NAME = "android-battery-optimizer"
DEFAULT_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
)

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    # Parent parser for global options
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--serial", help="ADB device serial to use")
    parent_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print mutating adb commands instead of executing them",
    )
    parent_parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Directory for whitelist and saved rollback state",
    )

    parser = argparse.ArgumentParser(
        description="Android battery optimizer", parents=[parent_parser]
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    def add_subparser(name, **kwargs):
        return subparsers.add_parser(name, parents=[parent_parser], **kwargs)

    add_subparser("status", help="Checks ADB environment and device info")
    add_subparser("apply-safe", help="Applies documented safe optimizations")

    parser_exp = add_subparser("apply-experimental", help="Applies experimental optimizations")
    parser_exp.add_argument("--yes", action="store_true", help="Confirm experimental optimizations")

    parser_sam = add_subparser("apply-samsung-experimental", help="Applies Samsung experimental optimizations")
    parser_sam.add_argument("--yes", action="store_true", help="Confirm Samsung experimental optimizations")

    parser_restrict = add_subparser("restrict-apps", help="Restrict background apps")
    parser_restrict.add_argument("--level", choices=["ignore", "deny", "allow"], default="ignore")
    parser_restrict.add_argument("--yes", action="store_true", help="Confirm restriction")

    parser_diagnose = add_subparser("diagnose", help="Run battery diagnostics")
    parser_diagnose.add_argument("--output", help="Save report to specified JSON file")

    # We want --third-party-only to be the default, but let user toggle it with --all-packages
    # We'll use a dest variable that defaults to True for third_party_only.
    # The requirement is: --third-party-only / --all-packages, default third-party only
    diag_group = parser_diagnose.add_mutually_exclusive_group()
    diag_group.add_argument("--third-party-only", action="store_true", default=True, help="Only diagnose third-party apps (default)")
    diag_group.add_argument("--all-packages", action="store_false", dest="third_party_only", help="Diagnose all apps")

    parser_smart_restrict = add_subparser("smart-restrict", help="Intelligently restrict apps based on usage")
    parser_smart_restrict.add_argument("--yes", action="store_true", help="Confirm smart restriction")
    parser_smart_restrict.add_argument("--aggressive", action="store_true", help="Use aggressive restriction mode")
    parser_smart_restrict.add_argument("--min-last-used-days", type=int, help="Skip restriction for apps used within this many days")

    add_subparser("revert", help="Reverts saved state for selected serial")

    parser_wl = add_subparser("whitelist", help="Manage whitelist")
    wl_sub = parser_wl.add_subparsers(dest="wl_command")
    wl_sub.add_parser("list", help="List whitelisted apps", parents=[parent_parser])

    parser_wl_add = wl_sub.add_parser("add", help="Add app to whitelist", parents=[parent_parser])
    parser_wl_add.add_argument("package", help="Package name")

    parser_wl_remove = wl_sub.add_parser("remove", help="Remove app from whitelist", parents=[parent_parser])
    parser_wl_remove.add_argument("package", help="Package name")

    add_subparser(
        "doctor-state",
        help="Check saved state for non-restorable standby bucket entries"
    )

    return parser.parse_args(argv)

class BatteryOptimizerCLI:
    def __init__(
        self,
        app: BatteryOptimizerApp,
        output: Callable[[str], None] = print,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        self.app = app
        self.client = app.client
        self.output = output
        self.input = input_fn

    def check_environment(self) -> bool:
        if not self.client.adb_exists():
            self.output("ADB was not found in PATH. Install Android Platform Tools first.")
            return False

        devices = parse_adb_devices(self.client.local_text(["adb", "devices"], check=False))
        if not devices:
            self.output("No ADB devices detected. Connect a device and authorize USB debugging.")
            return False

        if self.client.serial:
            matching = [device for device in devices if device["serial"] == self.client.serial]
            if not matching:
                self.output(f"Device {self.client.serial} was not found in `adb devices` output.")
                return False
            if matching[0]["status"] != "device":
                self.output(
                    f"Device {self.client.serial} is {matching[0]['status']}. Resolve that before continuing."
                )
                return False
            return True

        ready = [device for device in devices if device["status"] == "device"]
        blocked = [device for device in devices if device["status"] != "device"]
        for device in blocked:
            self.output(
                f"Skipping device {device['serial']} because it is {device['status']}."
            )

        if not ready:
            self.output("No authorized online device is available.")
            return False

        if len(ready) == 1:
            self.client.serial = ready[0]["serial"]
            self.app.rebind_device()
            return True

        self.output("Multiple devices detected:")
        for index, device in enumerate(ready, start=1):
            self.output(f"  {index}. {device['serial']}")
        choice = self.input("Select device number: ").strip()
        if not choice.isdigit():
            self.output("Invalid device selection.")
            return False
        selected = int(choice)
        if selected < 1 or selected > len(ready):
            self.output("Invalid device selection.")
            return False
        self.client.serial = ready[selected - 1]["serial"]
        self.app.rebind_device()
        return True

    def confirm(self, prompt: str) -> bool:
        answer = self.input(f"{prompt} [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    def confirm_experimental(self, label: str) -> bool:
        return self.confirm(
            f"{label} may affect notifications, sync, or device stability. Continue?"
        )

    def check_battery(self) -> None:
        self.output("\n--- Battery Status ---")
        self.output(self.client.shell_text(["dumpsys", "battery"], check=False))
        self.output("\n--- BatteryStats Summary (Since Charged) ---")
        output = self.client.shell_text(["dumpsys", "batterystats", "--charged"], check=False)
        found = False
        for line in output.splitlines():
            stripped = line.strip()
            if any(token in stripped for token in ("Estimated power use", "Capacity:", "Computed drain:")):
                found = True
                self.output(stripped)
                continue
            if found:
                if stripped and ("mAh" in stripped or ":" in stripped):
                    self.output(stripped)
                elif stripped and not line.startswith("  "):
                    break

    def manage_whitelist(self) -> None:
        whitelist = self.app.load_whitelist()
        installed = self.app.get_packages(third_party=True)
        while True:
            self.output("\n--- Whitelist Management ---")
            if whitelist:
                for index, package in enumerate(whitelist, start=1):
                    self.output(f"  {index}. {package}")
            else:
                self.output("  (empty)")

            self.output("\n1. Add App to Whitelist")
            self.output("2. Remove App from Whitelist")
            self.output("3. Back")
            choice = self.input("Select an option: ").strip()
            if choice == "1":
                query = self.input("Enter package name or a search term: ").strip()
                matches = resolve_package_choice(query, installed)
                if not matches:
                    self.output("No installed packages matched that query.")
                    continue
                if len(matches) == 1:
                    package = matches[0]
                else:
                    for index, package in enumerate(matches, start=1):
                        self.output(f"  {index}. {package}")
                    selected = self.input("Select number to add (or 0 to cancel): ").strip()
                    if not selected.isdigit():
                        self.output("Invalid selection.")
                        continue
                    item = int(selected)
                    if item == 0:
                        continue
                    if item < 1 or item > len(matches):
                        self.output("Invalid selection.")
                        continue
                    package = matches[item - 1]
                if package not in whitelist:
                    whitelist.append(package)
                    whitelist.sort()
                    self.app.save_whitelist(whitelist)
                    self.output(f"Added {package}.")
                else:
                    self.output(f"{package} is already whitelisted.")
            elif choice == "2":
                if not whitelist:
                    self.output("Whitelist is empty.")
                    continue
                selected = self.input("Enter number to remove: ").strip()
                if not selected.isdigit():
                    self.output("Invalid selection.")
                    continue
                item = int(selected)
                if item < 1 or item > len(whitelist):
                    self.output("Invalid selection.")
                    continue
                removed = whitelist.pop(item - 1)
                self.app.save_whitelist(whitelist)
                self.output(f"Removed {removed}.")
            elif choice == "3":
                return
            else:
                self.output("Invalid selection.")

    def run_command(self, args: argparse.Namespace) -> int:
        if not self.check_environment():
            return 1

        try:
            if args.command == "status":
                self.output(f"Selected device: {self.client.serial}")
                self.output(f"Device info: {self.app.get_device_info()}")
                has_rollback = self.app.store.has_entries()
                self.output(f"Rollback state exists: {has_rollback}")
                return 0

            elif args.command == "apply-safe":
                self.output("Applying documented safe optimizations...")
                self.app.apply_documented_safe_optimizations()
                self.output("Applied abusive-app auto restriction tracking from AOSP documentation.")
                return 0

            elif args.command == "apply-experimental":
                if not args.yes:
                    self.output("Error: --yes is required for experimental optimizations in non-interactive mode.")
                    return 1
                self.output("Applying experimental optimizations...")
                self.app.apply_experimental_optimizations()
                self.output("Experimental optimizations applied.")
                return 0

            elif args.command == "apply-samsung-experimental":
                if not args.yes:
                    self.output("Error: --yes is required for Samsung experimental optimizations in non-interactive mode.")
                    return 1
                self.output("Applying Samsung experimental optimizations...")
                self.app.apply_samsung_experimental_optimizations()
                self.output("Samsung experimental optimizations applied.")
                return 0

            elif args.command == "restrict-apps":
                if not args.yes and not args.dry_run:
                    self.output("Error: --yes is required for restricting apps unless --dry-run is used.")
                    return 1
                self.output(f"Setting RUN_ANY_IN_BACKGROUND={args.level} for third-party apps...")
                res = self.app.restrict_background_apps(level=args.level)
                for pkg in res["skipped_whitelisted"]:
                    self.output(f"  Skipping whitelisted app: {pkg}")
                for pkg in res["skipped_non_restorable"]:
                    self.output(f"  Skipping app with non-restorable standby bucket: {pkg}")
                self.output("Background restrictions updated.")
                return 0

            elif args.command == "diagnose":
                import json
                self.output("Running diagnostics. This may take a moment...")
                report = self.app.diagnose(third_party_only=args.third_party_only)

                if report["warnings"]:
                    self.output("\nWarnings:")
                    for w in report["warnings"]:
                        self.output(f"  {w}")

                self.output("\nDiagnosis Summary:")
                for pkg in report["packages"]:
                    self.output(
                        f"  {pkg['package']}: {pkg['recommendation']} "
                        f"({pkg['reason']})"
                    )

                if report.get("doze_whitelist_user"):
                    self.output("\nApps bypassing Doze (user whitelisted):")
                    for pkg in report["doze_whitelist_user"]:
                        self.output(f"  {pkg} (bypasses Doze)")

                if args.output:
                    with open(args.output, "w") as f:
                        json.dump(report, f, indent=2)
                    self.output(f"\nReport saved to {args.output}")
                return 0

            elif args.command == "smart-restrict":
                if not args.yes and not args.dry_run:
                    self.output("Error: --yes is required for smart-restrict unless --dry-run is used.")
                    return 1

                mode = "aggressive" if args.aggressive else "balanced"
                self.output(f"Running smart-restrict in {mode} mode...")

                result = self.app.smart_restrict(
                    aggressive=args.aggressive,
                    min_last_used_days=args.min_last_used_days
                )

                if result.get("warnings"):
                    self.output("\nWarnings:")
                    for w in result["warnings"]:
                        self.output(f"  {w}")

                applied = result.get("applied", [])
                skipped = result.get("skipped", [])
                kept = result.get("kept", [])

                self.output("\nSmart restrict summary:")
                self.output(f"  Restricted: {len(applied)}")
                self.output(f"  Skipped: {len(skipped)}")
                self.output(f"  Kept: {len(kept)}")

                if applied:
                    if args.dry_run:
                        self.output("\nWould restrict (dry-run):")
                    else:
                        self.output("\nRestricted:")
                    for item in applied:
                        msg = (
                            f"  {item['package']} -> "
                            f"RUN_ANY_IN_BACKGROUND={item['appop']}, "
                            f"bucket={item['bucket']}"
                        )
                        if "wake_lock" in item:
                            msg += f", WAKE_LOCK={item['wake_lock']}"
                        self.output(msg)
                        self.output(f"    Reason: {item['reason']}")

                if skipped:
                    self.output("\nSkipped:")
                    for item in skipped:
                        self.output(f"  {item['package']} -> {item['reason']}")

                if not args.dry_run:
                    self.output("\nSmart restrict applied successfully.")
                return 0

            elif args.command == "revert":
                self.output("Restoring saved state...")
                messages = self.app.revert_saved_state()
                if not messages:
                    self.output("No saved state found to restore.")
                else:
                    for msg in messages:
                        self.output(f"  {msg}")
                    self.output("Restore finished.")
                return 0

            elif args.command == "doctor-state":
                self.output("Checking saved state for non-restorable standby bucket entries...")
                count = 0
                packages = self.app.store.data.get("packages", {})
                from .operations import is_restorable_bucket
                for package, item in packages.items():
                    bucket = item.get("standby_bucket")
                    if bucket is not None and not is_restorable_bucket(bucket):
                        self.output(f"  Package {package} has non-restorable prior standby bucket: {bucket}")
                        count += 1
                if count == 0:
                    self.output("No non-restorable entries found.")
                else:
                    self.output(f"Found {count} non-restorable entries. Manual intervention may be required upon restore.")
                return 0

            elif args.command == "whitelist":
                whitelist = self.app.load_whitelist()
                if args.wl_command == "list":
                    if not whitelist:
                        self.output("Whitelist is empty.")
                    for pkg in whitelist:
                        self.output(pkg)
                elif args.wl_command == "add":
                    try:
                        self.app.validate_package(args.package)
                    except ValueError as exc:
                        self.output(f"Error: {exc}")
                        return 1
                    if args.package not in whitelist:
                        whitelist.append(args.package)
                        whitelist.sort()
                        self.app.save_whitelist(whitelist)
                        self.output(f"Added {args.package} to whitelist.")
                    else:
                        self.output(f"{args.package} is already whitelisted.")
                elif args.wl_command == "remove":
                    if args.package in whitelist:
                        whitelist.remove(args.package)
                        self.app.save_whitelist(whitelist)
                        self.output(f"Removed {args.package} from whitelist.")
                    else:
                        self.output(f"{args.package} not found in whitelist.")
                return 0

        except (CommandError, ValueError, SnapshotError, VerificationError) as exc:
            self.output(f"Error: {exc}")
            return 1
        return 0

    def run(self) -> int:
        if not self.check_environment():
            return 1

        device = self.app.get_device_info()
        self.output(f"Connected to: {device}")
        while True:
            self.output("\n--- Android Battery Optimizer ---")
            self.output("1. Check Battery Status")
            self.output("2. Apply Documented Safe Optimizations")
            self.output("3. Apply Experimental Optimizations")
            self.output("4. Apply Samsung Experimental Optimizations")
            self.output("5. Restrict 3rd Party Apps (Experimental, with Whitelist)")
            self.output("6. Manage Whitelist")
            self.output("7. Run Background Optimization (Dexopt, Experimental)")
            self.output("8. Revert Saved State")
            self.output("9. Exit")
            choice = self.input("\nSelect an option: ").strip()
            try:
                if choice == "1":
                    self.check_battery()
                elif choice == "2":
                    self.output("Applying documented safe optimizations...")
                    self.app.apply_documented_safe_optimizations()
                    self.output("Applied abusive-app auto restriction tracking from AOSP documentation.")
                elif choice == "3":
                    if not self.confirm_experimental("Experimental optimizations"):
                        self.output("Skipped experimental optimizations.")
                        continue
                    self.output("Applying experimental optimizations...")
                    self.app.apply_experimental_optimizations()
                    self.output("Experimental optimizations applied.")
                elif choice == "4":
                    brand = self.app.get_device_info()
                    if "samsung" not in brand.lower():
                        self.output("Connected device is not Samsung.")
                        continue
                    if not self.confirm_experimental("Samsung experimental optimizations"):
                        self.output("Skipped Samsung experimental optimizations.")
                        continue
                    self.output("Applying Samsung experimental optimizations...")
                    self.app.apply_samsung_experimental_optimizations()
                    self.output("Samsung experimental optimizations applied.")
                elif choice == "5":
                    if not self.confirm_experimental("Third-party app background restrictions"):
                        self.output("Skipped third-party app restrictions.")
                        continue
                    self.output("Setting RUN_ANY_IN_BACKGROUND=ignore for third-party apps...")
                    res = self.app.restrict_background_apps(level="ignore")
                    for pkg in res["skipped_whitelisted"]:
                        self.output(f"  Skipping whitelisted app: {pkg}")
                    for pkg in res["skipped_non_restorable"]:
                        self.output(f"  Skipping app with non-restorable standby bucket: {pkg}")
                    self.output("Background restrictions updated.")
                elif choice == "6":
                    self.manage_whitelist()
                elif choice == "7":
                    if not self.confirm_experimental("Background dexopt job"):
                        self.output("Skipped dexopt.")
                        continue
                    self.output("Triggering background package optimization (dexopt)...")
                    self.app.run_bg_dexopt()
                    self.output("Dexopt job triggered.")
                elif choice == "8":
                    self.output("Restoring saved state...")
                    messages = self.app.revert_saved_state()
                    if not messages:
                        self.output("No saved state found to restore.")
                    else:
                        for msg in messages:
                            self.output(f"  {msg}")
                        self.output("Restore finished.")
                elif choice == "9":
                    return 0
                else:
                    self.output("Invalid selection.")
            except (CommandError, ValueError, SnapshotError, VerificationError) as exc:
                self.output(f"Error: {exc}")

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    state_dir = Path(args.state_dir).expanduser()
    client = AdbClient(
        runner=SubprocessRunner(),
        serial=args.serial,
        dry_run=args.dry_run,
    )
    app = BatteryOptimizerApp(client=client, state_dir=state_dir)
    cli = BatteryOptimizerCLI(app=app)
    if args.command:
        return cli.run_command(args)
    return cli.run()


if __name__ == "__main__":
    sys.exit(main())
