from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, TextIO

from .backend import CodexCliBackend, _WINDOWS_CREATE_SUSPENDED, _WindowsJob


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


class BaselineVerdict(str, Enum):
    VALID_RED = "valid_red"
    ALREADY_SATISFIED = "already_satisfied"
    REFUSED = "refused"


def _canonical_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    if os.name != "nt":
        return resolved
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetLongPathNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetLongPathNameW.restype = wintypes.DWORD
    required = kernel32.GetLongPathNameW(str(resolved), None, 0)
    if required == 0:
        return resolved
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = kernel32.GetLongPathNameW(str(resolved), buffer, len(buffer))
    return Path(buffer.value).resolve() if written else resolved


def _linux_subreaper_primitives_available() -> bool:
    return CodexCliBackend._linux_subreaper_primitives_available()


def _require_posix_verifier_support() -> Path:
    if not sys.platform.startswith("linux"):
        raise RuntimeError(
            "POSIX verifier execution requires a trusted Linux supervisor"
        )
    if not _linux_subreaper_primitives_available():
        raise RuntimeError("required Linux subreaper primitives are unavailable")
    supervisor_path = Path(__file__).with_name("posix_supervisor.py")
    if not supervisor_path.is_file():
        raise RuntimeError("trusted Linux supervisor helper is unavailable")
    return supervisor_path


def _read_supervisor_message(
    control: TextIO,
    *,
    timeout_seconds: float,
    phase: str,
) -> dict[str, Any]:
    readable, _, _ = select.select([control], [], [], timeout_seconds)
    if not readable:
        raise RuntimeError(f"Linux verifier supervisor {phase} timed out")
    line = control.readline()
    if not line:
        raise RuntimeError(f"Linux verifier supervisor {phase} channel closed")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Linux verifier supervisor {phase} returned invalid status"
        ) from error
    if not isinstance(message, dict) or not isinstance(message.get("event"), str):
        raise RuntimeError(
            f"Linux verifier supervisor {phase} returned invalid status"
        )
    return message


def _await_supervisor_ready(
    process: subprocess.Popen[str],
    control: TextIO,
) -> None:
    message = _read_supervisor_message(
        control,
        timeout_seconds=5.0,
        phase="readiness",
    )
    if message.get("event") == "error":
        raise RuntimeError(
            "Linux verifier supervisor readiness failed: "
            + str(message.get("message", "unknown error"))
        )
    if message.get("event") != "ready" or message.get("mode") != "command":
        raise RuntimeError("Linux verifier supervisor did not establish command mode")
    supervisor_pid = message.get("supervisor_pid")
    if isinstance(supervisor_pid, bool) or not isinstance(supervisor_pid, int):
        raise RuntimeError("Linux verifier supervisor omitted its exact PID")
    if supervisor_pid != process.pid:
        raise RuntimeError(
            "Linux verifier supervisor PID did not match the exact Popen PID"
        )


def _supervisor_returncode(
    control: TextIO,
    supervisor_returncode: int,
) -> int:
    message = _read_supervisor_message(
        control,
        timeout_seconds=2.0,
        phase="completion",
    )
    if message.get("event") == "error":
        raise RuntimeError(
            "Linux verifier supervisor cleanup failed: "
            + str(message.get("message", "unknown error"))
        )
    if message.get("event") != "finished" or message.get("mode") != "command":
        raise RuntimeError("Linux verifier supervisor did not verify cleanup")
    model_returncode = message.get("model_returncode")
    if isinstance(model_returncode, bool) or not isinstance(model_returncode, int):
        raise RuntimeError("Linux verifier supervisor omitted command return code")
    if int(supervisor_returncode) != 0:
        raise RuntimeError(
            f"Linux verifier supervisor exited with {int(supervisor_returncode)}"
        )
    return int(model_returncode)


def _start_capture_threads(
    process: subprocess.Popen[str],
) -> tuple[list[str], list[str], threading.Thread, threading.Thread]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def drain(stream: TextIO, sink: list[str]) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                sink.append(chunk)
        finally:
            stream.close()

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=drain,
        args=(process.stdout, stdout_chunks),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=(process.stderr, stderr_chunks),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    return stdout_chunks, stderr_chunks, stdout_thread, stderr_thread


def _finish_capture(
    stdout_chunks: list[str],
    stderr_chunks: list[str],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
) -> tuple[str, str]:
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise RuntimeError("verifier output pipes remained open after containment cleanup")
    return "".join(stdout_chunks), "".join(stderr_chunks)


def _cleanup_verifier_artifacts(
    sandbox_home: Path,
    base_staging_path: Path | None,
    profile_path: Path | None,
    temp_root: Path | None,
) -> None:
    failures: list[BaseException] = []

    if base_staging_path is not None:
        safe_base_staging = (
            base_staging_path.parent == sandbox_home
            and base_staging_path.name.startswith("mochicode-verifier-base-")
            and base_staging_path.name.endswith(".tmp")
        )
        if not safe_base_staging:
            failures.append(
                RuntimeError(
                    f"refusing unsafe verifier base staging cleanup: {base_staging_path}"
                )
            )
        else:
            try:
                base_staging_path.unlink()
            except FileNotFoundError:
                pass
            except BaseException as error:
                failures.append(error)
            if os.path.lexists(str(base_staging_path)):
                failures.append(
                    OSError(
                        f"verifier base staging file still exists: {base_staging_path}"
                    )
                )

    if profile_path is not None:
        safe_profile = (
            profile_path.parent == sandbox_home
            and profile_path.name.startswith("mochicode-verifier-")
            and profile_path.name.endswith(".config.toml")
        )
        if not safe_profile:
            failures.append(
                RuntimeError(f"refusing unsafe verifier profile cleanup: {profile_path}")
            )
        else:
            try:
                profile_path.unlink()
            except FileNotFoundError:
                pass
            except BaseException as error:
                failures.append(error)
            if os.path.lexists(str(profile_path)):
                failures.append(OSError(f"verifier profile still exists: {profile_path}"))

    if temp_root is not None:
        safe_temp = (
            temp_root.parent == sandbox_home
            and temp_root.name.startswith("mochicode-verifier-temp-")
        )
        if not safe_temp:
            failures.append(
                RuntimeError(f"refusing unsafe verifier temp cleanup: {temp_root}")
            )
        else:
            try:
                shutil.rmtree(temp_root)
            except FileNotFoundError:
                pass
            except BaseException as error:
                failures.append(error)
            if os.path.lexists(str(temp_root)):
                failures.append(OSError(f"verifier temp root still exists: {temp_root}"))

    if failures:
        paths = ", ".join(
            str(path)
            for path in (base_staging_path, profile_path, temp_root)
            if path is not None
        )
        raise RuntimeError(f"verifier artifact cleanup failed: {paths}") from failures[0]


def _run_windows_contained(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
) -> tuple[int, str, str, bool]:
    job = _WindowsJob()
    process: subprocess.Popen[str] | None = None
    cleanup_attempted = False
    capture: tuple[list[str], list[str], threading.Thread, threading.Thread] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env=environment,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED
            ),
            start_new_session=False,
        )
        try:
            job.assign(process)
        except BaseException:
            job.terminate_unassigned_process(process)
            raise
        try:
            job.resume(process)
        except BaseException:
            job.terminate_and_verify()
            raise

        capture = _start_capture_threads(process)
        try:
            returncode = int(process.wait(timeout=timeout_seconds))
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            job.terminate_and_verify()
            cleanup_attempted = True
            process.wait(timeout=5)
            returncode = 124
        if not cleanup_attempted:
            job.terminate_and_verify()
            cleanup_attempted = True
        stdout, stderr = _finish_capture(*capture)
        return returncode, stdout, stderr, timed_out
    finally:
        try:
            if process is not None and not cleanup_attempted:
                cleanup_attempted = True
                job.terminate_and_verify()
        finally:
            job.close()


def _run_linux_contained(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    supervisor_path: Path,
) -> tuple[int, str, str, bool]:
    control_read_fd, control_write_fd = os.pipe()
    control: TextIO | None = None
    process: subprocess.Popen[str] | None = None
    process_group_id: int | None = None
    cleanup_verified = False
    readiness_established = False
    capture: tuple[list[str], list[str], threading.Thread, threading.Thread] | None = None
    try:
        supervisor_command = [
            sys.executable,
            str(supervisor_path),
            "command",
            str(control_write_fd),
            json.dumps(command, separators=(",", ":")),
        ]
        process = subprocess.Popen(
            supervisor_command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env=environment,
            creationflags=0,
            start_new_session=True,
            pass_fds=(control_write_fd,),
        )
        process_group_id = int(process.pid)
        actual_group_id = os.getpgid(process.pid)
        if int(actual_group_id) != process_group_id:
            raise RuntimeError("Linux verifier supervisor did not own its process group")
        os.close(control_write_fd)
        control_write_fd = -1
        control = os.fdopen(
            control_read_fd,
            "r",
            encoding="utf-8",
            newline="\n",
        )
        control_read_fd = -1
        _await_supervisor_ready(process, control)
        readiness_established = True
        capture = _start_capture_threads(process)

        try:
            supervisor_returncode = int(process.wait(timeout=timeout_seconds))
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process_group_id, signal.SIGTERM)
            supervisor_returncode = int(process.wait(timeout=15))
        command_returncode = _supervisor_returncode(
            control,
            supervisor_returncode,
        )
        cleanup_verified = True
        stdout, stderr = _finish_capture(*capture)
        return (
            124 if timed_out else command_returncode,
            stdout,
            stderr,
            timed_out,
        )
    finally:
        try:
            if process is not None and not cleanup_verified:
                if process.poll() is None:
                    assert process_group_id is not None
                    os.killpg(process_group_id, signal.SIGTERM)
                supervisor_returncode = int(process.wait(timeout=15))
                if control is not None and readiness_established:
                    _supervisor_returncode(control, supervisor_returncode)
        finally:
            try:
                if control is not None:
                    control.close()
            finally:
                if control_read_fd >= 0:
                    os.close(control_read_fd)
                if control_write_fd >= 0:
                    os.close(control_write_fd)


def run_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> CommandResult:
    if not argv or not argv[0].strip():
        raise ValueError("verification argv must not be empty")
    windows = os.name == "nt"
    if not windows:
        raise RuntimeError(
            "verifier execution is unavailable on this Windows-only release"
        )
    cwd = _canonical_path(Path(cwd))
    if not cwd.is_dir():
        raise ValueError(f"verification cwd does not exist: {cwd}")
    if timeout_seconds <= 0:
        raise ValueError("verification timeout must be positive")

    started = time.monotonic()
    posix_supervisor_path: Path | None = None
    codex_name = "codex.cmd" if windows else "codex"
    codex = shutil.which(codex_name) or shutil.which("codex")
    if not codex:
        raise ValueError("Codex CLI is required to sandbox verifier commands")

    local_data = os.environ.get("LOCALAPPDATA")
    sandbox_home = (
        Path(local_data) / "MochiCode" / "verifier-sandbox"
        if local_data
        else Path.home() / ".local" / "share" / "MochiCode" / "verifier-sandbox"
    ).absolute()
    sandbox_home.mkdir(parents=True, exist_ok=True)
    base_staging_path: Path | None = None
    profile_path: Path | None = None
    temp_root: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="mochicode-verifier-base-",
            suffix=".tmp",
            dir=sandbox_home,
            delete=False,
        ) as base_staging_file:
            candidate_base_staging_path = Path(base_staging_file.name).absolute()
            if candidate_base_staging_path.parent != sandbox_home:
                raise RuntimeError(
                    "verifier base staging file was not created under sandbox home"
                )
            base_staging_path = candidate_base_staging_path
            base_staging_file.write(
                "# MochiCode verifier base config intentionally contains no permissions.\n"
            )
        os.replace(base_staging_path, sandbox_home / "config.toml")

        candidate_temp_root = Path(
            tempfile.mkdtemp(prefix="mochicode-verifier-temp-", dir=sandbox_home)
        ).absolute()
        if candidate_temp_root.parent != sandbox_home:
            raise RuntimeError("verifier temp root was not created under sandbox home")
        temp_root = candidate_temp_root

        config = (
            '[permissions.mochicode-verifier]\n'
            'description = "Verifier-only sandbox"\n\n'
            '[permissions.mochicode-verifier.filesystem]\n'
            '":minimal" = "read"\n'
            f"{json.dumps(str(temp_root))} = \"write\"\n\n"
            '[permissions.mochicode-verifier.filesystem.":workspace_roots"]\n'
            '"." = "read"\n\n'
            '[permissions.mochicode-verifier.network]\n'
            'enabled = false\n'
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="mochicode-verifier-",
            suffix=".config.toml",
            dir=sandbox_home,
            delete=False,
        ) as profile_file:
            candidate_profile_path = Path(profile_file.name).absolute()
            if candidate_profile_path.parent != sandbox_home:
                raise RuntimeError("verifier profile was not created under sandbox home")
            profile_path = candidate_profile_path
            profile_file.write(config)
        profile_name = profile_path.name.removesuffix(".config.toml")
        if not profile_name.isascii() or not all(
            character.isalnum() or character in "-_" for character in profile_name
        ):
            raise RuntimeError("verifier profile name contains unsafe characters")

        git_common = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=30,
        )
        readable_roots: list[Path] = []
        sandbox_argv = list(argv)
        for index, raw in enumerate(sandbox_argv):
            candidate = Path(raw)
            if candidate.is_absolute() and candidate.exists():
                sandbox_argv[index] = str(_canonical_path(candidate))
        raw_executable = Path(sandbox_argv[0])
        if raw_executable.is_absolute():
            executable = raw_executable.resolve()
        else:
            resolved_executable = shutil.which(argv[0])
            if not resolved_executable:
                raise ValueError(f"verification executable not found: {argv[0]}")
            executable = _canonical_path(Path(resolved_executable))
            if not executable.is_file():
                raise ValueError(f"verification executable does not exist: {executable}")
            sandbox_argv[0] = str(executable)
        if executable.is_file():
            readable_roots.append(executable.parent)
        if git_common.returncode == 0 and git_common.stdout.strip():
            raw_common = Path(git_common.stdout.strip())
            common = raw_common.resolve() if raw_common.is_absolute() else (cwd / raw_common).resolve()
            readable_roots.append(common)

        sensitive_markers = (
            "API_KEY",
            "AUTH",
            "COOKIE",
            "CREDENTIAL",
            "PASSWORD",
            "SECRET",
            "TOKEN",
        )
        sensitive_prefixes = (
            "ANTHROPIC_",
            "AWS_",
            "AZURE_",
            "GITHUB_",
            "GOOGLE_",
            "OPENAI_",
            "SLACK_",
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in sensitive_markers)
            and not key.upper().startswith(sensitive_prefixes)
        }
        environment["CODEX_HOME"] = str(sandbox_home)
        environment["TMP"] = str(temp_root)
        environment["TEMP"] = str(temp_root)
        environment["TMPDIR"] = str(temp_root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [codex, "sandbox", "-p", profile_name]
        if windows:
            command.extend(["-c", 'windows.sandbox="elevated"'])
        command.extend(
            [
                "-P",
                "mochicode-verifier",
                "-C",
                str(cwd),
                "--sandbox-state-disable-network",
            ]
        )
        for readable_root in readable_roots:
            command.extend(["--sandbox-state-readable-root", str(readable_root)])
        command.extend(["--", *sandbox_argv])
        for sandbox_attempt in range(2):
            if windows:
                returncode, stdout, stderr, timed_out = _run_windows_contained(
                    command,
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                )
            else:
                assert posix_supervisor_path is not None
                returncode, stdout, stderr, timed_out = _run_linux_contained(
                    command,
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    supervisor_path=posix_supervisor_path,
                )
            transient_windows_setup = (
                windows
                and not timed_out
                and returncode != 0
                and any(
                    marker in (stderr or "")
                    for marker in (
                        "CreateProcessAsUserW failed: 5",
                        "CreateProcessWithLogonW failed: 267",
                    )
                )
            )
            if sandbox_attempt == 0 and transient_windows_setup:
                continue
            break

        return CommandResult(
            argv=tuple(argv),
            returncode=returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
        )
    finally:
        _cleanup_verifier_artifacts(
            sandbox_home,
            base_staging_path,
            profile_path,
            temp_root,
        )


def classify_baseline(
    result: CommandResult,
    *,
    expected_failure_codes: tuple[int, ...] = (1,),
) -> BaselineVerdict:
    if result.timed_out:
        return BaselineVerdict.REFUSED
    lowered_error = result.stderr.lower()
    if any(
        marker in lowered_error
        for marker in ("access is denied", "permission denied", "permissionerror")
    ):
        return BaselineVerdict.REFUSED
    if result.returncode == 0:
        return BaselineVerdict.ALREADY_SATISFIED
    if result.returncode in expected_failure_codes:
        return BaselineVerdict.VALID_RED
    return BaselineVerdict.REFUSED


def final_verification_passed(result: CommandResult) -> bool:
    return not result.timed_out and result.returncode == 0
