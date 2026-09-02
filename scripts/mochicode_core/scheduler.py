from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time

from .models import PacketStatus, RunState


class DecisionKind(str, Enum):
    RUN = "run"
    DONE = "done"
    STOP = "stop"
    BLOCKED = "blocked"
    REPLAN = "replan"


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    kind: DecisionKind
    packet_id: str | None = None
    reason: str = ""


def validate_plan(state: RunState) -> None:
    if not state.goal.strip():
        raise ValueError("goal must not be empty")
    if not state.packets:
        raise ValueError("plan must contain at least one packet")

    packet_ids = [packet.packet_id for packet in state.packets]
    if len(packet_ids) != len(set(packet_ids)):
        raise ValueError("packet ids must be unique")
    if set(state.queue) != set(packet_ids) or len(state.queue) != len(packet_ids):
        raise ValueError("queue must contain every packet exactly once")

    known = set(packet_ids)
    for packet in state.packets:
        if not packet.packet_id.strip():
            raise ValueError("packet id must not be empty")
        if packet.wave < 1:
            raise ValueError(f"packet {packet.packet_id!r} has an invalid wave")
        if not packet.acceptance_criteria:
            raise ValueError(f"packet {packet.packet_id!r} has no acceptance criteria")
        if not packet.verification_commands:
            raise ValueError(f"packet {packet.packet_id!r} has no verification commands")
        if packet.packet_id in packet.dependencies:
            raise ValueError(f"packet {packet.packet_id!r} depends on itself")
        missing = set(packet.dependencies) - known
        if missing:
            raise ValueError(
                f"packet {packet.packet_id!r} has unknown dependencies: {sorted(missing)}"
            )

    if not any(packet.wave == 1 and packet.vertical_slice for packet in state.packets):
        raise ValueError("wave one must contain a runnable vertical slice")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(packet_id: str) -> None:
        if packet_id in visiting:
            raise ValueError("packet dependencies contain a cycle")
        if packet_id in visited:
            return
        visiting.add(packet_id)
        for dependency in state.packet(packet_id).dependencies:
            visit(dependency)
        visiting.remove(packet_id)
        visited.add(packet_id)

    for packet_id in packet_ids:
        visit(packet_id)


def next_decision(state: RunState, *, now: float) -> SchedulingDecision:
    if state.stop_requested:
        state.status = "stopped"
        return SchedulingDecision(DecisionKind.STOP, reason="stop requested")
    if state.model_calls >= state.budget.max_model_calls:
        state.status = "budget_exhausted"
        return SchedulingDecision(DecisionKind.STOP, reason="model-call budget exhausted")
    if state.rounds >= state.budget.max_rounds:
        state.status = "budget_exhausted"
        return SchedulingDecision(DecisionKind.STOP, reason="round budget exhausted")
    if now - state.started_at >= state.budget.max_wall_seconds:
        state.status = "budget_exhausted"
        return SchedulingDecision(DecisionKind.STOP, reason="wall-clock budget exhausted")

    unfinished = [
        packet
        for packet in state.packets
        if packet.status
        not in {
            PacketStatus.ACCEPTED,
            PacketStatus.ALREADY_SATISFIED,
            PacketStatus.REFUSED,
            PacketStatus.PARKED,
            PacketStatus.BLOCKED,
        }
    ]
    if not unfinished:
        if all(
            packet.status in {PacketStatus.ACCEPTED, PacketStatus.ALREADY_SATISFIED}
            for packet in state.packets
        ):
            state.status = "complete"
            return SchedulingDecision(DecisionKind.DONE, reason="all packets accepted")
        state.status = "blocked"
        return SchedulingDecision(DecisionKind.BLOCKED, reason="no runnable packets remain")

    accepted = {
        packet.packet_id
        for packet in state.packets
        if packet.status in {PacketStatus.ACCEPTED, PacketStatus.ALREADY_SATISFIED}
    }
    for packet_id in tuple(state.queue):
        packet = state.packet(packet_id)
        if packet.status != PacketStatus.PENDING:
            continue
        if all(dependency in accepted for dependency in packet.dependencies):
            packet.status = PacketStatus.RUNNING
            state.status = "running"
            state.updated_at = now
            return SchedulingDecision(DecisionKind.RUN, packet.packet_id)

    if state.replans < 1:
        return SchedulingDecision(
            DecisionKind.REPLAN,
            reason="remaining packets are dependency-blocked",
        )
    state.status = "blocked"
    return SchedulingDecision(
        DecisionKind.BLOCKED,
        reason="remaining packets are dependency-blocked after replan",
    )


def record_attempt(
    state: RunState,
    packet_id: str,
    *,
    success: bool,
    fingerprint: str,
    failure_reason: str = "",
) -> None:
    packet = state.packet(packet_id)
    duplicate = bool(fingerprint) and fingerprint in packet.fingerprints
    packet.attempts += 1
    state.rounds += 1
    state.updated_at = time.time()
    if fingerprint:
        packet.fingerprints.append(fingerprint)
    if packet_id in state.queue:
        state.queue.remove(packet_id)

    if success:
        packet.status = PacketStatus.ACCEPTED
        packet.last_failure = None
        return

    packet.last_failure = failure_reason or "attempt failed"
    if duplicate or packet.attempts >= state.budget.max_attempts_per_packet:
        packet.status = PacketStatus.PARKED
        return

    packet.status = PacketStatus.PENDING
    insertion_index = len(state.queue)
    for index, queued_id in enumerate(state.queue):
        if state.packet(queued_id).wave > packet.wave:
            insertion_index = index
            break
    state.queue.insert(insertion_index, packet_id)
