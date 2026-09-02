from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import adaptive_config  # noqa: E402


def capability_report(
    *,
    agent_defaults: bool = False,
    max_context_window: int = 872000,
    effective_context_window: int = 272000,
    multi_agent: bool = True,
    fast_mode: bool = True,
) -> dict[str, object]:
    return {
        "executable": "codex.exe",
        "version": "0.144.0",
        "available": True,
        "catalog_available": True,
        "model_catalog": [],
        "selected_model": "gpt-5.6-sol",
        "selected_model_bounds": {
            "slug": "gpt-5.6-sol",
            "max_context_window": max_context_window,
            "effective_context_window": effective_context_window,
        },
        "agent_defaults_probe": {
            "attempted": True,
            "supported": agent_defaults,
            "detail": "test probe",
        },
        "feature_probe": {
            "attempted": True,
            "features": {
                "multi_agent": {
                    "supported": multi_agent,
                    "stage": "stable",
                    "enabled": multi_agent,
                },
                "fast_mode": {
                    "supported": fast_mode,
                    "stage": "stable",
                    "enabled": fast_mode,
                },
            },
            "detail": "test probe",
        },
        "unsupported_assumptions": [],
        "warnings": [],
    }


BASE_CONFIG = '''\
# Preserve this comment and all local values.
model = "gpt-5.6-sol"
model_context_window = 1000000 # old context note
model_auto_compact_token_limit = 900000
provider = "local-provider"
plugin_path = "C:/local/plugins/example"
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[features]
custom_feature = true

[apps._default]
enabled = false
default_tools_approval_mode = "approve"

[projects."C:/work/project"]
trust_level = "trusted"

[mcp_servers."alpha.beta"]
command = ["node", "server.js"]
url = "https://mcp.example.test/sse"
headers = { Authorization = "Bearer token-reference" }
env = { API_TOKEN = "TOKEN_ENV" }
enabled = true # preserve this comment
custom = "keep"

[mcp_servers.other]
command = "other-server"
enabled = false
'''


class AdaptiveConfigTests(unittest.TestCase):
    def _write_config(self, directory: Path, text: str = BASE_CONFIG) -> Path:
        path = directory / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def _merge(
        self,
        directory: Path,
        *,
        caps: dict[str, object] | None = None,
        disable_mcp: list[str] | None = None,
        enable_agent_defaults: bool = False,
        remove_stale_context: bool = False,
        direct_first: bool = False,
        terra_first: bool = False,
        text: str = BASE_CONFIG,
    ) -> tuple[str, dict[str, object]]:
        config = self._write_config(directory, text)
        output = directory / "merged.toml"
        report = directory / "report.json"
        with mock.patch.object(
            adaptive_config,
            "audit_capabilities",
            return_value=caps or capability_report(),
        ):
            payload = adaptive_config.merge_config(
                adaptive_config.load_config_document(config),
                output=output,
                report=report,
                codex_exe="codex.exe",
                disable_mcp=disable_mcp or [],
                enable_agent_defaults=enable_agent_defaults,
                remove_stale_context=remove_stale_context,
                direct_first=direct_first,
                terra_first=terra_first,
            )
        self.assertEqual(payload, json.loads(report.read_text(encoding="utf-8")))
        return output.read_text(encoding="utf-8"), payload

    def test_default_merge_preserves_context_and_unowned_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(Path(raw), disable_mcp=["alpha.beta"])

        self.assertIn("model_context_window = 1000000 # old context note", output)
        self.assertIn("model_auto_compact_token_limit = 900000", output)
        self.assertIn('provider = "local-provider"', output)
        self.assertIn('plugin_path = "C:/local/plugins/example"', output)
        self.assertIn('url = "https://mcp.example.test/sse"', output)
        self.assertIn('headers = { Authorization = "Bearer token-reference" }', output)
        self.assertIn('env = { API_TOKEN = "TOKEN_ENV" }', output)
        self.assertIn('enabled = false # preserve this comment', output)
        self.assertEqual(payload["changes"]["removed_stale_context"], [])
        self.assertEqual(payload["changes"]["disabled_mcp"], ["alpha.beta"])
        self.assertIn('project_doc_max_bytes = 65536', output)
        self.assertIn(
            'project_doc_fallback_filenames = ["CLAUDE.md", "TEAM_GUIDE.md", ".agents.md"]',
            output,
        )

    def test_terra_first_sets_owned_defaults_preserves_context_and_turns_off_persistent_fast(self) -> None:
        text = BASE_CONFIG.replace(
            'model = "gpt-5.6-sol"',
            'service_tier = "priority" # user can still enable Fast manually\nmodel = "gpt-5.6-sol"',
        )
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(Path(raw), terra_first=True, text=text)

        self.assertIn('model = "gpt-5.6-terra"', output)
        self.assertIn('model_reasoning_effort = "high"', output)
        self.assertIn('model_context_window = 1000000 # old context note', output)
        self.assertIn('model_auto_compact_token_limit = 400000', output)
        self.assertIn('model_auto_compact_token_limit_scope = "total"', output)
        self.assertIn('model_reasoning_summary = "concise"', output)
        self.assertIn('model_verbosity = "low"', output)
        self.assertIn('tool_output_token_limit = 10000', output)
        self.assertIn('review_model = "gpt-5.6-sol"', output)
        self.assertNotIn('service_tier = "priority"', output)
        self.assertIn('# user can still enable Fast manually', output)
        self.assertIn("model_context_window", payload["changes"]["preserved_context"])
        self.assertEqual(payload["changes"]["removed_default_service_tier"], ["priority"])

    def test_direct_first_sets_sol_high_defaults_and_preserves_requested_context(self) -> None:
        text = BASE_CONFIG.replace(
            'model = "gpt-5.6-sol"',
            'service_tier = "priority" # user can still enable Fast manually\nmodel = "gpt-5.6-terra"',
        )
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(Path(raw), direct_first=True, text=text)

        self.assertIn('model = "gpt-5.6-sol"', output)
        self.assertIn('model_reasoning_effort = "high"', output)
        self.assertIn('model_context_window = 1000000', output)
        self.assertIn('model_auto_compact_token_limit = 850000', output)
        self.assertIn('model_auto_compact_token_limit_scope = "total"', output)
        self.assertIn('review_model = "gpt-5.6-sol"', output)
        self.assertNotIn('service_tier = "priority"', output)
        self.assertIn("model", payload["changes"]["set_direct_first_defaults"])
        self.assertEqual(payload["changes"]["removed_stale_context"], [])

    def test_opt_in_removes_only_proven_root_context_values_and_records_exact_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(
                Path(raw),
                remove_stale_context=True,
                caps=capability_report(
                    max_context_window=872000,
                    effective_context_window=272000,
                ),
            )

        self.assertNotIn("model_context_window =", output)
        self.assertNotIn("model_auto_compact_token_limit =", output)
        removed = payload["changes"]["removed_stale_context"]
        self.assertEqual([item["key"] for item in removed], [
            "model_context_window",
            "model_auto_compact_token_limit",
        ])
        self.assertEqual([item["value"] for item in removed], [1000000, 900000])
        self.assertEqual([item["raw_value"] for item in removed], ["1000000", "900000"])
        self.assertIn("# old context note", output)

    def test_context_values_at_advertised_and_effective_bounds_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(
                Path(raw),
                remove_stale_context=True,
                caps=capability_report(
                    max_context_window=1000000,
                    effective_context_window=900000,
                ),
            )

        self.assertIn("model_context_window = 1000000 # old context note", output)
        self.assertIn("model_auto_compact_token_limit = 900000", output)
        self.assertEqual(payload["changes"]["removed_stale_context"], [])

    def test_agent_defaults_require_probe_and_do_not_replace_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            blocked, blocked_payload = self._merge(
                directory,
                enable_agent_defaults=True,
                caps=capability_report(agent_defaults=False),
            )
            self.assertNotIn("[agents]", blocked)
            self.assertEqual(blocked_payload["changes"]["added_agent_defaults"], [])

            allowed_text = BASE_CONFIG + '\n[agents]\nmax_concurrent_threads_per_session = 7\n'
            allowed, allowed_payload = self._merge(
                directory,
                enable_agent_defaults=True,
                caps=capability_report(agent_defaults=True),
                text=allowed_text,
            )

        self.assertIn('enabled = true', allowed)
        self.assertIn('default_subagent_model = "gpt-5.6-luna"', allowed)
        self.assertIn('default_subagent_reasoning_effort = "medium"', allowed)
        self.assertIn('interrupt_message = true', allowed)
        self.assertIn('max_concurrent_threads_per_session = 7', allowed)
        self.assertNotIn('max_concurrent_threads_per_session = 8', allowed)
        self.assertEqual(
            allowed_payload["changes"]["added_agent_defaults"],
            ["enabled", "default_subagent_model", "default_subagent_reasoning_effort", "interrupt_message"],
        )

    def test_features_are_added_only_when_supported_and_false_values_are_preserved(self) -> None:
        text = BASE_CONFIG.replace(
            "custom_feature = true",
            "multi_agent = false\ncustom_feature = true",
        )
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(
                Path(raw),
                caps=capability_report(multi_agent=True, fast_mode=True),
                text=text,
            )

        self.assertIn("multi_agent = false", output)
        self.assertIn("fast_mode = true", output)
        self.assertEqual(payload["changes"]["added_features"], ["fast_mode"])

    def test_missing_exact_mcp_name_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(
                Path(raw), disable_mcp=["alpha", "alpha.beta", "missing"]
            )

        self.assertNotIn("[mcp_servers.alpha]", output)
        self.assertEqual(payload["changes"]["missing_mcp"], ["alpha", "missing"])
        self.assertEqual(payload["changes"]["already_disabled_mcp"], [])

    def test_quoted_enabled_key_is_replaced_without_duplicate_assignment(self) -> None:
        text = '''\
model = "gpt-5.6-sol"

[mcp_servers.alpha]
"enabled" = true # keep this comment
url = "https://example.test"
'''
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(
                Path(raw), disable_mcp=["alpha"], text=text
            )

        self.assertIn('"enabled" = false # keep this comment', output)
        self.assertNotIn("\nenabled = false\n", output)
        self.assertEqual(payload["changes"]["disabled_mcp"], ["alpha"])

    def test_invalid_and_duplicate_toml_are_refused_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            invalid = self._write_config(directory, "model = [\n")
            with self.assertRaises(adaptive_config.ConfigError):
                adaptive_config.load_config_document(invalid)
            duplicate = self._write_config(directory, "model = \"a\"\nmodel = \"b\"\n")
            with self.assertRaises(adaptive_config.ConfigError):
                adaptive_config.load_config_document(duplicate)
            self.assertFalse((directory / "merged.toml").exists())


if __name__ == "__main__":
    unittest.main()
