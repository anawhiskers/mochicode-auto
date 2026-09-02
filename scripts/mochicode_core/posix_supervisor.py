"""Experimental Linux containment helper.

Default model and verifier release entry points refuse every non-Windows host
before selecting or starting this helper.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from typing import Any, TextIO


_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_MODEL_STOP_GRACE_SECONDS = 1.0
_CLEANUP_TIMEOUT_SECONDS = 10.0
_EMPTY_VERIFICATION_PASSES = 5
_shutdown_signal: int | None = None


def _control_message(handle: TextIO, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    handle.flush()


def _required_linux_primitives() -> Path:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("trusted supervisor requires Linux")
    children_path = Path("/proc/self/task") / str(os.getpid()) / "children"
    if not children_path.is_file():
        raise RuntimeError("kernel proc child-list interface is unavailable")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("kernel pidfd signaling primitives are unavailable")
    probe_fd = os.pidfd_open(os.getpid(), 0)
    os.close(probe_fd)
    return children_path


def _set_and_verify_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        prctl = libc.prctl
    except AttributeError as error:
        raise RuntimeError("prctl is unavailable") from error
    prctl.restype = ctypes.c_int
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]

    ctypes.set_errno(0)
    if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "PR_SET_CHILD_SUBREAPER failed")

    enabled = ctypes.c_int()
    ctypes.set_errno(0)
    if prctl(
        _PR_GET_CHILD_SUBREAPER,
        ctypes.addressof(enabled),
        0,
        0,
        0,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "PR_GET_CHILD_SUBREAPER failed")
    if enabled.value != 1:
        raise RuntimeError("kernel did not retain child-subreaper authority")


def _request_shutdown(signum: int, frame: object) -> None:
    del frame
    global _shutdown_signal
    _shutdown_signal = int(signum)


def _signal_group(group_id: int, value: int) -> None:
    try:
        os.killpg(group_id, value)
    except ProcessLookupError:
        return


def _wait_for_model_exit(pidfd: int, model_group_id: int) -> None:
    stop_sent = False
    stop_deadline = 0.0
    while True:
        readable, _, _ = select.select([pidfd], [], [], 0.02)
        if readable:
            return
        if _shutdown_signal is not None and not stop_sent:
            _signal_group(model_group_id, signal.SIGTERM)
            stop_sent = True
            stop_deadline = time.monotonic() + _MODEL_STOP_GRACE_SECONDS
        elif stop_sent and time.monotonic() >= stop_deadline:
            _signal_group(model_group_id, signal.SIGKILL)


def _finish_model_leader(model: subprocess.Popen[bytes], pidfd: int) -> int:
    try:
        _wait_for_model_exit(pidfd, model.pid)
        _signal_group(model.pid, signal.SIGKILL)
        return int(model.wait())
    finally:
        os.close(pidfd)


def _read_direct_children(children_path: Path) -> tuple[int, ...]:
    raw = children_path.read_text(encoding="ascii").strip()
    if not raw:
        return ()
    children = tuple(sorted({int(value) for value in raw.split()}))
    if any(pid <= 0 for pid in children):
        raise RuntimeError("kernel returned an invalid adopted-child PID")
    return children


def _kill_exact_pid(pid: int) -> None:
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
    except ProcessLookupError:
        return
    finally:
        os.close(pidfd)


def _reap_available_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _cleanup_adopted_descendants(children_path: Path) -> None:
    deadline = time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
    empty_passes = 0
    last_children: tuple[int, ...] = ()
    cleanup_error: BaseException | None = None
    while True:
        verified_empty = False
        try:
            _reap_available_children()
            last_children = _read_direct_children(children_path)
            for pid in last_children:
                _kill_exact_pid(pid)
            _reap_available_children()

            remaining_children = _read_direct_children(children_path)
            if not remaining_children:
                empty_passes += 1
                if empty_passes >= _EMPTY_VERIFICATION_PASSES:
                    verified_empty = True
            else:
                empty_passes = 0
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

        if verified_empty:
            if cleanup_error is not None:
                raise RuntimeError(
                    "supervisor cleanup recovered only after an error"
                ) from cleanup_error
            return

        if time.monotonic() >= deadline and cleanup_error is None:
            cleanup_error = RuntimeError(
                "supervisor cleanup exceeded its convergence deadline; adopted PIDs="
                + ",".join(str(pid) for pid in last_children)
            )
        time.sleep(0.02)


def _validated_command(raw: str) -> list[str]:
    value = json.loads(raw)
    if not isinstance(value, list) or not value:
        raise ValueError("model command must be a non-empty JSON list")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError("model command entries must be non-empty strings")
    return value


def _run(control: TextIO, command: list[str], *, mode: str = "model") -> int:
    if mode not in {"model", "command"}:
        raise ValueError("unsupported supervisor mode")
    children_path: Path | None = None
    model: subprocess.Popen[bytes] | None = None
    model_pidfd: int | None = None
    model_returncode: int | None = None
    failure: BaseException | None = None
    try:
        children_path = _required_linux_primitives()
        _set_and_verify_subreaper()
        signal.signal(signal.SIGTERM, _request_shutdown)
        signal.signal(signal.SIGINT, _request_shutdown)
        _control_message(
            control,
            "ready",
            supervisor_pid=os.getpid(),
            mode=mode,
        )

        prompt = sys.stdin.buffer.read()
        if _shutdown_signal is not None:
            model_returncode = -int(_shutdown_signal)
        else:
            model = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=None,
                stderr=None,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
            model_pidfd = os.pidfd_open(model.pid, 0)
            assert model.stdin is not None
            try:
                model.stdin.write(prompt)
                model.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                model.stdin.close()
            try:
                model_returncode = _finish_model_leader(model, model_pidfd)
            finally:
                model_pidfd = None
    except BaseException as error:
        failure = error
    finally:
        if model is not None and model.returncode is None:
            cleanup_pidfd: int | None = model_pidfd
            if cleanup_pidfd is None:
                try:
                    cleanup_pidfd = os.pidfd_open(model.pid, 0)
                except OSError as pidfd_error:
                    failure = pidfd_error
            try:
                _signal_group(model.pid, signal.SIGKILL)
            except BaseException as signal_error:
                failure = signal_error
            try:
                model_returncode = int(model.wait())
            except BaseException as wait_error:
                failure = wait_error
            finally:
                if cleanup_pidfd is not None:
                    os.close(cleanup_pidfd)
                model_pidfd = None
        elif model_pidfd is not None:
            os.close(model_pidfd)
            model_pidfd = None
        if children_path is not None:
            try:
                _cleanup_adopted_descendants(children_path)
            except BaseException as cleanup_error:
                failure = cleanup_error

    if failure is not None:
        _control_message(
            control,
            "error",
            message=f"{type(failure).__name__}: {failure}",
        )
        return 125
    if model_returncode is None:
        _control_message(control, "error", message="model return code unavailable")
        return 125
    _control_message(
        control,
        "finished",
        model_returncode=int(model_returncode),
        mode=mode,
    )
    return 0


def main() -> int:
    if len(sys.argv) == 3:
        mode = "model"
        control_arg = sys.argv[1]
        command_arg = sys.argv[2]
    elif len(sys.argv) == 4 and sys.argv[1] in {"model", "command"}:
        mode = sys.argv[1]
        control_arg = sys.argv[2]
        command_arg = sys.argv[3]
    else:
        return 125
    try:
        control_fd = int(control_arg)
        command = _validated_command(command_arg)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 125
    with os.fdopen(control_fd, "w", encoding="utf-8", buffering=1) as control:
        try:
            return _run(control, command, mode=mode)
        except (BrokenPipeError, OSError):
            return 125


if __name__ == "__main__":
    raise SystemExit(main())
