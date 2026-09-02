from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import mochicode_core.cli as cli


class CliPlatformTests(unittest.TestCase):
    def _non_windows_call(self, argv: list[str]):
        stdout = io.StringIO()
        stderr = io.StringIO()
        guarded_names = (
            "doctor",
            "run_demo",
            "run_project",
            "status_payload",
            "StateStore",
            "LearningStore",
            "MochiController",
            "CodexCliBackend",
            "_git",
            "_read_command",
        )
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli.os, "name", "posix"))
            guarded = {
                name: stack.enter_context(mock.patch.object(cli, name))
                for name in guarded_names
            }
            which_mock = stack.enter_context(mock.patch.object(cli.shutil, "which"))
            run_mock = stack.enter_context(mock.patch.object(cli.subprocess, "run"))
            popen_mock = stack.enter_context(mock.patch.object(cli.subprocess, "Popen"))
            mkdir_mock = stack.enter_context(mock.patch.object(Path, "mkdir"))
            write_mock = stack.enter_context(mock.patch.object(Path, "write_text"))
            read_mock = stack.enter_context(mock.patch.object(Path, "read_text"))
            with redirect_stdout(stdout), redirect_stderr(stderr):
                returncode = cli.run_cli(argv)

        for value in guarded.values():
            value.assert_not_called()
        which_mock.assert_not_called()
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        mkdir_mock.assert_not_called()
        write_mock.assert_not_called()
        read_mock.assert_not_called()
        return returncode, stdout.getvalue(), stderr.getvalue()

    def test_non_windows_doctor_json_is_platform_only_and_side_effect_free(self) -> None:
        returncode, stdout, stderr = self._non_windows_call(["doctor", "--json"])

        self.assertEqual(returncode, 1)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertFalse(payload["ready"])
        self.assertEqual(
            payload["checks"],
            [
                {
                    "name": "platform",
                    "ok": False,
                    "detail": "unsupported platform: release commands require Windows",
                }
            ],
        )

    def test_non_windows_release_commands_refuse_before_dispatch_or_mutation(self) -> None:
        commands = {
            "doctor": ["doctor"],
            "demo": ["demo", "--state-root", "unused-demo"],
            "run": [
                "run",
                "--project",
                "unused-project",
                "--goal-file",
                "unused-goal.txt",
            ],
            "status": ["status", "--run-root", "unused-run"],
            "stop": ["stop", "--run-root", "unused-run"],
            "resume": ["resume", "--run-root", "unused-run"],
            "lessons": ["lessons", "list"],
        }
        for name, argv in commands.items():
            with self.subTest(command=name):
                returncode, stdout, stderr = self._non_windows_call(argv)
                self.assertEqual(returncode, 2)
                self.assertEqual(stdout, "")
                self.assertIn("release commands require Windows", stderr)

    def test_windows_doctor_includes_successful_platform_check(self) -> None:
        with (
            mock.patch.object(cli.os, "name", "nt"),
            mock.patch.object(cli.shutil, "which", return_value=None),
            mock.patch.object(cli, "load_config"),
        ):
            payload = cli.doctor()

        platform = next(
            check for check in payload["checks"] if check["name"] == "platform"
        )
        self.assertTrue(platform["ok"])
        self.assertEqual(platform["detail"], "Windows")


if __name__ == "__main__":
    unittest.main()
