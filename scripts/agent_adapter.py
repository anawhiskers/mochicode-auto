#!/usr/bin/env python3
"""Render, audit, and safely merge Ana's portable agent workflow policy.

The tool intentionally keeps provider model selections separate from workflow
policy. A release audit can report a candidate change, but only a human-reviewed
provider-specific update should alter a selected model or effort.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any

from mochicode_core.capabilities import KNOWN_REASONING_EFFORTS, parse_model_catalog


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_template_candidates = (
    PLUGIN_ROOT / "portable" / "templates" / "agent-adapters",
    PLUGIN_ROOT.parent / "portable" / "templates" / "agent-adapters",
)
TEMPLATE_ROOT = next((path for path in _template_candidates if path.is_dir()), _template_candidates[0])
MARKER_BEGIN = "<!-- ANA-ADAPTIVE-WORKFLOW:BEGIN -->"
MARKER_END = "<!-- ANA-ADAPTIVE-WORKFLOW:END -->"
SAFE_CLAUDE_KEYS = ("model", "effortLevel", "autoUpdatesChannel")


class AdapterError(ValueError):
    """A safe adapter operation could not be completed."""


def _read_template(relative: str) -> str:
    path = TEMPLATE_ROOT / relative
    if not path.is_file():
        raise AdapterError(f"adapter template is missing: {path}")
    return path.read_text(encoding="utf-8").strip()


def _default_target(agent: str, home: Path) -> Path | None:
    defaults = {
        "codex": home / ".codex" / "AGENTS.md",
        "claude": home / ".claude" / "CLAUDE.md",
    }
    return defaults.get(agent)


def _managed_block(agent: str) -> str:
    return "\n".join(
        (
            MARKER_BEGIN,
            "# Ana Adaptive Workflow",
            "",
            _read_template("CORE-WORKFLOW.md"),
            "",
            _read_template(f"adapters/{agent}.md"),
            MARKER_END,
        )
    ) + "\n"


def _replace_managed_block(existing: str, managed: str) -> str:
    start = existing.find(MARKER_BEGIN)
    end = existing.find(MARKER_END)
    if start == -1 and end == -1:
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        return existing + suffix + "\n" + managed
    if existing.count(MARKER_BEGIN) != 1 or existing.count(MARKER_END) != 1:
        raise AdapterError("existing workflow markers are ambiguous; refusing to overwrite")
    if start == -1 or end == -1 or end < start:
        raise AdapterError("existing workflow markers are incomplete; refusing to overwrite")
    end += len(MARKER_END)
    suffix = "\n" if end == len(existing) or existing[end:end + 1] == "\n" else "\n\n"
    return existing[:start] + managed.rstrip("\n") + suffix + existing[end:].lstrip("\n")


def _safe_codex_config(home: Path) -> dict[str, str]:
    path = home / ".codex" / "config.toml"
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeError):
        return {"parse_error": "config is not valid UTF-8 TOML"}
    return {
        key: value for key in ("model", "model_reasoning_effort", "review_model")
        if isinstance((value := raw.get(key)), str)
        and (
            value in KNOWN_REASONING_EFFORTS if key == "model_reasoning_effort"
            else re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
        )
    }


def _safe_claude_settings(home: Path) -> dict[str, Any]:
    path = home / ".claude" / "settings.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return {"parse_error": "settings is not valid UTF-8 JSON"}
    if not isinstance(raw, dict):
        return {"parse_error": "settings root is not an object"}
    return {
        key: raw[key] for key in SAFE_CLAUDE_KEYS
        if isinstance(raw.get(key), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", raw[key])
    }


def _codex_catalog(home: Path) -> dict[str, Any]:
    executable = shutil.which("codex") or shutil.which("codex.cmd")
    if executable is None:
        return {"available": False, "reason": "Codex executable not found"}
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home / ".codex")
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    try:
        result = subprocess.run(
            [executable, "debug", "models"],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "Codex model catalog command unavailable"}
    if result.returncode != 0:
        return {"available": False, "reason": "Codex model catalog command failed"}
    safe_catalog = parse_model_catalog(result.stdout)
    if not safe_catalog:
        return {"available": False, "reason": "Codex model catalog was not valid JSON"}
    safe_models = [
        {
            "slug": model["slug"],
            "reasoning_efforts": model.get("reasoning_efforts", []),
        }
        for model in safe_catalog
    ]
    return {"available": True, "account_access_verified": False, "models": safe_models}


def _agent_executable(agent: str) -> dict[str, Any]:
    executable = shutil.which(agent)
    if executable is None:
        return {"available": False}
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    version = " ".join((result.stdout + "\n" + result.stderr).split())[:240]
    return {"available": result.returncode == 0, "version": version if result.returncode == 0 else None}


def command_render(args: argparse.Namespace) -> int:
    text = _managed_block(args.agent)
    if args.output is None:
        print(text, end="")
        return 0
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps({"status": "rendered", "agent": args.agent, "output": str(output)}))
    return 0


def command_audit(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    target = Path(args.target).resolve() if args.target else _default_target(args.agent, home)
    report: dict[str, Any] = {
        "agent": args.agent,
        "workflow_template": "<plugin-root>/portable/templates/agent-adapters/CORE-WORKFLOW.md",
        "target": _public_path(target, home) if target else None,
        "target_exists": target.is_file() if target else False,
        "model_change_policy": "report_candidate_only",
    }
    if args.agent == "codex":
        report["selected_settings"] = _safe_codex_config(home)
        report["selected_settings_scope"] = "root config.toml only; -p profile files and runtime overrides are not resolved"
        report["catalog"] = _codex_catalog(home)
    elif args.agent == "claude":
        report["selected_settings"] = _safe_claude_settings(home)
        report["executable"] = _agent_executable("claude")
    elif args.agent == "generic":
        report["note"] = "Use --target with the Markdown instruction file documented by the installed coding agent."
    else:
        report["executable"] = _agent_executable(args.agent)
        report["note"] = "Use --target with the instruction file documented by this installed client."
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _public_path(path: Path, home: Path) -> str:
    try:
        relative = path.relative_to(home)
    except ValueError:
        return f"<external-target>/{path.name}"
    return "<user-home>/" + relative.as_posix()


def _default_backup_root(target: Path) -> Path:
    local_data = os.environ.get("LOCALAPPDATA")
    base = (
        Path(local_data) / "MochiCode" / "agent-workflow-backups"
        if local_data
        else Path.home() / ".local" / "state" / "mochicode" / "agent-workflow-backups"
    )
    identity = hashlib.sha256(str(target).casefold().encode("utf-8")).hexdigest()[:16]
    return base / identity


def command_apply(args: argparse.Namespace) -> int:
    if not args.confirm:
        raise AdapterError("apply is a protected write; re-run with --confirm after reviewing audit output")
    home = Path(args.home).resolve()
    target = Path(args.target).resolve() if args.target else _default_target(args.agent, home)
    if target is None:
        raise AdapterError("this adapter requires --target because no safe default instruction file is known")
    if args.agent == "generic":
        if target.suffix.lower() != ".md":
            raise AdapterError("generic adapter target must be an explicit Markdown instruction file")
    elif target.name.lower() not in {"agents.md", "agents.override.md", "claude.md"}:
        raise AdapterError("target must be an AGENTS.md, AGENTS.override.md, or CLAUDE.md instruction file")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    backup_root = (
        Path(args.backup_root).resolve()
        if args.backup_root
        else _default_backup_root(target)
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = backup_root / timestamp
    suffix = 1
    while backup_dir.exists():
        backup_dir = backup_root / f"{timestamp}-{suffix}"
        suffix += 1
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / target.name
    backup_path.write_text(existing, encoding="utf-8", newline="\n")
    candidate = _replace_managed_block(existing, _managed_block(args.agent))
    temporary = target.with_name(target.name + ".ana-workflow.tmp")
    temporary.write_text(candidate, encoding="utf-8", newline="\n")
    temporary.replace(target)
    verified = target.read_text(encoding="utf-8")
    if MARKER_BEGIN not in verified or MARKER_END not in verified:
        raise AdapterError("post-write verification failed; backup was retained")
    print(json.dumps({"status": "applied", "agent": args.agent, "target": str(target), "backup": str(backup_path)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portable Ana agent-workflow adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    choices = ("codex", "claude", "kimi", "zai", "generic")
    for name, handler in (("render", command_render), ("audit", command_audit), ("apply", command_apply)):
        child = subparsers.add_parser(name)
        child.add_argument("--agent", choices=choices, required=True)
        child.add_argument("--home", default=str(Path.home()))
        child.add_argument("--target")
        if name == "render":
            child.add_argument("--output")
        if name == "apply":
            child.add_argument("--backup-root")
            child.add_argument("--confirm", action="store_true")
        child.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (AdapterError, OSError, UnicodeError) as error:
        detail = str(error) if isinstance(error, AdapterError) else "file operation failed"
        print(f"agent-adapter: {detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
