import re
from contextlib import contextmanager
from typing import Dict, List, Optional, Sequence, cast

from .adb import AdbClient, CommandError
from .ledger import AnyLedgerEntry
from .operations import normalize_restorable_bucket
from .rollback import perform_rollback, restore_appop_value, restore_state
from .snapshot import (
    SnapshotError,
    prefetch_package_states,
    read_device_config_namespace,
    read_settings_namespace,
)
from .state import StateStore
from .verification import (
    VerificationError,
    normalize_value,
    parse_deviceidle_whitelist,
    parse_hibernation_state,
    parse_netpolicy_toggle,
    parse_netpolicy_whitelist,
    verify_app_hibernation,
    verify_appop,
    verify_device_config,
    verify_deviceidle_whitelist,
    verify_netpolicy_restrict_background,
    verify_netpolicy_whitelist,
    verify_package_enabled,
    verify_setting,
    verify_standby_bucket,
)

PACKAGE_USER_ID = "0"

class StateRecorder:
    def __init__(self, client: AdbClient, store: StateStore) -> None:
        self.client = client
        self.store = store
        self.verify = True
        self._in_transaction = False
        self._batched_commands: List[str] = []
        self._settings_cache: Dict[str, Dict[str, str]] = {}
        self._device_config_cache: Dict[str, Dict[str, str]] = {}
        self._appops_cache: Dict[str, Dict[str, str]] = {}
        self._standby_bucket_cache: Dict[str, str] = {}
        self._package_enabled_cache: Dict[str, bool] = {}
        self._ledger: List[AnyLedgerEntry] = []
        self._prefetch_package_enabled_success = False
        self._prefetch_appops_success = False
        self._prefetch_standby_bucket_success = False

    @staticmethod
    def _normalize_value(value: Optional[str]) -> Optional[str]:
        return normalize_value(value)

    @contextmanager
    def transaction(self):
        if self._in_transaction:
            yield
            return

        self._in_transaction = True
        self._batched_commands = []
        self._settings_cache.clear()
        self._device_config_cache.clear()
        self._appops_cache.clear()
        self._standby_bucket_cache.clear()
        self._package_enabled_cache.clear()
        self._ledger = []
        batch_dispatched = False

        try:
            with self.store.transaction():
                yield
                if self._batched_commands:
                    script_lines = []
                    for i, cmd in enumerate(self._batched_commands):
                        script_lines.append(f"{cmd} && echo \"SUCCESS_{i}\" || exit $?")
                    script = "\n".join(script_lines)
                    batch_dispatched = True
                    timeout = max(
                        AdbClient.DEFAULT_TIMEOUT_SECONDS,
                        min(
                            AdbClient.LONG_TIMEOUT_SECONDS,
                            2 * len(self._batched_commands)
                        )
                    )
                    self.client.shell([], mutate=True, input_data=script, timeout=timeout)
                    # If we reach here, batch shell succeeded.
                    if self.verify:
                        try:
                            from .verification import verify_entries_batched
                            verify_entries_batched(self.client, self._ledger)
                        except CommandError:
                            for entry in self._ledger:
                                self._verify_entry(entry)
        except Exception:
            if batch_dispatched:
                self._revert_ledger(list(range(len(self._ledger))))
            raise
        finally:
            self._in_transaction = False
            self._batched_commands = []
            self._settings_cache.clear()
            self._device_config_cache.clear()
            self._appops_cache.clear()
            self._standby_bucket_cache.clear()
            self._package_enabled_cache.clear()
            self._ledger = []

    def _queue_or_run(self, args: Sequence[object]) -> None:
        if self._in_transaction:
            cmd = self.client._format(self.client._stringify(args))
            self._batched_commands.append(cmd)
        else:
            self.client.shell(args, mutate=True)

    def _restore_appop_value(self, package: str, op: str, prior_value: Optional[str]) -> None:
        restore_appop_value(self.client, package, op, prior_value)

    def _revert_ledger(self, successful_indices: Optional[List[int]] = None) -> None:
        entries_to_revert = []
        if successful_indices is not None:
            # Revert in reverse order of indices to be safe
            for idx in sorted(successful_indices, reverse=True):
                if idx < len(self._ledger):
                    entries_to_revert.append(self._ledger[idx])
        else:
            entries_to_revert = list(reversed(self._ledger))

        had_failures = False
        for entry in entries_to_revert:
            try:
                self._perform_rollback(entry)
                self._remove_snapshot_for_entry(entry)
            except CommandError as exc:
                had_failures = True
                msg = f"Rollback failed for {entry}: {exc}"
                self.client.output(msg)
                self._persist_failed_rollback(entry)

        if had_failures:
            self.client.output("Warning: Partial state corruption due to rollback failures.")

        self.store.save_or_clear()

    def _perform_rollback(self, entry: AnyLedgerEntry) -> None:
        perform_rollback(self.client, entry)

    def _remove_snapshot_for_entry(self, entry: AnyLedgerEntry) -> None:
        type_ = entry["type"]
        if type_ == "setting":
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            self.store.data["settings"].pop(snapshot_key, None)
        elif type_ == "device_config":
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            self.store.data["device_config"].pop(snapshot_key, None)
        elif type_ == "appop":
            package = str(entry["package"])
            op = str(entry["op"])
            if package in self.store.data["packages"]:
                self.store.data["packages"][package]["appops"].pop(op, None)
                self._cleanup_package_entry(package)
        elif type_ == "standby_bucket":
            package = str(entry["package"])
            if package in self.store.data["packages"]:
                self.store.data["packages"][package]["standby_bucket"] = None
                self._cleanup_package_entry(package)
        elif type_ == "package_enabled":
            package = str(entry["package"])
            if package in self.store.data["packages"]:
                self.store.data["packages"][package]["enabled"] = None
                self._cleanup_package_entry(package)
        elif type_ == "netpolicy_restrict_background":
            self.store.data["netpolicy"].pop("restrict_background", None)
        elif type_ == "netpolicy_whitelist":
            uid = str(entry["uid"])
            self.store.data["netpolicy_whitelist"].pop(uid, None)
        elif type_ == "deviceidle_whitelist":
            package = str(entry["package"])
            self.store.data["deviceidle_whitelist"].pop(package, None)
        elif type_ == "app_hibernation":
            package = str(entry["package"])
            self.store.data["hibernation"].pop(package, None)

    def _remove_snapshots_for_entries(self, entries: List[AnyLedgerEntry]) -> None:
        for entry in entries:
            self._remove_snapshot_for_entry(entry)

    def _cleanup_package_entry(self, package: str) -> None:
        pkg = self.store.data["packages"].get(package)
        if pkg and not pkg["appops"] and pkg["standby_bucket"] is None and pkg["enabled"] is None:
            self.store.data["packages"].pop(package)

    def _persist_failed_rollback(self, entry: AnyLedgerEntry) -> None:
        type_ = entry["type"]
        if type_ == "setting":
            store = self.store.data["settings"]
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            if snapshot_key not in store:
                store[snapshot_key] = {
                    "namespace": entry["namespace"],
                    "key": entry["key"],
                    "value": entry.get("prior_value"),
                }
        elif type_ == "device_config":
            store = self.store.data["device_config"]
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            if snapshot_key not in store:
                store[snapshot_key] = {
                    "namespace": entry["namespace"],
                    "key": entry["key"],
                    "value": entry.get("prior_value"),
                }
        elif type_ == "appop":
            package = str(entry["package"])
            op = str(entry["op"])
            pkg_entry = self._package_entry(package)
            if op not in pkg_entry["appops"]:
                pkg_entry["appops"][op] = entry.get("prior_value")
        elif type_ == "standby_bucket":
            package = str(entry["package"])
            pkg_entry = self._package_entry(package)
            if pkg_entry["standby_bucket"] is None:
                pkg_entry["standby_bucket"] = entry.get("prior_value")
        elif type_ == "package_enabled":
            package = str(entry["package"])
            pkg_entry = self._package_entry(package)
            if pkg_entry["enabled"] is None:
                pkg_entry["enabled"] = entry.get("prior_value")
        elif type_ == "netpolicy_restrict_background":
            store = self.store.data["netpolicy"]
            if "restrict_background" not in store:
                store["restrict_background"] = entry.get("prior_value")
        elif type_ == "netpolicy_whitelist":
            store = self.store.data["netpolicy_whitelist"]
            uid = str(entry.get("uid"))
            if uid not in store:
                store[uid] = {
                    "package": entry.get("package"),
                    "prior_member": entry.get("prior_member"),
                }
        elif type_ == "deviceidle_whitelist":
            store = self.store.data["deviceidle_whitelist"]
            package = str(entry.get("package"))
            if package not in store:
                store[package] = entry.get("prior_member")
        elif type_ == "app_hibernation":
            store = self.store.data["hibernation"]
            package = str(entry.get("package"))
            if package not in store:
                store[package] = entry.get("prior_value")

    def prefetch_package_states(self) -> None:
        (
            self._prefetch_package_enabled_success,
            self._package_enabled_cache,
            self._prefetch_appops_success,
            self._appops_cache,
            self._prefetch_standby_bucket_success,
            self._standby_bucket_cache
        ) = prefetch_package_states(self.client)

    def _read_settings_namespace(self, namespace: str) -> Dict[str, str]:
        return read_settings_namespace(self.client, namespace)

    def _get_setting(self, namespace: str, key: str) -> Optional[str]:
        if namespace not in self._settings_cache:
            try:
                self._settings_cache[namespace] = self._read_settings_namespace(namespace)
            except SnapshotError as exc:
                raise SnapshotError(f"Failed to read setting {namespace}/{key}: {exc}") from exc
        return self._settings_cache[namespace].get(key)

    def snapshot_setting(self, namespace: str, key: str, new_value: Optional[str] = None) -> None:
        store = self.store.data["settings"]
        snapshot_key = f"{namespace}/{key}"
        value = self._get_setting(namespace, key)
        value = self._normalize_value(value)
        if snapshot_key not in store:
            store[snapshot_key] = {
                "namespace": namespace,
                "key": key,
                "value": value,
            }
            self.store.save()
        self._ledger.append(cast(AnyLedgerEntry, {
            "type": "setting",
            "namespace": namespace,
            "key": key,
            "prior_value": value,
            "new_value": self._normalize_value(new_value),
        }))

    def put_setting(self, namespace: str, key: str, value: object, verify: bool = True) -> None:
        self.snapshot_setting(namespace, key, new_value=str(value))
        self._queue_or_run(["settings", "put", namespace, key, value])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_setting(namespace, key, str(value))
            except VerificationError:
                self._revert_ledger()
                raise

    def delete_setting(self, namespace: str, key: str, verify: bool = True) -> None:
        self.snapshot_setting(namespace, key, new_value=None)
        self._queue_or_run(["settings", "delete", namespace, key])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_setting(namespace, key, None)
            except VerificationError:
                self._revert_ledger()
                raise

    def _read_device_config_namespace(self, namespace: str) -> Dict[str, str]:
        return read_device_config_namespace(self.client, namespace)

    def _get_device_config(self, namespace: str, key: str) -> Optional[str]:
        if namespace not in self._device_config_cache:
            try:
                self._device_config_cache[namespace] = self._read_device_config_namespace(namespace)
            except SnapshotError as exc:
                raise SnapshotError(f"Failed to read device_config {namespace}/{key}: {exc}") from exc
        return self._device_config_cache[namespace].get(key)

    def snapshot_device_config(self, namespace: str, key: str, new_value: Optional[str] = None) -> None:
        store = self.store.data["device_config"]
        snapshot_key = f"{namespace}/{key}"
        value = self._get_device_config(namespace, key)
        value = self._normalize_value(value)
        if snapshot_key not in store:
            store[snapshot_key] = {
                "namespace": namespace,
                "key": key,
                "value": value,
            }
            self.store.save()
        self._ledger.append(cast(AnyLedgerEntry, {
            "type": "device_config",
            "namespace": namespace,
            "key": key,
            "prior_value": value,
            "new_value": self._normalize_value(new_value),
        }))

    def put_device_config(self, namespace: str, key: str, value: object, verify: bool = True) -> None:
        self.snapshot_device_config(namespace, key, new_value=str(value))
        self._queue_or_run(["device_config", "put", namespace, key, value])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_device_config(namespace, key, str(value))
            except VerificationError:
                self._revert_ledger()
                raise

    def delete_device_config(self, namespace: str, key: str, verify: bool = True) -> None:
        self.snapshot_device_config(namespace, key, new_value=None)
        self._queue_or_run(["device_config", "delete", namespace, key])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_device_config(namespace, key, None)
            except VerificationError:
                self._revert_ledger()
                raise

    def _package_entry(self, package: str) -> Dict[str, object]:
        packages = self.store.data["packages"]
        return packages.setdefault(
            package,
            {
                "enabled": None,
                "appops": {},
                "standby_bucket": None,
            },
        )

    def _get_package_enabled(self, package: str) -> bool:
        if not self._prefetch_package_enabled_success or package not in self._package_enabled_cache:
            raise SnapshotError(f"Could not determine enabled state for package: {package}")
        return self._package_enabled_cache[package]

    def snapshot_package_enabled(self, package: str, new_value: Optional[bool] = None) -> None:
        entry = self._package_entry(package)
        value = self._get_package_enabled(package)
        if entry["enabled"] is None:
            entry["enabled"] = value
            self.store.save()
        self._ledger.append(cast(AnyLedgerEntry, {
            "type": "package_enabled",
            "package": package,
            "prior_value": value,
            "new_value": new_value,
        }))

    def _get_appop(self, package: str, op: str) -> str:
        if (
            self._prefetch_appops_success
            and package in self._appops_cache
            and op in self._appops_cache[package]
        ):
            return self._appops_cache[package][op]

        # Fallback read
        try:
            result = self.client.shell(["cmd", "appops", "get", package, op], check=False)
        except Exception as exc:
            raise SnapshotError(
                f"Failed to execute cmd appops get for package {package} op {op}: {exc}"
            ) from exc

        if result.returncode != 0:
            raise SnapshotError(
                f"cmd appops get failed for package {package} op {op} "
                f"with exit code {result.returncode}"
            )

        try:
            from .verification import parse_appop_output
            val = parse_appop_output(result.stdout)
        except Exception as exc:
            raise SnapshotError(
                f"Failed to parse appop output for package {package} op {op}: {exc}"
            ) from exc

        self._appops_cache.setdefault(package, {})[op] = val
        return val

    def snapshot_appop(self, package: str, op: str, new_value: Optional[str] = None) -> None:
        entry = self._package_entry(package)
        value = self._get_appop(package, op)
        if op not in entry["appops"]:
            entry["appops"][op] = value
            self.store.save()
        self._ledger.append(cast(AnyLedgerEntry, {
            "type": "appop",
            "package": package,
            "op": op,
            "prior_value": value,
            "new_value": new_value,
        }))

    def _get_standby_bucket(self, package: str) -> str:
        if self._prefetch_standby_bucket_success and package in self._standby_bucket_cache:
            return self._standby_bucket_cache[package]

        # Fallback to per-package am get-standby-bucket read
        try:
            result = self.client.shell(["am", "get-standby-bucket", package], check=False)
        except Exception as exc:
            raise SnapshotError(
                f"Failed to execute am get-standby-bucket for package {package}: {exc}"
            ) from exc

        if result.returncode != 0:
            raise SnapshotError(
                f"Failed to read standby bucket for package {package}: "
                f"exit code {result.returncode}"
            )

        val = result.stdout.strip()
        if not val or not re.match(r"^[0-9a-zA-Z_]+$", val):
            raise SnapshotError(
                f"Failed to read standby bucket for package {package}: "
                f"invalid output '{val}'"
            )

        self._standby_bucket_cache[package] = val
        return val

    def snapshot_standby_bucket(self, package: str, new_value: Optional[str] = None) -> None:
        entry = self._package_entry(package)
        value = self._get_standby_bucket(package)
        if entry["standby_bucket"] is None:
            entry["standby_bucket"] = value
            self.store.save()
        self._ledger.append(cast(AnyLedgerEntry, {
            "type": "standby_bucket",
            "package": package,
            "prior_value": value,
            "new_value": new_value,
        }))

    def set_package_enabled(self, package: str, enabled: bool, verify: bool = True) -> None:
        self.snapshot_package_enabled(package, new_value=enabled)
        command = ["pm", "enable", "--user", PACKAGE_USER_ID, package]
        if not enabled:
            command = ["pm", "disable-user", "--user", PACKAGE_USER_ID, package]
        self._queue_or_run(command)
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_package_enabled(package, enabled)
            except VerificationError:
                self._revert_ledger()
                raise

    def set_appop(self, package: str, op: str, value: str, verify: bool = True) -> None:
        self.snapshot_appop(package, op, new_value=value)
        self._queue_or_run(["cmd", "appops", "set", package, op, value])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_appop(package, op, value)
            except VerificationError:
                self._revert_ledger()
                raise

    def set_standby_bucket(self, package: str, bucket: str, verify: bool = True) -> None:
        prior_bucket = self._get_standby_bucket(package)
        try:
            normalize_restorable_bucket(prior_bucket)
        except ValueError as exc:
            raise SnapshotError(f"Prior standby bucket {prior_bucket} is not restorable: {exc}") from exc

        self.snapshot_standby_bucket(package, new_value=bucket)
        self._queue_or_run(["am", "set-standby-bucket", package, bucket])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_standby_bucket(package, bucket)
            except VerificationError:
                self._revert_ledger()
                raise

    def verify_setting(self, namespace: str, key: str, expected_value: Optional[str]) -> None:
        verify_setting(self.client, namespace, key, expected_value)

    def verify_device_config(self, namespace: str, key: str, expected_value: Optional[str]) -> None:
        verify_device_config(self.client, namespace, key, expected_value)

    def verify_appop(self, package: str, op: str, expected_value: str) -> None:
        verify_appop(self.client, package, op, expected_value)

    def verify_standby_bucket(self, package: str, expected_bucket: str) -> None:
        verify_standby_bucket(self.client, package, expected_bucket)

    def verify_package_enabled(self, package: str, expected_enabled: bool) -> None:
        verify_package_enabled(self.client, package, expected_enabled)

    def verify_netpolicy_restrict_background(
        self, expected_value: bool
    ) -> None:
        verify_netpolicy_restrict_background(self.client, expected_value)

    def verify_netpolicy_whitelist(
        self, package: str, uid: str, expected_member: bool
    ) -> None:
        verify_netpolicy_whitelist(self.client, package, uid, expected_member)

    def verify_deviceidle_whitelist(
        self, package: str, expected_member: bool
    ) -> None:
        verify_deviceidle_whitelist(self.client, package, expected_member)

    def verify_app_hibernation(
        self, package: str, expected_value: bool
    ) -> None:
        verify_app_hibernation(self.client, package, expected_value)

    def snapshot_netpolicy_restrict_background(self, new_value: bool) -> None:
        store = self.store.data["netpolicy"]
        if "restrict_background" not in store:
            try:
                res = self.client.shell(
                    ["cmd", "netpolicy", "get", "restrict-background"]
                )
                val = parse_netpolicy_toggle(res.stdout)
            except Exception as exc:
                raise SnapshotError(
                    f"Failed to read netpolicy toggle: {exc}"
                ) from exc
            store["restrict_background"] = val
            self.store.save()
        self._ledger.append(
            cast(
                AnyLedgerEntry,
                {
                    "type": "netpolicy_restrict_background",
                    "prior_value": store["restrict_background"],
                    "new_value": new_value,
                },
            )
        )

    def set_netpolicy_restrict_background(
        self, value: bool, verify: bool = True
    ) -> None:
        self.snapshot_netpolicy_restrict_background(new_value=value)
        val_str = "true" if value else "false"
        self._queue_or_run(
            ["cmd", "netpolicy", "set", "restrict-background", val_str]
        )
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_netpolicy_restrict_background(value)
            except VerificationError:
                self._revert_ledger()
                raise

    def get_package_uid(self, package: str) -> str:
        res = self.client.shell(["pm", "list", "packages", "-U", package])
        for line in res.stdout.splitlines():
            line = line.strip()
            match = re.match(
                r"^package:"
                + re.escape(package)
                + r"\s+uid:(\d+)(?:\s|$)",
                line,
            )
            if match:
                return match.group(1)
        raise SnapshotError(f"Could not resolve UID for package {package}")

    def snapshot_netpolicy_whitelist(
        self, package: str, uid: str, new_value: bool
    ) -> None:
        store = self.store.data["netpolicy_whitelist"]
        if uid not in store:
            try:
                res = self.client.shell(
                    [
                        "cmd",
                        "netpolicy",
                        "list",
                        "restrict-background-whitelist",
                    ]
                )
                current_whitelist = parse_netpolicy_whitelist(res.stdout)
            except Exception as exc:
                raise SnapshotError(
                    f"Failed to read netpolicy whitelist: {exc}"
                ) from exc
            prior_member = uid in current_whitelist
            store[uid] = {"package": package, "prior_member": prior_member}
            self.store.save()
        else:
            prior_member = store[uid]["prior_member"]

        self._ledger.append(
            cast(
                AnyLedgerEntry,
                {
                    "type": "netpolicy_whitelist",
                    "package": package,
                    "uid": uid,
                    "prior_member": prior_member,
                    "new_value": new_value,
                },
            )
        )

    def add_netpolicy_whitelist(
        self, package: str, verify: bool = True
    ) -> None:
        uid = self.get_package_uid(package)
        self.snapshot_netpolicy_whitelist(package, uid, new_value=True)
        self._queue_or_run(
            ["cmd", "netpolicy", "add", "restrict-background-whitelist", uid]
        )
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_netpolicy_whitelist(package, uid, True)
            except VerificationError:
                self._revert_ledger()
                raise

    def snapshot_deviceidle_whitelist(
        self, package: str, new_value: bool
    ) -> None:
        store = self.store.data["deviceidle_whitelist"]
        if package not in store:
            try:
                res = self.client.shell(["cmd", "deviceidle", "whitelist"])
                user_whitelist = parse_deviceidle_whitelist(res.stdout)
            except Exception as exc:
                raise SnapshotError(
                    f"Failed to read deviceidle whitelist: {exc}"
                ) from exc
            prior_member = package in user_whitelist
            store[package] = prior_member
            self.store.save()
        else:
            prior_member = store[package]

        self._ledger.append(
            cast(
                AnyLedgerEntry,
                {
                    "type": "deviceidle_whitelist",
                    "package": package,
                    "prior_member": prior_member,
                    "new_value": new_value,
                },
            )
        )

    def remove_deviceidle_whitelist(
        self, package: str, verify: bool = True
    ) -> None:
        self.snapshot_deviceidle_whitelist(package, new_value=False)
        self._queue_or_run(["cmd", "deviceidle", "whitelist", f"-{package}"])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_deviceidle_whitelist(package, False)
            except VerificationError:
                self._revert_ledger()
                raise

    def snapshot_app_hibernation(self, package: str, new_value: bool) -> None:
        store = self.store.data["hibernation"]
        if package not in store:
            try:
                res = self.client.shell(
                    ["cmd", "app_hibernation", "get-state", package]
                )
                val = parse_hibernation_state(res.stdout)
            except Exception as exc:
                raise SnapshotError(
                    f"Failed to read hibernation state for {package}: {exc}"
                ) from exc
            store[package] = val
            self.store.save()
        else:
            val = store[package]

        self._ledger.append(
            cast(
                AnyLedgerEntry,
                {
                    "type": "app_hibernation",
                    "package": package,
                    "prior_value": val,
                    "new_value": new_value,
                },
            )
        )

    def set_app_hibernation(
        self, package: str, value: bool, verify: bool = True
    ) -> None:
        self.snapshot_app_hibernation(package, new_value=value)
        val_str = "true" if value else "false"
        self._queue_or_run(
            ["cmd", "app_hibernation", "set-state", package, val_str]
        )
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_app_hibernation(package, value)
            except VerificationError:
                self._revert_ledger()
                raise

    def _verify_entry(self, entry: AnyLedgerEntry) -> None:
        type_ = entry["type"]
        new_value = entry.get("new_value")
        if type_ == "setting":
            self.verify_setting(
                str(entry["namespace"]),
                str(entry["key"]),
                str(new_value) if new_value is not None else None,
            )
        elif type_ == "device_config":
            self.verify_device_config(
                str(entry["namespace"]),
                str(entry["key"]),
                str(new_value) if new_value is not None else None,
            )
        elif type_ == "appop":
            self.verify_appop(str(entry["package"]), str(entry["op"]), str(new_value))
        elif type_ == "standby_bucket":
            self.verify_standby_bucket(str(entry["package"]), str(new_value))
        elif type_ == "package_enabled":
            self.verify_package_enabled(str(entry["package"]), bool(new_value))
        elif type_ == "netpolicy_restrict_background":
            self.verify_netpolicy_restrict_background(bool(new_value))
        elif type_ == "netpolicy_whitelist":
            self.verify_netpolicy_whitelist(
                str(entry["package"]), str(entry["uid"]), bool(new_value)
            )
        elif type_ == "deviceidle_whitelist":
            self.verify_deviceidle_whitelist(str(entry["package"]), bool(new_value))
        elif type_ == "app_hibernation":
            self.verify_app_hibernation(str(entry["package"]), bool(new_value))

    def restore(self) -> List[str]:
        return restore_state(self.client, self.store, self._remove_snapshot_for_entry)
