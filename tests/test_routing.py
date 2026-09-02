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
        self.assertEqual(limits["default_wave_size"], 3)
        self.assertEqual(limits["max_wave_size"], 8)
        self.assertEqual(limits["waves"], "unlimited")
        self.assertEqual(self.catalog["sole_top_level_workflow"], "mochicode-auto")
        self.assertTrue(routing["automatic"])
        self.assertEqual(routing["user_input_required"], "goal_only")
        self.assertEqual(routing["direct_sol"]["model"], "gpt-5.6-sol")
        self.assertEqual(routing["luna_medium_worker"]["reasoning_effort"], "medium")
        self.assertTrue(routing["luna_medium_worker"]["requires_real_child_receipt"])
        self.assertEqual(routing["luna_medium_worker"]["small_sequential_fallback"], "direct_sol")
        self.assertEqual(routing["sol_led_fanout"]["normal_live_child_limit"], 3)
        self.assertFalse(routing["deterministic_controller"]["automatic_selection"])

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
        self.assertIn("Sol is the default substantive parent", self.skill_text)
        self.assertIn("Direct Sol is a stock-quality passthrough", self.skill_text)
        self.assertIn("preserve the user's original goal", self.skill_text)
        self.assertIn("Luna Medium is a bounded worker for sizable independent leaves", self.skill_text)
        self.assertIn("Luna Max is an escalation", self.skill_text)
        self.assertIn("Terra is not part of the default native path", self.skill_text)
        self.assertIn("experimental controller run", self.skill_text)

    def test_sol_and_human_own_product_and_visual_quality(self) -> None:
        for decision_area in (
            "curriculum",
            "visual design",
            "UI",
            "UX",
            "motion",
            "major planning",
        ):
            self.assertIn(decision_area, self.skill_text)
        self.assertIn("Use High normally", self.skill_text)
        self.assertIn("Human judgment is final", self.skill_text)
        self.assertIn("AI-slop", self.skill_text)
        self.assertIn("Before a worker edits product behavior", self.skill_text)

    def test_sol_controls_bounded_effort_and_critic_escalation(self) -> None:
        self.assertIn("Sol parent selects and records every child model and effort automatically", self.skill_text)
        self.assertIn("Sol High is the substantive default", self.skill_text)
        self.assertIn("When a Luna worker is justified, Medium is the first implementation effort", self.skill_text)
        self.assertIn("Never label parent-executed work as Luna", self.skill_text)
        self.assertIn("three fresh read-only judges", self.skill_text)
        self.assertIn("at most one integrated repair pass", self.skill_text)

    def test_skill_prevents_false_blocks_and_empty_progress(self) -> None:
        self.assertIn("A human checkpoint is a readiness state", self.skill_text)
        self.assertIn("Before reporting a blocker", self.skill_text)
        self.assertIn("Internal failures are work, not blockers", self.skill_text)
        self.assertIn("The parent remains responsible when orchestration machinery fails", self.skill_text)
        self.assertIn("A valid blocked goal requires a specific human-only action", self.skill_text)
        self.assertIn('Never convert "I do not know yet,"', self.skill_text)
        self.assertIn("Do not emit repeated waiting messages", self.skill_text)
        self.assertIn("role, model and effort, owned paths", self.skill_text)


if __name__ == "__main__":
    unittest.main()
