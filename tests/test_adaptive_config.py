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
    astra_available: bool = False,
) -> dict[str, object]:
    return {
        "executable": "codex.exe",
        "version": "0.144.0",
        "available": True,
        "catalog_available": True,
        "account_access_verified": True,
        "model_catalog": [],
        "astra": {
            "model": "gpt-6-astra",
            "available": astra_available,
            "activation_ready": astra_available,
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]
            if astra_available
            else [],
            "fast": False,
        },
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
        astra_first: bool = False,
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
                astra_first=astra_first,
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
        self.assertIn('model_auto_compact_token_limit = 900000', output)
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
        self.assertIn('model_auto_compact_token_limit = 900000', output)
        self.assertIn('model_auto_compact_token_limit_scope = "total"', output)
        self.assertIn('review_model = "gpt-5.6-sol"', output)
        self.assertNotIn('service_tier = "priority"', output)
        self.assertIn("model", payload["changes"]["set_direct_first_defaults"])
        self.assertEqual(payload["changes"]["removed_stale_context"], [])

    def test_astra_first_is_capability_gated_and_preserves_requested_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(adaptive_config.ConfigError, "advertise gpt-6-astra"):
                self._merge(Path(raw), astra_first=True)

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(adaptive_config.ConfigError, "mutually exclusive"):
                self._merge(
                    Path(raw),
                    astra_first=True,
                    direct_first=True,
                    caps=capability_report(astra_available=True),
                )

        text = BASE_CONFIG.replace(
            'model = "gpt-5.6-sol"',
            'service_tier = "priority" # user can still enable Fast manually\nmodel = "gpt-5.6-sol"',
        )
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(
                Path(raw),
                astra_first=True,
                caps=capability_report(astra_available=True),
                text=text,
            )

        self.assertIn('model = "gpt-6-astra"', output)
        self.assertIn('model_reasoning_effort = "high"', output)
        self.assertIn('model_context_window = 1000000', output)
        self.assertIn('model_auto_compact_token_limit = 900000', output)
        self.assertIn('review_model = "gpt-5.6-sol"', output)
        self.assertNotIn('service_tier = "priority"', output)
        self.assertIn("model", payload["changes"]["set_astra_first_defaults"])
        self.assertEqual(payload["changes"]["removed_stale_context"], [])

    def test_switches_preserve_compatible_effort_and_nonstandard_context(self) -> None:
        text = (
            '"model" = """custom-model""" # keep\n'
            'model_reasoning_effort = "ultra"\n'
            'review_model = "custom-review"\n'
            'model_context_window = 1050000\n'
            'model_auto_compact_token_limit = 920000\n'
            'model_auto_compact_token_limit_scope = "input"\n'
        )
        for profile in ("direct_first", "terra_first", "astra_first"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as raw:
                target = {"direct_first": "gpt-5.6-sol", "terra_first": "gpt-5.6-terra", "astra_first": "gpt-6-astra"}[profile]
                caps = capability_report(astra_available=True)
                caps["model_catalog"] = [{"slug": target, "reasoning_efforts": ["high", "ultra"]}]
                output, payload = self._merge(
                    Path(raw), text=text, caps=caps,
                    remove_stale_context=True, **{profile: True},
                )
                settings = adaptive_config.tomllib.loads(output)
                self.assertEqual(settings["model"], target)
                self.assertEqual(settings["model_reasoning_effort"], "ultra")
                self.assertEqual(settings["model_context_window"], 1050000)
                self.assertEqual(settings["model_auto_compact_token_limit"], 920000)
                self.assertEqual(settings["model_auto_compact_token_limit_scope"], "input")
                self.assertIn("# keep", output)
                changed = payload["changes"][f"set_{profile}_defaults"]
                for key in ("model_reasoning_effort", *adaptive_config.CONTEXT_KEYS):
                    self.assertNotIn(key, changed)
                self.assertEqual(payload["changes"]["removed_stale_context"], [])

    def test_empty_direct_profile_still_defaults_to_sol_high(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output, _ = self._merge(Path(raw), direct_first=True, text="")
        settings = adaptive_config.tomllib.loads(output)
        self.assertEqual(settings["model"], "gpt-5.6-sol")
        self.assertEqual(settings["model_reasoning_effort"], "high")
        self.assertEqual(settings["model_context_window"], 1000000)
        self.assertEqual(settings["model_auto_compact_token_limit"], 850000)

    def test_profile_names_are_preserved_but_not_resolved_as_inline_settings(self) -> None:
        text = (
            'profile = "chosen"\nmodel = "root-model"\n'
            '[profiles.chosen]\nmodel = "chosen-model"\nmodel_reasoning_effort = "max"\n'
            'model_context_window = 1050000\nmodel_auto_compact_token_limit = 930000\n'
            '[profiles.other]\nmodel = "other-model"\n'
        )
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(Path(raw), text=text, direct_first=True)
            document = adaptive_config.load_config_document(Path(raw) / "config.toml")
            with mock.patch.object(adaptive_config, "audit_capabilities", return_value=capability_report()) as audit:
                adaptive_config.make_audit_report(document, "codex.exe")
            audit.assert_called_once_with("codex.exe", selected_model="root-model")
        settings = adaptive_config.tomllib.loads(output)
        self.assertEqual(settings["profiles"], adaptive_config.tomllib.loads(text)["profiles"])
        self.assertEqual(settings["model"], "gpt-5.6-sol")
        self.assertEqual(settings["profile"], "chosen")
        self.assertIn(adaptive_config.PROFILE_LIMITATION, payload["warnings"])

    def test_disposable_catalog_can_generate_opt_in_astra_candidate_without_account_claim(self) -> None:
        caps = capability_report(astra_available=True)
        caps["account_access_verified"] = False
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            output, payload = self._merge(directory, caps=caps, astra_first=True)
        self.assertIn('model = "gpt-6-astra"', output)
        self.assertFalse(payload["capabilities"]["account_access_verified"])
        self.assertIn("model_context_window = 1000000", output)
        self.assertEqual(payload["changes"]["removed_stale_context"], [])

    def test_unresolved_profile_values_are_preserved_without_resolution(self) -> None:
        for value in ('[]', '"missing"', '{ bad = true }'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                output, payload = self._merge(directory, text=f"profile = {value}\n", direct_first=True)
                self.assertIn(f"profile = {value}", output)
                self.assertIn(adaptive_config.PROFILE_LIMITATION, payload["warnings"])

    def test_unsupported_effort_maps_only_with_catalog_evidence_and_report(self) -> None:
        caps = capability_report()
        caps["model_catalog"] = [{"slug": "gpt-5.6-sol", "reasoning_efforts": ["medium", "high"]}]
        text = 'model = "old-model"\nmodel_reasoning_effort = "ultra"\n'
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(Path(raw), text=text, caps=caps, direct_first=True)
        self.assertIn('model_reasoning_effort = "high"', output)
        self.assertEqual(payload["changes"]["mapped_reasoning_effort"], [{
            "model": "gpt-5.6-sol", "from": "ultra", "to": "high",
            "reason": "existing effort absent from target catalog reasoning_efforts",
            "catalog_efforts": ["medium", "high"],
        }])
        with tempfile.TemporaryDirectory() as raw:
            output, payload = self._merge(Path(raw), text=text, direct_first=True)
        self.assertIn('model_reasoning_effort = "ultra"', output)
        self.assertEqual(payload["changes"]["mapped_reasoning_effort"], [])
        self.assertTrue(any("compatibility is unverified" in item for item in payload["warnings"]))

    def test_non_scalar_context_does_not_crash_or_get_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output, _ = self._merge(Path(raw), text="model_context_window = []\n", direct_first=True)
        self.assertEqual(adaptive_config.tomllib.loads(output)["model_context_window"], [])

    def test_scanner_does_not_treat_comment_quotes_or_multiline_contents_as_tables(self) -> None:
        text = (
            "# A comment containing '''\n"
            'notes = [\n"""\n[features]\nfast_mode = true\n""",\n]\n'
            '[features]\nmulti_agent = false\n'
            '[custom]\nvalue = "keep"\n'
        )
        with tempfile.TemporaryDirectory() as raw:
            output, _ = self._merge(Path(raw), text=text, direct_first=True)
        settings = adaptive_config.tomllib.loads(output)
        original = adaptive_config.tomllib.loads(text)
        self.assertEqual(settings["notes"], original["notes"])
        self.assertEqual(settings["custom"], original["custom"])
        self.assertIs(settings["features"]["multi_agent"], False)
        self.assertIs(settings["features"]["fast_mode"], True)

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
