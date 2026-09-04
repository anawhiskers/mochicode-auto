from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from jsonschema import Draft202012Validator


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.evidence import EvidenceLedger
from mochicode_core.manager_state import (
    ManagerStateError,
    apply_manager_replan,
    classify_manager_route,
    finish_phase,
    initialize_manager_run,
    load_manager_state,
    manager_status,
    next_ready_phase,
    request_manager_stop,
    resume_manager,
    start_phase,
)


BASE_REVISION = "1" * 40
SECOND_REVISION = "2" * 40
THIRD_REVISION = "3" * 40
FOURTH_REVISION = "4" * 40


def plan() -> dict[str, object]:
    return {
        "phases": [
            {
                "id": "vertical",
                "title": "Runnable vertical slice",
                "wave": 1,
                "priority": 1,
                "vertical_slice": True,
                "dependencies": [],
                "acceptance_criteria": ["vertical works"],
                "owned_paths": ["src/vertical.py"],
            },
            {
                "id": "support",
                "title": "Independent support path",
                "wave": 1,
                "priority": 2,
                "vertical_slice": False,
                "dependencies": [],
                "acceptance_criteria": ["support works"],
                "owned_paths": ["src/support.py"],
            },
            {
                "id": "integrate",
                "title": "Integrated path",
                "wave": 2,
                "priority": 1,
                "vertical_slice": False,
                "dependencies": ["vertical", "support"],
                "acceptance_criteria": ["integration works"],
                "owned_paths": ["src/integrate.py"],
            },
        ]
    }


def receipt(
    path: Path,
    phase_id: str,
    criterion: str,
    owned_path: str,
    *,
    base_revision: str = BASE_REVISION,
    result_revision: str = SECOND_REVISION,
    implementer_thread_id: str = "manager-child",
) -> Path:
    value = {
        "schema_version": 1,
        "status": "COMPLETED",
        "role": "manager_implementer",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "phase_id": phase_id,
        "implementer_thread_id": implementer_thread_id,
        "base_revision": base_revision,
        "result_revision": result_revision,
        "owned_paths": [owned_path],
        "acceptance_evidence": [
            {
                "criterion_id": criterion,
                "status": "PASS",
                "evidence": f"verified {phase_id}",
            }
        ],
        "commands": [{"argv": ["python", "-m", "unittest"], "exit_code": 0}],
        "evidence_locations": [f"evidence/{phase_id}.txt"],
        "unresolved_risks": [],
        "stop_reason": "completed",
        "telemetry": {
            "input_tokens": 10,
            "cached_input_tokens": 5,
            "output_tokens": 3,
            "reasoning_output_tokens": 2,
            "tool_calls": 1,
            "retry_count": 0,
            "duration_ms": 100,
            "termination_reason": "completed",
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def verification(
    path: Path,
    phase_id: str,
    criterion: str,
    changed_path: str,
    *,
    base_revision: str,
    verified_revision: str,
) -> Path:
    value = {
        "schema_version": 1,
        "phase_id": phase_id,
        "verifier_id": "manager-parent",
        "base_revision": base_revision,
        "verified_revision": verified_revision,
        "acceptance_evidence": [
            {
                "criterion_id": criterion,
                "status": "PASS",
                "evidence": f"parent verified {phase_id}",
            }
        ],
        "commands": [{"argv": ["python", "-m", "unittest"], "exit_code": 0}],
        "protected_unchanged": True,
        "changed_paths": [changed_path],
        "stop_reason": "completed",
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def initialize(root: Path, *, run_id: str, value: dict[str, object] | None = None):
    return initialize_manager_run(
        root,
        run_id=run_id,
        goal_hash="a" * 64,
        source_revision=BASE_REVISION,
        decision_hash="b" * 64,
        activation={"mode": "explicit", "criteria": ["explicit_manager_request"]},
        plan=plan() if value is None else value,
    )


class ManagerStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan_schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "manager-plan.schema.json").read_text(encoding="utf-8")
        )
        cls.verification_schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "manager-verification.schema.json").read_text(
                encoding="utf-8"
            )
        )
        cls.child_schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "manager-child-completion.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(cls.plan_schema)
        Draft202012Validator.check_schema(cls.verification_schema)
        Draft202012Validator.check_schema(cls.child_schema)

    @staticmethod
    def classifier_facts() -> dict[str, object]:
        return {
            "request_kind": "implementation",
            "explicit_trigger": False,
            "phase_count": 3,
            "production_file_count": 6,
            "component_count": 2,
            "wave_one_vertical_slice": True,
            "phase_oracles_complete": True,
            "final_oracle_exists": True,
            "decisions_frozen": True,
            "single_sequential_writer": True,
            "child_capabilities_confirmed": True,
            "parallel_advantage_proven": False,
            "requires_heavy_controller": False,
        }

    def test_classifier_is_explicit_beta_and_automatic_shadow_only(self) -> None:
        facts = self.classifier_facts()
        automatic = classify_manager_route(facts)
        self.assertTrue(automatic["automatic_candidate"])
        self.assertEqual(automatic["shadow_route"], "manager_automatic")
        self.assertEqual(automatic["route"], "direct_sol")
        self.assertFalse(automatic["automatic_promoted"])

        facts["explicit_trigger"] = True
        self.assertEqual(classify_manager_route(facts)["route"], "manager_explicit")

        facts["request_kind"] = "review"
        self.assertEqual(classify_manager_route(facts)["route"], "direct_sol")

        negative = self.classifier_facts()
        negative["production_file_count"] = 2
        result = classify_manager_route(negative)
        self.assertFalse(result["automatic_candidate"])
        self.assertEqual(result["route"], "direct_sol")

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ManagerStateError, "shadow-only"):
                initialize_manager_run(
                    Path(raw) / "automatic",
                    run_id="automatic",
                    goal_hash="a" * 64,
                    source_revision=BASE_REVISION,
                    decision_hash="b" * 64,
                    activation={"mode": "automatic", "criteria": ["shadow_candidate"]},
                    plan=plan(),
                )

    def test_manager_json_schemas_accept_the_runtime_fixtures(self) -> None:
        self.assertEqual(
            list(Draft202012Validator(self.plan_schema).iter_errors(plan())),
            [],
        )
        with tempfile.TemporaryDirectory() as raw:
            verification_path = verification(
                Path(raw) / "verification.json",
                "vertical",
                "vertical works",
                "src/vertical.py",
                base_revision=BASE_REVISION,
                verified_revision=SECOND_REVISION,
            )
            value = json.loads(verification_path.read_text(encoding="utf-8"))
            self.assertEqual(
                list(Draft202012Validator(self.verification_schema).iter_errors(value)),
                [],
            )
            child_path = receipt(
                Path(raw) / "child.json",
                "vertical",
                "vertical works",
                "src/vertical.py",
            )
            child_value = json.loads(child_path.read_text(encoding="utf-8"))
            self.assertEqual(
                list(Draft202012Validator(self.child_schema).iter_errors(child_value)),
                [],
            )

    def test_failure_rotates_then_repeated_fingerprint_parks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "manager"
            state = initialize(root, run_id="manager-rotation")
            self.assertEqual(next_ready_phase(state)["id"], "vertical")

            start_phase(
                root,
                "vertical",
                writer_id="manager-writer",
                implementer_thread_id="manager-child",
                source_revision=BASE_REVISION,
            )
            fingerprint = hashlib.sha256(b"same failure").hexdigest()
            state = finish_phase(
                root,
                "vertical",
                result="failed",
                fingerprint=fingerprint,
                current_revision=BASE_REVISION,
            )
            self.assertEqual(next_ready_phase(state)["id"], "support")

            start_phase(
                root,
                "support",
                writer_id="manager-writer",
                implementer_thread_id="manager-child",
                source_revision=BASE_REVISION,
            )
            support_receipt = receipt(
                Path(raw) / "support.json",
                "support",
                "support works",
                "src/support.py",
            )
            state = finish_phase(
                root,
                "support",
                result="accepted",
                receipt_path=support_receipt,
                verification_path=verification(
                    Path(raw) / "support-parent.json",
                    "support",
                    "support works",
                    "src/support.py",
                    base_revision=BASE_REVISION,
                    verified_revision=SECOND_REVISION,
                ),
            )
            self.assertEqual(next_ready_phase(state)["id"], "vertical")

            start_phase(
                root,
                "vertical",
                writer_id="manager-writer",
                implementer_thread_id="manager-child",
                source_revision=SECOND_REVISION,
            )
            state = finish_phase(
                root,
                "vertical",
                result="failed",
                fingerprint=fingerprint,
                current_revision=SECOND_REVISION,
            )
            self.assertEqual(state["status"], "needs_replan")
            self.assertEqual(state["phases"][0]["status"], "parked")
            self.assertIsNone(next_ready_phase(state))

            revised = plan()
            revised["phases"][0] = {
                "id": "vertical-recovery",
                "title": "Recovered vertical slice",
                "wave": 1,
                "priority": 1,
                "vertical_slice": True,
                "dependencies": [],
                "acceptance_criteria": ["recovered vertical works"],
                "owned_paths": ["src/vertical_recovery.py"],
            }
            revised["phases"][2]["dependencies"] = ["vertical-recovery", "support"]
            state = apply_manager_replan(root, revised, decision_hash="c" * 64)
            self.assertEqual(state["replans"], 1)
            self.assertEqual(next_ready_phase(state)["id"], "vertical-recovery")
            self.assertTrue(EvidenceLedger(root / "manager-evidence.jsonl").verify()[0])

    def test_first_failure_rotates_behind_untouched_ready_later_waves(self) -> None:
        value = plan()
        value["phases"][1]["wave"] = 2
        value["phases"][2]["wave"] = 3
        value["phases"][2]["dependencies"] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "manager"
            initialize(root, run_id="cross-wave-rotation", value=value)
            start_phase(
                root,
                "vertical",
                writer_id="manager-writer",
                implementer_thread_id="manager-child",
                source_revision=BASE_REVISION,
            )
            state = finish_phase(
                root,
                "vertical",
                result="failed",
                fingerprint=hashlib.sha256(b"first failure").hexdigest(),
                current_revision=BASE_REVISION,
            )
            self.assertEqual(next_ready_phase(state)["id"], "support")

    def test_acceptance_receipts_stop_resume_and_completion_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "manager"
            initialize(root, run_id="manager-complete")
            self.assertEqual(request_manager_stop(root)["status"], "stopped")
            self.assertIsNone(manager_status(root)["next_phase"])
            self.assertEqual(resume_manager(root)["status"], "active")

            definitions = {
                "vertical": ("vertical works", "src/vertical.py"),
                "support": ("support works", "src/support.py"),
                "integrate": ("integration works", "src/integrate.py"),
            }
            revisions = {
                "vertical": (BASE_REVISION, SECOND_REVISION),
                "support": (SECOND_REVISION, THIRD_REVISION),
                "integrate": (THIRD_REVISION, FOURTH_REVISION),
            }
            for phase_id in ("vertical", "support", "integrate"):
                base_revision, verified_revision = revisions[phase_id]
                start_phase(
                    root,
                    phase_id,
                    writer_id="manager-writer",
                    implementer_thread_id="manager-child",
                    source_revision=base_revision,
                )
                criterion, owned_path = definitions[phase_id]
                state = finish_phase(
                    root,
                    phase_id,
                    result="accepted",
                    receipt_path=receipt(
                        Path(raw) / f"{phase_id}.json",
                        phase_id,
                        criterion,
                        owned_path,
                        base_revision=base_revision,
                        result_revision=verified_revision,
                    ),
                    verification_path=verification(
                        Path(raw) / f"{phase_id}-parent.json",
                        phase_id,
                        criterion,
                        owned_path,
                        base_revision=base_revision,
                        verified_revision=verified_revision,
                    ),
                )

            self.assertEqual(state["status"], "complete")
            for phase in state["phases"]:
                self.assertTrue((root / phase["child_receipt_path"]).is_file())
                self.assertTrue((root / phase["verification_receipt_path"]).is_file())
                self.assertRegex(phase["child_receipt_hash"], r"^[0-9a-f]{64}$")
                self.assertRegex(phase["verification_receipt_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(state["usage"]["child_count"], 1)

    def test_child_receipt_cannot_replace_independent_parent_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "manager"
            initialize(root, run_id="independent-verification")
            start_phase(
                root,
                "vertical",
                writer_id="manager-writer",
                implementer_thread_id="manager-child",
                source_revision=BASE_REVISION,
            )
            child = receipt(
                Path(raw) / "child.json",
                "vertical",
                "vertical works",
                "src/vertical.py",
            )
            parent = verification(
                Path(raw) / "parent.json",
                "vertical",
                "vertical works",
                "src/vertical.py",
                base_revision=BASE_REVISION,
                verified_revision=SECOND_REVISION,
            )
            with self.assertRaisesRegex(ManagerStateError, "child and parent receipts"):
                finish_phase(root, "vertical", result="accepted", receipt_path=child)

            value = json.loads(child.read_text(encoding="utf-8"))
            for field, invalid in (
                ("role", "unrelated_worker"),
                ("phase_id", "support"),
                ("implementer_thread_id", "another-child"),
                ("base_revision", THIRD_REVISION),
                ("result_revision", THIRD_REVISION),
            ):
                original = value[field]
                value[field] = invalid
                child.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ManagerStateError, "identity or result"):
                    finish_phase(
                        root,
                        "vertical",
                        result="accepted",
                        receipt_path=child,
                        verification_path=parent,
                    )
                value[field] = original
            child.write_text(json.dumps(value), encoding="utf-8")

            self_verified = verification(
                Path(raw) / "self-verified.json",
                "vertical",
                "vertical works",
                "src/vertical.py",
                base_revision=BASE_REVISION,
                verified_revision=SECOND_REVISION,
            )
            value = json.loads(self_verified.read_text(encoding="utf-8"))
            value["verifier_id"] = "manager-child"
            self_verified.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManagerStateError, "independent"):
                finish_phase(
                    root,
                    "vertical",
                    result="accepted",
                    receipt_path=child,
                    verification_path=self_verified,
                )

    def test_pending_transition_recovers_both_persistence_boundaries(self) -> None:
        for failing_function in ("_append_event_atomic", "_write_state"):
            with self.subTest(failing_function=failing_function), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "manager"
                with patch(
                    f"mochicode_core.manager_state.{failing_function}",
                    side_effect=OSError("simulated interruption"),
                ):
                    with self.assertRaises(OSError):
                        initialize(root, run_id=f"recover-{failing_function.strip('_')}")
                state = load_manager_state(root)
                self.assertEqual(state["status"], "active")
                self.assertFalse((root / ".manager-transaction.json").exists())
                self.assertTrue(EvidenceLedger(root / "manager-evidence.jsonl").verify()[0])

    def test_invalid_overlap_cycle_and_state_tampering_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            overlapping = plan()
            overlapping["phases"][1]["owned_paths"] = ["src/vertical.py"]
            with self.assertRaisesRegex(ManagerStateError, "explicit dependency order"):
                initialize(root / "overlap", run_id="overlap", value=overlapping)

            cyclic = plan()
            cyclic["phases"][0]["dependencies"] = ["integrate"]
            with self.assertRaisesRegex(ManagerStateError, "cycle"):
                initialize(root / "cycle", run_id="cycle", value=cyclic)

            sequential = plan()
            sequential["phases"][2]["owned_paths"] = ["src/vertical.py"]
            state = initialize(
                root / "sequential-overlap",
                run_id="sequential-overlap",
                value=sequential,
            )
            self.assertEqual(len(state["phases"]), 3)

            manager_root = root / "tamper"
            initialize(manager_root, run_id="tamper")
            state_path = manager_root / "manager-state.json"
            value = json.loads(state_path.read_text(encoding="utf-8"))
            value["status"] = "complete"
            state_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManagerStateError, "latest evidence"):
                load_manager_state(manager_root)

    def test_cli_exposes_the_complete_manager_control_surface(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            plan_path = base / "plan.json"
            plan_path.write_text(json.dumps(plan()), encoding="utf-8")
            facts_path = base / "facts.json"
            facts_path.write_text(json.dumps(self.classifier_facts()), encoding="utf-8")
            run_root = base / "run"
            cli = PLUGIN_ROOT / "scripts" / "mochicode.py"
            classified = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "manager",
                    "classify",
                    "--facts",
                    str(facts_path),
                    "--json",
                ],
                cwd=PLUGIN_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(classified.returncode, 0, classified.stderr)
            self.assertEqual(json.loads(classified.stdout)["route"], "direct_sol")
            self.assertEqual(
                json.loads(classified.stdout)["shadow_route"],
                "manager_automatic",
            )
            automatic = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "manager",
                    "init",
                    "--run-root",
                    str(base / "automatic"),
                    "--run-id",
                    "automatic",
                    "--goal-hash",
                    "f" * 64,
                    "--source-revision",
                    BASE_REVISION,
                    "--decision-hash",
                    "e" * 64,
                    "--activation-mode",
                    "automatic",
                    "--activation-criterion",
                    "shadow_candidate",
                    "--plan",
                    str(plan_path),
                    "--json",
                ],
                cwd=PLUGIN_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertNotEqual(automatic.returncode, 0)
            self.assertFalse((base / "automatic" / "manager-state.json").exists())
            initialized = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "manager",
                    "init",
                    "--run-root",
                    str(run_root),
                    "--run-id",
                    "cli-manager",
                    "--goal-hash",
                    "f" * 64,
                    "--source-revision",
                    BASE_REVISION,
                    "--decision-hash",
                    "e" * 64,
                    "--activation-mode",
                    "explicit",
                    "--activation-criterion",
                    "explicit_manager_request",
                    "--plan",
                    str(plan_path),
                    "--json",
                ],
                cwd=PLUGIN_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(json.loads(initialized.stdout)["next_phase"], "vertical")
            started = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "manager",
                    "start",
                    "--run-root",
                    str(run_root),
                    "--phase",
                    "vertical",
                    "--writer-id",
                    "manager-writer",
                    "--thread-id",
                    "manager-child",
                    "--source-revision",
                    BASE_REVISION,
                    "--json",
                ],
                cwd=PLUGIN_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(json.loads(started.stdout)["current_phase"], "vertical")


if __name__ == "__main__":
    unittest.main()
