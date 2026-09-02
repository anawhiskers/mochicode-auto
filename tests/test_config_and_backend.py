from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.backend import CodexCliBackend, CodexInvocation
from mochicode_core.config import load_config


class ConfigAndBackendTests(unittest.TestCase):
    def test_default_roles_enforce_the_requested_model_split(self) -> None:
        config = load_config(PLUGIN_ROOT / "config" / "default.toml")

        self.assertEqual(config.roles["sol_plan"].model, "gpt-5.6-sol")
        self.assertEqual(config.roles["sol_plan"].reasoning_effort, "max")
        self.assertEqual(config.roles["sol_plan"].sandbox, "read-only")
        self.assertEqual(config.roles["terra_contract"].model, "gpt-5.6-terra")
        self.assertEqual(config.roles["terra_review"].sandbox, "read-only")
        self.assertEqual(config.roles["luna_execute"].model, "gpt-5.6-luna")
        self.assertEqual(config.roles["luna_execute"].reasoning_effort, "max")
        self.assertEqual(config.roles["luna_execute"].service_tier, "")
        self.assertEqual(config.roles["luna_execute"].sandbox, "workspace-write")
        self.assertEqual(config.roles["sol_final"].model, "gpt-5.6-sol")
        self.assertFalse(config.auto_merge_source_branch)
        self.assertFalse(hasattr(config, "ignore_execpolicy_rules"))
        self.assertEqual(config.max_attempts_per_packet, 2)
        self.assertEqual(config.windows_sandbox, "elevated")

    def test_child_command_is_explicit_bounded_and_non_recursive(self) -> None:
        config = load_config(PLUGIN_ROOT / "config" / "default.toml")
        backend = CodexCliBackend("codex.cmd", config)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schema = root / "schema.json"
            output = root / "output.json"
            events = root / "events.jsonl"
            invocation = CodexInvocation(
                role="luna_execute",
                cwd=root,
                prompt="SECRET GOAL THAT MUST NOT APPEAR IN ARGV",
                output_schema=schema,
                output_file=output,
                event_log=events,
                process_log=root / "processes.jsonl",
            )
            command = backend.build_command(invocation)

        joined = " ".join(command)
        self.assertNotIn("SECRET GOAL", joined)
        self.assertIn("--model", command)
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertNotIn('service_tier="fast"', command)
        self.assertNotIn("features.fast_mode=true", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--json", command)
        self.assertIn("--output-schema", command)
        self.assertIn("--disable", command)
        self.assertIn("multi_agent", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertNotIn("--ignore-rules", command)
        if os.name == "nt":
            self.assertIn('windows.sandbox="elevated"', command)
        self.assertIn("--ask-for-approval", command)
        self.assertIn("never", command)
        self.assertLess(command.index("--ask-for-approval"), command.index("exec"))
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_backend_never_emits_ignore_rules_for_any_role(self) -> None:
        config = load_config(PLUGIN_ROOT / "config" / "default.toml")
        backend = CodexCliBackend("codex.cmd", config)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for role in config.roles:
                with self.subTest(role=role):
                    invocation = CodexInvocation(
                        role=role,
                        cwd=root,
                        prompt="bounded role",
                        output_schema=root / "schema.json",
                        output_file=root / "output.json",
                        event_log=root / "events.jsonl",
                        process_log=root / "processes.jsonl",
                    )
                    command = backend.build_command(invocation)
                    self.assertNotIn("--ignore-rules", command)

    def test_removed_ignore_execpolicy_rules_key_is_rejected(self) -> None:
        source = (PLUGIN_ROOT / "config" / "default.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "invalid.toml"
            path.write_text(
                source.replace(
                    "auto_merge_source_branch = false",
                    "auto_merge_source_branch = false\nignore_execpolicy_rules = false",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "ignore_execpolicy_rules"):
                load_config(path)

    def test_unknown_role_is_refused(self) -> None:
        config = load_config(PLUGIN_ROOT / "config" / "default.toml")
        backend = CodexCliBackend("codex.cmd", config)

        with self.assertRaisesRegex(ValueError, "unknown role"):
            backend.role_config("planner-who-also-codes")


if __name__ == "__main__":
    unittest.main()
