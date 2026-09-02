from __future__ import annotations

import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.models import PacketState, PacketStatus, RunBudget, RunState
from mochicode_core.scheduler import DecisionKind, next_decision, record_attempt, validate_plan


def make_state() -> RunState:
    packets = [
        PacketState(
            "vertical",
            "Runnable vertical slice",
            wave=1,
            priority=1,
            vertical_slice=True,
            acceptance_criteria=("the user can run it",),
            verification_commands=("python -m unittest",),
        ),
        PacketState(
            "independent",
            "Independent support path",
            wave=1,
            priority=2,
            acceptance_criteria=("support path works",),
            verification_commands=("python -m unittest",),
        ),
        PacketState(
            "integrate",
            "Integrate accepted work",
            wave=2,
            dependencies=("vertical", "independent"),
            acceptance_criteria=("both paths integrate",),
            verification_commands=("python -m unittest",),
        ),
    ]
    return RunState(
        run_id="run-1",
        goal="Build a runnable thing",
        project_root="C:/project",
        packets=packets,
        queue=[packet.packet_id for packet in packets],
        budget=RunBudget(
            max_model_calls=12,
            max_rounds=8,
            max_attempts_per_packet=2,
            max_wall_seconds=600,
        ),
        started_at=100.0,
        updated_at=100.0,
    )


class SchedulerTests(unittest.TestCase):
    def test_plan_requires_a_runnable_vertical_slice_in_wave_one(self) -> None:
        state = make_state()
        state.packet("vertical").vertical_slice = False

        with self.assertRaisesRegex(ValueError, "vertical slice"):
            validate_plan(state)

    def test_first_failure_rotates_to_independent_work(self) -> None:
        state = make_state()
        validate_plan(state)

        first = next_decision(state, now=101.0)
        self.assertEqual((first.kind, first.packet_id), (DecisionKind.RUN, "vertical"))

        record_attempt(
            state,
            "vertical",
            success=False,
            fingerprint="diff-a:test-failed",
            failure_reason="verification failed",
        )

        second = next_decision(state, now=102.0)
        self.assertEqual((second.kind, second.packet_id), (DecisionKind.RUN, "independent"))
        self.assertEqual(state.queue[:2], ["independent", "vertical"])

    def test_repeated_fingerprint_parks_without_a_third_attempt(self) -> None:
        state = make_state()
        record_attempt(state, "vertical", success=False, fingerprint="same")
        record_attempt(state, "vertical", success=False, fingerprint="same")

        packet = state.packet("vertical")
        self.assertEqual(packet.status, PacketStatus.PARKED)
        self.assertEqual(packet.attempts, 2)

    def test_second_distinct_failure_also_parks(self) -> None:
        state = make_state()
        record_attempt(state, "vertical", success=False, fingerprint="first")
        record_attempt(state, "vertical", success=False, fingerprint="second")

        self.assertEqual(state.packet("vertical").status, PacketStatus.PARKED)

    def test_accepted_dependencies_unlock_the_next_wave(self) -> None:
        state = make_state()
        record_attempt(state, "vertical", success=True, fingerprint="vertical-green")
        record_attempt(state, "independent", success=True, fingerprint="independent-green")

        decision = next_decision(state, now=103.0)
        self.assertEqual((decision.kind, decision.packet_id), (DecisionKind.RUN, "integrate"))

    def test_stop_and_budgets_are_checked_before_scheduling(self) -> None:
        state = make_state()
        state.stop_requested = True
        self.assertEqual(next_decision(state, now=101.0).kind, DecisionKind.STOP)

        state.stop_requested = False
        state.model_calls = state.budget.max_model_calls
        self.assertEqual(next_decision(state, now=101.0).kind, DecisionKind.STOP)

        state.model_calls = 0
        self.assertEqual(next_decision(state, now=1000.0).kind, DecisionKind.STOP)


if __name__ == "__main__":
    unittest.main()
