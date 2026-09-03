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
        self.assertIn("**Direct Sol**", self.skill_text)
        self.assertIn("**Bounded Luna Medium worker**", self.skill_text)
        self.assertIn("**Bounded Sol-led fan-out**", self.skill_text)
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
        self.assertEqual(routing["luna_medium_worker"]["reasoning_effort"], "medium")
        self.assertTrue(routing["luna_medium_worker"]["requires_real_child_receipt"])
        self.assertEqual(routing["luna_medium_worker"]["small_sequential_fallback"], "direct_sol")
        self.assertEqual(routing["sol_led_fanout"]["normal_live_child_limit"], 3)
        self.assertEqual(routing["fresh_verifier"]["model"], "gpt-5.6-sol")
        self.assertEqual(routing["fresh_verifier"]["max_reviewers"], 1)
        self.assertEqual(routing["fresh_verifier"]["max_repairs"], 1)
        self.assertTrue(routing["fresh_verifier"]["read_only"])
        self.assertTrue(routing["fresh_verifier"]["evidence_bound"])
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
        self.assertIn("Sol is the substantive parent", self.skill_text)
        self.assertIn("Direct Sol is a stock-quality passthrough", self.skill_text)
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
        self.assertIn("Use High normally", self.skill_text)
        self.assertIn("human judgment", self.skill_text)
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
            "references/safety.md",
            "references/commands.md",
            "references/skill-system.md",
        ):
            self.assertIn(reference, self.skill_text)
        self.assertNotIn("baseline_argv", self.skill_text)
        self.assertNotIn("expected_failure_codes", self.skill_text)

    def test_skill_prevents_false_blocks_and_empty_progress(self) -> None:
        self.assertIn("future human test is readiness work, not a blocker", self.skill_text)
        self.assertIn("Internal failures are work", self.skill_text)
        self.assertIn("A valid blocker requires a specific human-only", self.skill_text)
        self.assertIn("After two matching failures, change method", self.skill_text)
        self.assertIn("Do not emit repeated waiting updates", self.skill_text)
        self.assertIn("role, model, effort, owned paths", self.skill_text)


if __name__ == "__main__":
    unittest.main()
