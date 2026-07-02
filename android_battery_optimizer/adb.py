import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .android import DeviceInfo


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

class CommandError(RuntimeError):
    def __init__(self, message: str, result: Optional[CommandResult] = None) -> None:
        super().__init__(message)
        self.result = result

class CommandRunner:
    def run(
        self,
        args: Sequence[str],
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        raise NotImplementedError

    def which(self, name: str) -> Optional[str]:
        raise NotImplementedError

class SubprocessRunner(CommandRunner):
    def run(
        self,
        args: Sequence[str],
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                input=input_data,
                timeout=timeout,
            )
            return CommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            result = CommandResult(returncode=-1, stdout=stdout, stderr=stderr)
            raise CommandError(
                f"Command timed out after {timeout}s: {' '.join(args)}",
                result=result,
            ) from exc

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

class AdbClient:
    DEFAULT_TIMEOUT_SECONDS = 30
    LONG_TIMEOUT_SECONDS = 300

    def __init__(
        self,
        runner: CommandRunner,
        serial: Optional[str] = None,
        dry_run: bool = False,
        output: Callable[[str], None] = print,
    ) -> None:
        self.runner = runner
        self._serial = serial
        self._device_info_cache: Dict[str, DeviceInfo] = {}
        self.dry_run = dry_run
        self.output = output

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @serial.setter
    def serial(self, value: Optional[str]) -> None:
        if self._serial != value:
            self._serial = value
            self._device_info_cache.clear()

    def get_device_info_struct(self) -> DeviceInfo:
        serial = self.serial or "unknown-device"
        if serial in self._device_info_cache:
            return self._device_info_cache[serial]

        cmd = (
            "getprop ro.product.brand; "
            "getprop ro.product.model; "
            "getprop ro.build.version.release; "
            "getprop ro.build.version.sdk; "
            "getprop ro.build.fingerprint"
        )
        out = self.shell_text([cmd], check=False)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) < 5:
            brand = self.shell_text(["getprop", "ro.product.brand"], check=False)
            model = self.shell_text(["getprop", "ro.product.model"], check=False)
            release = self.shell_text(["getprop", "ro.build.version.release"], check=False)
            sdk_str = self.shell_text(["getprop", "ro.build.version.sdk"], check=False)
            fingerprint = self.shell_text(["getprop", "ro.build.fingerprint"], check=False)
        else:
            brand = lines[0]
            model = lines[1]
            release = lines[2]
            sdk_str = lines[3]
            fingerprint = lines[4]

        try:
            sdk_int = int(sdk_str)
        except (ValueError, TypeError):
            sdk_int = 0

        info = DeviceInfo(
            serial=serial,
            brand=brand,
            model=model,
            android_release=release,
            sdk_int=sdk_int,
            fingerprint=fingerprint,
        )
        self._device_info_cache[serial] = info
        return info

    def get_device_metadata(self) -> Dict[str, str]:
        serial = self.serial or "unknown-device"
        brand = self.shell_text(["getprop", "ro.product.brand"], check=True)
        model = self.shell_text(["getprop", "ro.product.model"], check=True)
        release = self.shell_text(["getprop", "ro.build.version.release"], check=True)
        sdk_str = self.shell_text(["getprop", "ro.build.version.sdk"], check=True)
        fingerprint = self.shell_text(["getprop", "ro.build.fingerprint"], check=True)

        try:
            int(sdk_str)
        except (ValueError, TypeError):
            sdk_str = "0"

        return {
            "serial": serial,
            "brand": brand,
            "model": model,
            "android_release": release,
            "sdk": sdk_str,
            "fingerprint": fingerprint,
        }

    def get_device_metadata_with_fallback(self) -> Dict[str, str]:
        try:
            return self.get_device_metadata()
        except CommandError:
            return self.get_minimal_device_metadata()

    def get_minimal_device_metadata(self) -> Dict[str, str]:
        return {
            "serial": self.serial or "unknown-device",
            "brand": "",
            "model": "",
            "android_release": "",
            "sdk": "",
            "fingerprint": "",
        }

    def supports_device_config(self) -> bool:
        try:
            result = self.shell(["device_config", "list"], check=False)
            return result.returncode == 0
        except Exception:
            return False

    def supports_device_config_write(
        self, namespace: str, key: str, probe_value: str
    ) -> bool:
        # Android 14+ builds restrict shell device_config writes to a
        # build-time flag allowlist; reads still succeed, so probe by writing.
        if self.dry_run:
            return True
        try:
            current = self.shell(
                ["device_config", "get", namespace, key], check=False
            )
            value = current.stdout.strip()
            if current.returncode == 0 and value not in {"", "null"}:
                result = self.shell(
                    ["device_config", "put", namespace, key, value],
                    mutate=True,
                    check=False,
                )
                return self._device_config_write_ok(result)

            put_result = self.shell(
                ["device_config", "put", namespace, key, probe_value],
                mutate=True,
                check=False,
            )
            if not self._device_config_write_ok(put_result):
                return False
            delete_result = self.shell(
                ["device_config", "delete", namespace, key],
                mutate=True,
                check=False,
            )
            return self._device_config_write_ok(delete_result)
        except Exception:
            return False

    @staticmethod
    def _device_config_write_ok(result: CommandResult) -> bool:
        if result.returncode != 0:
            return False
        return "SecurityException" not in (result.stdout + result.stderr)

    def supports_appops(self) -> bool:
        try:
            # Some Android/Samsung builds do not support `cmd appops help`,
            # but still support the actual get/set appops commands.
            result = self.shell(["cmd", "appops", "get", "android"], check=False)
            output = (result.stdout + result.stderr).lower()

            if result.returncode == 0:
                return True

            # These mean the appops service/command exists, even if the probe package/op
            # produced no useful data.
            known_appops_responses = (
                "no operations",
                "unknown package",
                "bad package",
                "usage:",
                "appops",
            )
            if any(token in output for token in known_appops_responses):
                return True

            # These suggest the command/service really is unavailable.
            unavailable_responses = (
                "unknown command",
                "not found",
                "can't find service",
                "cmd: failure calling service appops",
            )
            if any(token in output for token in unavailable_responses):
                return False

            return False
        except Exception:
            return False

    def supports_standby_bucket(self) -> bool:
        try:
            info = self.get_device_info_struct()
            if info.sdk_int < 28:
                return False

            result = self.shell(["am", "get-standby-bucket", "android"], check=False)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return True

            help_result = self.shell(["am", "help"], check=False)
            help_output = help_result.stdout + help_result.stderr
            if "set-standby-bucket" in help_output and "get-standby-bucket" in help_output:
                return True

            return False
        except Exception:
            return False

    def supports_settings_namespace(self, namespace: str) -> bool:
        try:
            result = self.shell(["settings", "list", namespace], check=False)
            return result.returncode == 0
        except Exception:
            return False

    def adb_exists(self) -> bool:
        return self.runner.which("adb") is not None

    def require_bound_device_for_mutation(self) -> None:
        if self.serial is None:
            raise CommandError("Refusing to mutate device state without a selected ADB serial.")

    def _base_command(self) -> List[str]:
        command = ["adb"]
        if self.serial:
            command.extend(["-s", self.serial])
        return command

    def _stringify(self, args: Sequence[object]) -> List[str]:
        return [str(arg) for arg in args]

    def _format(self, args: Sequence[str]) -> str:
        return " ".join(shlex.quote(arg) for arg in args)

    def run_adb(
        self,
        args: Sequence[object],
        *,
        mutate: bool = False,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        if mutate and not self.dry_run:
            self.require_bound_device_for_mutation()

        command = self._base_command() + self._stringify(args)
        if mutate and self.dry_run:
            self.output(f"[dry-run] {self._format(command)}")
            if input_data:
                self.output(f"[dry-run-input]\n{input_data}")
            return CommandResult(returncode=0, stdout="", stderr="")

        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT_SECONDS

        result = self.runner.run(command, input_data=input_data, timeout=timeout)
        if check and result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            details = stderr or stdout or "unknown error"
            raise CommandError(f"{self._format(command)} failed: {details}", result=result)
        return result

    def shell(
        self,
        args: Sequence[object],
        *,
        mutate: bool = False,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        return self.run_adb(
            ["shell", *args],
            mutate=mutate,
            check=check,
            input_data=input_data,
            timeout=timeout,
        )

    def shell_text(
        self,
        args: Sequence[object],
        *,
        mutate: bool = False,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> str:
        return self.shell(
            args, mutate=mutate, check=check, input_data=input_data, timeout=timeout
        ).stdout.strip()

    def local_text(
        self,
        args: Sequence[object],
        *,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> str:
        str_args = self._stringify(args)
        if timeout is None and str_args and str_args[0] == "adb":
            timeout = self.DEFAULT_TIMEOUT_SECONDS

        result = self.runner.run(
            str_args, input_data=input_data, timeout=timeout
        )
        if check and result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise CommandError(
                f"{self._format(str_args)} failed: {details}", result=result
            )
        return result.stdout.strip()
