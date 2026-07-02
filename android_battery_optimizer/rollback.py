from typing import Callable, Dict, List, Optional, cast

from .adb import AdbClient, CommandError
from .ledger import AnyLedgerEntry
from .state import StateStore
from .verification import (
    VerificationError,
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

def restore_appop_value(client: AdbClient, package: str, op: str, prior_value: Optional[str]) -> None:
    value = "default" if prior_value is None else str(prior_value)
    client.shell(["cmd", "appops", "set", package, op, value], mutate=True)

def perform_rollback(client: AdbClient, entry: AnyLedgerEntry) -> None:
    type_ = entry["type"]
    if type_ == "setting":
        namespace = str(entry["namespace"])
        key = str(entry["key"])
        prior_value = entry.get("prior_value")
        if prior_value is None:
            client.shell(["settings", "delete", namespace, key], mutate=True)
        else:
            client.shell(["settings", "put", namespace, key, prior_value], mutate=True)
    elif type_ == "device_config":
        namespace = str(entry["namespace"])
        key = str(entry["key"])
        prior_value = entry.get("prior_value")
        if prior_value is None:
            client.shell(["device_config", "delete", namespace, key], mutate=True)
        else:
            client.shell(["device_config", "put", namespace, key, prior_value], mutate=True)
    elif type_ == "appop":
        package = str(entry["package"])
        op = str(entry["op"])
        prior_value = entry.get("prior_value")
        restore_appop_value(client, package, op, cast(Optional[str], prior_value))
    elif type_ == "standby_bucket":
        package = str(entry["package"])
        prior_value = entry.get("prior_value")
        if prior_value:
            client.shell(["am", "set-standby-bucket", package, str(prior_value)], mutate=True)
    elif type_ == "package_enabled":
        package = str(entry["package"])
        prior_value = bool(entry.get("prior_value"))
        command = ["pm", "enable", "--user", PACKAGE_USER_ID, package]
        if not prior_value:
            command = ["pm", "disable-user", "--user", PACKAGE_USER_ID, package]
        client.shell(command, mutate=True)
    elif type_ == "netpolicy_restrict_background":
        prior_val = bool(entry.get("prior_value"))
        val_str = "true" if prior_val else "false"
        client.shell(
            ["cmd", "netpolicy", "set", "restrict-background", val_str],
            mutate=True,
        )
    elif type_ == "netpolicy_whitelist":
        uid = str(entry.get("uid"))
        prior_mem = bool(entry.get("prior_member"))
        if prior_mem:
            client.shell(
                [
                    "cmd",
                    "netpolicy",
                    "add",
                    "restrict-background-whitelist",
                    uid,
                ],
                mutate=True,
            )
        else:
            client.shell(
                [
                    "cmd",
                    "netpolicy",
                    "remove",
                    "restrict-background-whitelist",
                    uid,
                ],
                mutate=True,
            )
    elif type_ == "deviceidle_whitelist":
        package = str(entry.get("package"))
        prior_mem = bool(entry.get("prior_member"))
        action = f"+{package}" if prior_mem else f"-{package}"
        client.shell(
            ["cmd", "deviceidle", "whitelist", action],
            mutate=True,
        )
    elif type_ == "app_hibernation":
        package = str(entry.get("package"))
        prior_val = bool(entry.get("prior_value"))
        val_str = "true" if prior_val else "false"
        client.shell(
            ["cmd", "app_hibernation", "set-state", package, val_str],
            mutate=True,
        )

def restore_and_verify(
    restore_action: Callable[[], object],
    verify_action: Callable[[], None],
    recover_if_verified: bool = False,
) -> bool:
    try:
        restore_action()
    except CommandError:
        if recover_if_verified:
            # The write path can be permanently blocked (e.g. the Android 14+
            # device_config allowlist) while the device already matches the
            # snapshot; treat that as restored instead of failing forever.
            try:
                verify_action()
                return False
            except (CommandError, VerificationError):
                pass
        raise
    verify_action()
    return True

def _describe(did_write: bool, label: str) -> str:
    if did_write:
        return f"Restored {label}"
    return f"Already at saved value: {label}"

def restore_state(
    client: AdbClient,
    store: StateStore,
    remove_snapshot_for_entry: Callable[[AnyLedgerEntry], None]
) -> List[str]:
    if client.serial is None and not client.dry_run:
        raise CommandError("Refusing to restore device state without a selected ADB serial.")

    current_metadata = client.get_device_metadata_with_fallback()
    saved_device = cast(Dict[str, object], store.data.get("device") or {})

    messages: List[str] = []

    if saved_device:
        saved_serial = saved_device.get("serial")
        if saved_serial is not None and client.serial != saved_serial:
            raise ValueError(
                f"Device serial mismatch: current={client.serial}, "
                f"saved={saved_serial}"
            )

        current_fp = current_metadata.get("fingerprint") or ""
        saved_fp = saved_device.get("fingerprint") or ""
        if saved_fp and current_fp:
            if current_fp != saved_fp:
                raise ValueError(
                    f"Device fingerprint mismatch: current={current_fp}, saved={saved_fp}"
                )
        elif saved_fp and not current_fp:
            warning = (
                "Warning: could not verify device fingerprint; proceeding with serial match only."
            )
            messages.append(warning)
            client.output(warning)

    had_failures = False
    settings = cast(Dict[str, Dict[str, object]], store.data.get("settings", {}))
    device_config = cast(Dict[str, Dict[str, object]], store.data.get("device_config", {}))
    packages = cast(Dict[str, Dict[str, object]], store.data.get("packages", {}))

    for item in list(settings.values()):
        namespace = cast(str, item["namespace"])
        key = cast(str, item["key"])
        value = cast(Optional[str], item["value"])
        try:
            if value is None:
                did_write = restore_and_verify(
                    lambda: client.shell(["settings", "delete", namespace, key], mutate=True),
                    lambda: verify_setting(client, namespace, key, None),
                    recover_if_verified=not client.dry_run,
                )
            else:
                did_write = restore_and_verify(
                    lambda: client.shell(
                        ["settings", "put", namespace, key, value],
                        mutate=True,
                    ),
                    lambda: verify_setting(client, namespace, key, value),
                    recover_if_verified=not client.dry_run,
                )
            messages.append(_describe(did_write, f"setting {namespace}/{key}"))
            if not client.dry_run:
                remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                    "type": "setting",
                    "namespace": namespace,
                    "key": key,
                }))
        except (CommandError, VerificationError) as exc:
            had_failures = True
            msg = f"Failed to restore setting {namespace}/{key}: {exc}"
            messages.append(msg)
            client.output(msg)

    for item in list(device_config.values()):
        namespace = cast(str, item["namespace"])
        key = cast(str, item["key"])
        value = cast(Optional[str], item["value"])
        try:
            if value is None:
                did_write = restore_and_verify(
                    lambda: client.shell(
                        ["device_config", "delete", namespace, key],
                        mutate=True,
                    ),
                    lambda: verify_device_config(client, namespace, key, None),
                    recover_if_verified=not client.dry_run,
                )
            else:
                did_write = restore_and_verify(
                    lambda: client.shell(
                        ["device_config", "put", namespace, key, value],
                        mutate=True,
                    ),
                    lambda: verify_device_config(client, namespace, key, value),
                    recover_if_verified=not client.dry_run,
                )
            messages.append(_describe(did_write, f"device_config {namespace}/{key}"))
            if not client.dry_run:
                remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                    "type": "device_config",
                    "namespace": namespace,
                    "key": key,
                }))
        except (CommandError, VerificationError) as exc:
            had_failures = True
            msg = f"Failed to restore device_config {namespace}/{key}: {exc}"
            messages.append(msg)
            client.output(msg)

    for package, item in list(packages.items()):
        appops = cast(Dict[str, Optional[str]], item.get("appops", {}))
        for op, value in list(appops.items()):
            try:
                did_write = restore_and_verify(
                    lambda: restore_appop_value(client, package, op, value),
                    lambda: verify_appop(client, package, op, str(value)),
                    recover_if_verified=not client.dry_run,
                )
                messages.append(_describe(did_write, f"{package} appop {op}"))
                if not client.dry_run:
                    remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                        "type": "appop",
                        "package": package,
                        "op": op,
                    }))
            except (CommandError, VerificationError) as exc:
                had_failures = True
                msg = f"Failed to restore {package} appop {op}: {exc}"
                messages.append(msg)
                client.output(msg)

        bucket = cast(Optional[str], item.get("standby_bucket"))
        if bucket is not None:
            try:
                from .operations import is_restorable_bucket
                if not is_restorable_bucket(bucket):
                    msg = f"Saved standby bucket {bucket} is not writable on this device; cannot restore automatically.\nManual fallback: adb shell am set-standby-bucket {package} active"
                    client.output(msg)
                    messages.append(msg)
                    had_failures = True
                    continue

                did_write = restore_and_verify(
                    lambda: client.shell(
                        ["am", "set-standby-bucket", package, bucket],
                        mutate=True,
                    ),
                    lambda: verify_standby_bucket(client, package, str(bucket)),
                    recover_if_verified=not client.dry_run,
                )
                messages.append(_describe(did_write, f"{package} standby bucket"))
                if not client.dry_run:
                    remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                        "type": "standby_bucket",
                        "package": package,
                    }))
            except (CommandError, VerificationError) as exc:
                had_failures = True
                msg = f"Failed to restore {package} standby bucket: {exc}"
                messages.append(msg)
                client.output(msg)

        enabled = cast(Optional[bool], item.get("enabled"))
        if enabled is not None:
            try:
                command = ["pm", "enable", "--user", PACKAGE_USER_ID, package]
                if not enabled:
                    command = ["pm", "disable-user", "--user", PACKAGE_USER_ID, package]
                did_write = restore_and_verify(
                    lambda: client.shell(command, mutate=True),
                    lambda: verify_package_enabled(client, package, enabled),
                    recover_if_verified=not client.dry_run,
                )
                messages.append(_describe(did_write, f"{package} enabled state"))
                if not client.dry_run:
                    remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                        "type": "package_enabled",
                        "package": package,
                    }))
            except (CommandError, VerificationError) as exc:
                had_failures = True
                msg = f"Failed to restore {package} enabled state: {exc}"
                messages.append(msg)
                client.output(msg)

    netpolicy = cast(Dict[str, bool], store.data.get("netpolicy", {}))
    for key, value in list(netpolicy.items()):
        if key == "restrict_background":
            try:
                did_write = restore_and_verify(
                    lambda: client.shell(
                        [
                            "cmd",
                            "netpolicy",
                            "set",
                            "restrict-background",
                            "true" if value else "false",
                        ],
                        mutate=True,
                    ),
                    lambda: verify_netpolicy_restrict_background(
                        client, value
                    ),
                    recover_if_verified=not client.dry_run,
                )
                messages.append(
                    _describe(did_write, "netpolicy restrict-background")
                )
                if not client.dry_run:
                    remove_snapshot_for_entry(
                        cast(
                            AnyLedgerEntry,
                            {"type": "netpolicy_restrict_background"},
                        )
                    )
            except (CommandError, VerificationError) as exc:
                had_failures = True
                msg = f"Failed to restore netpolicy restrict-background: {exc}"
                messages.append(msg)
                client.output(msg)

    netpolicy_whitelist = cast(
        Dict[str, Dict[str, object]],
        store.data.get("netpolicy_whitelist", {}),
    )
    for uid, item in list(netpolicy_whitelist.items()):
        package = cast(str, item["package"])
        prior_member = cast(bool, item["prior_member"])
        try:
            if prior_member:
                did_write = restore_and_verify(
                    lambda: client.shell(
                        [
                            "cmd",
                            "netpolicy",
                            "add",
                            "restrict-background-whitelist",
                            uid,
                        ],
                        mutate=True,
                    ),
                    lambda: verify_netpolicy_whitelist(
                        client, package, uid, True
                    ),
                    recover_if_verified=not client.dry_run,
                )
            else:
                did_write = restore_and_verify(
                    lambda: client.shell(
                        [
                            "cmd",
                            "netpolicy",
                            "remove",
                            "restrict-background-whitelist",
                            uid,
                        ],
                        mutate=True,
                    ),
                    lambda: verify_netpolicy_whitelist(
                        client, package, uid, False
                    ),
                    recover_if_verified=not client.dry_run,
                )
            messages.append(
                _describe(
                    did_write,
                    f"netpolicy whitelist for {package} (uid {uid})",
                )
            )
            if not client.dry_run:
                remove_snapshot_for_entry(
                    cast(
                        AnyLedgerEntry,
                        {
                            "type": "netpolicy_whitelist",
                            "package": package,
                            "uid": uid,
                        },
                    )
                )
        except (CommandError, VerificationError) as exc:
            had_failures = True
            msg = (
                f"Failed to restore netpolicy whitelist for {package} "
                f"(uid {uid}): {exc}"
            )
            messages.append(msg)
            client.output(msg)

    deviceidle_whitelist = cast(
        Dict[str, bool], store.data.get("deviceidle_whitelist", {})
    )
    for package, prior_member in list(deviceidle_whitelist.items()):
        try:
            action = f"+{package}" if prior_member else f"-{package}"
            did_write = restore_and_verify(
                lambda: client.shell(
                    ["cmd", "deviceidle", "whitelist", action],
                    mutate=True,
                ),
                lambda: verify_deviceidle_whitelist(
                    client, package, prior_member
                ),
                recover_if_verified=not client.dry_run,
            )
            messages.append(
                _describe(did_write, f"deviceidle whitelist for {package}")
            )
            if not client.dry_run:
                remove_snapshot_for_entry(
                    cast(
                        AnyLedgerEntry,
                        {
                            "type": "deviceidle_whitelist",
                            "package": package,
                        },
                    )
                )
        except (CommandError, VerificationError) as exc:
            had_failures = True
            msg = f"Failed to restore deviceidle whitelist for {package}: {exc}"
            messages.append(msg)
            client.output(msg)

    hibernation = cast(Dict[str, bool], store.data.get("hibernation", {}))
    for package, prior_value in list(hibernation.items()):
        try:
            did_write = restore_and_verify(
                lambda: client.shell(
                    [
                        "cmd",
                        "app_hibernation",
                        "set-state",
                        package,
                        "true" if prior_value else "false",
                    ],
                    mutate=True,
                ),
                lambda: verify_app_hibernation(client, package, prior_value),
                recover_if_verified=not client.dry_run,
            )
            messages.append(
                _describe(did_write, f"hibernation state for {package}")
            )
            if not client.dry_run:
                remove_snapshot_for_entry(
                    cast(
                        AnyLedgerEntry,
                        {
                            "type": "app_hibernation",
                            "package": package,
                        },
                    )
                )
        except (CommandError, VerificationError) as exc:
            had_failures = True
            msg = f"Failed to restore hibernation state for {package}: {exc}"
            messages.append(msg)
            client.output(msg)

    if client.dry_run:
        return messages

    if had_failures:
        store.save_or_clear()
        client.output("Warning: Partial state corruption due to restore failures.")
    else:
        store.clear()
    return messages
