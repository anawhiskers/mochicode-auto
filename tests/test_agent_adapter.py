from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "agent_adapter.py"


def run_adapter(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


class AgentAdapterTests(unittest.TestCase):
    def test_render_is_model_neutral_and_contains_safe_handoff_rule(self) -> None:
        result = run_adapter("render", "--agent", "zai")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Treat model names as replaceable provider details", result.stdout)
        self.assertIn("Do not send child results to a hardcoded global chat", result.stdout)
        self.assertNotIn("gpt-5.6", result.stdout)

    def test_audit_reads_only_safe_claude_choice_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            claude = home / ".claude"
            claude.mkdir()
            (claude / "settings.json").write_text(
                json.dumps({"model": "known", "effortLevel": "high", "env": {"SECRET": "nope"}}),
                encoding="utf-8",
            )
            result = run_adapter("audit", "--agent", "claude", "--home", str(home))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["selected_settings"], {"effortLevel": "high", "model": "known"})
        self.assertNotIn("SECRET", result.stdout)
        self.assertNotIn(str(home), result.stdout)
        self.assertEqual(report["workflow_template"], "<plugin-root>/portable/templates/agent-adapters/CORE-WORKFLOW.md")
        self.assertEqual(report["target"], "<user-home>/.claude/CLAUDE.md")

    def test_apply_requires_confirmation_and_preserves_existing_guidance_in_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            target = home / ".claude" / "CLAUDE.md"
            target.parent.mkdir()
            target.write_text("Existing guidance.\n", encoding="utf-8")
            blocked = run_adapter("apply", "--agent", "claude", "--home", str(home))
            self.assertNotEqual(blocked.returncode, 0)
            local_data = home / "local-data"
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_data)
            applied = run_adapter(
                "apply", "--agent", "claude", "--home", str(home), "--confirm", env=environment
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            payload = json.loads(applied.stdout)
            self.assertEqual(Path(payload["backup"]).read_text(encoding="utf-8"), "Existing guidance.\n")
            self.assertTrue(Path(payload["backup"]).is_relative_to(local_data))
            self.assertFalse(Path(payload["backup"]).is_relative_to(target.parent))
            merged = target.read_text(encoding="utf-8")
            self.assertIn("Existing guidance.", merged)
            self.assertIn("ANA-ADAPTIVE-WORKFLOW:BEGIN", merged)
            repeated = run_adapter(
                "apply", "--agent", "claude", "--home", str(home), "--confirm", env=environment
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(target.read_text(encoding="utf-8").count("ANA-ADAPTIVE-WORKFLOW:BEGIN"), 1)

    def test_packaged_layout_resolves_templates_outside_plugin_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "package"
            shutil.copytree(PLUGIN_ROOT / "scripts", package / "plugin" / "scripts")
            shutil.copytree(
                PLUGIN_ROOT / "portable" / "templates",
                package / "portable" / "templates",
            )
            result = subprocess.run(
                [sys.executable, "-B", str(package / "plugin" / "scripts" / "agent_adapter.py"), "render", "--agent", "claude"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ana Adaptive Agent Workflow Core", result.stdout)

    def test_generic_adapter_applies_to_explicit_markdown_target_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "OTHER-AGENT.md"
            target.write_text("Provider-specific guidance.\n", encoding="utf-8")
            applied = run_adapter(
                "apply",
                "--agent",
                "generic",
                "--target",
                str(target),
                "--confirm",
            )
            refused = run_adapter(
                "apply",
                "--agent",
                "generic",
                "--target",
                str(root / "settings.json"),
                "--confirm",
            )
            merged = target.read_text(encoding="utf-8")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("Provider-specific guidance.", merged)
        self.assertIn("Generic coding-agent adapter", merged)
        self.assertNotEqual(refused.returncode, 0)


if __name__ == "__main__":
    unittest.main()
