from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PacketStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    ALREADY_SATISFIED = "already_satisfied"
    REFUSED = "refused"
    FAILED = "failed"
    PARKED = "parked"
    BLOCKED = "blocked"


@dataclass(slots=True)
class PacketState:
    packet_id: str
    title: str
    wave: int
    goal: str = ""
    priority: int = 100
    dependencies: tuple[str, ...] = ()
    vertical_slice: bool = False
    acceptance_criteria: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    status: PacketStatus = PacketStatus.PENDING
    attempts: int = 0
    implementation_attempts: int = 0
    active_implementation_attempt: int | None = None
    fingerprints: list[str] = field(default_factory=list)
    last_failure: str | None = None


@dataclass(slots=True)
class RunBudget:
    max_model_calls: int = 24
    max_rounds: int = 16
    max_attempts_per_packet: int = 2
    max_wall_seconds: int = 7200


@dataclass(slots=True)
class RunState:
    run_id: str
    goal: str
    project_root: str
    packets: list[PacketState]
    queue: list[str]
    source_head: str = ""
    source_branch: str = ""
    integration_head: str = ""
    budget: RunBudget = field(default_factory=RunBudget)
    status: str = "running"
    model_calls: int = 0
    rounds: int = 0
    replans: int = 0
    stop_requested: bool = False
    started_at: float = 0.0
    updated_at: float = 0.0

    def packet(self, packet_id: str) -> PacketState:
        for packet in self.packets:
            if packet.packet_id == packet_id:
                return packet
        raise KeyError(packet_id)
