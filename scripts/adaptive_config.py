#!/usr/bin/env python3
"""Audit and conservatively merge an existing Codex ``config.toml``.

The merge is intentionally text based.  ``tomllib`` validates the input and
the candidate output, while line edits preserve comments, ordering, spelling,
and all values that this utility does not own.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
from typing import Any

from mochicode_core.capabilities import (
    AGENT_DEFAULTS,
    KNOWN_REASONING_EFFORTS,
    SAFE_FEATURE_DEFAULTS,
    audit_capabilities,
    model_readiness,
    selected_model_context_bounds,
)


ROOT_DEFAULTS: dict[str, object] = {
    "project_doc_fallback_filenames": ["CLAUDE.md", "TEAM_GUIDE.md", ".agents.md"],
    "project_doc_max_bytes": 65536,
}
TERRA_FIRST_DEFAULTS: dict[str, object] = {
    "model": "gpt-5.6-terra",
    "model_reasoning_effort": "high",
    "model_auto_compact_token_limit": 400000,
    "model_auto_compact_token_limit_scope": "total",
    "model_reasoning_summary": "concise",
    "model_verbosity": "low",
    "tool_output_token_limit": 10000,
    "review_model": "gpt-5.6-sol",
}
DIRECT_FIRST_DEFAULTS: dict[str, object] = {
    "model": "gpt-5.6-sol",
    "model_reasoning_effort": "high",
    "model_context_window": 1_000_000,
    "model_auto_compact_token_limit": 850_000,
    "model_auto_compact_token_limit_scope": "total",
    "model_reasoning_summary": "concise",
    "model_verbosity": "low",
    "tool_output_token_limit": 10000,
    "review_model": "gpt-5.6-sol",
}
ASTRA_FIRST_DEFAULTS: dict[str, object] = {
    "model": "gpt-6-astra",
    "model_reasoning_effort": "high",
    "model_context_window": 1_000_000,
    "model_auto_compact_token_limit": 850_000,
    "model_auto_compact_token_limit_scope": "total",
    "model_reasoning_summary": "concise",
    "model_verbosity": "low",
    "tool_output_token_limit": 10000,
    "review_model": "gpt-5.6-sol",
}
CONTEXT_KEYS = ("model_context_window", "model_auto_compact_token_limit")
PROFILE_LIMITATION = "Only the supplied config.toml is inspected; -p profile files and runtime overrides are not resolved."
_BARE_KEY = r"[A-Za-z0-9_-]+"
_ASSIGNMENT_KEY = rf'(?:{_BARE_KEY}|"(?:\\.|[^"\\])*"|\'[^\']*\')'
_ASSIGNMENT_RE = re.compile(
    rf"^(?P<indent>\s*)(?P<key>{_ASSIGNMENT_KEY})(?P<between>\s*=\s*)(?P<value>.*?)(?P<eol>\r?\n)?$"
)


class ConfigError(ValueError):
    """An invalid or unsafe configuration candidate."""


@dataclass(frozen=True)
class Assignment:
    line_index: int
    section: tuple[str, ...]
    key: str
    raw_value: str
    parsed_value: object | None
    value_start: int
    value_end: int
    comment_start: int | None


@dataclass(frozen=True)
class Section:
    path: tuple[str, ...]
    header_index: int
    body_start: int
    body_end: int
    is_array: bool = False


@dataclass(frozen=True)
class ConfigDocument:
    path: Path
    text: str
    data: dict[str, Any]
    lines: tuple[str, ...]
    sections: tuple[Section, ...]
    assignments: tuple[Assignment, ...]
    newline: str
    bom: bool = False

    def section(self, path: tuple[str, ...]) -> Section | None:
        return next((item for item in self.sections if item.path == path), None)

    def assignment(self, section: tuple[str, ...], key: str) -> Assignment | None:
        return next(
            (
                item
                for item in self.assignments
                if item.section == section and item.key == key
            ),
            None,
        )

    def root_assignment(self, key: str) -> Assignment | None:
        return self.assignment((), key)


class _Edits:
    def __init__(self, document: ConfigDocument) -> None:
        self.document = document
        self.replacements: dict[int, str] = {}
        self.removals: set[int] = set()
        self.insertions: dict[int, list[str]] = {}

    def replace(self, line_index: int, value: str) -> None:
        self.replacements[line_index] = value

    def remove(self, line_index: int) -> None:
        self.removals.add(line_index)

    def insert(self, line_index: int, values: Iterable[str]) -> None:
        self.insertions.setdefault(line_index, []).extend(values)

    def render(self) -> str:
        output: list[str] = []
        lines = self.document.lines
        for index in range(len(lines) + 1):
            if index in self.insertions:
                _ensure_output_boundary(output, self.document.newline)
                output.extend(self.insertions[index])
            if index >= len(lines):
                continue
            if index in self.removals:
                continue
            output.append(self.replacements.get(index, lines[index]))
        return "".join(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adaptive_config",
        description="Audit and conservatively merge a Codex config.toml.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="Audit config and Codex capabilities.")
    _add_common_arguments(audit)

    merge = subparsers.add_parser("merge", help="Write a conservative merged config.")
    _add_common_arguments(merge)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--report", type=Path, required=True)
    merge.add_argument(
        "--disable-mcp",
        action="append",
        default=[],
        metavar="NAME",
        help="Disable exactly one existing [mcp_servers.NAME] table; repeatable.",
    )
    merge.add_argument(
        "--enable-agent-defaults",
        action="store_true",
        help="Add absent scalar [agents] defaults only after a successful probe.",
    )
    merge.add_argument(
        "--remove-stale-context",
        action="store_true",
        help="Opt in to removing root context overrides proven too large at runtime.",
    )
    merge.add_argument(
        "--direct-first",
        action="store_true",
        help="Select Sol defaults, preserving existing context, compaction, and compatible reasoning effort.",
    )
    merge.add_argument(
        "--terra-first",
        action="store_true",
        help="Select Terra defaults, preserving existing context, compaction, and compatible reasoning effort.",
    )
    merge.add_argument(
        "--astra-first",
        action="store_true",
        help="Select catalog-gated Astra defaults, preserving existing context, compaction, and compatible reasoning effort.",
    )
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--codex-exe", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = load_config_document(args.config)
        if args.command == "audit":
            payload = make_audit_report(document, args.codex_exe)
            _emit(payload, args.as_json)
            return 0 if payload["ok"] else 1

        _validate_merge_paths(document.path, args.output, args.report)
        payload = merge_config(
            document,
            output=args.output,
            report=args.report,
            codex_exe=args.codex_exe,
            disable_mcp=args.disable_mcp,
            enable_agent_defaults=args.enable_agent_defaults,
            remove_stale_context=args.remove_stale_context,
            direct_first=args.direct_first,
            terra_first=args.terra_first,
            astra_first=args.astra_first,
        )
        _emit(payload, args.as_json)
        return 0
    except (ConfigError, OSError, ValueError) as error:
        print(f"adaptive_config refused: {_safe_error(error)}", file=sys.stderr)
        return 2


def load_config_document(path: str | Path) -> ConfigDocument:
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    except OSError as error:
        raise ConfigError(f"could not read config: {config_path}") from error
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigError("config is not valid UTF-8") from error
    return _document_from_text(config_path, text, bom=bom)


def _document_from_text(path: Path, text: str, *, bom: bool = False) -> ConfigDocument:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError("config contains invalid or duplicate TOML") from error
    if not isinstance(parsed, dict):
        raise ConfigError("config did not decode to a TOML table")
    lines = tuple(text.splitlines(keepends=True))
    sections, assignments = _scan_document(lines)
    if "\r\n" in text:
        newline = "\r\n"
    elif "\n" in text:
        newline = "\n"
    elif "\r" in text:
        newline = "\r"
    else:
        newline = "\n"
    return ConfigDocument(
        path=Path(path),
        text=text,
        data=parsed,
        lines=lines,
        sections=tuple(sections),
        assignments=tuple(assignments),
        newline=newline,
        bom=bom,
    )


def make_audit_report(
    document: ConfigDocument, codex_exe: str | Path
) -> dict[str, Any]:
    selected_model = document.data.get("model")
    if not isinstance(selected_model, str):
        selected_model = None
    capabilities = audit_capabilities(
        codex_exe,
        selected_model=selected_model,
    )
    context = {
        key: document.data[key]
        for key in CONTEXT_KEYS
        if key in document.data and _report_safe_context_value(document.data[key])
    }
    mcp_servers = _mcp_server_names(document)
    payload: dict[str, Any] = {
        "ok": bool(capabilities.get("available")),
        "config_valid": True,
        "config": {
            "path": str(document.path),
            "root_context_overrides": context,
            "mcp_server_names": mcp_servers,
            "root_key_count": len(document.data),
            "table_count": len(document.sections),
        },
        "capabilities": capabilities,
        "unsupported_assumptions": capabilities.get("unsupported_assumptions", []),
        "preservation": {
            "unowned_values_and_bytes": "preserved",
            "secrets_emitted": False,
        },
        "warnings": [*capabilities.get("warnings", []), PROFILE_LIMITATION],
    }
    if not capabilities.get("catalog_available"):
        payload["ok"] = False
    return payload


def merge_config(
    document: ConfigDocument,
    *,
    output: str | Path,
    report: str | Path,
    codex_exe: str | Path,
    disable_mcp: Sequence[str] = (),
    enable_agent_defaults: bool = False,
    remove_stale_context: bool = False,
    direct_first: bool = False,
    terra_first: bool = False,
    astra_first: bool = False,
) -> dict[str, Any]:
    """Create a validated merged output and a redacted JSON report."""

    output_path = Path(output)
    report_path = Path(report)
    _validate_merge_paths(document.path, output_path, report_path)
    names = _unique_nonempty_names(disable_mcp)
    selected_model = document.data.get("model")
    if not isinstance(selected_model, str):
        selected_model = None
    capabilities = audit_capabilities(
        codex_exe,
        selected_model=selected_model,
    )
    edits = _Edits(document)
    changes: dict[str, Any] = {
        "added_root_defaults": [],
        "added_features": [],
        "added_agent_defaults": [],
        "disabled_mcp": [],
        "already_disabled_mcp": [],
        "missing_mcp": [],
        "removed_stale_context": [],
        "preserved_context": [],
        "set_terra_first_defaults": [],
        "set_direct_first_defaults": [],
        "set_astra_first_defaults": [],
        "removed_default_service_tier": [],
        "mapped_reasoning_effort": [],
    }
    warnings: list[str] = [*capabilities.get("warnings", []), PROFILE_LIMITATION]

    _merge_root_defaults(document, edits, changes)
    _merge_mcp_disables(document, names, edits, changes)
    _remove_stale_context_if_proven(
        document,
        capabilities,
        edits,
        changes,
        warnings,
        requested=(
            remove_stale_context
            and not terra_first
            and not direct_first
            and not astra_first
        ),
    )
    selected_profiles = sum(bool(value) for value in (direct_first, terra_first, astra_first))
    if selected_profiles > 1:
        raise ConfigError("direct-first, terra-first, and astra-first are mutually exclusive")
    if direct_first:
        if remove_stale_context:
            warnings.append(
                "Stale context removal was skipped because direct-first preserves explicit context settings."
            )
        _merge_role_defaults(
            document,
            edits,
            changes,
            warnings,
            defaults=DIRECT_FIRST_DEFAULTS,
            change_key="set_direct_first_defaults",
            label="Direct-first",
            capabilities=capabilities,
        )
    if terra_first:
        if remove_stale_context:
            warnings.append(
                "Stale context removal was skipped because Terra-first preserves explicit context settings."
            )
        _merge_terra_first_defaults(document, edits, changes, warnings, capabilities)
    if astra_first:
        _require_astra_profile(capabilities)
        if remove_stale_context:
            warnings.append(
                "Stale context removal was skipped because Astra-first preserves explicit context settings."
            )
        _merge_role_defaults(
            document,
            edits,
            changes,
            warnings,
            defaults=ASTRA_FIRST_DEFAULTS,
            change_key="set_astra_first_defaults",
            label="Astra-first",
            capabilities=capabilities,
        )

    # Root and newly created tables may share EOF; insert root settings first.
    _merge_safe_features(document, capabilities, edits, changes, warnings)
    _merge_agent_defaults(
        document, capabilities, edits, changes, warnings,
        requested=enable_agent_defaults,
    )
    merged_text = edits.render()
    _validate_output_text(output_path, merged_text)
    output_bytes = (b"\xef\xbb\xbf" if document.bom else b"") + merged_text.encode(
        "utf-8"
    )
    removed = changes["removed_stale_context"]
    payload: dict[str, Any] = {
        "ok": True,
        "command": "merge",
        "config_valid": True,
        "output": str(output_path),
        "report": str(report_path),
        "capabilities": capabilities,
        "changes": changes,
        "removed_stale_context": removed,
        "warnings": _unique_strings(warnings),
        "validation": {
            "input_toml": True,
            "output_toml": True,
            "secrets_emitted": False,
        },
        "preservation": {
            "unowned_values_and_bytes": "preserved",
            "secrets_emitted": False,
        },
    }
    _write_atomic(output_path, output_bytes)
    _write_atomic(
        report_path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return payload


def _merge_root_defaults(
    document: ConfigDocument, edits: _Edits, changes: dict[str, Any]
) -> None:
    missing = [key for key in ROOT_DEFAULTS if key not in document.data]
    if not missing:
        return
    insertion: list[str] = []
    for key in missing:
        insertion.append(f"{key} = {_toml_literal(ROOT_DEFAULTS[key])}{document.newline}")
        changes["added_root_defaults"].append(key)
    edits.insert(_first_table_index(document), insertion)


def _merge_terra_first_defaults(
    document: ConfigDocument,
    edits: _Edits,
    changes: dict[str, Any],
    warnings: list[str],
    capabilities: Mapping[str, Any],
) -> None:
    """Set only the user-authorized Terra-first root values."""

    _merge_role_defaults(
        document,
        edits,
        changes,
        warnings,
        defaults=TERRA_FIRST_DEFAULTS,
        change_key="set_terra_first_defaults",
        label="Terra-first",
        capabilities=capabilities,
    )


def _require_astra_profile(capabilities: Mapping[str, Any]) -> None:
    astra = capabilities.get("astra")
    if (
        not isinstance(astra, Mapping)
        or astra.get("model") != "gpt-6-astra"
        or astra.get("available") is not True
        or astra.get("activation_ready") is not True
    ):
        raise ConfigError(
            "Astra-first requires the installed Codex catalog to advertise gpt-6-astra with High reasoning."
        )


def _merge_role_defaults(
    document: ConfigDocument,
    edits: _Edits,
    changes: dict[str, Any],
    warnings: list[str],
    *,
    defaults: Mapping[str, object],
    change_key: str,
    label: str,
    capabilities: Mapping[str, Any],
) -> None:
    """Apply an authorized model switch while preserving context and compatible effort."""

    insertion: list[str] = []
    for key, value in defaults.items():
        if key in (*CONTEXT_KEYS, "model_auto_compact_token_limit_scope") and key in document.data:
            continue
        if key == "model_reasoning_effort" and key in document.data:
            current = document.data[key]
            if not isinstance(current, str) or current not in KNOWN_REASONING_EFFORTS:
                raise ConfigError("existing reasoning effort is not a recognized effort name")
            readiness = model_readiness(capabilities, str(defaults["model"]), current)
            if readiness["activation_ready"]:
                continue
            if readiness["status"] != "effort_unsupported" or not readiness["reasoning_efforts"]:
                warnings.append(f"{label} preserved existing reasoning effort; target catalog compatibility is unverified.")
                continue
            if value not in readiness["reasoning_efforts"]:
                raise ConfigError("target catalog does not support the fallback reasoning effort")
            changes["mapped_reasoning_effort"].append({
                "model": defaults["model"], "from": current, "to": value,
                "reason": "existing effort absent from target catalog reasoning_efforts",
                "catalog_efforts": readiness["reasoning_efforts"],
            })
            warnings.append(f"{label} mapped unsupported reasoning effort {current} to {value} using the target catalog.")
        assignment = document.root_assignment(key)
        replacement = _toml_literal(value)
        if assignment is None:
            if key in document.data:
                raise ConfigError("cannot safely locate an existing root profile setting")
            insertion.append(f"{key} = {replacement}{document.newline}")
        elif document.data.get(key) == value:
            continue
        else:
            if assignment.parsed_value is None:
                raise ConfigError("cannot safely replace a multiline root profile setting")
            edits.replace(
                assignment.line_index,
                _replace_assignment_value(document.lines[assignment.line_index], assignment, replacement),
            )
        changes[change_key].append(key)
    if insertion:
        edits.insert(_first_table_index(document), insertion)

    for key in CONTEXT_KEYS:
        if key in document.data and key not in changes["preserved_context"]:
            changes["preserved_context"].append(key)

    service_tier = document.root_assignment("service_tier")
    if service_tier is None:
        return
    value = service_tier.parsed_value
    if not isinstance(value, str) or value.lower() not in {"fast", "priority"}:
        warnings.append(
            "The existing root service_tier was preserved because it is not a recognized persistent Fast value."
        )
        return
    line = document.lines[service_tier.line_index]
    if service_tier.comment_start is None:
        edits.remove(service_tier.line_index)
    else:
        leading = re.match(r"\s*", line).group(0)
        edits.replace(service_tier.line_index, leading + line[service_tier.comment_start :])
    changes["removed_default_service_tier"].append(value.lower())


def _merge_safe_features(
    document: ConfigDocument,
    capabilities: Mapping[str, Any],
    edits: _Edits,
    changes: dict[str, Any],
    warnings: list[str],
) -> None:
    feature_data = document.data.get("features")
    feature_section = document.section(("features",))
    missing: list[str] = []
    for name in SAFE_FEATURE_DEFAULTS:
        if isinstance(feature_data, Mapping) and name in feature_data:
            continue
        info = _feature_probe_info(capabilities, name)
        if info.get("supported") is True:
            missing.append(name)
    if not missing:
        return
    if feature_section is not None:
        additions = [
            f"{name} = true{document.newline}" for name in missing
            if document.assignment(("features",), name) is None
        ]
        edits.insert(feature_section.body_end, additions)
        changes["added_features"].extend(name for name in missing if additions)
        return
    if "features" not in document.data:
        additions = ["[features]" + document.newline]
        additions.extend(f"{name} = true{document.newline}" for name in missing)
        edits.insert(len(document.lines), additions)
        changes["added_features"].extend(missing)
        return
    warnings.append(
        "Feature defaults were not added because the existing features value is not a direct table."
    )


def _merge_agent_defaults(
    document: ConfigDocument,
    capabilities: Mapping[str, Any],
    edits: _Edits,
    changes: dict[str, Any],
    warnings: list[str],
    *,
    requested: bool,
) -> None:
    if not requested:
        return
    probe = capabilities.get("agent_defaults_probe")
    if not isinstance(probe, Mapping) or probe.get("supported") is not True:
        warnings.append(
            "Agent defaults were requested but the disposable [agents] probe did not succeed."
        )
        return

    agent_data = document.data.get("agents")
    agent_section = document.section(("agents",))
    missing = [
        key
        for key in AGENT_DEFAULTS
        if not (isinstance(agent_data, Mapping) and key in agent_data)
    ]
    if not missing:
        return
    if agent_section is not None:
        additions = [
            f"{key} = {_toml_literal(AGENT_DEFAULTS[key])}{document.newline}"
            for key in missing
            if document.assignment(("agents",), key) is None
        ]
        edits.insert(agent_section.body_end, additions)
        changes["added_agent_defaults"].extend(
            key for key in missing if additions
        )
        return
    if "agents" not in document.data:
        additions = ["[agents]" + document.newline]
        additions.extend(
            f"{key} = {_toml_literal(AGENT_DEFAULTS[key])}{document.newline}"
            for key in missing
        )
        edits.insert(len(document.lines), additions)
        changes["added_agent_defaults"].extend(missing)
        return
    warnings.append(
        "Agent defaults were not added because the existing agents value is not a direct table."
    )


def _merge_mcp_disables(
    document: ConfigDocument,
    names: Sequence[str],
    edits: _Edits,
    changes: dict[str, Any],
) -> None:
    for name in names:
        section_path = ("mcp_servers", name)
        section = document.section(section_path)
        if section is None:
            changes["missing_mcp"].append(name)
            continue
        assignment = document.assignment(section_path, "enabled")
        if assignment is None:
            edits.insert(section.body_end, [f"enabled = false{document.newline}"])
            changes["disabled_mcp"].append(name)
            continue
        if assignment.parsed_value is False:
            changes["already_disabled_mcp"].append(name)
            continue
        line = document.lines[assignment.line_index]
        edits.replace(assignment.line_index, _replace_assignment_value(line, assignment, "false"))
        changes["disabled_mcp"].append(name)


def _remove_stale_context_if_proven(
    document: ConfigDocument,
    capabilities: Mapping[str, Any],
    edits: _Edits,
    changes: dict[str, Any],
    warnings: list[str],
    *,
    requested: bool,
) -> None:
    if not requested:
        changes["preserved_context"].extend(
            key for key in CONTEXT_KEYS if document.root_assignment(key) is not None
        )
        return
    limits = selected_model_context_bounds(capabilities)
    if limits is not None and limits["slug"] != document.data.get("model"):
        limits = None
    if limits is None:
        warnings.append(
            "Stale context removal was requested but runtime model limits were not proven."
        )
        changes["preserved_context"].extend(
            key for key in CONTEXT_KEYS if document.root_assignment(key) is not None
        )
        return

    maximum = limits.get("max_context_window")
    effective = limits.get("effective_context_window")
    for key in CONTEXT_KEYS:
        assignment = document.root_assignment(key)
        if assignment is None:
            continue
        value = assignment.parsed_value
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            changes["preserved_context"].append(key)
            continue
        reasons: list[str] = []
        if key == "model_context_window":
            if isinstance(maximum, int) and value > maximum:
                reasons.append(f"{value} > advertised maximum {maximum}")
        else:
            compact_limit = effective if isinstance(effective, int) else maximum
            if isinstance(compact_limit, int) and value > compact_limit:
                reasons.append(f"{value} > effective window {compact_limit}")
        if not reasons:
            changes["preserved_context"].append(key)
            continue
        line = document.lines[assignment.line_index]
        if assignment.comment_start is None:
            edits.remove(assignment.line_index)
        else:
            leading = re.match(r"\s*", line).group(0)
            edits.replace(
                assignment.line_index,
                leading + line[assignment.comment_start :],
            )
        changes["removed_stale_context"].append(
            {
                "key": key,
                "value": value,
                "raw_value": assignment.raw_value,
                "line": assignment.line_index + 1,
                "reason": "; ".join(reasons),
            }
        )


def _validate_merge_paths(config: Path, output: Path, report: Path) -> None:
    config_resolved = config.resolve(strict=False)
    output_resolved = output.resolve(strict=False)
    report_resolved = report.resolve(strict=False)
    if output_resolved == config_resolved:
        raise ConfigError("output must not replace the input config")
    if report_resolved == config_resolved:
        raise ConfigError("report must not replace the input config")
    if output_resolved == report_resolved:
        raise ConfigError("output and report must be different files")


def _validate_output_text(path: Path, text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"generated output is invalid TOML: {path}") from error


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass


def _scan_document(
    lines: Sequence[str],
) -> tuple[list[Section], list[Assignment]]:
    sections: list[Section] = []
    assignments: list[Assignment] = []
    current_section: tuple[str, ...] = ()
    pending = ""

    for index, line in enumerate(lines):
        if pending:
            pending += line
            try:
                tomllib.loads(pending)
            except tomllib.TOMLDecodeError:
                pass
            else:
                pending = ""
            continue
        header = _table_header_path(line)
        if _is_array_table_header(line):
            if sections:
                previous = sections[-1]
                sections[-1] = Section(
                    previous.path,
                    previous.header_index,
                    previous.body_start,
                    index,
                    previous.is_array,
                )
            current_section = ("__array_table__",)
            sections.append(
                Section(current_section, index, index + 1, len(lines), True)
            )
            continue
        if header is not None:
            if sections:
                previous = sections[-1]
                sections[-1] = Section(
                    previous.path,
                    previous.header_index,
                    previous.body_start,
                    index,
                    previous.is_array,
                )
            current_section = header
            sections.append(Section(header, index, index + 1, len(lines)))
            continue

        assignment = _assignment_from_line(index, current_section, line)
        if assignment is not None:
            assignments.append(assignment)
        try:
            tomllib.loads(line)
        except tomllib.TOMLDecodeError:
            pending = line

    return sections, assignments


def _table_header_path(line: str) -> tuple[str, ...] | None:
    candidate = line.strip()
    if not candidate.startswith("[") or candidate.startswith("[["):
        return None
    try:
        parsed = tomllib.loads(candidate + "\n")
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    path: list[str] = []
    value: Any = parsed
    while isinstance(value, dict) and len(value) == 1:
        key, value = next(iter(value.items()))
        if not isinstance(key, str):
            return None
        path.append(key)
    return tuple(path) if value == {} and path else None


def _is_array_table_header(line: str) -> bool:
    return bool(re.match(r"^\s*\[\[", line))


def _assignment_from_line(
    index: int, section: tuple[str, ...], line: str
) -> Assignment | None:
    match = _ASSIGNMENT_RE.match(line)
    if match is None:
        return None
    raw_key = match.group("key")
    try:
        parsed_key = tomllib.loads(f"{raw_key} = 0\n")
    except tomllib.TOMLDecodeError:
        return None
    if len(parsed_key) != 1:
        return None
    canonical_key = next(iter(parsed_key))
    if not isinstance(canonical_key, str):
        return None
    raw_line = line[:-len(match.group("eol"))] if match.group("eol") else line
    value_offset = match.start("value")
    value_text = match.group("value")
    value_part, comment_offset = _value_without_comment(value_text)
    raw_value = value_part.strip()
    value_start = value_offset + (len(value_part) - len(value_part.lstrip()))
    value_end = value_offset + len(value_part.rstrip())
    comment_start = (
        value_offset + comment_offset if comment_offset is not None else None
    )
    parsed_value: object | None = None
    if raw_value:
        try:
            parsed = tomllib.loads(f"value = {raw_value}\n")
            parsed_value = parsed.get("value")
        except tomllib.TOMLDecodeError:
            parsed_value = None
    return Assignment(
        line_index=index,
        section=section,
        key=canonical_key,
        raw_value=raw_value,
        parsed_value=parsed_value,
        value_start=value_start,
        value_end=value_end,
        comment_start=comment_start,
    )


def _value_without_comment(value: str) -> tuple[str, int | None]:
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "#":
            return value[:index], index
        index += 1
    return value, None


def _opens_multiline(line: str) -> str | None:
    value = line
    for delimiter in ('"""', "'''"):
        if value.count(delimiter) % 2:
            return delimiter
    return None


def _closes_multiline(line: str, delimiter: str) -> bool:
    return line.count(delimiter) % 2 == 1


def _first_table_index(document: ConfigDocument) -> int:
    return document.sections[0].header_index if document.sections else len(document.lines)


def _ensure_output_boundary(output: list[str], newline: str) -> None:
    if output and not output[-1].endswith(("\n", "\r")):
        output[-1] += newline


def _replace_assignment_value(
    line: str, assignment: Assignment, replacement: str
) -> str:
    return line[: assignment.value_start] + replacement + line[assignment.value_end :]


def _feature_probe_info(
    capabilities: Mapping[str, Any], name: str
) -> Mapping[str, Any]:
    probe = capabilities.get("feature_probe")
    if not isinstance(probe, Mapping):
        return {}
    features = probe.get("features")
    if not isinstance(features, Mapping):
        return {}
    info = features.get(name)
    return info if isinstance(info, Mapping) else {}


def _mcp_server_names(document: ConfigDocument) -> list[str]:
    return [
        path[1]
        for path in (section.path for section in document.sections)
        if len(path) == 2 and path[0] == "mcp_servers"
    ]


def _unique_nonempty_names(names: Sequence[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name:
            raise ConfigError("MCP names must be non-empty strings")
        if name not in result:
            result.append(name)
    return result


def _unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


def _report_safe_context_value(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _safe_error(error: BaseException) -> str:
    if isinstance(error, ConfigError):
        return str(error)
    if isinstance(error, OSError):
        return "filesystem operation failed"
    return "invalid merge request"


def _emit(payload: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload.get("command") == "merge":
        changes = payload.get("changes", {})
        print("Adaptive config merge complete.")
        print(f"Output: {payload.get('output')}")
        print(f"Changed MCP tables: {len(changes.get('disabled_mcp', []))}")
        print(f"Removed stale context keys: {len(changes.get('removed_stale_context', []))}")
        return
    print("Adaptive config audit complete." if payload.get("ok") else "Adaptive config audit needs attention.")
    capabilities = payload.get("capabilities", {})
    print(f"Model catalog available: {bool(capabilities.get('catalog_available'))}")
    print(f"Unsupported assumptions: {len(payload.get('unsupported_assumptions', []))}")


if __name__ == "__main__":
    raise SystemExit(run_cli())
