from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import inspect
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import mochicode_core.verification as verification_module
from mochicode_core.verification import (
    BaselineVerdict,
    classify_baseline,
    final_verification_passed,
    run_command,
)


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
    return (
        Path("/proc/self/task") / str(os.getpid()) / "children"
    ).is_file()


class VerificationTests(unittest.TestCase):
    def test_real_failure_is_a_valid_red_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            argv = (sys.executable, "-c", "raise SystemExit(1)")
            result = run_command(
                argv,
                cwd=Path(raw),
                timeout_seconds=5,
            )

        self.assertEqual(result.argv, argv)
        self.assertEqual(classify_baseline(result), BaselineVerdict.VALID_RED)

    def test_green_baseline_is_already_satisfied_not_progress(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = run_command(
                (sys.executable, "-c", "raise SystemExit(0)"),
                cwd=Path(raw),
                timeout_seconds=5,
            )

        self.assertEqual(classify_baseline(result), BaselineVerdict.ALREADY_SATISFIED)

    def test_usage_and_empty_collection_codes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            usage = run_command(
                (sys.executable, "-c", "raise SystemExit(4)"),
                cwd=Path(raw),
                timeout_seconds=5,
            )
            empty = run_command(
                (sys.executable, "-c", "raise SystemExit(5)"),
                cwd=Path(raw),
                timeout_seconds=5,
            )

        self.assertEqual(classify_baseline(usage), BaselineVerdict.REFUSED)
        self.assertEqual(classify_baseline(empty), BaselineVerdict.REFUSED)

    def test_timeout_is_refused_and_never_final_green(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = run_command(
                (sys.executable, "-c", "import time; time.sleep(2)"),
                cwd=Path(raw),
                timeout_seconds=0.05,
            )

        self.assertTrue(result.timed_out)
        self.assertEqual(classify_baseline(result), BaselineVerdict.REFUSED)
        self.assertFalse(final_verification_passed(result))

    @unittest.skipUnless(os.name == "nt", "requires native Windows Job containment")
    def test_windows_verifier_normal_return_reaps_immediate_child(self) -> None:
        script = (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True); "
            "print(p.pid,flush=True)"
        )
        with tempfile.TemporaryDirectory() as raw:
            child_pid: int | None = None
            try:
                result = run_command(
                    (sys.executable, "-c", script),
                    cwd=Path(raw),
                    timeout_seconds=5,
                )
                child_pid = self._pid_from_stdout(result.stdout)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(result.timed_out)
                self.assertFalse(self._pid_is_alive(child_pid))
            finally:
                if child_pid is not None:
                    self._terminate_exact_pid_for_test(child_pid)

    @unittest.skipUnless(os.name == "nt", "requires native Windows Job containment")
    def test_windows_verifier_timeout_reaps_immediate_child(self) -> None:
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True); "
            "print(p.pid,flush=True); time.sleep(60)"
        )
        with tempfile.TemporaryDirectory() as raw:
            child_pid: int | None = None
            try:
                result = run_command(
                    (sys.executable, "-c", script),
                    cwd=Path(raw),
                    timeout_seconds=10,
                )
                child_pid = self._pid_from_stdout(result.stdout)

                self.assertTrue(result.timed_out)
                self.assertEqual(result.returncode, 124)
                self.assertFalse(self._pid_is_alive(child_pid))
            finally:
                if child_pid is not None:
                    self._terminate_exact_pid_for_test(child_pid)

    @unittest.skipUnless(
        _linux_subreaper_runtime_supported(),
        "requires Linux subreaper, proc child list, and pidfd signaling",
    )
    def _experimental_linux_verifier_reaps_setsid_child_on_normal_return(self) -> None:
        script = (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True,start_new_session=True); "
            "print(p.pid,flush=True)"
        )
        child_pid: int | None = None
        try:
            with tempfile.TemporaryDirectory() as raw:
                result = run_command(
                    (sys.executable, "-c", script),
                    cwd=Path(raw),
                    timeout_seconds=5,
                )
                child_pid = self._pid_from_stdout(result.stdout)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self._pid_is_alive(child_pid))
        finally:
            if child_pid is not None:
                self._terminate_exact_pid_for_test(child_pid)

    @unittest.skipUnless(
        _linux_subreaper_runtime_supported(),
        "requires Linux subreaper, proc child list, and pidfd signaling",
    )
    def _experimental_linux_verifier_timeout_reaps_setsid_child(self) -> None:
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True,start_new_session=True); "
            "print(p.pid,flush=True); time.sleep(60)"
        )
        child_pid: int | None = None
        try:
            with tempfile.TemporaryDirectory() as raw:
                result = run_command(
                    (sys.executable, "-c", script),
                    cwd=Path(raw),
                    timeout_seconds=3,
                )
                child_pid = self._pid_from_stdout(result.stdout)
                self.assertTrue(result.timed_out)
                self.assertEqual(result.returncode, 124)
                self.assertFalse(self._pid_is_alive(child_pid))
        finally:
            if child_pid is not None:
                self._terminate_exact_pid_for_test(child_pid)

    def test_non_windows_release_policy_refuses_verifier_before_popen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch.object(
                    verification_module,
                    "os",
                    SimpleNamespace(name="posix"),
                ),
                patch.object(
                    verification_module,
                    "_require_posix_verifier_support",
                    side_effect=AssertionError("experimental supervisor selected"),
                ) as supervisor_selector,
                patch.object(verification_module.subprocess, "Popen") as popen_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "Windows-only release"):
                    run_command(
                        (sys.executable, "-c", "raise SystemExit(0)"),
                        cwd=Path(raw),
                        timeout_seconds=5,
                    )

            supervisor_selector.assert_not_called()
            popen_mock.assert_not_called()

    def test_linux_release_policy_refuses_even_with_experimental_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch.object(
                    verification_module,
                    "os",
                    SimpleNamespace(name="posix"),
                ),
                patch.object(
                    verification_module,
                    "sys",
                    SimpleNamespace(platform="linux", executable=sys.executable),
                ),
                patch.object(
                    verification_module,
                    "_require_posix_verifier_support",
                    side_effect=AssertionError("experimental supervisor selected"),
                ) as supervisor_selector,
                patch.object(verification_module.subprocess, "Popen") as popen_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "Windows-only release"):
                    run_command(
                        (sys.executable, "-c", "raise SystemExit(0)"),
                        cwd=Path(raw),
                        timeout_seconds=5,
                    )

            supervisor_selector.assert_not_called()
            popen_mock.assert_not_called()

    def _experimental_linux_verifier_readiness_error_is_fail_closed(self) -> None:
        process = SimpleNamespace(pid=8201)
        with patch.object(
            verification_module,
            "_read_supervisor_message",
            return_value={
                "event": "error",
                "message": "subreaper verification failed",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "readiness failed"):
                verification_module._await_supervisor_ready(
                    process,
                    io.StringIO(),
                )

    def _experimental_linux_verifier_cleanup_error_is_fail_closed(self) -> None:
        with patch.object(
            verification_module,
            "_read_supervisor_message",
            return_value={
                "event": "error",
                "message": "escaped descendant remained",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                verification_module._supervisor_returncode(
                    io.StringIO(),
                    125,
                )

    def test_only_a_real_zero_exit_is_final_green(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = run_command(
                (sys.executable, "-c", "print('observable proof')"),
                cwd=Path(raw),
                timeout_seconds=5,
            )

        self.assertTrue(final_verification_passed(result))
        self.assertIn("observable proof", result.stdout)

    def test_python_verification_does_not_litter_protected_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "sample_module.py").write_text("VALUE = 1\n", encoding="utf-8")
            result = run_command(
                (sys.executable, "-c", "import sample_module; assert sample_module.VALUE == 1"),
                cwd=root,
                timeout_seconds=5,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "__pycache__").exists())

    def test_relative_python_is_canonical_only_in_sandbox_launch_argv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cwd = root / "workspace"
            cwd.mkdir()
            sandbox_home_parent = root / "local-app-data"
            preserved_path = str(root / "preserved-path")
            canonical_python = str(Path(sys.executable).resolve())
            codex = str(root / "tools" / "codex.cmd")
            contract_argv = ("python", "-B", "-m", "unittest", "tests.test_target")
            launches: list[tuple[list[str], dict[str, str]]] = []

            def record_launch(
                command: list[str],
                *,
                cwd: Path,
                environment: dict[str, str],
                timeout_seconds: float,
            ) -> tuple[int, str, str, bool]:
                launches.append((command, environment))
                return 1, "", "expected red", False

            def resolve_tool(name: str) -> str | None:
                if name in {"codex.cmd", "codex"}:
                    return codex
                if name == "python":
                    return str(sys.executable)
                return None

            with (
                patch.dict(
                    verification_module.os.environ,
                    {
                        "LOCALAPPDATA": str(sandbox_home_parent),
                        "PATH": preserved_path,
                    },
                    clear=True,
                ),
                patch.object(
                    verification_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
                ),
                patch.object(
                    verification_module.shutil,
                    "which",
                    side_effect=resolve_tool,
                ),
                patch.object(
                    verification_module,
                    "_run_windows_contained",
                    side_effect=record_launch,
                ),
            ):
                result = run_command(
                    contract_argv,
                    cwd=cwd,
                    timeout_seconds=5,
                )

        self.assertEqual(result.argv, contract_argv)
        self.assertEqual(len(launches), 1)
        launch_argv, launch_environment = launches[0]
        self.assertEqual(launch_argv[:3], [codex, "sandbox", "-p"])
        profile_name = launch_argv[3]
        self.assertTrue(profile_name.startswith("mochicode-verifier-"))
        self.assertTrue(profile_name.isascii())
        self.assertTrue(
            all(character.isalnum() or character in "-_" for character in profile_name)
        )
        self.assertEqual(
            launch_argv,
            [
                codex,
                "sandbox",
                "-p",
                profile_name,
                "-c",
                'windows.sandbox="elevated"',
                "-P",
                "mochicode-verifier",
                "-C",
                str(cwd.resolve()),
                "--sandbox-state-disable-network",
                "--sandbox-state-readable-root",
                str(Path(canonical_python).parent),
                "--",
                canonical_python,
                *contract_argv[1:],
            ],
        )
        self.assertNotIn("/c", launch_argv)
        self.assertEqual(launch_environment["PATH"], preserved_path)
        self.assertNotIn(str(Path(canonical_python).parent), launch_environment["PATH"])

    def test_absolute_python_remains_canonical_in_sandbox_launch_argv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cwd = root / "workspace"
            cwd.mkdir()
            canonical_python = str(Path(sys.executable).resolve())
            contract_argv = (str(Path(sys.executable)), "-B", "-m", "unittest")
            launches: list[list[str]] = []

            def record_launch(
                command: list[str],
                *,
                cwd: Path,
                environment: dict[str, str],
                timeout_seconds: float,
            ) -> tuple[int, str, str, bool]:
                launches.append(command)
                return 0, "", "", False

            with (
                patch.dict(
                    verification_module.os.environ,
                    {"LOCALAPPDATA": str(root / "local-app-data"), "PATH": "fixed"},
                    clear=True,
                ),
                patch.object(
                    verification_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
                ),
                patch.object(
                    verification_module.shutil,
                    "which",
                    return_value=str(root / "tools" / "codex.cmd"),
                ),
                patch.object(
                    verification_module,
                    "_run_windows_contained",
                    side_effect=record_launch,
                ),
            ):
                result = run_command(
                    contract_argv,
                    cwd=cwd,
                    timeout_seconds=5,
                )

        self.assertEqual(result.argv, contract_argv)
        self.assertEqual(launches[0][-len(contract_argv) :], [canonical_python, *contract_argv[1:]])

    def test_sandbox_profile_and_temp_are_unique_and_removed_after_each_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cwd = root / "workspace"
            cwd.mkdir()
            sandbox_home = root / "local-app-data" / "MochiCode" / "verifier-sandbox"
            sandbox_secrets = sandbox_home / ".sandbox-secrets"
            sandbox_secrets.mkdir(parents=True)
            secret_sentinel = sandbox_secrets / "sentinel"
            secret_sentinel.write_text("codex-owned\n", encoding="utf-8")
            base_config = sandbox_home / "config.toml"
            stale_write_path = str((root / "stale-write-root").resolve())
            base_config.write_text(
                '[permissions.stale.filesystem]\n'
                f'{json.dumps(stale_write_path)} = "write"\n',
                encoding="utf-8",
            )
            expected_base_config = (
                "# MochiCode verifier base config intentionally contains no permissions.\n"
            )
            canonical_python = str(Path(sys.executable).resolve())
            preserved_path = str(root / "preserved-path")
            codex = str(root / "tools" / "codex.cmd")
            contract_argv = ("python", "-B", "-m", "unittest", "tests.test_target")
            launches: list[dict[str, object]] = []

            def record_launch(
                command: list[str],
                *,
                cwd: Path,
                environment: dict[str, str],
                timeout_seconds: float,
            ) -> tuple[int, str, str, bool]:
                temp_root = Path(environment["TMP"])
                profile_name = (
                    command[command.index("-p") + 1] if "-p" in command else None
                )
                config_path = (
                    sandbox_home / f"{profile_name}.config.toml"
                    if profile_name is not None
                    else Path(environment["CODEX_HOME"]) / "config.toml"
                )
                launches.append(
                    {
                        "command": command,
                        "environment": environment.copy(),
                        "config": tomllib.loads(config_path.read_text(encoding="utf-8")),
                        "profile_name": profile_name,
                        "config_path": config_path,
                        "temp_root": temp_root,
                        "home_exists": sandbox_home.is_dir(),
                        "secrets_exist": sandbox_secrets.is_dir(),
                        "config_exists": config_path.is_file(),
                        "temp_exists": temp_root.is_dir(),
                        "base_config": base_config.read_text(encoding="utf-8"),
                    }
                )
                return (0, "", "", False) if len(launches) == 1 else (1, "", "expected red", False)

            def resolve_tool(name: str) -> str | None:
                if name in {"codex.cmd", "codex"}:
                    return codex
                if name == "python":
                    return str(sys.executable)
                return None

            with (
                patch.dict(
                    verification_module.os.environ,
                    {"LOCALAPPDATA": str(root / "local-app-data"), "PATH": preserved_path},
                    clear=True,
                ),
                patch.object(
                    verification_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
                ),
                patch.object(
                    verification_module.shutil,
                    "which",
                    side_effect=resolve_tool,
                ),
                patch.object(
                    verification_module,
                    "_run_windows_contained",
                    side_effect=record_launch,
                ),
            ):
                success = run_command(contract_argv, cwd=cwd, timeout_seconds=5)
                self.assertFalse(Path(launches[0]["config_path"]).exists())
                self.assertFalse(Path(launches[0]["temp_root"]).exists())
                self.assertTrue(sandbox_home.is_dir())
                self.assertTrue(sandbox_secrets.is_dir())
                self.assertEqual(base_config.read_text(encoding="utf-8"), expected_base_config)
                failure = run_command(contract_argv, cwd=cwd, timeout_seconds=5)
                self.assertFalse(Path(launches[1]["config_path"]).exists())
                self.assertFalse(Path(launches[1]["temp_root"]).exists())
                self.assertEqual(base_config.read_text(encoding="utf-8"), expected_base_config)

            self.assertEqual(success.argv, contract_argv)
            self.assertEqual(failure.argv, contract_argv)
            self.assertEqual(len(launches), 2)
            profile_names: list[str] = []
            config_paths: list[Path] = []
            temp_roots: list[Path] = []
            for launch in launches:
                launch_argv = launch["command"]
                environment = launch["environment"]
                config = launch["config"]
                profile_name = launch["profile_name"]
                config_path = Path(launch["config_path"])
                temp_root = Path(launch["temp_root"])
                self.assertIsInstance(profile_name, str)
                assert isinstance(profile_name, str)
                profile_names.append(profile_name)
                config_paths.append(config_path)
                temp_roots.append(temp_root)
                self.assertEqual(config_path, sandbox_home / f"{profile_name}.config.toml")
                self.assertEqual(Path(environment["CODEX_HOME"]), sandbox_home)
                self.assertEqual(environment["TMP"], str(temp_root))
                self.assertEqual(environment["TEMP"], str(temp_root))
                self.assertEqual(environment["TMPDIR"], str(temp_root))
                self.assertEqual(environment["PATH"], preserved_path)
                self.assertEqual(temp_root.parent, sandbox_home)
                self.assertTrue(launch["home_exists"])
                self.assertTrue(launch["secrets_exist"])
                self.assertTrue(launch["config_exists"])
                self.assertTrue(launch["temp_exists"])
                self.assertEqual(launch["base_config"], expected_base_config)
                self.assertEqual(tomllib.loads(launch["base_config"]), {})
                self.assertNotIn(stale_write_path, launch["base_config"])
                self.assertEqual(launch_argv[:4], [codex, "sandbox", "-p", profile_name])
                self.assertEqual(
                    launch_argv[launch_argv.index("-P") : launch_argv.index("-P") + 2],
                    ["-P", "mochicode-verifier"],
                )
                self.assertEqual(
                    launch_argv[launch_argv.index("--") + 1 :],
                    [canonical_python, *contract_argv[1:]],
                )

                permissions = config["permissions"]["mochicode-verifier"]
                filesystem = permissions["filesystem"]
                self.assertEqual(filesystem[":minimal"], "read")
                self.assertEqual(filesystem[":workspace_roots"], {".": "read"})
                self.assertEqual(
                    [path for path, access in filesystem.items() if access == "write"],
                    [str(temp_root)],
                )
                self.assertEqual(permissions["network"], {"enabled": False})

            self.assertNotEqual(*profile_names)
            self.assertNotEqual(*config_paths)
            self.assertNotEqual(*temp_roots)
            for config_path, temp_root in zip(config_paths, temp_roots, strict=True):
                self.assertFalse(config_path.exists())
                self.assertFalse(temp_root.exists())
            self.assertTrue(sandbox_home.is_dir())
            self.assertTrue(sandbox_secrets.is_dir())
            self.assertEqual(base_config.read_text(encoding="utf-8"), expected_base_config)
            self.assertEqual(secret_sentinel.read_text(encoding="utf-8"), "codex-owned\n")

    def test_concurrent_verifier_invocations_have_isolated_configuration_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cwd = root / "workspace"
            cwd.mkdir()
            sandbox_home = root / "local-app-data" / "MochiCode" / "verifier-sandbox"
            sandbox_secrets = sandbox_home / ".sandbox-secrets"
            sandbox_secrets.mkdir(parents=True)
            secret_sentinel = sandbox_secrets / "sentinel"
            secret_sentinel.write_text("codex-owned\n", encoding="utf-8")
            base_config = sandbox_home / "config.toml"
            stale_write_path = str((root / "stale-write-root").resolve())
            base_config.write_text(
                '[permissions.stale.filesystem]\n'
                f'{json.dumps(stale_write_path)} = "write"\n',
                encoding="utf-8",
            )
            expected_base_config = (
                "# MochiCode verifier base config intentionally contains no permissions.\n"
            )
            canonical_python = str(Path(sys.executable).resolve())
            preserved_path = str(root / "preserved-path")
            codex = str(root / "tools" / "codex.cmd")
            contract_argvs = {
                "first": ("python", "-B", "-c", "raise SystemExit(1)", "first"),
                "second": ("python", "-B", "-c", "raise SystemExit(1)", "second"),
            }
            launches: dict[str, dict[str, object]] = {}
            launch_lock = threading.Lock()
            launch_barrier = threading.Barrier(2, timeout=20)
            launches_ready = threading.Event()
            release_second = threading.Event()

            def record_launch(
                command: list[str],
                *,
                cwd: Path,
                environment: dict[str, str],
                timeout_seconds: float,
            ) -> tuple[int, str, str, bool]:
                invocation = command[-1]
                temp_root = Path(environment["TMP"])
                profile_name = (
                    command[command.index("-p") + 1] if "-p" in command else None
                )
                config_path = (
                    sandbox_home / f"{profile_name}.config.toml"
                    if profile_name is not None
                    else Path(environment["CODEX_HOME"]) / "config.toml"
                )
                try:
                    launch_barrier.wait()
                except threading.BrokenBarrierError as error:
                    raise AssertionError("concurrent verifier launches did not synchronize") from error
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
                with launch_lock:
                    launches[invocation] = {
                        "command": command,
                        "environment": environment.copy(),
                        "config": config,
                        "profile_name": profile_name,
                        "config_path": config_path,
                        "temp_root": temp_root,
                        "home_exists": sandbox_home.is_dir(),
                        "secrets_exist": sandbox_secrets.is_dir(),
                        "config_exists": config_path.is_file(),
                        "temp_exists": temp_root.is_dir(),
                        "base_config": base_config.read_text(encoding="utf-8"),
                    }
                    if len(launches) == 2:
                        launches_ready.set()
                if invocation == "second" and not release_second.wait(timeout=20):
                    raise AssertionError("second concurrent verifier was not released")
                return 1, "", f"expected red: {invocation}", False

            def resolve_tool(name: str) -> str | None:
                if name in {"codex.cmd", "codex"}:
                    return codex
                if name == "python":
                    return str(sys.executable)
                return None

            with (
                patch.dict(
                    verification_module.os.environ,
                    {"LOCALAPPDATA": str(root / "local-app-data"), "PATH": preserved_path},
                    clear=True,
                ),
                patch.object(
                    verification_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
                ),
                patch.object(
                    verification_module.shutil,
                    "which",
                    side_effect=resolve_tool,
                ),
                patch.object(
                    verification_module,
                    "_run_windows_contained",
                    side_effect=record_launch,
                ),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {
                        invocation: executor.submit(
                            run_command,
                            contract_argv,
                            cwd=cwd,
                            timeout_seconds=5,
                        )
                        for invocation, contract_argv in contract_argvs.items()
                    }
                    results: dict[str, object] = {}
                    try:
                        self.assertTrue(
                            launches_ready.wait(timeout=20),
                            "concurrent verifier launches were not both ready",
                        )
                        results["first"] = futures["first"].result(timeout=30)
                        self.assertFalse(Path(launches["first"]["config_path"]).exists())
                        self.assertFalse(Path(launches["first"]["temp_root"]).exists())
                        self.assertTrue(Path(launches["second"]["config_path"]).is_file())
                        self.assertTrue(Path(launches["second"]["temp_root"]).is_dir())
                        self.assertTrue(sandbox_home.is_dir())
                        self.assertTrue(sandbox_secrets.is_dir())
                        self.assertEqual(
                            base_config.read_text(encoding="utf-8"),
                            expected_base_config,
                        )
                    finally:
                        release_second.set()
                    results["second"] = futures["second"].result(timeout=30)

            self.assertEqual(set(launches), set(contract_argvs))
            self.assertEqual(set(results), set(contract_argvs))
            for invocation in contract_argvs:
                config = launches[invocation]["config"]
                temp_root = Path(launches[invocation]["temp_root"])
                permissions = config["permissions"]["mochicode-verifier"]
                filesystem = permissions["filesystem"]
                self.assertEqual(filesystem[":minimal"], "read")
                self.assertEqual(filesystem[":workspace_roots"], {".": "read"})
                self.assertEqual(
                    [path for path, access in filesystem.items() if access == "write"],
                    [str(temp_root)],
                )
                self.assertEqual(permissions["network"], {"enabled": False})

            profile_names: dict[str, str] = {}
            config_paths: dict[str, Path] = {}
            temp_roots: dict[str, Path] = {}
            for invocation, contract_argv in contract_argvs.items():
                result = results[invocation]
                self.assertIsInstance(result, verification_module.CommandResult)
                self.assertEqual(result.argv, contract_argv)
                launch = launches[invocation]
                launch_argv = launch["command"]
                environment = launch["environment"]
                profile_name = launch["profile_name"]
                config_path = Path(launch["config_path"])
                temp_root = Path(launch["temp_root"])
                self.assertIsInstance(profile_name, str)
                assert isinstance(profile_name, str)
                profile_names[invocation] = profile_name
                config_paths[invocation] = config_path
                temp_roots[invocation] = temp_root
                self.assertEqual(config_path, sandbox_home / f"{profile_name}.config.toml")
                self.assertEqual(Path(environment["CODEX_HOME"]), sandbox_home)
                self.assertEqual(environment["TMP"], str(temp_root))
                self.assertEqual(environment["TEMP"], str(temp_root))
                self.assertEqual(environment["TMPDIR"], str(temp_root))
                self.assertEqual(temp_root.parent, sandbox_home)
                self.assertEqual(environment["PATH"], preserved_path)
                self.assertTrue(launch["home_exists"])
                self.assertTrue(launch["secrets_exist"])
                self.assertTrue(launch["config_exists"])
                self.assertTrue(launch["temp_exists"])
                self.assertEqual(launch["base_config"], expected_base_config)
                self.assertEqual(tomllib.loads(launch["base_config"]), {})
                self.assertNotIn(stale_write_path, launch["base_config"])
                self.assertEqual(launch_argv[:4], [codex, "sandbox", "-p", profile_name])
                self.assertEqual(
                    launch_argv[launch_argv.index("-P") : launch_argv.index("-P") + 2],
                    ["-P", "mochicode-verifier"],
                )
                self.assertEqual(
                    launch_argv[launch_argv.index("--") + 1 :],
                    [canonical_python, *contract_argv[1:]],
                )

            self.assertNotEqual(*profile_names.values())
            self.assertNotEqual(*config_paths.values())
            self.assertNotEqual(*temp_roots.values())
            for invocation, config_path in config_paths.items():
                other = next(name for name in contract_argvs if name != invocation)
                filesystem = launches[invocation]["config"]["permissions"]["mochicode-verifier"]["filesystem"]
                self.assertNotIn(str(temp_roots[other]), filesystem)
                self.assertFalse(config_path.exists())
                self.assertFalse(temp_roots[invocation].exists())
            self.assertTrue(sandbox_home.is_dir())
            self.assertTrue(sandbox_secrets.is_dir())
            self.assertEqual(base_config.read_text(encoding="utf-8"), expected_base_config)
            self.assertEqual(secret_sentinel.read_text(encoding="utf-8"), "codex-owned\n")

    def test_verifier_sandbox_denies_outside_files_and_network(self) -> None:
        task_work = PLUGIN_ROOT.parents[1] / "work"
        task_work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=task_work) as outside_raw:
            outside = Path(outside_raw) / "sentinel.txt"
            outside.write_text("must remain private\n", encoding="utf-8")
            with tempfile.TemporaryDirectory() as workspace_raw:
                workspace = Path(workspace_raw)
                workspace_sentinel = workspace / "workspace-sentinel.txt"
                workspace_sentinel.write_text("must remain unchanged\n", encoding="utf-8")
                check = workspace / "sandbox_check.py"
                check.write_text(
                    "from pathlib import Path\n"
                    "import socket\n"
                    "import sys\n"
                    "import tempfile\n"
                    "workspace = Path(sys.argv[1])\n"
                    "outside = Path(sys.argv[2])\n"
                    "with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as handle:\n"
                    "    handle.write('private sandbox temp')\n"
                    "    handle.flush()\n"
                    "try:\n"
                    "    workspace.write_text('changed', encoding='utf-8')\n"
                    "except (OSError, PermissionError):\n"
                    "    pass\n"
                    "else:\n"
                    "    raise SystemExit(9)\n"
                    "try:\n"
                    "    outside.read_bytes()\n"
                    "except (OSError, PermissionError):\n"
                    "    pass\n"
                    "else:\n"
                    "    raise SystemExit(10)\n"
                    "try:\n"
                    "    outside.write_text('changed', encoding='utf-8')\n"
                    "except (OSError, PermissionError):\n"
                    "    pass\n"
                    "else:\n"
                    "    raise SystemExit(11)\n"
                    "try:\n"
                    "    socket.create_connection(('1.1.1.1', 53), timeout=0.2)\n"
                    "except OSError:\n"
                    "    pass\n"
                    "else:\n"
                    "    raise SystemExit(12)\n",
                    encoding="utf-8",
                )
                result = run_command(
                    (sys.executable, str(check), str(workspace_sentinel), str(outside)),
                    cwd=workspace,
                    timeout_seconds=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    workspace_sentinel.read_text(encoding="utf-8"),
                    "must remain unchanged\n",
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "must remain private\n")

    @staticmethod
    def _pid_from_stdout(stdout: str) -> int:
        for line in reversed(stdout.splitlines()):
            value = line.strip()
            if value.isdigit():
                return int(value)
        raise AssertionError(f"verifier stdout omitted child PID: {stdout!r}")

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


if __name__ == "__main__":
    unittest.main()
