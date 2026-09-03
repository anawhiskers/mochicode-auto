from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.learning import LearningStore


class LearningStoreTests(unittest.TestCase):
    @staticmethod
    def _outcome_pair(store: LearningStore, suffix: str) -> tuple[str, str]:
        failure = store.record_outcome(
            run_id=f"run-{suffix}",
            packet_id=f"packet-{suffix}",
            role="luna_execute",
            success=False,
            failure_class="verifier_failed",
            fingerprint=f"failure-{suffix}",
            goal_hash=f"goal-{suffix}",
        )
        success = store.record_outcome(
            run_id=f"run-{suffix}",
            packet_id=f"packet-{suffix}",
            role="luna_execute",
            success=True,
            failure_class=None,
            fingerprint=f"success-{suffix}",
            goal_hash=f"goal-{suffix}",
        )
        return str(failure["record_hash"]), str(success["record_hash"])

    @staticmethod
    def _lesson_trial(
        store: LearningStore,
        lesson_id: str,
        suffix: str,
        *,
        expected: bool,
        applied: bool,
    ) -> str:
        receipt_path = store.root / f"incoming-{suffix}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(
                {
                    "backend": "codex",
                    "role": "luna_execute",
                    "returncode": 0,
                    "timed_out": False,
                    "stopped": False,
                    "lesson_trial": {
                        "lesson_id": lesson_id,
                        "lesson_expected": expected,
                        "lesson_applied": applied,
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        outcome = store.record_trial_outcome(
            receipt_path=receipt_path,
            run_id=f"run-{suffix}",
            packet_id=f"packet-{suffix}",
            role="luna_execute",
            success=True,
            failure_class=None,
            fingerprint=f"trial-{suffix}",
            goal_hash=f"goal-{suffix}",
            lesson_id=lesson_id,
            lesson_expected=expected,
            lesson_applied=applied,
        )
        return str(outcome["record_hash"])

    def test_fabricated_trial_provenance_cannot_enter_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            with self.assertRaisesRegex(ValueError, "record_trial_outcome"):
                store.record_outcome(
                    run_id="run-fabricated",
                    packet_id="packet-fabricated",
                    role="luna_execute",
                    success=True,
                    failure_class=None,
                    fingerprint="fabricated",
                    goal_hash="fabricated-goal",
                    lesson_id="les-123456789abc",
                    lesson_expected=True,
                    lesson_applied=True,
                    lesson_backend="codex",
                    model_receipt_hash="a" * 64,
                )
            self.assertFalse((store.root / "outcomes.jsonl").exists())

    def test_promotion_rejects_a_tampered_trusted_model_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure_ref, success_ref = self._outcome_pair(store, "receipt-tamper")
            lesson = store.propose_recovery_lesson(
                role="luna_execute",
                scope="ignored",
                failure_class="verifier_failed",
                tags=("ignored",),
                failure_evidence=failure_ref,
                success_evidence=success_ref,
            )
            positive = self._lesson_trial(
                store,
                lesson.lesson_id,
                "receipt-positive",
                expected=True,
                applied=True,
            )
            negative = self._lesson_trial(
                store,
                lesson.lesson_id,
                "receipt-negative",
                expected=False,
                applied=False,
            )
            positive_record = next(
                item
                for item in store.outcomes.records()
                if item.get("record_hash") == positive
            )
            receipt_path = (
                store.root
                / "trial-receipts"
                / f"{positive_record['model_receipt_hash']}.json"
            )
            receipt_path.write_bytes(receipt_path.read_bytes() + b" ")

            with self.assertRaisesRegex(ValueError, "tampered"):
                store.promote(
                    lesson.lesson_id,
                    verification_refs=(positive,),
                    negative_control_refs=(negative,),
                    human_approved=True,
                )

    @classmethod
    def _active_lesson(cls, store: LearningStore, suffix: str):
        failure_ref, success_ref = cls._outcome_pair(store, f"{suffix}-candidate")
        lesson = store.propose_recovery_lesson(
            role="luna_execute",
            scope="ignored",
            failure_class="verifier_failed",
            tags=("ignored",),
            failure_evidence=failure_ref,
            success_evidence=success_ref,
        )
        verification_one = cls._lesson_trial(
            store, lesson.lesson_id, f"{suffix}-verification-one", expected=True, applied=True
        )
        verification_two = cls._lesson_trial(
            store, lesson.lesson_id, f"{suffix}-verification-two", expected=True, applied=True
        )
        negative_control = cls._lesson_trial(
            store, lesson.lesson_id, f"{suffix}-negative-control", expected=False, applied=False
        )
        return store.promote(
            lesson.lesson_id,
            verification_refs=(verification_one, verification_two),
            negative_control_refs=(negative_control,),
        )

    def test_failure_then_verified_recovery_creates_bounded_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure = store.record_outcome(
                run_id="run-1",
                packet_id="p1",
                role="luna_execute",
                success=False,
                failure_class="protected_input_changed",
                fingerprint="bad",
                goal_hash="goal-hash",
            )
            success = store.record_outcome(
                run_id="run-1",
                packet_id="p1",
                role="luna_execute",
                success=True,
                failure_class=None,
                fingerprint="good",
                goal_hash="goal-hash",
            )
            lesson = store.propose_recovery_lesson(
                role="luna_execute",
                scope="python tests",
                failure_class="protected_input_changed",
                tags=("python", "tests", "protected"),
                failure_evidence=failure["record_hash"],
                success_evidence=success["record_hash"],
            )

            self.assertEqual(lesson.status, "candidate")
            self.assertEqual(
                store.retrieve("python tests", role="luna_execute"),
                (),
            )
            candidates = store.retrieve(
                "protected measurement",
                role="sol_plan",
                include_candidates=True,
            )
            self.assertEqual(candidates[0].lesson_id, lesson.lesson_id)

    def test_promotion_requires_human_or_two_independent_verifications(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure_ref, success_ref = self._outcome_pair(store, "candidate")
            lesson = store.propose_recovery_lesson(
                role="luna_execute",
                scope="python",
                failure_class="verifier_failed",
                tags=("python",),
                failure_evidence=failure_ref,
                success_evidence=success_ref,
            )
            verification_one = self._lesson_trial(
                store, lesson.lesson_id, "verification-one", expected=True, applied=True
            )
            verification_two = self._lesson_trial(
                store, lesson.lesson_id, "verification-two", expected=True, applied=True
            )
            negative_control = self._lesson_trial(
                store, lesson.lesson_id, "negative-control", expected=False, applied=False
            )

            with self.assertRaisesRegex(ValueError, "two independent"):
                store.promote(
                    lesson.lesson_id,
                    verification_refs=(verification_one,),
                    negative_control_refs=(negative_control,),
                )

            with self.assertRaisesRegex(ValueError, "negative-control"):
                store.promote(
                    lesson.lesson_id,
                    verification_refs=(verification_one, verification_two),
                )

            active = store.promote(
                lesson.lesson_id,
                verification_refs=(verification_one, verification_two),
                negative_control_refs=(negative_control,),
            )
            self.assertEqual(active.status, "active")
            self.assertEqual(
                store.retrieve("python", role="luna_execute")[0].lesson_id,
                lesson.lesson_id,
            )

    def test_promotion_rejects_positive_evidence_without_lesson_activation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure_ref, success_ref = self._outcome_pair(store, "positive-binding")
            lesson = store.propose_recovery_lesson(
                role="luna_execute",
                scope="python",
                failure_class="verifier_failed",
                tags=("python",),
                failure_evidence=failure_ref,
                success_evidence=success_ref,
            )
            _, unrelated_one = self._outcome_pair(store, "unrelated-one")
            _, unrelated_two = self._outcome_pair(store, "unrelated-two")
            negative_control = self._lesson_trial(
                store, lesson.lesson_id, "bound-negative", expected=False, applied=False
            )

            with self.assertRaisesRegex(ValueError, "expected lesson activation"):
                store.promote(
                    lesson.lesson_id,
                    verification_refs=(unrelated_one, unrelated_two),
                    negative_control_refs=(negative_control,),
                )

    def test_promotion_rejects_a_negative_control_where_lesson_activated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure_ref, success_ref = self._outcome_pair(store, "negative-binding")
            lesson = store.propose_recovery_lesson(
                role="luna_execute",
                scope="python",
                failure_class="verifier_failed",
                tags=("python",),
                failure_evidence=failure_ref,
                success_evidence=success_ref,
            )
            positive_one = self._lesson_trial(
                store, lesson.lesson_id, "bound-positive-one", expected=True, applied=True
            )
            positive_two = self._lesson_trial(
                store, lesson.lesson_id, "bound-positive-two", expected=True, applied=True
            )
            bad_negative = self._lesson_trial(
                store, lesson.lesson_id, "bad-negative", expected=False, applied=True
            )

            with self.assertRaisesRegex(ValueError, "inactive lesson state"):
                store.promote(
                    lesson.lesson_id,
                    verification_refs=(positive_one, positive_two),
                    negative_control_refs=(bad_negative,),
                )

    def test_lesson_trial_fields_are_all_or_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            with self.assertRaisesRegex(ValueError, "record_trial_outcome"):
                store.record_outcome(
                    run_id="run-partial-trial",
                    packet_id="packet-partial-trial",
                    role="luna_execute",
                    success=True,
                    failure_class=None,
                    fingerprint="partial-trial",
                    goal_hash="partial-trial-goal",
                    lesson_id="les-123456789abc",
                )

    def test_retirement_preserves_history_but_stops_injection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure_ref, success_ref = self._outcome_pair(store, "retirement")
            lesson = store.propose_recovery_lesson(
                role="*",
                scope="review evidence",
                failure_class="review_missing_evidence",
                tags=("review", "evidence"),
                failure_evidence=failure_ref,
                success_evidence=success_ref,
            )
            human_evidence = self._lesson_trial(
                store, lesson.lesson_id, "human-review", expected=True, applied=True
            )
            negative_control = self._lesson_trial(
                store, lesson.lesson_id, "human-negative", expected=False, applied=False
            )
            store.promote(
                lesson.lesson_id,
                verification_refs=(human_evidence,),
                negative_control_refs=(negative_control,),
                human_approved=True,
            )
            retired = store.retire(lesson.lesson_id, reason="superseded")

            self.assertEqual(retired.status, "retired")
            self.assertEqual(store.retrieve("review evidence", role="terra_review"), ())
            self.assertTrue(store.verify()[0])

    def test_redacted_export_contains_active_lessons_not_raw_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure_ref, success_ref = self._outcome_pair(store, "export")
            lesson = store.propose_recovery_lesson(
                role="*",
                scope="C:\\private\\project",
                failure_class="permission_read_only",
                tags=("windows",),
                failure_evidence=failure_ref,
                success_evidence=success_ref,
            )
            verification_one = self._lesson_trial(
                store, lesson.lesson_id, "export-verification-one", expected=True, applied=True
            )
            verification_two = self._lesson_trial(
                store, lesson.lesson_id, "export-verification-two", expected=True, applied=True
            )
            negative_control = self._lesson_trial(
                store, lesson.lesson_id, "export-negative", expected=False, applied=False
            )
            store.promote(
                lesson.lesson_id,
                verification_refs=(verification_one, verification_two),
                negative_control_refs=(negative_control,),
            )

            exported = store.redacted_export()

            self.assertNotIn("outcomes", exported)
            self.assertNotIn("C:\\private", str(exported))
            self.assertNotIn("scope", exported["lessons"][0])
            self.assertNotIn("tags", exported["lessons"][0])
            self.assertEqual(len(exported["lessons"]), 1)

    def test_promotion_rejects_arbitrary_text_and_export_contains_only_known_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            failure_ref, success_ref = self._outcome_pair(store, "privacy")
            lesson = store.propose_recovery_lesson(
                role="luna_execute",
                scope="ignored",
                failure_class="verifier_failed",
                tags=("ignored",),
                failure_evidence=failure_ref,
                success_evidence=success_ref,
            )
            secret = "RAW-GOAL-AND-DIFF-MUST-NOT-EXPORT"

            with self.assertRaisesRegex(ValueError, "known hash-chained evidence"):
                store.promote(
                    lesson.lesson_id,
                    verification_refs=(secret, "process log text"),
                )

            self.assertNotIn(secret, (store.root / "lessons.jsonl").read_text(encoding="utf-8"))
            self.assertNotIn(secret, str(store.redacted_export()))

    def test_valid_documented_outcome_fields_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            record = store.record_outcome(
                run_id="run-documented",
                packet_id="packet-documented",
                role="terra_review",
                success=False,
                failure_class="review_missing_evidence",
                fingerprint="fingerprint-documented",
                goal_hash="goal-hash-documented",
                evidence_ref="a" * 64,
            )

            self.assertEqual(record["evidence_ref"], "a" * 64)
            self.assertEqual(record["fingerprint"], "fingerprint-documented")
            self.assertTrue(store.verify()[0])

    def test_record_outcome_rejects_unknown_fields_by_exact_allowlist(self) -> None:
        base = {
            "run_id": "run-unknown",
            "packet_id": "packet-unknown",
            "role": "luna_execute",
            "success": False,
            "failure_class": "verifier_failed",
            "fingerprint": "fingerprint-unknown",
            "goal_hash": "goal-hash-unknown",
        }
        unknown_fields = (
            "raw_goal",
            "goal_text",
            "prompt_text",
            "instructions",
            "transcript",
            "messages",
            "diff",
            "stdout",
            "stderr",
            "logs",
            "credentials",
        )

        for field in unknown_fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                store = LearningStore(Path(raw))
                with self.assertRaisesRegex(ValueError, "unknown outcome fields"):
                    store.record_outcome(**base, **{field: "SECRET_MARKER"})
                self.assertFalse((store.root / "outcomes.jsonl").exists())

    def test_record_outcome_rejects_oversized_allowed_strings(self) -> None:
        base = {
            "run_id": "run-oversize",
            "packet_id": "packet-oversize",
            "role": "luna_execute",
            "success": False,
            "failure_class": "verifier_failed",
            "fingerprint": "fingerprint-oversize",
            "goal_hash": "goal-hash-oversize",
            "evidence_ref": "a" * 64,
        }

        for field in ("run_id", "packet_id", "fingerprint", "goal_hash", "evidence_ref"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                store = LearningStore(Path(raw))
                values = dict(base)
                values[field] = "x" * 1001
                with self.assertRaisesRegex(ValueError, "length"):
                    store.record_outcome(**values)
                self.assertFalse((store.root / "outcomes.jsonl").exists())

    def test_record_outcome_rejects_wrong_and_nested_types(self) -> None:
        base = {
            "run_id": "run-types",
            "packet_id": "packet-types",
            "role": "luna_execute",
            "success": False,
            "failure_class": "verifier_failed",
            "fingerprint": "fingerprint-types",
            "goal_hash": "goal-hash-types",
        }
        invalid_values = {
            "run_id": 1,
            "packet_id": ["packet-types"],
            "role": {"value": "luna_execute"},
            "success": "false",
            "failure_class": ["verifier_failed"],
            "fingerprint": {"nested": "SECRET_MARKER"},
            "goal_hash": ("goal-hash-types",),
            "evidence_ref": 1,
        }

        for field, value in invalid_values.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                store = LearningStore(Path(raw))
                values = dict(base)
                values[field] = value
                with self.assertRaisesRegex(ValueError, "type|scalar|nested"):
                    store.record_outcome(**values)
                self.assertFalse((store.root / "outcomes.jsonl").exists())

    def test_record_outcome_rejects_secret_markers_in_allowed_scalars(self) -> None:
        base = {
            "run_id": "run-marker-check",
            "packet_id": "packet-marker-check",
            "role": "luna_execute",
            "success": False,
            "failure_class": "verifier_failed",
            "fingerprint": "fingerprint-safe",
            "goal_hash": "goal-hash-safe",
        }
        markers = ("RAW_GOAL_MARKER", "PROMPT_TEXT_MARKER", "SECRET_MARKER")

        for marker in markers:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as raw:
                store = LearningStore(Path(raw))
                values = dict(base)
                values["fingerprint"] = marker
                with self.assertRaisesRegex(ValueError, "unsafe|marker|scalar"):
                    store.record_outcome(**values)
                outcome_path = store.root / "outcomes.jsonl"
                self.assertFalse(outcome_path.exists())

    def test_tampered_lessons_chain_is_never_retrieved_or_exported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            self._active_lesson(store, "tampered-lessons")
            path = store.root / "lessons.jsonl"
            rows = path.read_text(encoding="utf-8").splitlines()
            changed = json.loads(rows[-1])
            changed["lesson"]["text"] = "RAW TAMPERED GOAL DIFF PROCESS LOG"
            rows[-1] = json.dumps(changed, separators=(",", ":"), sort_keys=True)
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "learning store verification failed"):
                store.retrieve("tampered", role="luna_execute")
            with self.assertRaisesRegex(ValueError, "learning store verification failed"):
                store.redacted_export()

    def test_tampered_outcomes_chain_blocks_lesson_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = LearningStore(Path(raw))
            self._active_lesson(store, "tampered-outcomes")
            path = store.root / "outcomes.jsonl"
            rows = path.read_text(encoding="utf-8").splitlines()
            changed = json.loads(rows[0])
            changed["goal_hash"] = "tampered"
            rows[0] = json.dumps(changed, separators=(",", ":"), sort_keys=True)
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "learning store verification failed"):
                store.retrieve("verifier", role="luna_execute")


if __name__ == "__main__":
    unittest.main()
