"""Offline, read-only trigger classifier. Suggestions never grant permission.

Feed a small JSON object on stdin. No model calls, writes, shell execution,
file discovery, scheduling, or automatic configuration changes.
"""
from __future__ import annotations

import json
import sys


FLAGS = {
    "implementation_requested", "goal_conflict", "verification_missing",
    "measured_regression", "optimization_requested", "model_changed",
    "cleanup_requested", "independent_work_available", "recovery_used",
    "external_gate",
}
COUNTS = {"matching_failures", "planning_cycles_without_progress"}


def advise(evidence: dict) -> dict:
    if not isinstance(evidence, dict) or set(evidence) - FLAGS - COUNTS:
        raise ValueError("Expected an object with recognized evidence fields only")
    for key, value in evidence.items():
        if key in FLAGS and type(value) is not bool:
            raise ValueError(f"{key} must be boolean")
        if key in COUNTS and (type(value) is not int or not 0 <= value <= 10000):
            raise ValueError(f"{key} must be an integer from 0 to 10000")
    actions = []
    if evidence.get("goal_conflict"):
        actions.append("resolve_product_conflict_with_user")
    elif evidence.get("implementation_requested"):
        stalled = (evidence.get("matching_failures", 0) >= 2 or
                   evidence.get("planning_cycles_without_progress", 0) >= 2)
        if stalled:
            if evidence.get("recovery_used"):
                actions.append("park_packet_continue_independent_work" if evidence.get(
                    "independent_work_available") else "report_unresolved_core_dependency")
            else:
                actions.append("bounded_parent_recovery")
        if evidence.get("verification_missing"):
            actions.append("repair_smallest_verification_gap")
        if evidence.get("measured_regression") or evidence.get("optimization_requested"):
            actions.append("measure_then_optimize_in_scope")
        if evidence.get("cleanup_requested"):
            actions.append("bounded_behavior_preserving_cleanup")
    if evidence.get("verification_missing") and not evidence.get("implementation_requested") and not evidence.get("goal_conflict"):
        actions.append("inspect_verification_gap_read_only")
    if evidence.get("external_gate"):
        actions.append("request_specific_authority_continue_independent_work" if evidence.get(
            "independent_work_available") else "request_specific_authority")
    if evidence.get("model_changed"):
        actions.append("recommend_compatibility_comparison_only")
    return {
        "actions": actions or ["continue_direct"],
        "advisory_only": True,
        "grants_permission": False,
        "changes_model_or_effort": False,
        "starts_workers": False,
        "max_recovery_attempts": 1,
        "evidence_source": "caller_supplied_not_independently_verified",
    }


def main() -> int:
    try:
        raw = sys.stdin.read(16385)
        if len(raw) > 16384:
            raise ValueError("Input exceeds 16 KiB")
        print(json.dumps(advise(json.loads(raw)), sort_keys=True))
        return 0
    except (ValueError, TypeError) as exc:
        print(f"Invalid evidence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
