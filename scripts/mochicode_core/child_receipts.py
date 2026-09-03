from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
import re
from typing import Any


REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "role",
        "model",
        "effort",
        "owned_paths",
        "acceptance_evidence",
        "commands",
        "evidence_locations",
        "unresolved_risks",
        "stop_reason",
        "telemetry",
    }
)
ACCEPTANCE_FIELDS = frozenset({"criterion_id", "status", "evidence"})
COMMAND_FIELDS = frozenset({"argv", "exit_code"})
TELEMETRY_FIELDS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "tool_calls",
        "retry_count",
        "duration_ms",
        "termination_reason",
    }
)
STATUSES = frozenset({"COMPLETED", "PARTIAL", "FAILED"})
EVIDENCE_STATUSES = frozenset({"PASS", "FAIL", "UNVERIFIED"})
MAX_ARRAY_ITEMS = 128
MAX_STRING_LENGTH = 2000
TRUNCATION_MARKERS = (
    "[mochicode output limit exceeded]",
    "[truncated]",
    "output truncated",
)
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


class ChildReceiptError(ValueError):
    pass


def validate_child_receipt(
    payload: dict[str, Any],
    *,
    allowed_paths: tuple[str, ...],
    required_criteria: tuple[str, ...],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ChildReceiptError("child receipt must be a JSON object")
    _require_exact_fields(payload, REQUIRED_FIELDS, "child receipt")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ChildReceiptError("child receipt schema_version must be 1")
    status = _bounded_string(payload["status"], "status")
    if status not in STATUSES:
        raise ChildReceiptError("child receipt status is unsupported")
    for field in ("role", "model", "effort"):
        value = _bounded_string(payload[field], field)
        if SAFE_ID.fullmatch(value) is None:
            raise ChildReceiptError(f"child receipt {field} is not a safe identifier")

    owned_paths = _string_array(payload["owned_paths"], "owned_paths")
    normalized_owned = tuple(_relative_path(value, "owned_paths") for value in owned_paths)
    if len(set(normalized_owned)) != len(normalized_owned):
        raise ChildReceiptError("child receipt owned_paths contains duplicates")
    for path in normalized_owned:
        if not any(_path_matches(path, pattern) for pattern in allowed_paths):
            raise ChildReceiptError(f"child receipt claims an unowned path: {path}")

    raw_acceptance = _object_array(payload["acceptance_evidence"], "acceptance_evidence")
    acceptance_ids: list[str] = []
    acceptance_statuses: list[str] = []
    for index, entry in enumerate(raw_acceptance):
        _require_exact_fields(entry, ACCEPTANCE_FIELDS, f"acceptance_evidence[{index}]")
        criterion_id = _bounded_string(
            entry["criterion_id"], f"acceptance_evidence[{index}].criterion_id"
        )
        evidence_status = _bounded_string(
            entry["status"], f"acceptance_evidence[{index}].status"
        )
        evidence = _bounded_string(
            entry["evidence"], f"acceptance_evidence[{index}].evidence"
        )
        _reject_truncation(evidence, f"acceptance_evidence[{index}].evidence")
        if evidence_status not in EVIDENCE_STATUSES:
            raise ChildReceiptError("child receipt acceptance status is unsupported")
        acceptance_ids.append(criterion_id)
        acceptance_statuses.append(evidence_status)
    if len(set(acceptance_ids)) != len(acceptance_ids):
        raise ChildReceiptError("child receipt repeats an acceptance criterion")
    if set(acceptance_ids) != set(required_criteria):
        raise ChildReceiptError("child receipt does not cover every required acceptance criterion")

    raw_commands = _object_array(payload["commands"], "commands")
    if status == "COMPLETED" and not raw_commands:
        raise ChildReceiptError("completed child receipt must include at least one command")
    command_exit_codes: list[int] = []
    for index, entry in enumerate(raw_commands):
        _require_exact_fields(entry, COMMAND_FIELDS, f"commands[{index}]")
        argv = _string_array(entry["argv"], f"commands[{index}].argv")
        if not argv:
            raise ChildReceiptError("child receipt command argv must not be empty")
        exit_code = entry["exit_code"]
        if type(exit_code) is not int:
            raise ChildReceiptError("child receipt command exit_code must be an integer")
        command_exit_codes.append(exit_code)

    evidence_locations = _string_array(
        payload["evidence_locations"], "evidence_locations"
    )
    for value in evidence_locations:
        _relative_path(value, "evidence_locations")
    unresolved_risks = _string_array(payload["unresolved_risks"], "unresolved_risks")
    for index, value in enumerate(unresolved_risks):
        _reject_truncation(value, f"unresolved_risks[{index}]")
    stop_reason = _bounded_string(payload["stop_reason"], "stop_reason")
    _reject_truncation(stop_reason, "stop_reason")

    telemetry = payload["telemetry"]
    if not isinstance(telemetry, dict):
        raise ChildReceiptError("child receipt telemetry must be an object")
    _require_exact_fields(telemetry, TELEMETRY_FIELDS, "telemetry")
    for field in TELEMETRY_FIELDS - {"termination_reason"}:
        value = telemetry[field]
        if type(value) is not int or value < 0:
            raise ChildReceiptError(f"child receipt telemetry {field} must be a nonnegative integer")
    termination_reason = _bounded_string(
        telemetry["termination_reason"], "telemetry.termination_reason"
    )

    if status == "COMPLETED":
        if any(value != "PASS" for value in acceptance_statuses):
            raise ChildReceiptError("completed child receipt contains non-passing acceptance evidence")
        if any(value != 0 for value in command_exit_codes):
            raise ChildReceiptError("completed child receipt contains a failing exit code")
        if stop_reason != "completed" or termination_reason != "completed":
            raise ChildReceiptError("completed child receipt has an inconsistent stop reason")

    return payload


def _require_exact_fields(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ChildReceiptError(f"{label} is missing required fields: {missing}")
    if unknown:
        raise ChildReceiptError(f"{label} contains unsupported fields: {unknown}")


def _bounded_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChildReceiptError(f"child receipt {label} must be a non-empty string")
    if len(value) > MAX_STRING_LENGTH:
        raise ChildReceiptError(f"child receipt {label} exceeds the string limit")
    return value


def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_ARRAY_ITEMS:
        raise ChildReceiptError(f"child receipt {label} must be a bounded array")
    return tuple(_bounded_string(item, f"{label}[]") for item in value)


def _object_array(value: Any, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) > MAX_ARRAY_ITEMS:
        raise ChildReceiptError(f"child receipt {label} must be a bounded array")
    if not all(isinstance(item, dict) for item in value):
        raise ChildReceiptError(f"child receipt {label} must contain objects")
    return tuple(value)


def _relative_path(value: str, label: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ChildReceiptError(f"child receipt {label} contains an unsafe path: {value}")
    return path.as_posix()


def _path_matches(path: str, pattern: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/")
    if (
        not normalized_pattern
        or normalized_pattern.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized_pattern)
        or any(part in {"", ".", ".."} for part in PurePosixPath(normalized_pattern).parts)
    ):
        raise ChildReceiptError(f"child receipt contains an unsafe allowed path: {pattern}")
    path_parts = PurePosixPath(path).parts
    pattern_parts = PurePosixPath(normalized_pattern).parts

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        token = pattern_parts[pattern_index]
        if token == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], token)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _reject_truncation(value: str, label: str) -> None:
    lowered = value.lower()
    if any(marker in lowered for marker in TRUNCATION_MARKERS):
        raise ChildReceiptError(f"child receipt {label} contains a truncation marker")
