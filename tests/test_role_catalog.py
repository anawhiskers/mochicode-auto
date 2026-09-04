from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]

ARCHIVE_ROLE_NAMES = {
    "agent_harness",
    "batch_coordinator",
    "embedded_engineer",
    "luna_batch",
    "luna_builder",
    "luna_frontend",
    "luna_solver",
    "ml_mentor",
    "quality_coordinator",
    "repo_curator",
    "researcher",
    "simulation_engineer",
    "sol_architect",
    "sol_critic",
    "sol_judge",
    "sol_solver",
    "systems_forensics",
    "terra_context",
    "terra_integrator",
    "terra_verifier",
}

CORE_CONFIGS = {
    "mochicode-sol": {
        "agent_name": "mochicode_sol",
        "path": "config/agents/mochicode-sol.toml",
        "model": "gpt-5.6-sol",
        "sandbox": "read-only",
    },
    "mochicode-manager-implementer": {
        "agent_name": "mochicode_manager_implementer",
        "path": "config/agents/mochicode-manager-implementer.toml",
        "model": "gpt-5.6-sol",
        "sandbox": "workspace-write",
    },
    "mochicode-terra-contract": {
        "agent_name": "mochicode_terra_contract",
        "path": "config/agents/mochicode-terra-contract.toml",
        "model": "gpt-5.6-terra",
        "sandbox": "workspace-write",
    },
    "mochicode-terra-review": {
        "agent_name": "mochicode_terra_review",
        "path": "config/agents/mochicode-terra-review.toml",
        "model": "gpt-5.6-terra",
        "sandbox": "read-only",
    },
    "mochicode-luna": {
        "agent_name": "mochicode_luna",
        "path": "config/agents/mochicode-luna.toml",
        "model": "gpt-5.6-luna",
        "sandbox": "workspace-write",
    },
}


class RoleCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(
            (PLUGIN_ROOT / "config" / "role-dispositions.json").read_text(encoding="utf-8")
        )

    def test_catalog_contains_a_complete_disposition_for_all_twenty_archive_roles(self) -> None:
        roles = self.catalog["archive_roles"]
        self.assertEqual(len(roles), 20)
        self.assertEqual(set(roles), ARCHIVE_ROLE_NAMES)

        for name, entry in roles.items():
            with self.subTest(role=name):
                self.assertIn(
                    entry["disposition"],
                    {"consolidated", "optional_native", "optional_overlay"},
                )
                self.assertIn("target_core_agent", entry)
                self.assertIn("native_role", entry)
                self.assertIn("distinct", entry)
                self.assertIn("route", entry)
                self.assertIsInstance(entry["reason"], str)
                if entry["disposition"] == "consolidated":
                    self.assertFalse(entry["distinct"])
                    self.assertIsNone(entry["native_role"])
                elif entry["disposition"] == "optional_native":
                    self.assertTrue(entry["distinct"])
                    self.assertIsInstance(entry["native_role"], str)
                    self.assertIn("fallback", entry)
                else:
                    self.assertTrue(entry["distinct"])
                    self.assertIsNone(entry["native_role"])
                    self.assertIn("fallback", entry)
                    if entry["target_core_agent"] is None:
                        self.assertEqual(entry["route"], "direct")
                        self.assertEqual(entry["fallback"], "direct_parent")
                    else:
                        self.assertIn(entry["target_core_agent"], self.catalog["core_agents"])
                        self.assertIn(
                            entry["route"],
                            {"native_delegation", "deterministic_controller"},
                        )

    def test_only_five_core_agents_are_active_and_their_contracts_are_safe(self) -> None:
        self.assertEqual(set(self.catalog["core_agents"]), set(CORE_CONFIGS))
        agent_dir = PLUGIN_ROOT / "config" / "agents"

        for agent_id, expected in CORE_CONFIGS.items():
            with self.subTest(agent=agent_id):
                path = PLUGIN_ROOT / expected["path"]
                self.assertTrue(path.is_file())
                self.assertEqual(path.parent, agent_dir)
                raw = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(self.catalog["core_agents"][agent_id]["agent_name"], raw["name"])
                self.assertEqual(raw["name"], expected["agent_name"])
                self.assertEqual(raw["model"], expected["model"])
                self.assertEqual(raw["sandbox_mode"], expected["sandbox"])
                self.assertNotIn("model_context_window", raw)
                if agent_id == "mochicode-manager-implementer":
                    self.assertEqual(raw["agents"], {"enabled": False})
                    self.assertEqual(raw["features"], {"multi_agent": False})
                else:
                    self.assertNotIn("agents", raw)
                    self.assertNotIn("features", raw)
                instructions = raw["developer_instructions"]
                self.assertNotIn("1050000", instructions)
                self.assertNotIn("danger" + "-full-access", instructions)

        luna = self.catalog["core_agents"]["mochicode-luna"]
        self.assertEqual(luna["reasoning_effort"], "max")
        self.assertNotIn("service_tier", luna)
        luna_config = tomllib.loads(
            (PLUGIN_ROOT / CORE_CONFIGS["mochicode-luna"]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(luna_config["model_reasoning_effort"], "max")
        self.assertNotIn("service_tier", luna_config)
        self.assertFalse(self.catalog["core_agents"]["mochicode-sol"]["may_implement"])
        self.assertFalse(self.catalog["core_agents"]["mochicode-terra-review"]["may_implement"])
        manager = self.catalog["core_agents"]["mochicode-manager-implementer"]
        self.assertTrue(manager["may_implement"])
        self.assertFalse(manager["may_spawn"])
        self.assertEqual(manager["reasoning_effort"], "high")
        manager_config = tomllib.loads(
            (
                PLUGIN_ROOT
                / CORE_CONFIGS["mochicode-manager-implementer"]["path"]
            ).read_text(encoding="utf-8")
        )
        manager_instructions = manager_config["developer_instructions"]
        self.assertIn("role exactly `manager_implementer`", manager_instructions)
        self.assertIn("Do not read or write memories", manager_instructions)
        self.assertIn("Do not load optional skills or MCP", manager_instructions)
        self.assertIn("The parent owns live UI checks", manager_instructions)

        repo_curator = self.catalog["archive_roles"]["repo_curator"]
        self.assertEqual(repo_curator["target_core_agent"], "mochicode-luna")
        self.assertEqual(repo_curator["route"], "deterministic_controller")

        for role in ("ml_mentor", "researcher", "systems_forensics"):
            with self.subTest(direct_overlay=role):
                entry = self.catalog["archive_roles"][role]
                self.assertEqual(entry["disposition"], "optional_overlay")
                self.assertIsNone(entry["target_core_agent"])
                self.assertEqual(entry["route"], "direct")

        self.assertFalse(
            any(
                entry["disposition"] == "optional_native"
                for entry in self.catalog["archive_roles"].values()
            ),
            "No callable optional role may be advertised without an installed agent config.",
        )

        terra_contract_support = {
            name
            for name, entry in self.catalog["archive_roles"].items()
            if entry["target_core_agent"] == "mochicode-terra-contract"
        }
        terra_review_support = {
            name
            for name, entry in self.catalog["archive_roles"].items()
            if entry["target_core_agent"] == "mochicode-terra-review"
        }
        self.assertEqual(terra_contract_support, {"terra_context", "terra_integrator"})
        self.assertEqual(terra_review_support, {"quality_coordinator", "terra_verifier"})

    def test_upgrader_and_portable_template_are_present_and_scoped(self) -> None:
        upgrader = (PLUGIN_ROOT / "skills" / "repository-workflow-upgrader" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        template = (PLUGIN_ROOT / "portable" / "templates" / "repository" / "AGENTS.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("repository-workflow-upgrader", upgrader)
        self.assertIn("repository-workflow-upgrader", template)
        self.assertIn("Every repository-workflow migration that writes files", upgrader)
        self.assertIn("Terra defines the migration contract", upgrader)
        self.assertIn("bounded Luna worktree", upgrader)
        for text in (upgrader, template):
            with self.subTest(document="upgrader" if text == upgrader else "template"):
                self.assertIn("one writer", text)
                self.assertIn("depth", text)
                self.assertIn("eight", text.lower())
                self.assertNotIn("1050000", text)
                self.assertNotIn("danger" + "-full-access", text)
                self.assertNotIn("[agents]", text)


if __name__ == "__main__":
    unittest.main()
