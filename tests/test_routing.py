from __future__ import annotations

import json
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class RoutingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill_text = (PLUGIN_ROOT / "skills" / "mochicode-auto" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        cls.catalog = json.loads(
            (PLUGIN_ROOT / "config" / "role-dispositions.json").read_text(encoding="utf-8")
        )

    def test_skill_exposes_direct_first_routes_and_single_top_level_workflow(self) -> None:
        self.assertIn("MochiCode Auto is the only automatic top-level workflow", self.skill_text)
        self.assertIn("**Direct current parent**", self.skill_text)
        self.assertIn("**Direct selected authority**", self.skill_text)
        self.assertIn("**Bounded Luna Medium worker**", self.skill_text)
        self.assertIn("**Bounded authority-led fan-out**", self.skill_text)
        self.assertIn("**Bounded Manager Mode beta**", self.skill_text)
        self.assertIn("**Experimental controller**", self.skill_text)
        self.assertIn("The user supplies only the real goal once", self.skill_text)

    def test_machine_readable_limits_preserve_the_packet_boundaries(self) -> None:
        routing = self.catalog["routing"]
        limits = routing["limits"]

        self.assertEqual(limits["max_orchestration_depth"], 1)
        self.assertTrue(limits["one_writer_per_path"])
        self.assertEqual(limits["active_child_ceiling"], 8)
        self.assertTrue(limits["ceiling_is_hard"])
        self.assertTrue(limits["ceiling_applies_where_host_supported"])
        self.assertEqual(limits["default_wave_size"], 2)
        self.assertEqual(limits["max_automatic_wave_size"], 3)
        self.assertEqual(limits["waves"], "bounded_by_goal_and_budgets")
        self.assertEqual(self.catalog["sole_top_level_workflow"], "mochicode-auto")
        self.assertTrue(routing["automatic"])
        self.assertEqual(routing["user_input_required"], "goal_only")
        self.assertEqual(routing["direct_sol"]["model"], "gpt-5.6-sol")
        astra = routing["astra_candidate"]
        self.assertEqual(astra["model"], "gpt-6-astra")
        self.assertEqual(astra["activation_status"], "capability_gated_unbenchmarked")
        self.assertFalse(astra["automatic_default"])
        self.assertTrue(astra["requires_live_catalog"])
        self.assertEqual(astra["unavailable_fallback"], "direct_sol")
        self.assertIn("matched_direct_sol_comparison", astra["promotion_requires"])
        self.assertEqual(routing["luna_medium_worker"]["reasoning_effort"], "medium")
        self.assertTrue(routing["luna_medium_worker"]["requires_real_child_receipt"])
        self.assertEqual(routing["luna_medium_worker"]["small_sequential_fallback"], "direct_sol")
        self.assertEqual(routing["sol_led_fanout"]["normal_live_child_limit"], 3)
        self.assertEqual(routing["fresh_verifier"]["model"], "gpt-5.6-sol")
        self.assertEqual(routing["fresh_verifier"]["max_reviewers"], 1)
        self.assertEqual(routing["fresh_verifier"]["max_repairs"], 1)
        self.assertTrue(routing["fresh_verifier"]["read_only"])
        self.assertTrue(routing["fresh_verifier"]["evidence_bound"])
        manager = routing["manager_mode"]
        self.assertFalse(manager["automatic_selection"])
        self.assertTrue(manager["automatic_shadow_classification"])
        self.assertEqual(manager["preferred_implementer"], "direct_non_spawning_sol_high_child")
        self.assertEqual(manager["max_automatic_phases"], 6)
        self.assertEqual(manager["max_attempts_per_phase"], 2)
        self.assertEqual(manager["max_replans"], 1)
        self.assertTrue(manager["requires_typed_completion_receipt"])
        self.assertFalse(routing["deterministic_controller"]["automatic_selection"])

    def test_portable_limits_match_the_machine_contract(self) -> None:
        portable = (
            PLUGIN_ROOT / "portable" / "docs" / "MOCHICODE-HYBRID-ROUTING.md"
        ).read_text(encoding="utf-8")

        self.assertIn("Automatic waves start with two and cap at three", portable)
        self.assertIn("Total waves are bounded by the goal", portable)
        self.assertNotIn("Total waves are unlimited", portable)
        self.assertNotIn("larger waves earn benchmark promotion", portable)

    def test_plan_and_contract_schema_required_fields_are_unchanged(self) -> None:
        plan = json.loads((PLUGIN_ROOT / "schemas" / "plan.schema.json").read_text(encoding="utf-8"))
        contract = json.loads(
            (PLUGIN_ROOT / "schemas" / "contract.schema.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            set(plan["properties"]["packets"]["items"]["required"]),
            {
                "id",
                "title",
                "goal",
                "wave",
                "priority",
                "vertical_slice",
                "dependencies",
                "acceptance_criteria",
                "verification_hints",
            },
        )
        self.assertEqual(
            set(contract["required"]),
            {
                "packet_id",
                "goal",
                "execution_mode",
                "verification_class",
                "acceptance_criteria",
                "baseline_argv",
                "final_argvs",
                "expected_failure_codes",
                "protected_patterns",
                "allowed_paths",
                "evidence_requirements",
            },
        )

    def test_core_role_split_is_direct_first_and_quality_gated(self) -> None:
        self.assertIn("current selected authority is the substantive parent", self.skill_text)
        self.assertIn("Direct selected authority is a stock-quality passthrough", self.skill_text)
        self.assertIn("Preserve the user's original goal", self.skill_text)
        self.assertIn("Bounded Luna Medium worker", self.skill_text)
        self.assertIn("Luna Max only after failed acceptance", self.skill_text)
        self.assertIn("Terra is absent from the default native path", self.skill_text)
        self.assertIn("only in the experimental route", self.skill_text)

    def test_sol_and_human_own_product_and_visual_quality(self) -> None:
        for decision_area in (
            "curriculum",
            "visual design",
            "UI",
            "UX",
            "motion",
        ):
            self.assertIn(decision_area, self.skill_text)
        self.assertIn("Preserve the running parent and selected effort", self.skill_text)
        self.assertIn("human judgment", self.skill_text.lower())
        self.assertIn("AI-slop", self.skill_text)
        self.assertIn("cannot redesign the product", self.skill_text)

    def test_sol_controls_bounded_effort_and_critic_escalation(self) -> None:
        self.assertIn("Sol High is the default", self.skill_text)
        self.assertIn("Bounded Luna Medium worker", self.skill_text)
        self.assertIn("Never report Luna work without a real child receipt", self.skill_text)
        self.assertIn("one fresh read-only Sol High verifier", self.skill_text)
        self.assertIn("at most one repair", self.skill_text)

    def test_skill_entrypoint_uses_progressive_disclosure(self) -> None:
        self.assertLess(len(self.skill_text.encode("utf-8")), 9000)
        for reference in (
            "references/workflow.md",
            "references/astra-mode.md",
            "references/manager-mode.md",
            "references/safety.md",
            "references/commands.md",
            "references/skill-system.md",
        ):
            self.assertIn(reference, self.skill_text)
        self.assertNotIn("baseline_argv", self.skill_text)
        self.assertNotIn("expected_failure_codes", self.skill_text)

    def test_astra_guidance_is_progressive_capability_gated_and_direct_first(self) -> None:
        astra = (
            PLUGIN_ROOT / "skills" / "mochicode-auto" / "references" / "astra-mode.md"
        ).read_text(encoding="utf-8")
        self.assertIn("installed Codex catalog must list that exact model", astra)
        self.assertIn("Keep substantial, coupled, visual", astra)
        self.assertIn("does not automatically add Manager Mode", astra)
        self.assertIn("catalog omission does not disprove it", astra)
        self.assertIn("Ultra is exceptional but permitted when that host advertises it", astra)
        self.assertNotIn("Astra does not support Ultra", astra)
        self.assertIn("Use them only when the active runtime advertises them", astra)
        self.assertIn("matched task against the current direct Sol route", astra)

    def test_skill_prevents_false_blocks_and_empty_progress(self) -> None:
        self.assertIn("future human test is readiness work, not a blocker", self.skill_text)
        self.assertIn("Internal failures are work", self.skill_text)
        self.assertIn("A valid blocker requires a specific human-only", self.skill_text)
        self.assertIn("After two matching failures, change method", self.skill_text)
        self.assertIn("Do not emit repeated waiting updates", self.skill_text)
        self.assertIn("role, model, effort, owned paths", self.skill_text)

    def test_manager_mode_is_bounded_and_does_not_create_grandchildren(self) -> None:
        manager = (
            PLUGIN_ROOT / "skills" / "mochicode-auto" / "references" / "manager-mode.md"
        ).read_text(encoding="utf-8")
        self.assertIn("exactly one direct `mochicode_manager_implementer` Sol High child", manager)
        self.assertIn("must never spawn descendants", manager)
        self.assertIn("Configuration alone is not runtime proof", manager)
        self.assertIn("Automatic classification stays shadow-only", self.skill_text)
        self.assertIn("independently rerun", manager)
        self.assertIn("manager-verification.schema.json", manager)
        self.assertNotIn("separate top-level", manager)

    def test_receipt_instructions_select_the_route_specific_schema(self) -> None:
        workflow = (PLUGIN_ROOT / "skills/mochicode-auto/references/workflow.md").read_text(encoding="utf-8")
        expected = {
            "native leaf": "child-completion.schema.json",
            "manager implementer": "manager-child-completion.schema.json",
            "experimental controller": "implementation.schema.json",
        }
        for document in (self.skill_text, workflow):
            for route, schema in expected.items():
                with self.subTest(route=route, document=document[:30]):
                    self.assertRegex(document.lower(), rf"{route}(?::| uses) `schemas/{schema.replace('.', '[.]')}`")
                    self.assertTrue((PLUGIN_ROOT / "schemas" / schema).is_file())

    def test_published_manager_trigger_and_authority_are_consistent(self) -> None:
        paths = (
            "README.md", "docs/BRIEF.md", "docs/ARCHITECTURE.md",
            "portable/docs/MOCHICODE-HYBRID-ROUTING.md",
            "portable/templates/repository/AGENTS.md",
            "skills/mochicode-auto/references/manager-mode.md",
            ".codex-plugin/plugin.json", "install.ps1",
        )
        for path in paths:
            text = (PLUGIN_ROOT / path).read_text(encoding="utf-8")
            if path == "install.ps1":
                text = text.split("$block = @(", 1)[1].split(") -join $lineEnding", 1)[0]
            with self.subTest(path=path):
                self.assertIn("explicit Manager Mode implementation request", text)
                self.assertNotIn("Sol adjudicates once", text)
                self.assertNotIn("one Sol adjudication", text)
                self.assertNotIn("after Sol proves", text)
        metadata = json.loads((PLUGIN_ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertIn("shadow-only", metadata["interface"]["longDescription"])

    def test_source_adapter_instructions_do_not_use_package_wrapper(self) -> None:
        for path in ("README.md", "portable/docs/MULTI-AGENT-PORTABILITY.md"):
            text = (PLUGIN_ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("python -B .\\scripts\\agent_adapter.py audit --agent", text)
                self.assertIn("python -B .\\scripts\\agent_adapter.py apply --agent", text)
                self.assertIn("extracted verified release package", text)
                self.assertNotRegex(text, r"(?m)^pwsh .*agent-sync[.]ps1")

    def test_native_guarantees_are_qualified_in_policy_surfaces(self) -> None:
        for path in (
            "README.md", "docs/ARCHITECTURE.md",
            "skills/mochicode-auto/SKILL.md",
            "skills/mochicode-auto/references/workflow.md",
            "skills/mochicode-auto/references/safety.md",
            "portable/templates/repository/AGENTS.md",
            "portable/docs/MOCHICODE-HYBRID-ROUTING.md",
        ):
            text = (PLUGIN_ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("behavioral instructions", text)
                self.assertNotIn("children cannot delegate", text)
                self.assertNotIn("child cannot spawn descendants", text)


if __name__ == "__main__":
    unittest.main()
