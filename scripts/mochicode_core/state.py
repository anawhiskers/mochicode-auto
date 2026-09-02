from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from collections.abc import Iterator

from .models import PacketState, PacketStatus, RunBudget, RunState


class StateLockError(RuntimeError):
    pass


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def exclusive_run_lease(root: Path) -> Iterator[None]:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".run.lease.json"
    token = secrets.token_hex(16)
    payload = (
        json.dumps(
            {
                "pid": os.getpid(),
                "token": token,
                "created_at": time.time(),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            try:
                owner = json.loads(path.read_text(encoding="utf-8"))
                owner_pid = int(owner["pid"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as parse_error:
                raise StateLockError(f"run lease is unreadable and was left in place: {path}") from parse_error
            if _pid_is_live(owner_pid):
                raise StateLockError(
                    f"run lease is held by live PID {owner_pid}: {path}"
                ) from error
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        break

    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict) and current.get("token") == token:
            path.unlink(missing_ok=True)


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float = 5.0,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
        os.fsync(descriptor)
    acquired = False
    while not acquired:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise StateLockError(f"state lock is busy: {path}") from error
            time.sleep(poll_seconds)
    try:
        payload = (
            json.dumps(
                {
                    "pid": os.getpid(),
                    "token": secrets.token_hex(16),
                    "acquired_at": time.time(),
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        os.ftruncate(descriptor, len(payload))
        os.fsync(descriptor)
        yield
    finally:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _state_to_dict(state: RunState) -> dict[str, object]:
    data = asdict(state)
    for packet in data["packets"]:  # type: ignore[index]
        packet["status"] = str(packet["status"].value if isinstance(packet["status"], PacketStatus) else packet["status"])
    return data


def _state_from_dict(data: dict[str, object]) -> RunState:
    raw_packets = data.get("packets")
    if not isinstance(raw_packets, list):
        raise ValueError("state packets must be a list")
    packets: list[PacketState] = []
    for raw in raw_packets:
        if not isinstance(raw, dict):
            raise ValueError("state packet must be an object")
        packets.append(
            PacketState(
                packet_id=str(raw["packet_id"]),
                title=str(raw["title"]),
                wave=int(raw["wave"]),
                goal=str(raw.get("goal", "")),
                priority=int(raw.get("priority", 100)),
                dependencies=tuple(str(item) for item in raw.get("dependencies", [])),
                vertical_slice=bool(raw.get("vertical_slice", False)),
                acceptance_criteria=tuple(
                    str(item) for item in raw.get("acceptance_criteria", [])
                ),
                verification_commands=tuple(
                    str(item) for item in raw.get("verification_commands", [])
                ),
                status=PacketStatus(str(raw.get("status", PacketStatus.PENDING.value))),
                attempts=int(raw.get("attempts", 0)),
                implementation_attempts=int(raw.get("implementation_attempts", 0)),
                active_implementation_attempt=(
                    None
                    if raw.get("active_implementation_attempt") is None
                    else int(raw["active_implementation_attempt"])
                ),
                fingerprints=[str(item) for item in raw.get("fingerprints", [])],
                last_failure=(
                    None if raw.get("last_failure") is None else str(raw["last_failure"])
                ),
            )
        )
    raw_budget = data.get("budget")
    if not isinstance(raw_budget, dict):
        raise ValueError("state budget must be an object")
    budget = RunBudget(
        max_model_calls=int(raw_budget.get("max_model_calls", 24)),
        max_rounds=int(raw_budget.get("max_rounds", 16)),
        max_attempts_per_packet=int(raw_budget.get("max_attempts_per_packet", 2)),
        max_wall_seconds=int(raw_budget.get("max_wall_seconds", 7200)),
    )
    return RunState(
        run_id=str(data["run_id"]),
        goal=str(data["goal"]),
        project_root=str(data["project_root"]),
        packets=packets,
        queue=[str(item) for item in data.get("queue", [])],
        source_head=str(data.get("source_head", "")),
        source_branch=str(data.get("source_branch", "")),
        integration_head=str(data.get("integration_head", "")),
        budget=budget,
        status=str(data.get("status", "running")),
        model_calls=int(data.get("model_calls", 0)),
        rounds=int(data.get("rounds", 0)),
        replans=int(data.get("replans", 0)),
        stop_requested=bool(data.get("stop_requested", False)),
        started_at=float(data.get("started_at", 0.0)),
        updated_at=float(data.get("updated_at", 0.0)),
    )


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.stop_path = self.root / "STOP"
        self.lock_path = self.root / ".state.lock"

    def save(self, state: RunState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_state_to_dict(state), indent=2, sort_keys=True) + "\n"
        with exclusive_file_lock(self.lock_path):
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix="state-",
                    suffix=".tmp",
                    dir=self.root,
                    delete=False,
                ) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary_path = Path(handle.name)
                os.replace(temporary_path, self.state_path)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    def load(self) -> RunState:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root must be an object")
        return _state_from_dict(data)

    def request_stop(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text("stop requested\n", encoding="utf-8")

    def resume(self) -> None:
        self.stop_path.unlink(missing_ok=True)

    def apply_stop_state(self, state: RunState) -> None:
        state.stop_requested = self.stop_path.exists()
