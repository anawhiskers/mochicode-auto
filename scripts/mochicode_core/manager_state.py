from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from typing import Any

from .child_receipts import path_matches, validate_child_receipt
from .evidence import EvidenceLedger
from .state import exclusive_file_lock


STATE_FILE = "manager-state.json"
EVIDENCE_FILE = "manager-evidence.jsonl"
TRANSACTION_FILE = ".manager-transaction.json"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
AUTOMATIC_MANAGER_PROMOTED = False
CLASSIFIER_FIELDS = frozenset(
    {
        "request_kind",
        "explicit_trigger",
        "phase_count",
        "production_file_count",
        "component_count",
        "wave_one_vertical_slice",
        "phase_oracles_complete",
        "final_oracle_exists",
        "decisions_frozen",
        "single_sequential_writer",
        "child_capabilities_confirmed",
        "parallel_advantage_proven",
        "requires_heavy_controller",
    }
)
PHASE_FIELDS = frozenset(
    {
        "id",
        "title",
        "wave",
        "priority",
        "vertical_slice",
        "dependencies",
        "acceptance_criteria",
        "owned_paths",
    }
)
PARENT_VERIFICATION_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "verifier_id",
        "base_revision",
        "verified_revision",
        "acceptance_evidence",
        "commands",
        "protected_unchanged",
        "changed_paths",
        "stop_reason",
    }
)
MAX_PHASES = 12
MAX_ATTEMPTS = 2


class ManagerStateError(ValueError):
    pass


def classify_manager_route(facts: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(facts, dict) or set(facts) != CLASSIFIER_FIELDS:
        raise ManagerStateError("manager classifier facts have invalid fields")
    request_kind = facts["request_kind"]
    if request_kind not in {"implementation", "diagnosis", "review", "research", "planning"}:
        raise ManagerStateError("manager request kind is invalid")
    for field in CLASSIFIER_FIELDS - {
        "request_kind",
        "phase_count",
        "production_file_count",
        "component_count",
    }:
        if type(facts[field]) is not bool:
            raise ManagerStateError(f"manager classifier field {field} must be boolean")
    for field in ("phase_count", "production_file_count", "component_count"):
        if type(facts[field]) is not int or facts[field] < 0:
            raise ManagerStateError(f"manager classifier field {field} must be nonnegative")
    base_safety = (
        request_kind == "implementation"
        and facts["single_sequential_writer"]
        and facts["child_capabilities_confirmed"]
        and not facts["requires_heavy_controller"]
    )
    explicit = facts["explicit_trigger"] and base_safety
    criteria = {
        "implementation_request": request_kind == "implementation",
        "phase_count_3_to_6": 3 <= facts["phase_count"] <= 6,
        "wave_one_vertical_slice": facts["wave_one_vertical_slice"],
        "at_least_6_production_files": facts["production_file_count"] >= 6,
        "at_least_2_components": facts["component_count"] >= 2,
        "phase_oracles_complete": facts["phase_oracles_complete"],
        "final_oracle_exists": facts["final_oracle_exists"],
        "decisions_frozen": facts["decisions_frozen"],
        "single_sequential_writer": facts["single_sequential_writer"],
        "child_capabilities_confirmed": facts["child_capabilities_confirmed"],
        "parallel_fanout_rejected": not facts["parallel_advantage_proven"],
        "heavy_controller_not_required": not facts["requires_heavy_controller"],
    }
    automatic_candidate = all(criteria.values())
    if explicit:
        route = "manager_explicit"
    elif automatic_candidate and AUTOMATIC_MANAGER_PROMOTED:
        route = "manager_automatic"
    else:
        route = "direct_sol"
    return {
        "route": route,
        "explicit": explicit,
        "automatic_candidate": automatic_candidate,
        "automatic_promoted": AUTOMATIC_MANAGER_PROMOTED,
        "shadow_route": "manager_automatic" if automatic_candidate else "direct_sol",
        "criteria": criteria,
    }


def initialize_manager_run(
    run_root: Path,
    *,
    run_id: str,
    goal_hash: str,
    source_revision: str,
    decision_hash: str,
    activation: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(_lock_path(root)):
        if root.exists() and any(root.iterdir()):
            raise ManagerStateError("manager run root must be new or empty")
        if SAFE_ID.fullmatch(run_id) is None:
            raise ManagerStateError("manager run id is invalid")
        if HASH.fullmatch(goal_hash) is None:
            raise ManagerStateError("manager goal hash must be lowercase SHA-256")
        if REVISION.fullmatch(source_revision) is None:
            raise ManagerStateError("manager source revision is invalid")
        if HASH.fullmatch(decision_hash) is None:
            raise ManagerStateError("manager decision hash must be lowercase SHA-256")
        if (
            not isinstance(activation, dict)
            or set(activation) != {"mode", "criteria"}
            or activation["mode"] not in {"explicit", "automatic"}
            or not isinstance(activation["criteria"], list)
            or not activation["criteria"]
            or not all(isinstance(item, str) and item for item in activation["criteria"])
        ):
            raise ManagerStateError("manager activation evidence is invalid")
        if activation["mode"] == "automatic" and not AUTOMATIC_MANAGER_PROMOTED:
            raise ManagerStateError("automatic Manager Mode is shadow-only and cannot initialize")
        phases = _validate_plan(plan)
        plan_hash = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "goal_hash": goal_hash,
            "source_revision": source_revision,
            "decision_hash": decision_hash,
            "plan_hash": plan_hash,
            "activation": activation,
            "status": "active",
            "stop_requested": False,
            "replans": 0,
            "current_phase": None,
            "active_writer_id": None,
            "implementer_thread_id": None,
            "replacement_implementers": 0,
            "usage": {
                "model_calls": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "tool_calls": 0,
                "duration_ms": 0,
                "repairs": 0,
                "child_count": 0
            },
            "created_at": now,
            "updated_at": now,
            "phases": [
                {
                    **phase,
                    "status": "pending",
                    "attempts": 0,
                    "fingerprints": [],
                    "started_revision": None,
                    "accepted_revision": None,
                    "child_receipt_hash": None,
                    "child_receipt_path": None,
                    "verification_receipt_hash": None,
                    "verification_receipt_path": None,
                    "last_failure": None,
                }
                for phase in phases
            ],
        }
        _commit_transition(root, state, "manager_initialized", phase_id=None)
        return state


def load_manager_state(run_root: Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with exclusive_file_lock(_lock_path(root)):
        return _load_manager_state_unlocked(root)


def _load_manager_state_unlocked(root: Path) -> dict[str, Any]:
    _recover_pending_transition(root)
    path = root / STATE_FILE
    if not path.is_file():
        raise ManagerStateError(f"manager state is missing: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManagerStateError("manager state is unreadable") from error
    _validate_state(state)
    ledger = EvidenceLedger(root / EVIDENCE_FILE)
    ledger_ok, reason = ledger.verify()
    if not ledger_ok:
        raise ManagerStateError(f"manager evidence is invalid: {reason}")
    records = ledger.records()
    state_hash = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if not records or records[-1].get("state_hash") != state_hash:
        raise ManagerStateError("manager state does not match its latest evidence")
    _verify_stored_receipts(root, state)
    return state


def next_ready_phase(state: dict[str, Any]) -> dict[str, Any] | None:
    _validate_state(state)
    if state["status"] != "active" or state["stop_requested"]:
        return None
    if state["current_phase"] is not None:
        return _phase(state, state["current_phase"])
    accepted = {
        phase["id"] for phase in state["phases"] if phase["status"] == "accepted"
    }
    ready = [
        phase
        for phase in state["phases"]
        if phase["status"] == "pending"
        and set(phase["dependencies"]) <= accepted
    ]
    if not ready:
        return None
    ready.sort(
        key=lambda phase: (
            phase["attempts"],
            phase["wave"],
            phase["priority"],
            phase["id"],
        )
    )
    return ready[0]


def start_phase(
    run_root: Path,
    phase_id: str,
    *,
    writer_id: str,
    implementer_thread_id: str,
    source_revision: str,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with exclusive_file_lock(_lock_path(root)):
        return _start_phase_unlocked(
            root,
            phase_id,
            writer_id=writer_id,
            implementer_thread_id=implementer_thread_id,
            source_revision=source_revision,
        )


def _start_phase_unlocked(
    root: Path,
    phase_id: str,
    *,
    writer_id: str,
    implementer_thread_id: str,
    source_revision: str,
) -> dict[str, Any]:
    state = _load_manager_state_unlocked(root)
    if state["stop_requested"] or state["status"] != "active":
        raise ManagerStateError("manager run is not accepting phase starts")
    if state["current_phase"] is not None:
        raise ManagerStateError("another manager phase is already active")
    expected = next_ready_phase(state)
    if expected is None or expected["id"] != phase_id:
        raise ManagerStateError("phase is not the next breadth-first ready phase")
    phase = _phase(state, phase_id)
    if phase["attempts"] >= MAX_ATTEMPTS:
        raise ManagerStateError("phase attempt budget is exhausted")
    if SAFE_ID.fullmatch(writer_id) is None or SAFE_ID.fullmatch(implementer_thread_id) is None:
        raise ManagerStateError("manager implementer identity is invalid")
    if REVISION.fullmatch(source_revision) is None:
        raise ManagerStateError("manager phase source revision is invalid")
    if source_revision != state["source_revision"]:
        raise ManagerStateError("manager phase source revision does not match accepted state")
    known_thread = state["implementer_thread_id"]
    if known_thread is not None and known_thread != implementer_thread_id:
        raise ManagerStateError("manager phase must reuse the same implementer")
    if known_thread is None:
        state["implementer_thread_id"] = implementer_thread_id
        state["usage"]["child_count"] += 1
    phase["attempts"] += 1
    phase["status"] = "active"
    phase["started_revision"] = source_revision
    state["current_phase"] = phase_id
    state["active_writer_id"] = writer_id
    state["updated_at"] = time.time()
    _commit_transition(root, state, "phase_started", phase_id=phase_id)
    return state


def finish_phase(
    run_root: Path,
    phase_id: str,
    *,
    result: str,
    receipt_path: Path | None = None,
    verification_path: Path | None = None,
    fingerprint: str | None = None,
    current_revision: str | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with exclusive_file_lock(_lock_path(root)):
        return _finish_phase_unlocked(
            root,
            phase_id,
            result=result,
            receipt_path=receipt_path,
            verification_path=verification_path,
            fingerprint=fingerprint,
            current_revision=current_revision,
        )


def _finish_phase_unlocked(
    root: Path,
    phase_id: str,
    *,
    result: str,
    receipt_path: Path | None,
    verification_path: Path | None,
    fingerprint: str | None,
    current_revision: str | None,
) -> dict[str, Any]:
    state = _load_manager_state_unlocked(root)
    if state["current_phase"] != phase_id:
        raise ManagerStateError("only the active manager phase can finish")
    phase = _phase(state, phase_id)
    if result == "accepted":
        if receipt_path is None or verification_path is None:
            raise ManagerStateError("accepted manager phase requires child and parent receipts")
        receipt, receipt_bytes = _read_receipt_snapshot(receipt_path)
        verification, verification_bytes = _read_receipt_snapshot(verification_path)
        _validate_parent_verification(
            verification,
            phase=phase,
            active_writer_id=state["active_writer_id"],
            implementer_thread_id=state["implementer_thread_id"],
        )
        _validate_manager_child_receipt(
            receipt,
            phase=phase,
            implementer_thread_id=state["implementer_thread_id"],
            verified_revision=verification["verified_revision"],
        )
        child_hash = hashlib.sha256(receipt_bytes).hexdigest()
        verification_hash = hashlib.sha256(verification_bytes).hexdigest()
        if any(
            existing.get("child_receipt_hash") == child_hash
            for existing in state["phases"]
            if existing["id"] != phase_id
        ):
            raise ManagerStateError("manager child receipt was already used by another phase")
        receipt_root = root / "receipts" / phase_id
        if not receipt_root.resolve().is_relative_to(root):
            raise ManagerStateError("manager receipt directory escapes the run root")
        receipt_root.mkdir(parents=True, exist_ok=True)
        trusted_child = receipt_root / f"child-{child_hash}.json"
        trusted_verification = receipt_root / f"parent-{verification_hash}.json"
        _write_trusted_receipt(trusted_child, receipt_bytes)
        _write_trusted_receipt(trusted_verification, verification_bytes)
        phase["child_receipt_hash"] = child_hash
        phase["child_receipt_path"] = trusted_child.relative_to(root).as_posix()
        phase["verification_receipt_hash"] = verification_hash
        phase["verification_receipt_path"] = trusted_verification.relative_to(root).as_posix()
        phase["accepted_revision"] = verification["verified_revision"]
        state["source_revision"] = verification["verified_revision"]
        telemetry = receipt["telemetry"]
        state["usage"]["model_calls"] += 1
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "tool_calls",
            "duration_ms",
        ):
            state["usage"][field] += telemetry[field]
        state["usage"]["repairs"] += telemetry["retry_count"]
        phase["status"] = "accepted"
        phase["last_failure"] = None
        event = "phase_accepted"
    elif result == "failed":
        if fingerprint is None or HASH.fullmatch(fingerprint) is None:
            raise ManagerStateError("failed manager phase requires a SHA-256 fingerprint")
        if current_revision != phase["started_revision"]:
            raise ManagerStateError("failed manager phase must restore its starting revision")
        repeated = fingerprint in phase["fingerprints"]
        phase["fingerprints"].append(fingerprint)
        phase["last_failure"] = fingerprint
        if repeated or phase["attempts"] >= MAX_ATTEMPTS:
            phase["status"] = "parked"
            event = "phase_parked"
        else:
            phase["status"] = "pending"
            event = "phase_rotated"
    else:
        raise ManagerStateError("manager phase result must be accepted or failed")
    state["current_phase"] = None
    state["active_writer_id"] = None
    state["updated_at"] = time.time()
    _refresh_run_status(state)
    _commit_transition(root, state, event, phase_id=phase_id)
    return state


def request_manager_stop(run_root: Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with exclusive_file_lock(_lock_path(root)):
        state = _load_manager_state_unlocked(root)
        return _request_manager_stop_unlocked(root, state)


def _request_manager_stop_unlocked(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state["status"] in {"complete", "stopped"}:
        return state
    state["stop_requested"] = True
    state["status"] = "stopped"
    state["updated_at"] = time.time()
    _commit_transition(root, state, "manager_stopped", phase_id=state["current_phase"])
    return state


def resume_manager(run_root: Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with exclusive_file_lock(_lock_path(root)):
        state = _load_manager_state_unlocked(root)
        return _resume_manager_unlocked(root, state)


def _resume_manager_unlocked(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state["status"] != "stopped":
        raise ManagerStateError("only a stopped manager run can resume")
    state["stop_requested"] = False
    _refresh_run_status(state)
    state["updated_at"] = time.time()
    _commit_transition(root, state, "manager_resumed", phase_id=state["current_phase"])
    return state


def _refresh_run_status(state: dict[str, Any]) -> None:
    if all(phase["status"] == "accepted" for phase in state["phases"]):
        state["status"] = "complete"
        state["stop_requested"] = False
    elif state["stop_requested"]:
        state["status"] = "stopped"
    else:
        state["status"] = "active"
        if next_ready_phase(state) is None:
            state["status"] = "needs_replan"


def apply_manager_replan(
    run_root: Path,
    plan: dict[str, Any],
    *,
    decision_hash: str,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with exclusive_file_lock(_lock_path(root)):
        state = _load_manager_state_unlocked(root)
        if state["status"] != "needs_replan" or state["current_phase"] is not None:
            raise ManagerStateError("manager replan requires an exhausted ready queue")
        if state["replans"] >= 1:
            raise ManagerStateError("manager replan budget is exhausted")
        if HASH.fullmatch(decision_hash) is None:
            raise ManagerStateError("manager replan decision hash is invalid")
        new_phases = _validate_plan(plan)
        new_by_id = {phase["id"]: phase for phase in new_phases}
        accepted = {
            phase["id"]: phase
            for phase in state["phases"]
            if phase["status"] == "accepted"
        }
        plan_keys = PHASE_FIELDS
        for phase_id, old in accepted.items():
            candidate = new_by_id.get(phase_id)
            if candidate is None or any(candidate[key] != old[key] for key in plan_keys):
                raise ManagerStateError("manager replan must preserve every accepted phase exactly")
        state["phases"] = [
            accepted.get(
                phase["id"],
                {
                    **phase,
                    "status": "pending",
                    "attempts": 0,
                    "fingerprints": [],
                    "started_revision": None,
                    "accepted_revision": None,
                    "child_receipt_hash": None,
                    "child_receipt_path": None,
                    "verification_receipt_hash": None,
                    "verification_receipt_path": None,
                    "last_failure": None,
                },
            )
            for phase in new_phases
        ]
        state["replans"] += 1
        state["decision_hash"] = decision_hash
        state["plan_hash"] = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        state["status"] = "active"
        state["updated_at"] = time.time()
        _commit_transition(root, state, "manager_replanned", phase_id=None)
        return state


def manager_status(run_root: Path) -> dict[str, Any]:
    state = load_manager_state(run_root)
    ready = next_ready_phase(state)
    counts: dict[str, int] = {}
    for phase in state["phases"]:
        counts[phase["status"]] = counts.get(phase["status"], 0) + 1
    return {
        "run_id": state["run_id"],
        "status": state["status"],
        "stop_requested": state["stop_requested"],
        "activation": state["activation"],
        "replans": state["replans"],
        "implementer_thread_id": state["implementer_thread_id"],
        "active_writer_id": state["active_writer_id"],
        "current_phase": state["current_phase"],
        "next_phase": None if ready is None else ready["id"],
        "counts": counts,
        "usage": state["usage"],
        "usage_scope": "accepted_phase_receipts_only",
        "phases": state["phases"],
    }


def _validate_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(plan, dict) or set(plan) != {"phases"}:
        raise ManagerStateError("manager plan must contain only phases")
    raw_phases = plan["phases"]
    if not isinstance(raw_phases, list) or not 3 <= len(raw_phases) <= MAX_PHASES:
        raise ManagerStateError("manager plan requires 3 to 12 phases")
    phases: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_phases):
        if not isinstance(raw, dict) or set(raw) != PHASE_FIELDS:
            raise ManagerStateError(f"manager phase {index} has invalid fields")
        phase_id = _safe_string(raw["id"], f"phase {index} id", identifier=True)
        if phase_id in ids:
            raise ManagerStateError("manager phase ids must be unique")
        ids.add(phase_id)
        title = _safe_string(raw["title"], f"phase {phase_id} title")
        wave = _positive_int(raw["wave"], f"phase {phase_id} wave")
        priority = _positive_int(raw["priority"], f"phase {phase_id} priority")
        if type(raw["vertical_slice"]) is not bool:
            raise ManagerStateError("manager vertical_slice must be boolean")
        dependencies = _string_list(raw["dependencies"], "dependencies", identifiers=True)
        criteria = _string_list(raw["acceptance_criteria"], "acceptance_criteria")
        paths = _string_list(raw["owned_paths"], "owned_paths")
        if not criteria or not paths:
            raise ManagerStateError("manager phases require criteria and owned paths")
        normalized_paths = [_safe_pattern(path) for path in paths]
        phases.append(
            {
                "id": phase_id,
                "title": title,
                "wave": wave,
                "priority": priority,
                "vertical_slice": raw["vertical_slice"],
                "dependencies": dependencies,
                "acceptance_criteria": criteria,
                "owned_paths": normalized_paths,
            }
        )
    if not any(phase["vertical_slice"] and phase["wave"] == 1 for phase in phases):
        raise ManagerStateError("manager plan requires a wave-one vertical slice")
    for phase in phases:
        if phase["id"] in phase["dependencies"] or not set(phase["dependencies"]) <= ids:
            raise ManagerStateError("manager dependencies are invalid")
    _require_acyclic(phases)
    _require_sequential_overlap(phases)
    return phases


def _validate_state(state: Any) -> None:
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ManagerStateError("manager state schema is invalid")
    if state.get("status") not in {"active", "stopped", "needs_replan", "complete"}:
        raise ManagerStateError("manager state status is invalid")
    if type(state.get("stop_requested")) is not bool:
        raise ManagerStateError("manager stop state is invalid")
    if (
        SAFE_ID.fullmatch(str(state.get("run_id", ""))) is None
        or HASH.fullmatch(str(state.get("goal_hash", ""))) is None
        or HASH.fullmatch(str(state.get("decision_hash", ""))) is None
        or HASH.fullmatch(str(state.get("plan_hash", ""))) is None
        or REVISION.fullmatch(str(state.get("source_revision", ""))) is None
        or type(state.get("replans")) is not int
        or not 0 <= state["replans"] <= 1
    ):
        raise ManagerStateError("manager state identity or budget is invalid")
    activation = state.get("activation")
    if (
        not isinstance(activation, dict)
        or activation.get("mode") not in {"explicit", "automatic"}
        or not isinstance(activation.get("criteria"), list)
        or not activation["criteria"]
    ):
        raise ManagerStateError("manager activation state is invalid")
    usage = state.get("usage")
    expected_usage = {
        "model_calls",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "tool_calls",
        "duration_ms",
        "repairs",
        "child_count",
    }
    if (
        not isinstance(usage, dict)
        or set(usage) != expected_usage
        or any(type(value) is not int or value < 0 for value in usage.values())
    ):
        raise ManagerStateError("manager usage state is invalid")
    if not isinstance(state.get("phases"), list):
        raise ManagerStateError("manager phases are invalid")
    ids = {phase.get("id") for phase in state["phases"] if isinstance(phase, dict)}
    if len(ids) != len(state["phases"]):
        raise ManagerStateError("manager state phase ids are invalid")
    active = [phase for phase in state["phases"] if phase.get("status") == "active"]
    for phase in state["phases"]:
        if (
            phase.get("status") not in {"pending", "active", "accepted", "parked"}
            or type(phase.get("attempts")) is not int
            or not 0 <= phase["attempts"] <= MAX_ATTEMPTS
            or not isinstance(phase.get("fingerprints"), list)
            or any(HASH.fullmatch(str(value)) is None for value in phase["fingerprints"])
        ):
            raise ManagerStateError("manager phase runtime state is invalid")
    if len(active) > 1:
        raise ManagerStateError("manager state has multiple active phases")
    current = state.get("current_phase")
    if current is None and active:
        raise ManagerStateError("manager current phase is missing")
    if current is not None and (len(active) != 1 or active[0].get("id") != current):
        raise ManagerStateError("manager current phase does not match active state")
    writer = state.get("active_writer_id")
    if (current is None) != (writer is None):
        raise ManagerStateError("manager active writer does not match current phase")
    if writer is not None and SAFE_ID.fullmatch(str(writer)) is None:
        raise ManagerStateError("manager active writer identity is invalid")
    implementer = state.get("implementer_thread_id")
    if implementer is not None and SAFE_ID.fullmatch(str(implementer)) is None:
        raise ManagerStateError("manager implementer thread identity is invalid")


def _phase(state: dict[str, Any], phase_id: str) -> dict[str, Any]:
    for phase in state["phases"]:
        if phase["id"] == phase_id:
            return phase
    raise ManagerStateError(f"unknown manager phase: {phase_id}")


def _write_state(root: Path, state: dict[str, Any]) -> None:
    _validate_state(state)
    target = root / STATE_FILE
    _atomic_write(
        target,
        (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}.manager.lock"


def _commit_transition(
    root: Path,
    state: dict[str, Any],
    event: str,
    *,
    phase_id: str | None,
) -> None:
    state_hash = _state_hash(state)
    ledger = EvidenceLedger(root / EVIDENCE_FILE)
    ok, reason = ledger.verify()
    if not ok:
        raise ManagerStateError(f"manager evidence is invalid before transaction: {reason}")
    records = ledger.records()
    transaction = {
        "schema_version": 2,
        "previous_record_hash": records[-1]["record_hash"] if records else None,
        "event": event,
        "phase_id": phase_id,
        "state_hash": state_hash,
        "state": state,
    }
    transaction_path = root / TRANSACTION_FILE
    _atomic_write(
        transaction_path,
        (json.dumps(transaction, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    _append_event_atomic(root, state, event, phase_id=phase_id)
    _write_state(root, state)
    transaction_path.unlink(missing_ok=True)


def _recover_pending_transition(root: Path) -> None:
    transaction_path = root / TRANSACTION_FILE
    if not transaction_path.is_file():
        return
    try:
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManagerStateError("manager pending transaction is unreadable") from error
    if (
        not isinstance(transaction, dict)
        or set(transaction) != {"schema_version", "event", "phase_id", "state_hash", "state", "previous_record_hash"}
        or transaction.get("schema_version") != 2
        or not isinstance(transaction.get("event"), str)
        or not transaction["event"]
        or (
            transaction.get("phase_id") is not None
            and not isinstance(transaction["phase_id"], str)
        )
        or not isinstance(transaction.get("state"), dict)
    ):
        raise ManagerStateError("manager pending transaction is invalid")
    state = transaction["state"]
    _validate_state(state)
    if transaction.get("state_hash") != _state_hash(state):
        raise ManagerStateError("manager pending transaction hash is invalid")
    ledger = EvidenceLedger(root / EVIDENCE_FILE)
    ledger_ok, reason = ledger.verify()
    if not ledger_ok:
        raise ManagerStateError(f"manager evidence is invalid during recovery: {reason}")
    records = ledger.records()
    matching_latest = bool(records and records[-1].get("state_hash") == transaction["state_hash"])
    expected_previous = transaction["previous_record_hash"]
    actual_previous = (
        records[-1].get("previous_hash") if matching_latest
        else records[-1]["record_hash"] if records else None
    )
    if expected_previous != actual_previous:
        raise ManagerStateError("manager pending transaction does not extend the current evidence")
    _verify_stored_receipts(root, state)
    if not matching_latest:
        if any(record.get("state_hash") == transaction["state_hash"] for record in records):
            raise ManagerStateError("manager pending transaction is stale")
        _append_event_atomic(
            root,
            state,
            transaction["event"],
            phase_id=transaction["phase_id"],
        )
    _write_state(root, state)
    transaction_path.unlink(missing_ok=True)


def _append_event_atomic(
    root: Path,
    state: dict[str, Any],
    event: str,
    *,
    phase_id: str | None,
) -> None:
    path = root / EVIDENCE_FILE
    ledger = EvidenceLedger(path)
    ledger_ok, reason = ledger.verify()
    if not ledger_ok:
        raise ManagerStateError(f"manager evidence is invalid before append: {reason}")
    records = list(ledger.records())
    stored: dict[str, Any] = {
        "event": event,
        "run_id": state["run_id"],
        "phase_id": phase_id,
        "state_status": state["status"],
        "state_hash": _state_hash(state),
        "created_at": time.time(),
        "seq": len(records) + 1,
        "previous_hash": records[-1]["record_hash"] if records else None,
    }
    stored["record_hash"] = hashlib.sha256(
        json.dumps(stored, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    records.append(stored)
    content = "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")
    _atomic_write(path, content)


def _state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        for attempt in range(3):
            try:
                os.replace(temporary_path, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_receipt(path: Path) -> dict[str, Any]:
    return _read_receipt_snapshot(path)[0]


def _read_receipt_snapshot(path: Path) -> tuple[dict[str, Any], bytes]:
    resolved = Path(path).resolve()
    try:
        with resolved.open("rb") as handle:
            content = handle.read(1_000_001)
        if len(content) > 1_000_000:
            raise ManagerStateError("manager receipt is oversized")
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManagerStateError("manager receipt is not valid JSON") from error
    if not isinstance(value, dict):
        raise ManagerStateError("manager receipt must be an object")
    return value, content


def _verify_stored_receipts(root: Path, state: dict[str, Any]) -> None:
    for phase in state["phases"]:
        if phase["status"] != "accepted":
            continue
        for kind, prefix in (("child", "child"), ("verification", "parent")):
            digest = phase.get(f"{kind}_receipt_hash")
            relative = phase.get(f"{kind}_receipt_path")
            expected = f"receipts/{phase['id']}/{prefix}-{digest}.json"
            if not isinstance(digest, str) or HASH.fullmatch(digest) is None or relative != expected:
                raise ManagerStateError("manager stored receipt binding is invalid")
            path = root / relative
            if not path.resolve().is_relative_to(root.resolve()):
                raise ManagerStateError("manager stored receipt escapes the run root")
            _, content = _read_receipt_snapshot(path)
            if hashlib.sha256(content).hexdigest() != digest:
                raise ManagerStateError("manager stored receipt hash mismatch")


def _write_trusted_receipt(path: Path, content: bytes) -> None:
    if path.exists() and path.read_bytes() != content:
        raise ManagerStateError("manager receipt hash collision")
    if not path.exists():
        path.write_bytes(content)


def _validate_manager_child_receipt(
    receipt: dict[str, Any],
    *,
    phase: dict[str, Any],
    implementer_thread_id: str | None,
    verified_revision: str,
) -> None:
    identity_fields = {
        "phase_id",
        "implementer_thread_id",
        "base_revision",
        "result_revision",
    }
    if not identity_fields <= set(receipt):
        raise ManagerStateError("manager child receipt is missing phase identity")
    generic = {key: value for key, value in receipt.items() if key not in identity_fields}
    validate_child_receipt(
        generic,
        allowed_paths=tuple(phase["owned_paths"]),
        required_criteria=tuple(phase["acceptance_criteria"]),
    )
    if (
        receipt.get("status") != "COMPLETED"
        or receipt.get("role") != "manager_implementer"
        or receipt.get("model") != "gpt-5.6-sol"
        or receipt.get("effort") != "high"
        or receipt.get("phase_id") != phase["id"]
        or receipt.get("implementer_thread_id") != implementer_thread_id
        or receipt.get("base_revision") != phase["started_revision"]
        or receipt.get("result_revision") != verified_revision
        or not isinstance(receipt.get("result_revision"), str)
        or REVISION.fullmatch(receipt["result_revision"]) is None
    ):
        raise ManagerStateError("manager child receipt identity or result is invalid")


def _validate_parent_verification(
    receipt: dict[str, Any],
    *,
    phase: dict[str, Any],
    active_writer_id: str,
    implementer_thread_id: str | None,
) -> None:
    if (set(receipt) != PARENT_VERIFICATION_FIELDS
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1):
        raise ManagerStateError("manager parent verification fields are invalid")
    verifier_id = receipt.get("verifier_id")
    if (
        not isinstance(verifier_id, str)
        or SAFE_ID.fullmatch(verifier_id) is None
        or verifier_id == active_writer_id
        or verifier_id == implementer_thread_id
    ):
        raise ManagerStateError("manager parent verifier must be independent from the writer")
    if (
        receipt.get("phase_id") != phase["id"]
        or receipt.get("base_revision") != phase["started_revision"]
        or not isinstance(receipt.get("verified_revision"), str)
        or REVISION.fullmatch(receipt["verified_revision"]) is None
        or receipt["verified_revision"] == phase["started_revision"]
        or receipt.get("protected_unchanged") is not True
        or receipt.get("stop_reason") != "completed"
    ):
        raise ManagerStateError("manager parent verification identity or result is invalid")
    evidence = receipt.get("acceptance_evidence")
    if not isinstance(evidence, list):
        raise ManagerStateError("manager parent acceptance evidence is invalid")
    criterion_ids: list[str] = []
    for item in evidence:
        if (
            not isinstance(item, dict)
            or set(item) != {"criterion_id", "status", "evidence"}
            or item.get("status") != "PASS"
            or not isinstance(item.get("criterion_id"), str)
            or not isinstance(item.get("evidence"), str)
            or not item["evidence"].strip()
        ):
            raise ManagerStateError("manager parent acceptance evidence is invalid")
        criterion_ids.append(item["criterion_id"])
    if set(criterion_ids) != set(phase["acceptance_criteria"]) or len(criterion_ids) != len(set(criterion_ids)):
        raise ManagerStateError("manager parent verification does not cover every criterion")
    commands = receipt.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ManagerStateError("manager parent verification requires commands")
    for command in commands:
        if (
            not isinstance(command, dict)
            or set(command) != {"argv", "exit_code"}
            or not isinstance(command.get("argv"), list)
            or not command["argv"]
            or not all(isinstance(item, str) and item for item in command["argv"])
            or type(command.get("exit_code")) is not int
            or command["exit_code"] != 0
        ):
            raise ManagerStateError("manager parent verification command failed or is invalid")
    changed_paths = receipt.get("changed_paths")
    if not isinstance(changed_paths, list) or not changed_paths:
        raise ManagerStateError("manager parent verification requires changed paths")
    normalized = [_safe_pattern(item) for item in changed_paths]
    if len(normalized) != len(set(normalized)):
        raise ManagerStateError("manager parent verification repeats changed paths")
    for changed in normalized:
        if not any(path_matches(changed, pattern) for pattern in phase["owned_paths"]):
            raise ManagerStateError(f"manager parent verification contains an unowned path: {changed}")


def _safe_string(value: Any, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ManagerStateError(f"manager {label} is invalid")
    normalized = value.strip()
    if identifier and SAFE_ID.fullmatch(normalized) is None:
        raise ManagerStateError(f"manager {label} is invalid")
    return normalized


def _string_list(value: Any, label: str, *, identifiers: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise ManagerStateError(f"manager {label} must be a bounded array")
    result = [
        _safe_string(item, label, identifier=identifiers)
        for item in value
    ]
    if len(set(result)) != len(result):
        raise ManagerStateError(f"manager {label} contains duplicates")
    return result


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ManagerStateError(f"manager {label} must be a bounded positive integer")
    return value


def _safe_pattern(value: str) -> str:
    if not isinstance(value, str):
        raise ManagerStateError("manager owned path must be a string")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or ":" in normalized
        or any(ord(character) < 32 for character in normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ManagerStateError(f"manager owned path is unsafe: {value}")
    return path.as_posix()


def _patterns_overlap(left: str, right: str) -> bool:
    def prefix(pattern: str) -> tuple[str, ...]:
        parts: list[str] = []
        for part in PurePosixPath(pattern).parts:
            if any(token in part for token in "*?["):
                break
            parts.append(part)
        return tuple(parts)

    left_prefix = prefix(left)
    right_prefix = prefix(right)
    if not left_prefix or not right_prefix:
        return True
    shorter = min(len(left_prefix), len(right_prefix))
    return left_prefix[:shorter] == right_prefix[:shorter]


def _require_sequential_overlap(phases: list[dict[str, Any]]) -> None:
    dependencies = {phase["id"]: set(phase["dependencies"]) for phase in phases}

    def depends_on(phase_id: str, possible_ancestor: str) -> bool:
        pending = list(dependencies[phase_id])
        seen: set[str] = set()
        while pending:
            item = pending.pop()
            if item == possible_ancestor:
                return True
            if item not in seen:
                seen.add(item)
                pending.extend(dependencies[item])
        return False

    for index, left in enumerate(phases):
        for right in phases[index + 1 :]:
            if any(
                _patterns_overlap(left_path, right_path)
                for left_path in left["owned_paths"]
                for right_path in right["owned_paths"]
            ) and not (
                depends_on(left["id"], right["id"])
                or depends_on(right["id"], left["id"])
            ):
                raise ManagerStateError(
                    "manager overlapping paths require an explicit dependency order"
                )


def _require_acyclic(phases: list[dict[str, Any]]) -> None:
    dependencies = {phase["id"]: set(phase["dependencies"]) for phase in phases}
    resolved: set[str] = set()
    while len(resolved) < len(phases):
        ready = {
            phase_id
            for phase_id, needs in dependencies.items()
            if phase_id not in resolved and needs <= resolved
        }
        if not ready:
            raise ManagerStateError("manager phase dependencies contain a cycle")
        resolved.update(ready)
