from __future__ import annotations

import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.contracts import VerificationClass, contract_from_dict, plan_from_dict
from mochicode_core.models import RunBudget


VALID_PLAN = {
    "summary": "Build one runnable path first",
    "packets": [
        {
            "id": "vertical",
            "title": "Runnable vertical slice",
            "goal": "Make the ordinary entrypoint work end to end",
            "wave": 1,
            "priority": 1,
            "vertical_slice": True,
            "dependencies": [],
            "acceptance_criteria": ["the ordinary entrypoint works"],
            "verification_hints": ["exercise the real entrypoint"],
        },
        {
            "id": "support",
            "title": "Support path",
            "goal": "Add support behavior",
            "wave": 1,
            "priority": 2,
            "vertical_slice": False,
            "dependencies": [],
            "acceptance_criteria": ["support behavior works"],
            "verification_hints": ["run focused checks"],
        },
    ],
}


class ContractTests(unittest.TestCase):
    def test_plan_compiles_to_a_valid_breadth_first_state(self) -> None:
        state = plan_from_dict(
            VALID_PLAN,
            run_id="run-1",
            goal="Build it",
            project_root="C:/project",
            budget=RunBudget(),
            started_at=100.0,
        )

        self.assertEqual(state.queue, ["vertical", "support"])
        self.assertTrue(state.packet("vertical").vertical_slice)
        self.assertEqual(state.packet("support").wave, 1)

    def test_plan_without_vertical_slice_is_refused(self) -> None:
        data = {**VALID_PLAN, "packets": [dict(VALID_PLAN["packets"][1])]}

        with self.assertRaisesRegex(ValueError, "vertical slice"):
            plan_from_dict(
                data,
                run_id="run-1",
                goal="Build it",
                project_root="C:/project",
                budget=RunBudget(),
                started_at=100.0,
            )

    def test_packet_id_cannot_escape_run_artifact_directories(self) -> None:
        packet = dict(VALID_PLAN["packets"][0])
        packet["id"] = "../escape"
        data = {"summary": "unsafe id", "packets": [packet]}

        with self.assertRaisesRegex(ValueError, "packet id"):
            plan_from_dict(
                data,
                run_id="run-1",
                goal="Build it",
                project_root="C:/project",
                budget=RunBudget(),
                started_at=100.0,
            )

    def test_terra_contract_cannot_drop_sol_acceptance_criteria(self) -> None:
        packet = plan_from_dict(
            VALID_PLAN,
            run_id="run-1",
            goal="Build it",
            project_root="C:/project",
            budget=RunBudget(),
            started_at=100.0,
        ).packet("vertical")
        data = {
            "packet_id": "vertical",
            "goal": packet.title,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": ["a weaker replacement"],
            "baseline_argv": ["python", "-m", "unittest"],
            "final_argvs": [["python", "-m", "unittest"]],
            "expected_failure_codes": [1],
            "protected_patterns": ["tests/**/*.py"],
            "allowed_paths": ["src/**"],
            "evidence_requirements": ["test output"],
        }

        with self.assertRaisesRegex(ValueError, "acceptance criteria"):
            contract_from_dict(data, packet)

    def test_valid_contract_uses_argument_arrays_not_a_shell(self) -> None:
        packet = plan_from_dict(
            VALID_PLAN,
            run_id="run-1",
            goal="Build it",
            project_root="C:/project",
            budget=RunBudget(),
            started_at=100.0,
        ).packet("vertical")
        data = {
            "packet_id": "vertical",
            "goal": "Make the entrypoint work",
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": ["the ordinary entrypoint works", "failure is visible"],
            "baseline_argv": [sys.executable, "-m", "unittest"],
            "final_argvs": [[sys.executable, "-m", "unittest"]],
            "expected_failure_codes": [1],
            "protected_patterns": ["tests/**/*.py"],
            "allowed_paths": ["src/**"],
            "evidence_requirements": ["raw test output"],
        }

        contract = contract_from_dict(data, packet)

        self.assertEqual(contract.verification_class, VerificationClass.HARD)
        self.assertEqual(contract.baseline_argv[0], sys.executable)
        self.assertEqual(contract.final_argvs[0], contract.baseline_argv)

    def test_verify_only_contract_can_expect_an_already_green_baseline(self) -> None:
        packet = plan_from_dict(
            VALID_PLAN,
            run_id="run-1",
            goal="Build it",
            project_root="C:/project",
            budget=RunBudget(),
            started_at=100.0,
        ).packet("vertical")
        data = {
            "packet_id": "vertical",
            "goal": "Review the integrated behavior without editing",
            "execution_mode": "verify_only",
            "verification_class": "hard",
            "acceptance_criteria": ["the ordinary entrypoint works"],
            "baseline_argv": [sys.executable, "-c", "raise SystemExit(0)"],
            "final_argvs": [[sys.executable, "-c", "raise SystemExit(0)"]],
            "expected_failure_codes": [],
            "protected_patterns": ["tests/**/*.py"],
            "allowed_paths": ["src/**"],
            "evidence_requirements": ["raw verification output"],
        }

        contract = contract_from_dict(data, packet)

        self.assertEqual(contract.execution_mode.value, "verify_only")
        self.assertEqual(contract.expected_failure_codes, ())


if __name__ == "__main__":
    unittest.main()
