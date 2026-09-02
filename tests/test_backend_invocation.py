from __future__ import annotations

import ctypes
from dataclasses import replace
import inspect
import io
import json
from functools import partial
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import mochicode_core.backend as backend_module
import mochicode_core.posix_supervisor as posix_supervisor
from mochicode_core.backend import CodexCliBackend, CodexInvocation
from mochicode_core.config import load_config


FAKE_CODEX = """
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
output_path = Path(args[args.index("--output-last-message") + 1])
prompt = sys.stdin.read()
output_path.write_text(
    json.dumps(
        {
            "prompt": prompt,
            "argv": args,
            "api_key_present": bool(
                os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
            ),
        }
    ),
    encoding="utf-8",
)
print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
print(
    json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "output_tokens": 3,
                "reasoning_output_tokens": 2,
            },
        }
    ),
    flush=True,
)
"""


FLOODING_FAKE_CODEX = """
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
output_path = Path(args[args.index("--output-last-message") + 1])
output_path.write_text(json.dumps({"status": "should-not-pass"}), encoding="utf-8")
print("X" * 8192, flush=True)
time.sleep(60)
"""


SPAWNING_FAKE_CODEX = """
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
output_path = Path(args[args.index("--output-last-message") + 1])
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
(Path.cwd() / "descendant.pid").write_text(str(child.pid), encoding="utf-8")
output_path.write_text(json.dumps({"status": "normal", "child_pid": child.pid}), encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "spawning-fake-thread"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}), flush=True)
"""


POSIX_ESCAPING_FAKE_CODEX = """
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
output_path = Path(args[args.index("--output-last-message") + 1])
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    start_new_session=True,
)
(Path.cwd() / "escaped-descendant.pid").write_text(str(child.pid), encoding="utf-8")
output_path.write_text(json.dumps({"status": "normal", "child_pid": child.pid}), encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "escaping-fake-thread"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}), flush=True)
"""


def _linux_subreaper_runtime_supported() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None)
        getattr(libc, "prctl")
    except (AttributeError, OSError):
        return False
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        probe_fd = os.pidfd_open(os.getpid(), 0)
    except OSError:
        return False
    os.close(probe_fd)
    return (
        Path("/proc/self/task") / str(os.getpid()) / "children"
    ).is_file()


class RecordingStdin(io.StringIO):
    def __init__(self, events: list[tuple[str, int]], pid: int) -> None:
        super().__init__()
        self._events = events
        self._pid = pid

    def write(self, value: str) -> int:
        self._events.append(("prompt", self._pid))
        return super().write(value)


class BlockingFakeChild:
    """A Popen-shaped child that stays alive until exact containment releases it."""

    def __init__(
        self,
        pid: int,
        *,
        events: list[tuple[str, int]] | None = None,
        process_handle: int | None = None,
        simulate_pid_reuse: bool = False,
        poll_hook: object | None = None,
    ) -> None:
        self.pid = pid
        self._handle = process_handle if process_handle is not None else pid + 100_000
        self.stdin = RecordingStdin(events, pid) if events is not None else io.StringIO()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.signals: list[int] = []
        self.returncode: int | None = None
        self.released = threading.Event()
        self.simulate_pid_reuse = simulate_pid_reuse
        self.pid_reused_after_poll = False
        self.poll_hook = poll_hook
        self.poll_hook_called = False

    def poll(self) -> int | None:
        if self.poll_hook is not None and not self.poll_hook_called:
            self.poll_hook_called = True
            self.poll_hook()
        if self.simulate_pid_reuse:
            self.pid_reused_after_poll = True
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self.released.wait(timeout):
            raise subprocess.TimeoutExpired("fake-codex", timeout)
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, value: int) -> None:
        self.signals.append(value)
        raise OSError("blocking fake child does not handle CTRL_BREAK_EVENT")

    def release_by_termination(self) -> None:
        self.returncode = -9
        self.released.set()


class CompletedFakeChild:
    def __init__(
        self,
        pid: int,
        *,
        events: list[tuple[str, int]] | None = None,
        returncode: int = 0,
        process_handle: int | None = None,
    ) -> None:
        self.pid = pid
        self._handle = process_handle if process_handle is not None else pid + 100_000
        self.stdin = RecordingStdin(events, pid) if events is not None else io.StringIO()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode


class FakeWindowsContainment:
    def __init__(
        self,
        *,
        assign_error: BaseException | None = None,
        resume_error: BaseException | None = None,
        terminate_error: BaseException | None = None,
        events: list[tuple[str, int]] | None = None,
    ) -> None:
        self.assigned_pid: int | None = None
        self.assigned_process_handle: int | None = None
        self.resumed_pid: int | None = None
        self.terminate_calls = 0
        self.verify_calls = 0
        self.close_calls = 0
        self.unassigned_process_handles: list[int] = []
        self._assigned_process: object | None = None
        self._terminated = False
        self.events = events if events is not None else []
        self.assign_error = assign_error
        self.resume_error = resume_error
        self.terminate_error = terminate_error

    def assign(self, process: object) -> None:
        self.events.append(("assign", int(process.pid)))
        if self.assign_error is not None:
            raise self.assign_error
        self.assigned_pid = int(process.pid)
        self.assigned_process_handle = int(process._handle)
        self._assigned_process = process

    def resume(self, process: object) -> None:
        self.events.append(("resume", int(process.pid)))
        if self.resume_error is not None:
            raise self.resume_error
        self.resumed_pid = int(process.pid)

    def terminate_and_verify(self) -> None:
        self.verify_calls += 1
        if self.assigned_pid is None:
            return
        if not self._terminated:
            self.terminate_calls += 1
            if self.terminate_error is not None:
                raise self.terminate_error
            self._terminated = True
            release = getattr(self._assigned_process, "release_by_termination", None)
            if release is not None:
                release()

    def terminate_unassigned_process(self, process: object) -> None:
        process_handle = int(process._handle)
        self.unassigned_process_handles.append(process_handle)
        release = getattr(process, "release_by_termination", None)
        if release is not None:
            release()

    def close(self) -> None:
        self.close_calls += 1


class BackendInvocationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires native Windows Job containment")
    def test_output_flood_is_bounded_and_terminates_the_owned_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "flooding_fake_codex.py"
            fake.write_text(textwrap.dedent(FLOODING_FAKE_CODEX), encoding="utf-8")
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            invocation = CodexInvocation(
                role="sol_plan",
                cwd=root,
                prompt="bounded output test",
                output_schema=schema,
                output_file=root / "result.json",
                event_log=root / "events.jsonl",
                process_log=root / "processes.jsonl",
                stop_path=root / "STOP",
            )
            backend = CodexCliBackend(
                (sys.executable, str(fake)),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )
            bounded = partial(backend_module.BoundedTextCapture, 256)
            with patch.object(backend_module, "BoundedTextCapture", bounded):
                result = backend.invoke(invocation)

            self.assertEqual(result.returncode, 125)
            self.assertTrue(result.resource_limited)
            self.assertFalse(result.timed_out)
            self.assertLess(invocation.event_log.stat().st_size, 1024)
            self.assertIn("output limit exceeded", invocation.event_log.read_text(encoding="utf-8"))
            finished = json.loads(invocation.process_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertTrue(finished["resource_limited"])

    def test_prompt_uses_stdin_and_process_and_usage_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "fake_codex.py"
            fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            invocation = CodexInvocation(
                role="sol_plan",
                cwd=root,
                prompt="private project goal",
                output_schema=schema,
                output_file=root / "result.json",
                event_log=root / "events.jsonl",
                process_log=root / "processes.jsonl",
                stop_path=root / "STOP",
            )
            backend = CodexCliBackend(
                (sys.executable, str(fake)),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "must-not-reach-child",
                    "CODEX_API_KEY": "must-not-reach-child",
                },
            ):
                result = backend.invoke(invocation)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.thread_id, "fake-thread")
            self.assertEqual(result.usage["input_tokens"], 10)
            self.assertIn("[MOCHICODE_CHILD role=sol_plan]", result.output["prompt"])
            self.assertIn("private project goal", result.output["prompt"])
            self.assertNotIn("private project goal", " ".join(result.output["argv"]))
            self.assertFalse(result.output["api_key_present"])
            process_rows = [
                json.loads(line)
                for line in invocation.process_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["event"] for row in process_rows], ["started", "finished"])
            self.assertEqual(process_rows[0]["pid"], process_rows[1]["pid"])
            command_receipt = json.loads((root / "command.json").read_text(encoding="utf-8"))
            self.assertIs(command_receipt["ignore_rules"], False)
            self.assertNotIn("--ignore-rules", command_receipt["argv"])
            for row in process_rows:
                self.assertEqual(row["argv"], command_receipt["argv"])
                self.assertEqual(row["command_sha256"], command_receipt["command_sha256"])
                self.assertIs(row["ignore_rules"], False)

    def test_ignore_rules_are_false_in_command_and_process_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "fake_codex.py"
            fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            invocation = CodexInvocation(
                role="terra_review",
                cwd=root,
                prompt="bounded benchmark review",
                output_schema=schema,
                output_file=root / "result.json",
                event_log=root / "events.jsonl",
                process_log=root / "processes.jsonl",
                stop_path=root / "STOP",
            )
            config = load_config(PLUGIN_ROOT / "config" / "default.toml")
            result = CodexCliBackend((sys.executable, str(fake)), config).invoke(invocation)

            self.assertEqual(result.returncode, 0)
            command_receipt = json.loads((root / "command.json").read_text(encoding="utf-8"))
            self.assertIs(command_receipt["ignore_rules"], False)
            self.assertNotIn("--ignore-rules", command_receipt["argv"])
            process_rows = [
                json.loads(line)
                for line in invocation.process_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["event"] for row in process_rows], ["started", "finished"])
            for row in process_rows:
                self.assertEqual(row["argv"], command_receipt["argv"])
                self.assertEqual(row["command_sha256"], command_receipt["command_sha256"])
                self.assertIs(row["ignore_rules"], False)

    def test_normal_return_terminates_exact_process_group_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "spawning_fake_codex.py"
            fake.write_text(textwrap.dedent(SPAWNING_FAKE_CODEX), encoding="utf-8")
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, str(fake)),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )
            child_pid_path = root / "descendant.pid"
            child_pid: int | None = None
            try:
                result = backend.invoke(invocation)
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.output, {"status": "normal", "child_pid": child_pid})
                self.assertEqual(result.thread_id, "spawning-fake-thread")
                self.assertFalse(self._pid_is_alive(child_pid))
            finally:
                if child_pid is None and child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                if child_pid is not None:
                    self._terminate_exact_pid_for_test(child_pid)

    @unittest.skipUnless(
        _linux_subreaper_runtime_supported(),
        "requires Linux prctl subreaper and proc child-list support",
    )
    def _experimental_linux_supervisor_reaps_descendant_that_escapes_with_setsid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "escaping_fake_codex.py"
            fake.write_text(textwrap.dedent(POSIX_ESCAPING_FAKE_CODEX), encoding="utf-8")
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, str(fake)),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )
            child_pid_path = root / "escaped-descendant.pid"
            child_pid: int | None = None
            try:
                result = backend.invoke(invocation)
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                self.assertEqual(result.returncode, 0)
                self.assertEqual(
                    result.output,
                    {"status": "normal", "child_pid": child_pid},
                )
                self.assertEqual(result.thread_id, "escaping-fake-thread")
                self.assertFalse(self._pid_is_alive(child_pid))
            finally:
                if child_pid is None and child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                if child_pid is not None:
                    self._terminate_exact_pid_for_test(child_pid)

    def _experimental_linux_supervisor_signals_group_before_reaping_leader(self) -> None:
        events: list[tuple[object, ...]] = []

        class FakeModel:
            pid = 4401

            def wait(self) -> int:
                events.append(("wait", self.pid))
                return 0

        with (
            patch.object(
                posix_supervisor,
                "_wait_for_model_exit",
                side_effect=lambda pidfd, group_id: events.append(
                    ("pidfd-ready", pidfd, group_id)
                ),
                create=True,
            ),
            patch.object(
                posix_supervisor,
                "_signal_group",
                side_effect=lambda group_id, value: events.append(
                    ("killpg", group_id, value)
                ),
            ),
            patch.object(
                posix_supervisor.os,
                "close",
                side_effect=lambda fd: events.append(("close", fd)),
            ),
            patch.object(posix_supervisor.signal, "SIGKILL", 9, create=True),
        ):
            returncode = posix_supervisor._finish_model_leader(FakeModel(), 77)

        self.assertEqual(returncode, 0)
        self.assertEqual(
            events,
            [
                ("pidfd-ready", 77, 4401),
                ("killpg", 4401, 9),
                ("wait", 4401),
                ("close", 77),
            ],
        )

    def _experimental_linux_adopted_cleanup_contains_no_numeric_group_signal(self) -> None:
        source = inspect.getsource(posix_supervisor._cleanup_adopted_descendants)
        self.assertNotIn("killpg", source)
        self.assertNotIn("_signal_group", source)
        self.assertIn("_kill_exact_pid", source)

    def _experimental_non_linux_posix_refuses_before_starting_popen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            def missing_group(*args: object) -> None:
                raise ProcessLookupError

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(
                        name="posix",
                        environ=os.environ,
                        fsync=os.fsync,
                        getpgid=lambda pid: pid,
                        killpg=missing_group,
                    ),
                ),
                patch.object(
                    backend_module,
                    "sys",
                    SimpleNamespace(platform="darwin", executable=sys.executable),
                    create=True,
                ),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=CompletedFakeChild(pid=7101),
                ) as popen_mock,
                patch.object(backend_module.signal, "SIGTERM", 15, create=True),
                patch.object(backend_module.signal, "SIGKILL", 9, create=True),
            ):
                with self.assertRaisesRegex(RuntimeError, "Linux supervisor"):
                    backend.invoke(invocation)

            popen_mock.assert_not_called()

    def _experimental_linux_missing_subreaper_primitives_refuses_before_starting_popen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            def missing_group(*args: object) -> None:
                raise ProcessLookupError

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(
                        name="posix",
                        environ=os.environ,
                        fsync=os.fsync,
                        getpgid=lambda pid: pid,
                        killpg=missing_group,
                    ),
                ),
                patch.object(
                    backend_module,
                    "sys",
                    SimpleNamespace(platform="linux", executable=sys.executable),
                    create=True,
                ),
                patch.object(
                    CodexCliBackend,
                    "_linux_subreaper_primitives_available",
                    return_value=False,
                    create=True,
                ),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=CompletedFakeChild(pid=7102),
                ) as popen_mock,
                patch.object(backend_module.signal, "SIGTERM", 15, create=True),
                patch.object(backend_module.signal, "SIGKILL", 9, create=True),
            ):
                with self.assertRaisesRegex(RuntimeError, "subreaper primitives"):
                    backend.invoke(invocation)

            popen_mock.assert_not_called()

    def _experimental_linux_supervisor_readiness_error_fails_before_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events: list[tuple[str, int]] = []
            child = CompletedFakeChild(
                pid=7201,
                events=events,
                returncode=125,
            )
            control = io.StringIO()
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            def missing_group(*args: object) -> None:
                raise ProcessLookupError

            fake_os = SimpleNamespace(
                name="posix",
                environ=os.environ,
                fsync=os.fsync,
                pipe=lambda: (81, 82),
                close=lambda fd: None,
                fdopen=lambda *args, **kwargs: control,
                getpgid=lambda pid: pid,
                killpg=missing_group,
            )
            with (
                patch.object(backend_module, "os", fake_os),
                patch.object(
                    backend_module,
                    "sys",
                    SimpleNamespace(platform="linux", executable=sys.executable),
                ),
                patch.object(
                    backend,
                    "_require_posix_supervisor",
                    return_value=Path("trusted-supervisor.py"),
                ),
                patch.object(
                    backend,
                    "_read_posix_supervisor_message",
                    return_value={
                        "event": "error",
                        "message": "subreaper verification failed",
                    },
                ),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=child,
                ) as popen_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "containment"):
                    backend.invoke(invocation)

            popen_mock.assert_called_once()
            self.assertNotIn(("prompt", child.pid), events)
            self.assertFalse(invocation.process_log.exists())

    def _experimental_linux_supervisor_cleanup_error_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events: list[tuple[str, int]] = []
            child = CompletedFakeChild(
                pid=7202,
                events=events,
                returncode=125,
            )
            control = io.StringIO()
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            def missing_group(*args: object) -> None:
                raise ProcessLookupError

            fake_os = SimpleNamespace(
                name="posix",
                environ=os.environ,
                fsync=os.fsync,
                pipe=lambda: (91, 92),
                close=lambda fd: None,
                fdopen=lambda *args, **kwargs: control,
                getpgid=lambda pid: pid,
                killpg=missing_group,
            )
            with (
                patch.object(backend_module, "os", fake_os),
                patch.object(
                    backend_module,
                    "sys",
                    SimpleNamespace(platform="linux", executable=sys.executable),
                ),
                patch.object(
                    backend,
                    "_require_posix_supervisor",
                    return_value=Path("trusted-supervisor.py"),
                ),
                patch.object(
                    backend,
                    "_read_posix_supervisor_message",
                    side_effect=(
                        {"event": "ready", "supervisor_pid": child.pid},
                        {"event": "error", "message": "adopted child remained"},
                    ),
                ),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=child,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    backend.invoke(invocation)

            self.assertIn(("prompt", child.pid), events)
            process_rows = [
                json.loads(line)
                for line in invocation.process_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["event"] for row in process_rows], ["started"])

    def _experimental_linux_stop_signals_exact_supervisor_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stop_path = root / "STOP"
            stop_path.write_text("stop", encoding="utf-8")
            child = BlockingFakeChild(pid=7301)
            control = io.StringIO()
            signals: list[tuple[int, int]] = []
            invocation = self._make_invocation(root, stop_path=stop_path)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            def killpg(group_id: int, value: int) -> None:
                signals.append((group_id, value))
                if value == 0:
                    raise ProcessLookupError
                child.returncode = 0
                child.released.set()

            fake_os = SimpleNamespace(
                name="posix",
                environ=os.environ,
                fsync=os.fsync,
                pipe=lambda: (101, 102),
                close=lambda fd: None,
                fdopen=lambda *args, **kwargs: control,
                getpgid=lambda pid: pid,
                killpg=killpg,
            )
            with (
                patch.object(backend_module, "os", fake_os),
                patch.object(
                    backend_module,
                    "sys",
                    SimpleNamespace(platform="linux", executable=sys.executable),
                ),
                patch.object(
                    backend,
                    "_require_posix_supervisor",
                    return_value=Path("trusted-supervisor.py"),
                ),
                patch.object(
                    backend,
                    "_read_posix_supervisor_message",
                    side_effect=(
                        {"event": "ready", "supervisor_pid": child.pid},
                        {"event": "finished", "model_returncode": -15},
                    ),
                ),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=child,
                ),
                patch.object(backend_module.signal, "SIGTERM", 15, create=True),
            ):
                result = backend.invoke(invocation)

            self.assertTrue(result.stopped)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.returncode, -15)
            self.assertEqual(signals, [(child.pid, 15)])
            process_rows = [
                json.loads(line)
                for line in invocation.process_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(process_rows[0]["pid"], child.pid)
            self.assertEqual(process_rows[1]["pid"], child.pid)

    def _experimental_linux_timeout_signals_exact_supervisor_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = BlockingFakeChild(pid=7302)
            control = io.StringIO()
            signals: list[tuple[int, int]] = []
            invocation = self._make_invocation(root)
            config = load_config(PLUGIN_ROOT / "config" / "default.toml")
            timed_config = replace(
                config,
                roles={
                    **config.roles,
                    "sol_plan": replace(config.roles["sol_plan"], timeout_seconds=1),
                },
            )
            backend = CodexCliBackend((sys.executable, "fake-codex"), timed_config)

            def killpg(group_id: int, value: int) -> None:
                signals.append((group_id, value))
                if value == 0:
                    raise ProcessLookupError
                child.returncode = 0
                child.released.set()

            fake_os = SimpleNamespace(
                name="posix",
                environ=os.environ,
                fsync=os.fsync,
                pipe=lambda: (111, 112),
                close=lambda fd: None,
                fdopen=lambda *args, **kwargs: control,
                getpgid=lambda pid: pid,
                killpg=killpg,
            )
            self._timeout_clock = iter((100.0, 101.0))
            with (
                patch.object(backend_module, "os", fake_os),
                patch.object(
                    backend_module,
                    "sys",
                    SimpleNamespace(platform="linux", executable=sys.executable),
                ),
                patch.object(
                    backend,
                    "_require_posix_supervisor",
                    return_value=Path("trusted-supervisor.py"),
                ),
                patch.object(
                    backend,
                    "_read_posix_supervisor_message",
                    side_effect=(
                        {"event": "ready", "supervisor_pid": child.pid},
                        {"event": "finished", "model_returncode": -15},
                    ),
                ),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=child,
                ),
                patch.object(backend_module.signal, "SIGTERM", 15, create=True),
                patch.object(
                    backend_module.time,
                    "monotonic",
                    side_effect=lambda: next(self._timeout_clock, 101.25),
                ),
            ):
                result = backend.invoke(invocation)

            self.assertTrue(result.timed_out)
            self.assertFalse(result.stopped)
            self.assertEqual(result.returncode, -15)
            self.assertEqual(signals, [(child.pid, 15)])

    def test_non_windows_release_policy_refuses_model_before_popen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )
            invocation = self._make_invocation(Path(raw))
            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="posix"),
                ),
                patch.object(
                    backend,
                    "_require_posix_supervisor",
                    side_effect=AssertionError("experimental supervisor selected"),
                ) as supervisor_selector,
                patch.object(backend_module.subprocess, "Popen") as popen_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "Windows-only release"):
                    backend.invoke(invocation)

            supervisor_selector.assert_not_called()
            popen_mock.assert_not_called()

    def test_linux_release_policy_refuses_even_with_experimental_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )
            invocation = self._make_invocation(Path(raw))
            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="posix"),
                ),
                patch.object(
                    backend_module,
                    "sys",
                    SimpleNamespace(platform="linux", executable=sys.executable),
                ),
                patch.object(
                    backend,
                    "_require_posix_supervisor",
                    side_effect=AssertionError("experimental supervisor selected"),
                ) as supervisor_selector,
                patch.object(backend_module.subprocess, "Popen") as popen_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "Windows-only release"):
                    backend.invoke(invocation)

            supervisor_selector.assert_not_called()
            popen_mock.assert_not_called()

    def test_windows_launch_is_suspended_assigned_and_resumed_before_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events: list[tuple[str, int]] = []
            child = CompletedFakeChild(pid=6201, events=events)
            containment = FakeWindowsContainment(events=events)
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="nt", fsync=os.fsync, environ=os.environ),
                ),
                patch.object(backend_module, "_WindowsJob", return_value=containment),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=child,
                ) as popen_mock,
                patch.object(
                    backend_module.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    512,
                    create=True,
                ),
            ):
                result = backend.invoke(invocation)

            self.assertEqual(result.returncode, 0)
            creationflags = popen_mock.call_args.kwargs["creationflags"]
            self.assertEqual(creationflags, 512 | 0x00000004)
            self.assertEqual(
                events,
                [
                    ("assign", child.pid),
                    ("resume", child.pid),
                    ("prompt", child.pid),
                ],
            )
            self.assertEqual(containment.assigned_pid, child.pid)
            self.assertEqual(containment.resumed_pid, child.pid)
            self.assertEqual(containment.close_calls, 1)

    def test_preexisting_stop_is_refused_immediately_before_popen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stop_path = root / "STOP"
            stop_path.write_text("stop\n", encoding="utf-8")
            invocation = self._make_invocation(root, stop_path=stop_path)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )
            child = CompletedFakeChild(pid=6209)
            containment = FakeWindowsContainment()

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="nt", fsync=os.fsync, environ=os.environ),
                ),
                patch.object(backend_module, "_WindowsJob", return_value=containment),
                patch.object(
                    backend_module.subprocess,
                    "Popen",
                    return_value=child,
                ) as popen_mock,
                patch.object(
                    backend_module.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    512,
                    create=True,
                ),
            ):
                result = backend.invoke(invocation)

            self.assertTrue(result.stopped)
            self.assertEqual(result.returncode, 130)
            popen_mock.assert_not_called()
            self.assertFalse(invocation.process_log.exists())
            command_receipt = json.loads((root / "command.json").read_text(encoding="utf-8"))
            self.assertIs(command_receipt["ignore_rules"], False)

    def test_stop_race_probe_is_refused_without_popen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stop_path = root / "STOP"

            class StopRaceProbe:
                def __init__(self) -> None:
                    self.calls = 0

                def exists(self) -> bool:
                    self.calls += 1
                    if self.calls == 1:
                        stop_path.write_text("stop race\n", encoding="utf-8")
                        return False
                    return stop_path.exists()

            invocation = self._make_invocation(root, stop_path=StopRaceProbe())
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )
            child = CompletedFakeChild(pid=6210)
            containment = FakeWindowsContainment()

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="nt", fsync=os.fsync, environ=os.environ),
                ),
                patch.object(backend_module, "_WindowsJob", return_value=containment),
                patch.object(backend_module.subprocess, "Popen", return_value=child) as popen_mock,
                patch.object(
                    backend_module.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    512,
                    create=True,
                ),
            ):
                result = backend.invoke(invocation)

            self.assertTrue(result.stopped)
            self.assertEqual(result.returncode, 130)
            self.assertEqual(invocation.stop_path.calls, 2)
            popen_mock.assert_not_called()

    def test_windows_resume_failure_uses_assigned_job_and_never_pid_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events: list[tuple[str, int]] = []
            child = BlockingFakeChild(pid=6202, events=events)
            containment = FakeWindowsContainment(
                resume_error=RuntimeError("resume unavailable"),
                events=events,
            )
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            def pid_lookup(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                child.release_by_termination()
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="nt", fsync=os.fsync, environ=os.environ),
                ),
                patch.object(backend_module, "_WindowsJob", return_value=containment),
                patch.object(backend_module.subprocess, "Popen", return_value=child),
                patch.object(
                    backend_module.subprocess,
                    "run",
                    side_effect=pid_lookup,
                ) as pid_lookup_mock,
                patch.object(
                    backend_module.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    512,
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "resume"):
                    backend.invoke(invocation)

            pid_lookup_mock.assert_not_called()
            self.assertEqual(
                events,
                [("assign", child.pid), ("resume", child.pid)],
            )
            self.assertNotIn(("prompt", child.pid), events)
            self.assertEqual(containment.assigned_process_handle, child._handle)
            self.assertEqual(containment.terminate_calls, 1)
            self.assertEqual(containment.unassigned_process_handles, [])
            self.assertEqual(containment.close_calls, 1)

    def test_windows_stop_uses_job_handle_after_poll_to_pid_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stop_path = root / "STOP"
            child = BlockingFakeChild(
                pid=7311,
                simulate_pid_reuse=True,
                poll_hook=lambda: stop_path.write_text("stop", encoding="utf-8"),
            )
            invocation = self._make_invocation(root, stop_path=stop_path)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            job_result = self._run_windows_case(backend, invocation, child)

            self.assertTrue(job_result.result.stopped)
            self.assertFalse(job_result.result.timed_out)
            self.assertEqual(job_result.result.returncode, -9)
            self.assertTrue(child.pid_reused_after_poll)
            job_result.pid_lookup.assert_not_called()
            self.assertEqual(
                job_result.containment.assigned_process_handle,
                child._handle,
            )
            self.assertEqual(job_result.containment.terminate_calls, 1)

    def test_windows_timeout_uses_job_handle_after_poll_to_pid_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = BlockingFakeChild(pid=8422, simulate_pid_reuse=True)
            invocation = self._make_invocation(root)
            config = load_config(PLUGIN_ROOT / "config" / "default.toml")
            timed_config = replace(
                config,
                roles={
                    **config.roles,
                    "sol_plan": replace(config.roles["sol_plan"], timeout_seconds=1),
                },
            )
            backend = CodexCliBackend((sys.executable, "fake-codex"), timed_config)

            self._timeout_clock = iter((100.0, 101.0))
            with patch.object(
                backend_module.time,
                "monotonic",
                side_effect=lambda: next(self._timeout_clock, 101.25),
            ):
                job_result = self._run_windows_case(backend, invocation, child)

            self.assertTrue(job_result.result.timed_out)
            self.assertFalse(job_result.result.stopped)
            self.assertEqual(job_result.result.returncode, -9)
            self.assertTrue(child.pid_reused_after_poll)
            job_result.pid_lookup.assert_not_called()
            self.assertEqual(
                job_result.containment.assigned_process_handle,
                child._handle,
            )
            self.assertEqual(job_result.containment.terminate_calls, 1)

    def test_windows_containment_setup_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = BlockingFakeChild(pid=9533)
            containment = FakeWindowsContainment(
                assign_error=RuntimeError("assignment unavailable")
            )
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            def pid_lookup(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                child.release_by_termination()
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="nt", fsync=os.fsync, environ=os.environ),
                ),
                patch.object(backend_module, "_WindowsJob", return_value=containment),
                patch.object(backend_module.subprocess, "Popen", return_value=child),
                patch.object(backend_module.subprocess, "run", side_effect=pid_lookup) as pid_lookup_mock,
                patch.object(backend_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
            ):
                with self.assertRaisesRegex(RuntimeError, "containment"):
                    backend.invoke(invocation)

            pid_lookup_mock.assert_not_called()
            self.assertIsNone(containment.assigned_pid)
            self.assertEqual(
                containment.unassigned_process_handles,
                [child._handle],
            )
            self.assertEqual(containment.close_calls, 1)

    def test_windows_containment_cleanup_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = CompletedFakeChild(pid=9644)
            containment = FakeWindowsContainment(
                terminate_error=RuntimeError("verification unavailable")
            )
            invocation = self._make_invocation(root)
            backend = CodexCliBackend(
                (sys.executable, "fake-codex"),
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
            )

            with (
                patch.object(
                    backend_module,
                    "os",
                    SimpleNamespace(name="nt", fsync=os.fsync, environ=os.environ),
                ),
                patch.object(backend_module, "_WindowsJob", return_value=containment),
                patch.object(backend_module.subprocess, "Popen", return_value=child),
                patch.object(backend_module.subprocess, "run") as pid_lookup_mock,
                patch.object(backend_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
                patch.object(
                    backend,
                    "_append_process_event",
                    side_effect=RuntimeError("body failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "verification unavailable"):
                    backend.invoke(invocation)

            self.assertEqual(containment.assigned_pid, child.pid)
            self.assertEqual(containment.resumed_pid, child.pid)
            self.assertEqual(containment.terminate_calls, 1)
            self.assertEqual(containment.close_calls, 1)
            pid_lookup_mock.assert_not_called()

    @staticmethod
    def _make_invocation(root: Path, *, stop_path: Path | None = None) -> CodexInvocation:
        schema = root / "schema.json"
        schema.write_text('{"type":"object"}', encoding="utf-8")
        return CodexInvocation(
            role="sol_plan",
            cwd=root,
            prompt="bounded fake child termination test",
            output_schema=schema,
            output_file=root / "result.json",
            event_log=root / "events.jsonl",
            process_log=root / "processes.jsonl",
            stop_path=stop_path,
        )

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
            return f'"{pid}"' in result.stdout
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _terminate_exact_pid_for_test(cls, pid: int) -> None:
        if not cls._pid_is_alive(pid):
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        else:
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and cls._pid_is_alive(pid):
            time.sleep(0.05)

    def _run_windows_case(
        self,
        backend: CodexCliBackend,
        invocation: CodexInvocation,
        child: BlockingFakeChild,
    ) -> SimpleNamespace:
        containment = FakeWindowsContainment()

        def pid_lookup(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            child.release_by_termination()
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(
                backend_module,
                "os",
                SimpleNamespace(name="nt", fsync=os.fsync, environ=os.environ),
            ),
            patch.object(backend_module, "_WindowsJob", return_value=containment),
            patch.object(backend_module.subprocess, "Popen", return_value=child),
            patch.object(backend_module.subprocess, "run", side_effect=pid_lookup) as pid_lookup_mock,
            patch.object(backend_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
            patch.object(backend_module.signal, "CTRL_BREAK_EVENT", 1, create=True),
        ):
            result = backend.invoke(invocation)
        return SimpleNamespace(
            result=result,
            pid_lookup=pid_lookup_mock,
            containment=containment,
        )


if __name__ == "__main__":
    unittest.main()
