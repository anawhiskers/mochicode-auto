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
    model_readiness,
    parse_model_catalog,
    selected_model_context_bounds,
)


class FakeCodexRunner:
    def __init__(self, *, catalog_exit: int = 0, include_astra: bool = True) -> None:
        self.catalog_exit = catalog_exit
        self.include_astra = include_astra
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
            if self.include_astra:
                catalog["models"].append(
                    {
                        "slug": "gpt-6-astra",
                        "display_name": "Astra",
                        "context_window": 1050000,
                        "max_context_window": 1050000,
                        "supported_reasoning_levels": [
                            {"effort": effort}
                            for effort in ("low", "medium", "high", "xhigh", "max")
                        ],
                    }
                )
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
        self.assertEqual(
            report["astra"],
            {
                "model": "gpt-6-astra",
                "required_effort": "high",
                "status": "ready",
                "available": True,
                "activation_ready": True,
                "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
                "fast": False,
            },
        )
        self.assertTrue(report["unsupported_assumptions"])
        self.assertEqual(report["catalog_source"], "disposable_home")
        self.assertFalse(report["account_access_verified"])
        self.assertIsNotNone(selected_model_context_bounds(report))
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
        runner = FakeCodexRunner(include_astra=False)
        report = audit_capabilities("codex.exe", command_runner=runner)

        self.assertIsNone(report["selected_model_bounds"])
        self.assertFalse(report["astra"]["available"])
        self.assertEqual(report["astra"]["status"], "slug_absent")
        self.assertEqual(
            selected_model_context_bounds(
                {
                    "account_access_verified": True,
                    "selected_model": "gpt-5.6-sol",
                    "selected_model_bounds": {
                        "slug": "gpt-5.6-sol",
                        "max_context_window": 872000,
                        "effective_context_window": 272000,
                    }
                }
            )["max_context_window"],
            872000,
        )

    def test_model_readiness_distinguishes_catalog_slug_and_effort_gates(self) -> None:
        self.assertEqual(
            model_readiness({}, "gpt-6-astra", "high")["status"],
            "catalog_unavailable",
        )
        absent = {"catalog_available": True, "model_catalog": []}
        self.assertEqual(
            model_readiness(absent, "gpt-6-astra", "high")["status"],
            "slug_absent",
        )
        unsupported = {
            "catalog_available": True,
            "model_catalog": [
                {
                    "slug": "gpt-6-astra",
                    "reasoning_efforts": ["medium"],
                    "fast": False,
                }
            ],
        }
        self.assertEqual(
            model_readiness(unsupported, "gpt-6-astra", "high")["status"],
            "effort_unsupported",
        )

    def test_readiness_is_catalog_compatibility_only_and_accepts_all_known_efforts(self) -> None:
        report = {
            "catalog_available": True,
            "account_access_verified": False,
            "model_catalog": [None, [], "bad", {
                "slug": "gpt-6-astra",
                "reasoning_efforts": ["none", "minimal", "high", "ultra", {}, "unsafe"],
            }],
        }
        for effort in ("none", "minimal", "high", "ultra"):
            with self.subTest(effort=effort):
                readiness = model_readiness(report, "gpt-6-astra", effort)
                self.assertTrue(readiness["activation_ready"])
                self.assertEqual(readiness["status"], "ready")
                self.assertFalse(report["account_access_verified"])
        with self.assertRaises(ValueError):
            model_readiness(report, "gpt-6-astra", [])
        report["model_catalog"].append(report["model_catalog"][-1])
        self.assertEqual(
            model_readiness(report, "gpt-6-astra", "high")["status"],
            "catalog_ambiguous",
        )

    def test_malformed_catalogs_and_numeric_bounds_do_not_crash_or_invent_limits(self) -> None:
        for value in (None, [], {}, "not JSON", "[null]", "[" * 2000):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(parse_model_catalog(value), [])
        for percent in (float("inf"), float("nan"), 101, True, 10**400):
            with self.subTest(percent=percent):
                catalog = parse_model_catalog(json.dumps({"models": [{
                    "slug": "gpt-6-astra", "max_context_window": 1000000,
                    "effective_context_window_percent": percent,
                }]}))
                self.assertNotIn("effective_context_window", catalog[0])
        catalog = parse_model_catalog(json.dumps({"models": [
            None, {"slug": []}, {"slug": "valid", "context_window": 272000},
            {"slug": "fractional", "max_context_window": 1.5},
        ]}))
        self.assertNotIn("max_context_window", catalog[0])
        self.assertNotIn("max_context_window", catalog[1])

    def test_context_removal_bounds_require_matching_verified_model(self) -> None:
        report = {
            "account_access_verified": True,
            "selected_model": "custom-model",
            "selected_model_bounds": {"slug": "other-model", "max_context_window": 1},
        }
        self.assertIsNone(selected_model_context_bounds(report))


if __name__ == "__main__":
    unittest.main()
