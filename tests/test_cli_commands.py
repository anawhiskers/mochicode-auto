from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CLI = PLUGIN_ROOT / "scripts" / "mochicode.py"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.cli import run_cli
from mochicode_core.evidence import EvidenceLedger
from mochicode_core.runner import MochiController


def invoke(
    *args: str,
    timeout_seconds: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=timeout_seconds,
    )


class CliCommandTests(unittest.TestCase):
    def test_demo_status_stop_and_resume_are_reachable_from_normal_commands(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "demo"
            demo = invoke(
                "demo",
                "--state-root",
                str(base),
                "--json",
                timeout_seconds=180,
            )
            self.assertEqual(demo.returncode, 0, demo.stderr)
            payload = json.loads(demo.stdout)
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(
                {
                    "backend": payload["backend"],
                    "fresh": payload["fresh"],
                    "cost_usd": payload["cost_usd"],
                    "ledger_ok": payload["ledger_ok"],
                    "accepted": payload["accepted"],
                    "total": payload["total"],
                    "model_calls": payload["model_calls"],
                },
                {
                    "backend": "stub",
                    "fresh": True,
                    "cost_usd": 0,
                    "ledger_ok": True,
                    "accepted": 3,
                    "total": 3,
                    "model_calls": 0,
                },
            )
            run_root = payload["run_root"]

            status = invoke("status", "--run-root", run_root, "--verbose", "--json")
            self.assertEqual(status.returncode, 0, status.stderr)
            status_payload = json.loads(status.stdout)
            self.assertTrue(status_payload["ledger_ok"])
            self.assertEqual(status_payload["accepted"], 3)

            stop = invoke("stop", "--run-root", run_root)
            self.assertEqual(stop.returncode, 0, stop.stderr)
            stopped = json.loads(
                invoke("status", "--run-root", run_root, "--json").stdout
            )
            self.assertTrue(stopped["stop_requested"])

            run_path = Path(run_root)
            state_before = (run_path / "state.json").read_bytes()
            stop_before = (run_path / "STOP").read_bytes()
            lease = run_path / ".run.lease.json"
            lease.write_text(
                json.dumps({"pid": os.getpid(), "token": "live-cli-test"}) + "\n",
                encoding="utf-8",
            )
            blocked = invoke(
                "resume",
                "--run-root",
                run_root,
                "--continue-run",
                "--backend",
                "stub",
                "--json",
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertEqual((run_path / "state.json").read_bytes(), state_before)
            self.assertEqual((run_path / "STOP").read_bytes(), stop_before)
            lease.unlink()

            resume = invoke("resume", "--run-root", run_root)
            self.assertEqual(resume.returncode, 0, resume.stderr)
            resumed = json.loads(
                invoke("status", "--run-root", run_root, "--json").stdout
            )
            self.assertFalse(resumed["stop_requested"])

    def test_demo_refuses_a_tampered_completed_ledger_without_success_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "tampered-demo"
            original_run_new = MochiController.run_new

            def tamper_completed_ledger(controller, *args, **kwargs):
                result = original_run_new(controller, *args, **kwargs)
                ledger_path = result.run_root / "evidence.jsonl"
                lines = ledger_path.read_text(encoding="utf-8").splitlines()
                first = json.loads(lines[0])
                first["event"] = "tampered_demo_event"
                lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
                ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return result

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    MochiController,
                    "run_new",
                    new=tamper_completed_ledger,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                returncode = run_cli(
                    [
                        "demo",
                        "--state-root",
                        str(base),
                        "--json",
                    ]
                )

            self.assertEqual(returncode, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("demo evidence ledger is invalid", stderr.getvalue())

    def test_demo_refuses_a_nonzero_actual_model_call_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "model-call-demo"
            fake_result = SimpleNamespace(
                state=SimpleNamespace(status="complete"),
                run_root=base / "run",
                integration=SimpleNamespace(branch="demo-integration"),
                final_review={"verdict": "MERGE"},
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    MochiController,
                    "run_new",
                    return_value=fake_result,
                ),
                mock.patch.object(
                    EvidenceLedger,
                    "verify",
                    return_value=(True, "verified"),
                ),
                mock.patch.object(
                    MochiController,
                    "_finished_model_call_count",
                    return_value=1,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                returncode = run_cli(
                    [
                        "demo",
                        "--state-root",
                        str(base),
                        "--json",
                    ]
                )

            self.assertEqual(returncode, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("zero-cost demo used 1 model calls", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
