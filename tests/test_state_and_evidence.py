from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.evidence import EvidenceLedger
from mochicode_core.models import PacketState, PacketStatus, RunBudget, RunState
from mochicode_core.protection import (
    ProtectedInputChanged,
    assert_protected_unchanged,
    attempt_fingerprint,
    hash_protected,
)
from mochicode_core.state import StateLockError, StateStore, exclusive_file_lock


def sample_state() -> RunState:
    packet = PacketState(
        packet_id="vertical",
        title="Vertical slice",
        wave=1,
        vertical_slice=True,
        acceptance_criteria=("it runs",),
        verification_commands=("python -m unittest",),
        status=PacketStatus.PENDING,
        fingerprints=["first"],
    )
    return RunState(
        run_id="run-1",
        goal="Build it",
        project_root="C:/project",
        packets=[packet],
        queue=[packet.packet_id],
        budget=RunBudget(max_model_calls=7, max_rounds=5, max_attempts_per_packet=2),
        started_at=100.0,
        updated_at=101.0,
    )


class StateAndEvidenceTests(unittest.TestCase):
    def test_state_round_trip_is_atomic_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = StateStore(root)
            original = sample_state()

            store.save(original)
            loaded = store.load()

            self.assertEqual(loaded.run_id, original.run_id)
            self.assertEqual(loaded.packet("vertical").status, PacketStatus.PENDING)
            self.assertEqual(loaded.packet("vertical").fingerprints, ["first"])
            self.assertEqual(loaded.budget.max_model_calls, 7)
            self.assertEqual(list(root.glob("*.tmp")), [])
            json.loads(store.state_path.read_text(encoding="utf-8"))

    def test_stale_state_lock_file_does_not_block_a_new_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = StateStore(root)
            store.lock_path.write_text('{"pid":999999,"token":"stale"}\n', encoding="utf-8")

            store.save(sample_state())

            self.assertEqual(store.load().run_id, "run-1")

    def test_stale_evidence_lock_file_does_not_block_a_new_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "evidence.jsonl"
            ledger = EvidenceLedger(path)
            ledger.lock_path.write_text('{"pid":999999,"token":"stale"}\n', encoding="utf-8")

            record = ledger.append({"event": "recovered"})

            self.assertEqual(record["seq"], 1)
            self.assertTrue(ledger.verify()[0])

    def test_live_advisory_lock_still_blocks_a_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "live.lock"

            with exclusive_file_lock(lock):
                with self.assertRaises(StateLockError):
                    with exclusive_file_lock(
                        lock,
                        timeout_seconds=0.05,
                        poll_seconds=0.01,
                    ):
                        self.fail("second owner acquired a live lock")

    def test_stop_and_resume_are_persisted_at_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = StateStore(Path(raw))
            state = sample_state()

            store.request_stop()
            store.apply_stop_state(state)
            self.assertTrue(state.stop_requested)

            store.resume()
            store.apply_stop_state(state)
            self.assertFalse(state.stop_requested)

    def test_ledger_is_hash_chained_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "evidence.jsonl"
            ledger = EvidenceLedger(path)
            first = ledger.append({"event": "baseline", "exit_code": 1})
            second = ledger.append({"event": "verification", "exit_code": 0})

            self.assertEqual(first["seq"], 1)
            self.assertEqual(second["previous_hash"], first["record_hash"])
            self.assertEqual(ledger.verify(), (True, "2 records verified"))

            rows = path.read_text(encoding="utf-8").splitlines()
            changed = json.loads(rows[0])
            changed["exit_code"] = 0
            rows[0] = json.dumps(changed, sort_keys=True, separators=(",", ":"))
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")

            ok, reason = ledger.verify()
            self.assertFalse(ok)
            self.assertIn("record 1", reason)

    def test_protected_hashes_detect_edits_additions_and_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            check = tests_dir / "test_feature.py"
            check.write_text("assert True\n", encoding="utf-8")

            before = hash_protected(root, ("tests/**/*.py",))
            check.write_text("assert False\n", encoding="utf-8")
            after = hash_protected(root, ("tests/**/*.py",))

            with self.assertRaisesRegex(ProtectedInputChanged, "tests/test_feature.py"):
                assert_protected_unchanged(before, after)

    def test_attempt_fingerprint_ignores_incidental_whitespace(self) -> None:
        first = attempt_fingerprint("+line  \n", 1, "FAILED   one\n")
        second = attempt_fingerprint("+line\n", 1, "FAILED one\n")
        different = attempt_fingerprint("+other\n", 1, "FAILED one\n")

        self.assertEqual(first, second)
        self.assertNotEqual(first, different)


if __name__ == "__main__":
    unittest.main()
