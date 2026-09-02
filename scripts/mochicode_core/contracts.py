from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

from .models import PacketState, RunBudget, RunState
from .scheduler import validate_plan


class VerificationClass(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class ExecutionMode(str, Enum):
    IMPLEMENT = "implement"
    VERIFY_ONLY = "verify_only"


@dataclass(frozen=True, slots=True)
class PacketContract:
    packet_id: str
    goal: str
    execution_mode: ExecutionMode
    verification_class: VerificationClass
    acceptance_criteria: tuple[str, ...]
    baseline_argv: tuple[str, ...]
    final_argvs: tuple[tuple[str, ...], ...]
    expected_failure_codes: tuple[int, ...]
    protected_patterns: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    evidence_requirements: tuple[str, ...]


def plan_from_dict(
    data: dict[str, Any],
    *,
    run_id: str,
    goal: str,
    project_root: str,
    budget: RunBudget,
    started_at: float,
    source_head: str = "",
    source_branch: str = "",
    integration_head: str = "",
) -> RunState:
    raw_packets = data.get("packets")
    if not isinstance(raw_packets, list) or not raw_packets:
        raise ValueError("plan packets must be a non-empty list")
    if len(raw_packets) > 12:
        raise ValueError("plan may contain at most 12 packets")
    packets: list[PacketState] = []
    for raw in raw_packets:
        if not isinstance(raw, dict):
            raise ValueError("every plan packet must be an object")
        packet_id = str(raw.get("id", "")).strip()
        title = str(raw.get("title", "")).strip()
        packet_goal = str(raw.get("goal", "")).strip()
        criteria = _string_tuple(raw.get("acceptance_criteria"), "acceptance criteria")
        hints = _string_tuple(raw.get("verification_hints"), "verification hints")
        dependencies = _string_tuple(raw.get("dependencies", []), "dependencies", allow_empty=True)
        if not packet_id or not title or not packet_goal:
            raise ValueError("packet id, title, and goal must not be empty")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", packet_id) is None:
            raise ValueError(
                "packet id must be 1-64 letters, numbers, underscores, or hyphens"
            )
        packets.append(
            PacketState(
                packet_id=packet_id,
                title=title,
                wave=int(raw.get("wave", 0)),
                goal=packet_goal,
                priority=int(raw.get("priority", 100)),
                dependencies=dependencies,
                vertical_slice=bool(raw.get("vertical_slice", False)),
                acceptance_criteria=criteria,
                verification_commands=hints,
            )
        )
    packets.sort(key=lambda packet: (packet.wave, packet.priority, packet.packet_id))
    state = RunState(
        run_id=run_id,
        goal=goal,
        project_root=project_root,
        packets=packets,
        queue=[packet.packet_id for packet in packets],
        source_head=source_head,
        source_branch=source_branch,
        integration_head=integration_head,
        budget=budget,
        started_at=started_at,
        updated_at=started_at,
    )
    validate_plan(state)
    return state


def contract_from_dict(data: dict[str, Any], packet: PacketState) -> PacketContract:
    packet_id = str(data.get("packet_id", "")).strip()
    if packet_id != packet.packet_id:
        raise ValueError("contract packet id does not match the planned packet")
    criteria = _string_tuple(data.get("acceptance_criteria"), "acceptance criteria")
    if not set(packet.acceptance_criteria).issubset(criteria):
        raise ValueError("Terra contract cannot remove Sol acceptance criteria")
    baseline = _argv(data.get("baseline_argv"), "baseline_argv")
    raw_final = data.get("final_argvs")
    if not isinstance(raw_final, list) or not raw_final:
        raise ValueError("final_argvs must contain at least one argument array")
    final_argvs = tuple(_argv(value, "final_argvs") for value in raw_final)
    if baseline not in final_argvs:
        raise ValueError("the baseline command must also run during final verification")
    expected_codes_raw = data.get("expected_failure_codes", [1])
    if not isinstance(expected_codes_raw, list):
        raise ValueError("expected_failure_codes must be a list")
    expected_codes = tuple(int(value) for value in expected_codes_raw)
    if any(value in {0, 4, 5} or value < 0 for value in expected_codes):
        raise ValueError("expected failure codes cannot include success, usage, or empty collection")
    raw_execution_mode = data.get("execution_mode")
    if raw_execution_mode is None:
        execution_mode = (
            ExecutionMode.VERIFY_ONLY if not expected_codes else ExecutionMode.IMPLEMENT
        )
    else:
        try:
            execution_mode = ExecutionMode(str(raw_execution_mode))
        except ValueError as error:
            raise ValueError("execution_mode must be implement or verify_only") from error
    if execution_mode == ExecutionMode.IMPLEMENT and not expected_codes:
        raise ValueError("implementation contracts require an expected failing exit code")
    if execution_mode == ExecutionMode.VERIFY_ONLY and expected_codes:
        raise ValueError("verify-only contracts must expect an already-green baseline")
    protected = _string_tuple(data.get("protected_patterns"), "protected patterns")
    allowed = _string_tuple(
        data.get("allowed_paths"),
        "allowed paths",
        allow_empty=execution_mode == ExecutionMode.VERIFY_ONLY,
    )
    _validate_repo_patterns(protected, "protected patterns")
    _validate_repo_patterns(allowed, "allowed paths")
    if set(protected) & set(allowed):
        raise ValueError("protected patterns cannot also be allowed write paths")
    try:
        verification_class = VerificationClass(str(data.get("verification_class", "")))
    except ValueError as error:
        raise ValueError("verification_class must be hard or soft") from error
    goal = str(data.get("goal", "")).strip()
    if not goal:
        raise ValueError("contract goal must not be empty")
    return PacketContract(
        packet_id=packet_id,
        goal=goal,
        execution_mode=execution_mode,
        verification_class=verification_class,
        acceptance_criteria=criteria,
        baseline_argv=baseline,
        final_argvs=final_argvs,
        expected_failure_codes=expected_codes,
        protected_patterns=protected,
        allowed_paths=allowed,
        evidence_requirements=_string_tuple(
            data.get("evidence_requirements"),
            "evidence requirements",
        ),
    )


def validate_review(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("review must be a JSON object")
    _exact_keys(data, {"verdict", "findings", "evidence_summary"}, "review")
    verdict = data.get("verdict")
    if verdict not in {"GREEN", "RED"}:
        raise ValueError("review verdict must be GREEN or RED")
    evidence_summary = data.get("evidence_summary")
    if not isinstance(evidence_summary, str) or not evidence_summary.strip():
        raise ValueError("review evidence_summary must be a non-empty string")
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise ValueError("review findings must be a list")
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            raise ValueError(f"review finding {index} must be an object")
        _exact_keys(
            finding,
            {"severity", "title", "evidence", "correction"},
            f"review finding {index}",
        )
        if finding.get("severity") not in {"P0", "P1", "P2"}:
            raise ValueError(f"review finding {index} has an invalid severity")
        for field in ("title", "evidence", "correction"):
            value = finding.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"review finding {index} field {field} must be a non-empty string"
                )
    if verdict == "GREEN" and findings:
        raise ValueError("a GREEN review cannot contain blocking findings")
    if verdict == "RED" and not findings:
        raise ValueError("a RED review must contain at least one finding")


def validate_final_review(
    data: dict[str, Any],
    *,
    expected_criteria: tuple[str, ...],
) -> None:
    if not isinstance(data, dict):
        raise ValueError("final review must be a JSON object")
    _exact_keys(
        data,
        {"verdict", "criteria", "remaining_risks", "merge_recommendation"},
        "final review",
    )
    verdict = data.get("verdict")
    if verdict not in {"MERGE", "DO_NOT_MERGE"}:
        raise ValueError("final review verdict must be MERGE or DO_NOT_MERGE")
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        raise ValueError("final review criteria must be a list")
    reported: dict[str, str] = {}
    for index, item in enumerate(criteria, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"final review criterion {index} must be an object")
        _exact_keys(item, {"criterion", "status", "evidence"}, f"final review criterion {index}")
        criterion = item.get("criterion")
        status = item.get("status")
        evidence = item.get("evidence")
        if not isinstance(criterion, str) or not criterion.strip():
            raise ValueError(f"final review criterion {index} must name a criterion")
        if status not in {"PASS", "FAIL", "UNVERIFIED"}:
            raise ValueError(f"final review criterion {index} has an invalid status")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"final review criterion {index} must cite evidence")
        reported[criterion.strip()] = str(status)
    risks = data.get("remaining_risks")
    if not isinstance(risks, list) or any(
        not isinstance(risk, str) or not risk.strip()
        for risk in risks
    ):
        raise ValueError("final review remaining_risks must be a list of non-empty strings")
    recommendation = data.get("merge_recommendation")
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise ValueError("final review merge_recommendation must be a non-empty string")
    if verdict == "MERGE":
        missing = [criterion for criterion in expected_criteria if criterion not in reported]
        if missing:
            raise ValueError("MERGE review omitted acceptance criteria: " + "; ".join(missing))
        failed = [criterion for criterion in expected_criteria if reported.get(criterion) != "PASS"]
        if failed:
            raise ValueError("MERGE review has non-PASS acceptance criteria: " + "; ".join(failed))


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise ValueError(f"{label} has invalid fields: {'; '.join(details)}")


def _string_tuple(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    values = tuple(str(item).strip() for item in value if str(item).strip())
    if not allow_empty and not values:
        raise ValueError(f"{field} must not be empty")
    return values


def _argv(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty argument array")
    argv = tuple(str(item) for item in value)
    if not argv[0].strip():
        raise ValueError(f"{field} executable must not be empty")
    return argv


def _validate_repo_patterns(patterns: tuple[str, ...], field: str) -> None:
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        parts = tuple(part for part in normalized.split("/") if part)
        if (
            normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in parts
        ):
            raise ValueError(f"{field} must stay inside the repository: {pattern}")
