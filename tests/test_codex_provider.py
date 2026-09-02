from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.backend import CodexCliBackend
from mochicode_core.config import load_config
from mochicode_core.models import PacketStatus
from mochicode_core.providers import CodexRoleProvider
from mochicode_core.runner import MochiController


FAKE_CODEX = r'''
from __future__ import annotations
import json
from pathlib import Path
import sys

args = sys.argv[1:]
schema = Path(args[args.index("--output-schema") + 1]).name
output = Path(args[args.index("--output-last-message") + 1])
cwd = Path(args[args.index("--cd") + 1])
prompt = sys.stdin.read()

if schema == "plan.schema.json":
    value = {
        "summary": "one vertical slice",
        "packets": [{
            "id": "vertical",
            "title": "Runnable path",
            "goal": "create app.txt",
            "wave": 1,
            "priority": 1,
            "vertical_slice": True,
            "dependencies": [],
            "acceptance_criteria": ["app.txt contains runnable"],
            "verification_hints": ["run focused check"]
        }]
    }
elif schema == "contract.schema.json":
    checks = cwd / "checks"
    checks.mkdir(exist_ok=True)
    check = checks / "vertical_check.py"
    check.write_text("from pathlib import Path\nassert Path('app.txt').read_text(encoding='utf-8') == 'runnable\\n'\n", encoding="utf-8")
    command = [sys.executable, str(check)]
    value = {
        "packet_id": "vertical",
        "goal": "create app.txt",
        "verification_class": "hard",
        "acceptance_criteria": ["app.txt contains runnable"],
        "baseline_argv": command,
        "final_argvs": [command],
        "expected_failure_codes": [1],
        "protected_patterns": ["checks/**/*.py"],
        "allowed_paths": ["app.txt"],
        "evidence_requirements": ["raw command result"]
    }
elif schema == "implementation.schema.json":
    (cwd / "app.txt").write_text("runnable\n", encoding="utf-8")
    value = {"summary": "created app", "changed_files": ["app.txt"], "commands_run": [], "remaining_assumptions": []}
elif schema == "review.schema.json":
    value = {"verdict": "GREEN", "findings": [], "evidence_summary": "verified"}
elif schema == "final-review.schema.json":
    value = {
        "verdict": "MERGE",
        "criteria": [{"criterion": "app.txt contains runnable", "status": "PASS", "evidence": "app.txt and check"}],
        "remaining_risks": [],
        "merge_recommendation": "human may merge"
    }
else:
    raise SystemExit(3)

output.write_text(json.dumps(value), encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": schema}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1}}), flush=True)
'''


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False, shell=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class CodexProviderTests(unittest.TestCase):
    def test_process_backed_role_chain_reaches_reviewed_integration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            git(source, "init")
            (source / "README.md").write_text("base\n", encoding="utf-8")
            git(source, "add", "README.md")
            git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
            source_head = git(source, "rev-parse", "HEAD")
            fake = root / "fake_codex.py"
            fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
            run_root = root / "run"
            config = load_config(PLUGIN_ROOT / "config" / "default.toml")
            backend = CodexCliBackend((sys.executable, str(fake)), config)
            provider = CodexRoleProvider(backend, run_root=run_root, plugin_root=PLUGIN_ROOT)

            result = MochiController(config, provider).run_new(
                goal="Build the app",
                project=source,
                run_root=run_root,
                run_id="provider",
            )

            self.assertEqual(result.state.status, "complete")
            self.assertEqual(result.state.packet("vertical").status, PacketStatus.ACCEPTED)
            self.assertEqual(result.state.model_calls, 5)
            self.assertEqual(git(source, "rev-parse", "HEAD"), source_head)
            self.assertFalse((source / "app.txt").exists())
            self.assertEqual((result.integration.path / "app.txt").read_text(encoding="utf-8"), "runnable\n")
            rows = [json.loads(line) for line in (run_root / "model-processes.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(row["event"] == "started" for row in rows), 5)
            self.assertEqual(sum(row["event"] == "finished" for row in rows), 5)

    def test_resume_does_not_reuse_completed_contract_without_controller_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            call_root = root / "model-calls" / "005-terra_contract"
            call_root.mkdir(parents=True)
            cached = {
                "packet_id": "vertical",
                "goal": "cached completed contract",
                "execution_mode": "implement",
                "verification_class": "hard",
                "acceptance_criteria": ["app.txt contains runnable"],
                "baseline_argv": ["git", "cat-file", "-e", "HEAD:missing"],
                "final_argvs": [["git", "cat-file", "-e", "HEAD:missing"]],
                "expected_failure_codes": [1],
                "protected_patterns": ["README.md"],
                "allowed_paths": ["app.txt"],
                "evidence_requirements": ["unbound output"],
            }
            (call_root / "result.json").write_text(json.dumps(cached), encoding="utf-8")
            fake = root / "fake_codex.py"
            fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
            config = load_config(PLUGIN_ROOT / "config" / "default.toml")
            provider = CodexRoleProvider(
                CodexCliBackend((sys.executable, str(fake)), config),
                run_root=root,
                plugin_root=PLUGIN_ROOT,
                reuse_existing=True,
            )
            from mochicode_core.models import PacketState

            packet = PacketState(
                "vertical",
                "Runnable path",
                wave=1,
                goal="create app.txt",
                vertical_slice=True,
                acceptance_criteria=("app.txt contains runnable",),
                verification_commands=("check",),
            )

            result = provider.contract(packet, root)

            self.assertNotEqual(result["goal"], cached["goal"])
            self.assertFalse(provider.last_call_reused)
            self.assertEqual(provider.call_index, 6)

    def test_contract_recovery_does_not_reuse_an_unsafe_cached_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            call_root = root / "model-calls" / "005-terra_contract"
            call_root.mkdir(parents=True)
            cached = {
                "packet_id": "vertical",
                "goal": "cached unsafe contract",
                "execution_mode": "implement",
                "verification_class": "hard",
                "acceptance_criteria": ["app.txt contains runnable"],
                "baseline_argv": [sys.executable, "-c", "raise SystemExit(1)"],
                "final_argvs": [[sys.executable, "-c", "raise SystemExit(1)"],],
                "expected_failure_codes": [1],
                "protected_patterns": ["checks/vertical_check.py"],
                "allowed_paths": ["app.txt"],
                "evidence_requirements": ["output"],
            }
            (call_root / "result.json").write_text(json.dumps(cached), encoding="utf-8")
            fake = root / "fake_codex.py"
            fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
            config = load_config(PLUGIN_ROOT / "config" / "default.toml")
            provider = CodexRoleProvider(
                CodexCliBackend((sys.executable, str(fake)), config),
                run_root=root,
                plugin_root=PLUGIN_ROOT,
                reuse_existing=True,
            )
            from mochicode_core.models import PacketState

            packet = PacketState(
                "vertical",
                "Runnable path",
                wave=1,
                goal="create app.txt",
                vertical_slice=True,
                attempts=1,
                acceptance_criteria=("app.txt contains runnable",),
                verification_commands=("check",),
                last_failure=(
                    "Terra contract modified or deleted existing checks: tests/test_existing.py"
                ),
            )

            result = provider.contract(packet, root)

            self.assertNotEqual(result["goal"], cached["goal"])
            self.assertFalse(provider.last_call_reused)
            self.assertEqual(provider.call_index, 6)

    def test_unbound_cached_contract_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            call_root = root / "model-calls" / "005-terra_contract"
            call_root.mkdir(parents=True)
            cached = {
                "packet_id": "vertical",
                "goal": "unbound cached contract",
                "execution_mode": "implement",
                "verification_class": "hard",
                "acceptance_criteria": ["app.txt contains runnable"],
                "baseline_argv": ["git", "cat-file", "-e", "HEAD:missing"],
                "final_argvs": [["git", "cat-file", "-e", "HEAD:missing"]],
                "expected_failure_codes": [1],
                "protected_patterns": ["README.md"],
                "allowed_paths": ["app.txt"],
                "evidence_requirements": ["unbound output"],
            }
            (call_root / "result.json").write_text(json.dumps(cached), encoding="utf-8")
            fake = root / "fake_codex.py"
            fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
            config = load_config(PLUGIN_ROOT / "config" / "default.toml")
            provider = CodexRoleProvider(
                CodexCliBackend((sys.executable, str(fake)), config),
                run_root=root,
                plugin_root=PLUGIN_ROOT,
                reuse_existing=True,
            )
            from mochicode_core.models import PacketState

            packet = PacketState(
                "vertical",
                "Runnable path",
                wave=1,
                goal="create app.txt",
                vertical_slice=True,
                acceptance_criteria=("app.txt contains runnable",),
                verification_commands=("check",),
            )

            result = provider.contract(packet, root)

            self.assertNotEqual(result["goal"], cached["goal"])
            self.assertTrue((root / "checks" / "vertical_check.py").is_file())
            self.assertFalse(provider.last_call_reused)
            self.assertEqual(provider.call_index, 6)


if __name__ == "__main__":
    unittest.main()
