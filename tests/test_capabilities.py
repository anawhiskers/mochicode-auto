from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.capabilities import (  # noqa: E402
    CONTEXT_ASSUMPTION_TOKENS,
    CommandResult,
    audit_capabilities,
    selected_model_context_bounds,
)


class FakeCodexRunner:
    def __init__(self, *, catalog_exit: int = 0) -> None:
        self.catalog_exit = catalog_exit
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(
        self,
        executable: str | Path,
        arguments: tuple[str, ...] | list[str],
        environment: dict[str, str],
        cwd: Path,
        timeout: float,
    ) -> CommandResult:
        del executable, timeout
        args = tuple(arguments)
        home = Path(environment["CODEX_HOME"])
        self.calls.append((args, home))
        self.environments.append(dict(environment))
        if args == ("--version",):
            return CommandResult(0, "codex-cli 0.144.0\n")
        if args == ("debug", "models"):
            catalog = {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "Sol",
                        "description": "do not expose token=secret-value",
                        "context_window": 272000,
                        "max_context_window": 872000,
                        "effective_context_window_percent": 95,
                        "supported_reasoning_levels": [
                            {"effort": "high"},
                            {"effort": "ultra"},
                        ],
                        "service_tiers": [{"id": "priority", "name": "Fast"}],
                    },
                    {
                        "slug": "gpt-5.4",
                        "context_window": 272000,
                        "max_context_window": 1000000,
                    },
                ]
            }
            return CommandResult(self.catalog_exit, json.dumps(catalog), "hidden stderr")
        if args == ("features", "list"):
            probe_config = (home / "config.toml").read_text(encoding="utf-8")
            if "[agents]" in probe_config:
                return CommandResult(1, "", "unknown agents setting")
            return CommandResult(
                0,
                "multi_agent stable true\nfast_mode stable true\n",
            )
        raise AssertionError(f"unexpected probe: {args}")


class CapabilitiesTests(unittest.TestCase):
    def test_context_assumption_matches_preserved_user_preference(self) -> None:
        self.assertEqual(CONTEXT_ASSUMPTION_TOKENS, 1_000_000)

    def test_probe_environment_does_not_inherit_parent_secrets(self) -> None:
        runner = FakeCodexRunner()
        sentinels = {
            "OPENAI_API_KEY": "must-not-pass",
            "PRIVATE_TOKEN": "must-not-pass",
            "AUTH_COOKIE": "must-not-pass",
            "LC_CAPABILITY_SENTINEL": "must-not-pass",
        }
        with mock.patch.dict("os.environ", sentinels, clear=False):
            audit_capabilities(
                "codex.exe",
                selected_model="gpt-5.6-sol",
                command_runner=runner,
            )

        self.assertTrue(runner.environments)
        for environment in runner.environments:
            for name in sentinels:
                self.assertNotIn(name, environment)
            self.assertIn("CODEX_HOME", environment)
            self.assertIn("TEMP", environment)

    def test_catalog_bounds_and_feature_states_are_redacted_and_selected(self) -> None:
        runner = FakeCodexRunner()
        report = audit_capabilities(
            "codex.exe",
            selected_model="gpt-5.6-sol",
            command_runner=runner,
        )

        self.assertEqual(report["version"], "0.144.0")
        self.assertTrue(report["available"])
        self.assertTrue(report["catalog_available"])
        self.assertEqual(
            report["selected_model_bounds"]["max_context_window"], 872000
        )
        self.assertEqual(
            report["selected_model_bounds"]["effective_context_window"], 272000
        )
        self.assertEqual(report["selected_model_bounds"]["reasoning_efforts"], ["high", "ultra"])
        self.assertTrue(report["selected_model_bounds"]["fast"])
        self.assertFalse(report["agent_defaults_probe"]["supported"])
        self.assertTrue(report["feature_probe"]["features"]["multi_agent"]["supported"])
        self.assertTrue(report["feature_probe"]["features"]["fast_mode"]["supported"])
        self.assertTrue(report["unsupported_assumptions"])
        self.assertNotIn("secret-value", json.dumps(report))
        self.assertTrue(all(home != Path.cwd() for _, home in runner.calls))

    def test_unavailable_catalog_does_not_invent_context_bounds(self) -> None:
        runner = FakeCodexRunner(catalog_exit=1)
        report = audit_capabilities(
            "missing-codex.exe",
            selected_model="gpt-5.6-sol",
            command_runner=runner,
        )

        self.assertFalse(report["catalog_available"])
        self.assertIsNone(report["selected_model_bounds"])
        self.assertEqual(report["unsupported_assumptions"], [])
        self.assertIsNone(selected_model_context_bounds(report))

    def test_model_without_selected_slug_does_not_claim_a_bound(self) -> None:
        runner = FakeCodexRunner()
        report = audit_capabilities("codex.exe", command_runner=runner)

        self.assertIsNone(report["selected_model_bounds"])
        self.assertEqual(
            selected_model_context_bounds(
                {
                    "selected_model_bounds": {
                        "slug": "gpt-5.6-sol",
                        "max_context_window": 872000,
                        "effective_context_window": 272000,
                    }
                }
            )["max_context_window"],
            872000,
        )


if __name__ == "__main__":
    unittest.main()
