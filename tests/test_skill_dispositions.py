from __future__ import annotations

import json
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class SkillDispositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(
            (PLUGIN_ROOT / "config" / "skill-dispositions.json").read_text(encoding="utf-8")
        )

    def test_all_user_owned_skills_have_exactly_one_disposition(self) -> None:
        modes = self.catalog["modes"]
        all_names = [name for names in modes.values() for name in names]
        self.assertEqual(len(all_names), 39)
        self.assertEqual(len(set(all_names)), 39)
        self.assertEqual(self.catalog["sole_automatic_top_level_workflow"], "mochicode-auto")

    def test_competing_legacy_workflows_are_bound_to_candidate_retirement(self) -> None:
        retired = set(self.catalog["modes"]["candidate_retirement_after_canary"])
        self.assertTrue(
            {
                "ask-clarify",
                "coder",
                "debugger",
                "design-intake",
                "goal-loop",
                "model-routing",
                "obsidian-second-brain",
                "orchestrator",
                "reviewer",
                "token-economy",
            }.issubset(retired)
        )

    def test_document_skills_remain_fallbacks_until_canaries_pass(self) -> None:
        self.assertEqual(
            set(self.catalog["modes"]["replacement_backed_fallback"]),
            {"docx", "pdf", "pptx", "xlsx"},
        )
        invariants = self.catalog["invariants"]
        self.assertTrue(invariants["managed_skill_caches_are_read_only"])
        self.assertTrue(invariants["retirement_requires_exact_version_fresh_task_canary"])
        self.assertTrue(
            invariants["replacement_backed_fallbacks_require_read_and_write_canaries_before_retirement"]
        )

    def test_dispatcher_documents_the_registry_and_non_recursion(self) -> None:
        dispatcher = (PLUGIN_ROOT / "skills" / "mochicode-auto" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("config/skill-dispositions.json", dispatcher)
        self.assertIn("only automatic top-level workflow", dispatcher)
        self.assertIn("never invokes this workflow", dispatcher)
        self.assertIn("No other skill may start a competing top-level loop", dispatcher)

    def test_dispatcher_requires_visible_model_and_effort_labels_for_new_work(self) -> None:
        dispatcher = (PLUGIN_ROOT / "skills" / "mochicode-auto" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("[ROLE | MODEL | EFFORT] concise objective", dispatcher)
        self.assertIn("Record each real child role, model, effort", dispatcher)


if __name__ == "__main__":
    unittest.main()
