from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Any, TextIO
from ctypes import wintypes

from .config import ControllerConfig, RoleConfig
from .state import exclusive_file_lock


_WINDOWS_CREATE_SUSPENDED = 0x00000004
_FORBIDDEN_EXECUTION_POLICY_FLAG = "--ignore-" + "rules"


class _WindowsJobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _WindowsJobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsJobObjectBasicLimitInformation),
        ("IoInfo", _WindowsIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _WindowsThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class _WindowsJob:
    """Own one model-call process tree through a Windows Job Object."""

    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _ERROR_NO_MORE_FILES = 18
    _RESUME_THREAD_FAILED = 0xFFFFFFFF
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_FAILED = 0xFFFFFFFF
    _TERMINATION_WAIT_MILLISECONDS = 5000

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        self._handle: int | None = None
        self._assigned_pid: int | None = None
        self._termination_requested = False

        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WindowsThreadEntry32),
        ]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WindowsThreadEntry32),
        ]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise self._last_error("CreateJobObjectW")
        self._handle = int(handle)
        try:
            limits = _WindowsJobObjectExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = (
                self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                self._handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise self._last_error("SetInformationJobObject")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _last_error(operation: str) -> OSError:
        error = ctypes.get_last_error()
        return OSError(error, f"{operation} failed")

    def assign(self, process: subprocess.Popen[str]) -> None:
        handle = self._handle
        process_handle = self._exact_process_handle(process)
        if handle is None:
            raise RuntimeError("Windows Job Object is already closed")
        if not self._kernel32.AssignProcessToJobObject(handle, process_handle):
            raise self._last_error("AssignProcessToJobObject")
        self._assigned_pid = int(process.pid)

    @staticmethod
    def _exact_process_handle(process: subprocess.Popen[str]) -> int:
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise RuntimeError("Popen did not expose an exact process handle")
        return int(process_handle)

    def terminate_unassigned_process(
        self,
        process: subprocess.Popen[str],
    ) -> None:
        if self._assigned_pid == int(process.pid):
            self.terminate_and_verify()
            return
        process_handle = self._exact_process_handle(process)
        wait_result = int(self._kernel32.WaitForSingleObject(process_handle, 0))
        if wait_result == self._WAIT_FAILED:
            raise self._last_error("WaitForSingleObject")
        if wait_result == self._WAIT_TIMEOUT:
            if not self._kernel32.TerminateProcess(process_handle, 1):
                race_result = int(
                    self._kernel32.WaitForSingleObject(process_handle, 0)
                )
                if race_result != self._WAIT_OBJECT_0:
                    raise self._last_error("TerminateProcess")
            wait_result = int(
                self._kernel32.WaitForSingleObject(
                    process_handle,
                    self._TERMINATION_WAIT_MILLISECONDS,
                )
            )
        if wait_result != self._WAIT_OBJECT_0:
            raise RuntimeError(
                "exact suspended model process handle did not terminate"
            )
        process.wait(timeout=5)

    def resume(self, process: subprocess.Popen[str]) -> None:
        process_id = int(process.pid)
        if self._assigned_pid != process_id:
            raise RuntimeError(
                f"model process {process_id} must be assigned before it is resumed"
            )
        thread_id = self._suspended_primary_thread_id(process_id)
        thread_handle = self._kernel32.OpenThread(
            self._THREAD_SUSPEND_RESUME,
            False,
            thread_id,
        )
        if not thread_handle:
            raise self._last_error("OpenThread")
        thread_handle_value = int(thread_handle)
        try:
            previous_suspend_count = int(
                self._kernel32.ResumeThread(thread_handle_value)
            )
            if previous_suspend_count == self._RESUME_THREAD_FAILED:
                raise self._last_error("ResumeThread")
            if previous_suspend_count != 1:
                raise RuntimeError(
                    "suspended model primary thread had an unexpected suspend count "
                    f"of {previous_suspend_count}"
                )
        finally:
            self._close_native_handle(thread_handle_value, "primary thread")

    def _suspended_primary_thread_id(self, process_id: int) -> int:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(
            self._TH32CS_SNAPTHREAD,
            0,
        )
        if not snapshot:
            raise self._last_error("CreateToolhelp32Snapshot")
        snapshot_value = int(snapshot)
        if snapshot_value == self._INVALID_HANDLE_VALUE:
            raise self._last_error("CreateToolhelp32Snapshot")

        matching_thread_ids: list[int] = []
        try:
            entry = _WindowsThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            if not self._kernel32.Thread32First(snapshot_value, ctypes.byref(entry)):
                raise self._last_error("Thread32First")
            while True:
                if int(entry.th32OwnerProcessID) == process_id:
                    matching_thread_ids.append(int(entry.th32ThreadID))
                entry.dwSize = ctypes.sizeof(entry)
                ctypes.set_last_error(0)
                if not self._kernel32.Thread32Next(
                    snapshot_value,
                    ctypes.byref(entry),
                ):
                    error = ctypes.get_last_error()
                    if error != self._ERROR_NO_MORE_FILES:
                        raise OSError(error, "Thread32Next failed")
                    break
        finally:
            self._close_native_handle(snapshot_value, "thread snapshot")

        if len(matching_thread_ids) != 1:
            raise RuntimeError(
                f"suspended model process {process_id} exposed "
                f"{len(matching_thread_ids)} primary-thread candidates"
            )
        return matching_thread_ids[0]

    def _close_native_handle(self, handle: int, label: str) -> None:
        if not self._kernel32.CloseHandle(handle):
            error = ctypes.get_last_error()
            raise OSError(error, f"CloseHandle failed for {label}")

    def terminate_and_verify(self, *, timeout_seconds: float = 5.0) -> None:
        handle = self._handle
        if handle is None or self._assigned_pid is None:
            return
        if not self._termination_requested:
            if not self._kernel32.TerminateJobObject(handle, 1):
                raise self._last_error("TerminateJobObject")
            self._termination_requested = True

        deadline = time.monotonic() + timeout_seconds
        while True:
            active_processes = _WindowsJobObjectBasicAccountingInformation()
            returned = wintypes.DWORD()
            if not self._kernel32.QueryInformationJobObject(
                handle,
                self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(active_processes),
                ctypes.sizeof(active_processes),
                ctypes.byref(returned),
            ):
                raise self._last_error("QueryInformationJobObject")
            if int(active_processes.ActiveProcesses) == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Windows Job Object still has active model-call processes"
                )
            time.sleep(0.01)

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        if not self._kernel32.CloseHandle(handle):
            raise self._last_error("CloseHandle")


@dataclass(frozen=True, slots=True)
class CodexInvocation:
    role: str
    cwd: Path
    prompt: str
    output_schema: Path
    output_file: Path
    event_log: Path
    process_log: Path
    stop_path: Path | None = None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    role: str
    returncode: int
    output: dict[str, Any] | None
    usage: dict[str, int]
    thread_id: str | None
    duration_seconds: float
    timed_out: bool = False
    stopped: bool = False


class CodexCliBackend:
    def __init__(self, executable: str | tuple[str, ...], config: ControllerConfig) -> None:
        self.executable = (executable,) if isinstance(executable, str) else executable
        self.config = config

    def build_command(self, invocation: CodexInvocation) -> tuple[str, ...]:
        role = self.role_config(invocation.role)
        command: list[str] = [
            *self.executable,
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--json",
            "--strict-config",
        ]
        if not self.config.inherit_user_config:
            command.append("--ignore-user-config")
        if os.name == "nt":
            command.extend(
                ["-c", f'windows.sandbox="{self.config.windows_sandbox}"']
            )
        if role.service_tier:
            command.extend(
                [
                    "-c",
                    f'service_tier="{role.service_tier}"',
                    "-c",
                    "features.fast_mode=true",
                ]
            )
        command.extend(
            [
                "--model",
                role.model,
                "--sandbox",
                role.sandbox,
                "--cd",
                str(Path(invocation.cwd).resolve()),
                "--output-schema",
                str(Path(invocation.output_schema).resolve()),
                "--output-last-message",
                str(Path(invocation.output_file).resolve()),
                "--disable",
                "multi_agent",
                "-c",
                f'model_reasoning_effort="{role.reasoning_effort}"',
                "-",
            ]
        )
        if _FORBIDDEN_EXECUTION_POLICY_FLAG in command:
            raise ValueError("built command may not override execution-policy rules")
        return tuple(command)

    @staticmethod
    def _linux_subreaper_primitives_available() -> bool:
        if not sys.platform.startswith("linux"):
            return False
        children_path = Path("/proc/self/task") / str(os.getpid()) / "children"
        if not children_path.is_file():
            return False
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            return False
        try:
            libc = ctypes.CDLL(None)
            getattr(libc, "prctl")
            probe_fd = os.pidfd_open(os.getpid(), 0)
        except (AttributeError, OSError):
            return False
        os.close(probe_fd)
        return True

    def _require_posix_supervisor(self) -> Path:
        if not sys.platform.startswith("linux"):
            raise RuntimeError(
                "POSIX model invocation requires a trusted Linux supervisor"
            )
        if not self._linux_subreaper_primitives_available():
            raise RuntimeError(
                "required Linux subreaper primitives are unavailable"
            )
        supervisor_path = Path(__file__).with_name("posix_supervisor.py")
        if not supervisor_path.is_file():
            raise RuntimeError("trusted Linux supervisor helper is unavailable")
        return supervisor_path

    @staticmethod
    def _read_posix_supervisor_message(
        control: TextIO,
        *,
        timeout_seconds: float,
        phase: str,
    ) -> dict[str, Any]:
        readable, _, _ = select.select([control], [], [], timeout_seconds)
        if not readable:
            raise RuntimeError(f"Linux supervisor {phase} timed out")
        line = control.readline()
        if not line:
            raise RuntimeError(f"Linux supervisor {phase} channel closed")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Linux supervisor {phase} returned invalid status"
            ) from error
        if not isinstance(value, dict) or not isinstance(value.get("event"), str):
            raise RuntimeError(f"Linux supervisor {phase} returned invalid status")
        return value

    def _await_posix_supervisor_ready(
        self,
        process: subprocess.Popen[str],
        control: TextIO,
    ) -> None:
        message = self._read_posix_supervisor_message(
            control,
            timeout_seconds=5.0,
            phase="readiness",
        )
        if message.get("event") == "error":
            raise RuntimeError(
                "Linux supervisor readiness failed: "
                + str(message.get("message", "unknown error"))
            )
        if message.get("event") != "ready":
            raise RuntimeError("Linux supervisor did not establish readiness")
        supervisor_pid = message.get("supervisor_pid")
        if isinstance(supervisor_pid, bool) or not isinstance(supervisor_pid, int):
            raise RuntimeError("Linux supervisor readiness omitted its exact PID")
        if supervisor_pid != process.pid:
            raise RuntimeError(
                "Linux supervisor readiness PID did not match the exact Popen PID"
            )

    def _finish_posix_supervisor(
        self,
        control: TextIO,
        supervisor_returncode: int,
    ) -> int:
        message = self._read_posix_supervisor_message(
            control,
            timeout_seconds=2.0,
            phase="completion",
        )
        event = message.get("event")
        if event == "error":
            raise RuntimeError(
                "Linux supervisor cleanup failed: "
                + str(message.get("message", "unknown error"))
            )
        if event != "finished":
            raise RuntimeError("Linux supervisor did not verify cleanup")
        model_returncode = message.get("model_returncode")
        if isinstance(model_returncode, bool) or not isinstance(model_returncode, int):
            raise RuntimeError("Linux supervisor omitted the model return code")
        if int(supervisor_returncode) != 0:
            raise RuntimeError(
                f"Linux supervisor exited with {int(supervisor_returncode)}"
            )
        return int(model_returncode)

    def _shutdown_posix_supervisor(
        self,
        process: subprocess.Popen[str],
        process_group_id: int,
        control: TextIO,
    ) -> None:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            self._terminate_process_group(
                process,
                group_id=process_group_id,
            )
        try:
            supervisor_returncode = process.wait(timeout=15)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "Linux supervisor did not complete exact descendant cleanup"
            ) from error
        self._finish_posix_supervisor(
            control,
            int(supervisor_returncode),
        )

    def invoke(self, invocation: CodexInvocation) -> InvocationResult:
        windows = os.name == "nt"
        if not windows:
            raise RuntimeError(
                "model execution is unavailable on this Windows-only release"
            )
        role = self.role_config(invocation.role)
        command = self.build_command(invocation)
        command_sha256 = hashlib.sha256(
            json.dumps(command, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        invocation.event_log.parent.mkdir(parents=True, exist_ok=True)
        invocation.output_file.parent.mkdir(parents=True, exist_ok=True)
        invocation.process_log.parent.mkdir(parents=True, exist_ok=True)
        self._write_command_receipt(
            invocation,
            command=command,
            command_sha256=command_sha256,
        )
        stderr_path = invocation.event_log.with_suffix(".stderr.log")
        posix_supervisor_path: Path | None = None
        started = time.monotonic()
        sensitive_markers = (
            "API_KEY",
            "AUTH",
            "COOKIE",
            "CREDENTIAL",
            "PASSWORD",
            "SECRET",
            "TOKEN",
        )
        child_environment = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in sensitive_markers)
        }
        containment = _WindowsJob() if windows else None
        process: subprocess.Popen[str] | None = None
        process_group_id: int | None = None
        containment_cleanup_attempted = False
        posix_control_read_fd: int | None = None
        posix_control_write_fd: int | None = None
        posix_control: TextIO | None = None
        try:
            process_command = list(command)
            popen_extra: dict[str, Any] = {}
            if posix_supervisor_path is not None:
                posix_control_read_fd, posix_control_write_fd = os.pipe()
                process_command = [
                    sys.executable,
                    str(posix_supervisor_path),
                    str(posix_control_write_fd),
                    json.dumps(command, separators=(",", ":")),
                ]
                popen_extra["pass_fds"] = (posix_control_write_fd,)
            if invocation.stop_path is not None and invocation.stop_path.exists():
                return InvocationResult(
                    role=invocation.role,
                    returncode=130,
                    output=None,
                    usage={},
                    thread_id=None,
                    duration_seconds=time.monotonic() - started,
                    stopped=True,
                )
            if invocation.stop_path is not None and invocation.stop_path.exists():
                return InvocationResult(
                    role=invocation.role,
                    returncode=130,
                    output=None,
                    usage={},
                    thread_id=None,
                    duration_seconds=time.monotonic() - started,
                    stopped=True,
                )
            process = subprocess.Popen(
                process_command,
                cwd=Path(invocation.cwd).resolve(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=child_environment,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED
                    if windows
                    else 0
                ),
                start_new_session=not windows,
                **popen_extra,
            )
            if containment is not None:
                try:
                    containment.assign(process)
                except BaseException as error:
                    containment.terminate_unassigned_process(process)
                    raise RuntimeError(
                        "Windows Job Object containment could not be established"
                    ) from error
                try:
                    containment.resume(process)
                except BaseException as error:
                    containment.terminate_and_verify()
                    raise RuntimeError(
                        "Windows suspended model process could not be resumed"
                    ) from error
            else:
                try:
                    process_group_id = self._establish_posix_process_group(process)
                    assert posix_control_write_fd is not None
                    os.close(posix_control_write_fd)
                    posix_control_write_fd = None
                    assert posix_control_read_fd is not None
                    posix_control = os.fdopen(
                        posix_control_read_fd,
                        "r",
                        encoding="utf-8",
                        newline="\n",
                    )
                    posix_control_read_fd = None
                    self._await_posix_supervisor_ready(process, posix_control)
                except BaseException as error:
                    self._terminate_exact_process(process)
                    containment_cleanup_attempted = True
                    raise RuntimeError(
                        "trusted Linux supervisor containment could not be established"
                    ) from error

            self._append_process_event(
                invocation.process_log,
                {
                    "event": "started",
                    "pid": process.pid,
                    "role": invocation.role,
                    "argv": list(command),
                    "command_hash": command_sha256,
                    "command_sha256": command_sha256,
                    "ignore_rules": False,
                    "time": time.time(),
                },
            )

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def drain(stream: Any, path: Path, sink: list[str]) -> None:
                with path.open("w", encoding="utf-8", newline="\n") as handle:
                    for line in iter(stream.readline, ""):
                        sink.append(line)
                        handle.write(line)
                        handle.flush()
                stream.close()

            stdout_thread = threading.Thread(
                target=drain,
                args=(process.stdout, invocation.event_log, stdout_lines),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=drain,
                args=(process.stderr, stderr_path, stderr_lines),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            assert process.stdin is not None
            child_prompt = (
                f"[MOCHICODE_CHILD role={invocation.role}]\n"
                "Perform only this assigned role. Do not invoke MochiCode, spawn subagents, "
                "or broaden the task.\n\n"
                f"{invocation.prompt}"
            )
            process.stdin.write(child_prompt)
            process.stdin.close()

            timed_out = False
            stopped = False
            deadline = started + role.timeout_seconds
            while process.poll() is None:
                if invocation.stop_path is not None and invocation.stop_path.exists():
                    stopped = True
                    if windows:
                        assert containment is not None
                        containment.terminate_and_verify()
                    else:
                        self._terminate_process_group(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    if windows:
                        assert containment is not None
                        containment.terminate_and_verify()
                    else:
                        self._terminate_process_group(process)
                    break
                time.sleep(0.1)
            try:
                supervisor_returncode = process.wait(
                    timeout=5 if windows else 15
                )
            except subprocess.TimeoutExpired:
                if windows:
                    assert containment is not None
                    containment.terminate_and_verify()
                    supervisor_returncode = process.wait(timeout=5)
                else:
                    self._terminate_process_group(
                        process,
                        group_id=process_group_id,
                    )
                    supervisor_returncode = process.wait(timeout=15)

            containment_cleanup_attempted = True
            if windows:
                self._cleanup_process_containment(
                    process,
                    containment=containment,
                    process_group_id=process_group_id,
                )
                returncode = int(supervisor_returncode)
            else:
                assert process_group_id is not None
                assert posix_control is not None
                returncode = self._finish_posix_supervisor(
                    posix_control,
                    int(supervisor_returncode),
                )
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            duration = time.monotonic() - started
            self._append_process_event(
                invocation.process_log,
                {
                    "event": "finished",
                    "pid": process.pid,
                    "role": invocation.role,
                    "argv": list(command),
                    "command_sha256": command_sha256,
                    "ignore_rules": False,
                    "returncode": int(returncode),
                    "timed_out": timed_out,
                    "stopped": stopped,
                    "duration_seconds": round(duration, 6),
                    "time": time.time(),
                },
            )

            thread_id: str | None = None
            usage: dict[str, int] = {}
            for line in stdout_lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "thread.started":
                    value = event.get("thread_id")
                    thread_id = str(value) if value else None
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    usage = {
                        str(key): int(value)
                        for key, value in event["usage"].items()
                        if isinstance(value, int)
                    }

            output: dict[str, Any] | None = None
            if invocation.output_file.exists():
                try:
                    value = json.loads(invocation.output_file.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        output = value
                except json.JSONDecodeError:
                    output = None
            return InvocationResult(
                role=invocation.role,
                returncode=int(returncode),
                output=output,
                usage=usage,
                thread_id=thread_id,
                duration_seconds=duration,
                timed_out=timed_out,
                stopped=stopped,
            )
        finally:
            try:
                if process is not None and not containment_cleanup_attempted:
                    containment_cleanup_attempted = True
                    if windows:
                        self._cleanup_process_containment(
                            process,
                            containment=containment,
                            process_group_id=process_group_id,
                        )
                    elif process_group_id is not None and posix_control is not None:
                        self._shutdown_posix_supervisor(
                            process,
                            process_group_id,
                            posix_control,
                        )
                    else:
                        self._terminate_exact_process(process)
            finally:
                try:
                    if posix_control is not None:
                        posix_control.close()
                finally:
                    try:
                        if posix_control_read_fd is not None:
                            os.close(posix_control_read_fd)
                    finally:
                        try:
                            if posix_control_write_fd is not None:
                                os.close(posix_control_write_fd)
                        finally:
                            if containment is not None:
                                containment.close()

    def role_config(self, role: str) -> RoleConfig:
        try:
            return self.config.roles[role]
        except KeyError as error:
            raise ValueError(f"unknown role: {role}") from error

    @staticmethod
    def _write_command_receipt(
        invocation: CodexInvocation,
        *,
        command: tuple[str, ...],
        command_sha256: str,
    ) -> None:
        if _FORBIDDEN_EXECUTION_POLICY_FLAG in command:
            raise ValueError("command receipt may not override execution-policy rules")
        path = invocation.output_file.parent / "command.json"
        receipt = {
            "schema_version": 1,
            "role": invocation.role,
            "argv": list(command),
            "command_sha256": command_sha256,
            "ignore_rules": False,
        }
        line = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _append_process_event(path: Path, event: dict[str, Any]) -> None:
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        lock_path = path.with_suffix(path.suffix + ".lock")
        with exclusive_file_lock(lock_path):
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    @staticmethod
    def _terminate_exact_process(process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            raise RuntimeError(
                "Windows process termination requires its held Job or process handle"
            )
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _establish_posix_process_group(process: subprocess.Popen[str]) -> int:
        group_id = int(process.pid)
        try:
            actual_group_id = os.getpgid(process.pid)
        except ProcessLookupError:
            if process.poll() is not None:
                return group_id
            raise
        if int(actual_group_id) != group_id:
            raise RuntimeError(
                f"model process {process.pid} is not the leader of its process group"
            )
        return group_id

    def _cleanup_process_containment(
        self,
        process: subprocess.Popen[str],
        *,
        containment: _WindowsJob | None,
        process_group_id: int | None,
    ) -> None:
        del process, process_group_id
        if containment is None:
            raise RuntimeError("Windows Job Object containment is unavailable")
        containment.terminate_and_verify()

    @staticmethod
    def _terminate_process_group(
        process: subprocess.Popen[str],
        *,
        force: bool = False,
        group_id: int | None = None,
    ) -> None:
        if os.name == "nt":
            raise RuntimeError(
                "Windows process-group termination requires the held Job handle"
            )
        target_group_id = process.pid if group_id is None else group_id
        try:
            os.killpg(target_group_id, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return
