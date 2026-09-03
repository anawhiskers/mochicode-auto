from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any

from .backend import CodexCliBackend
from .capabilities import audit_capabilities
from .child_receipts import validate_child_receipt
from .config import load_config
from .evidence import EvidenceLedger
from .learning import LearningStore
from .providers import CodexRoleProvider
from .runner import MochiController, StubRoleProvider
from .state import StateStore, exclusive_run_lease


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PLUGIN_ROOT / "config" / "default.toml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mochicode",
        description="MochiCode automatic Sol, Terra, and Luna workflow controller.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="Check Codex, Git, Python, configuration, schemas, and login readiness.",
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")

    demo = subparsers.add_parser(
        "demo",
        help="Run the zero-cost deterministic stub demonstration.",
    )
    demo.add_argument("--state-root", type=Path)
    demo.add_argument("--json", action="store_true", dest="as_json")

    run = subparsers.add_parser("run", help="Start an orchestrated project run.")
    run.add_argument("--project", type=Path, required=True)
    run.add_argument("--goal-file", type=str, required=True)
    run.add_argument("--backend", choices=("stub", "codex"), default="codex")
    run.add_argument("--run-root", type=Path)
    run.add_argument("--run-id", type=str)
    run.add_argument("--learning-root", type=Path)
    run.add_argument("--lesson-trial", type=str)
    run.add_argument("--lesson-expected", choices=("true", "false"))
    run.add_argument("--json", action="store_true", dest="as_json")

    status = subparsers.add_parser("status", help="Show persisted run status.")
    status.add_argument("--run-root", type=Path, required=True)
    status.add_argument("--verbose", action="store_true")
    status.add_argument("--json", action="store_true", dest="as_json")

    stop = subparsers.add_parser("stop", help="Request a safe stop at the next boundary.")
    stop.add_argument("--run-root", type=Path, required=True)

    resume = subparsers.add_parser("resume", help="Resume a safely stopped run.")
    resume.add_argument("--run-root", type=Path, required=True)
    resume.add_argument("--continue-run", action="store_true")
    resume.add_argument("--repair-review", type=str)
    resume.add_argument("--repair-final", action="store_true")
    resume.add_argument("--backend", choices=("stub", "codex"), default="codex")
    resume.add_argument("--json", action="store_true", dest="as_json")

    lessons = subparsers.add_parser("lessons", help="Inspect and control cross-run lessons.")
    lessons.add_argument("--learning-root", type=Path)
    lesson_actions = lessons.add_subparsers(dest="lesson_action", required=True)
    lesson_list = lesson_actions.add_parser("list", help="List candidate, active, and retired lessons.")
    lesson_list.add_argument("--json", action="store_true", dest="as_json")
    lesson_promote = lesson_actions.add_parser("promote", help="Promote one candidate lesson.")
    lesson_promote.add_argument("lesson_id")
    lesson_promote.add_argument("--evidence", action="append", required=True)
    lesson_promote.add_argument(
        "--negative-control-evidence",
        action="append",
        required=True,
    )
    lesson_promote.add_argument("--human-approved", action="store_true")
    lesson_retire = lesson_actions.add_parser("retire", help="Retire one lesson without deleting history.")
    lesson_retire.add_argument("lesson_id")
    lesson_retire.add_argument("--reason", required=True)
    lesson_export = lesson_actions.add_parser("export", help="Write a redacted active-lesson export.")
    lesson_export.add_argument("--output", type=Path, required=True)

    receipt = subparsers.add_parser(
        "child-receipt",
        help="Validate a typed native child completion receipt.",
    )
    receipt_actions = receipt.add_subparsers(dest="receipt_action", required=True)
    receipt_validate = receipt_actions.add_parser(
        "validate",
        help="Fail closed on malformed, incomplete, or contradictory child evidence.",
    )
    receipt_validate.add_argument("--file", type=Path, required=True)
    receipt_validate.add_argument("--allowed-path", action="append", required=True)
    receipt_validate.add_argument("--criterion", action="append", required=True)
    receipt_validate.add_argument("--json", action="store_true", dest="as_json")
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.name != "nt":
        if args.command == "doctor" and args.as_json:
            _emit(_platform_only_doctor_payload(), True)
            return 1
        print(
            "MochiCode refused: unsupported platform; release commands require Windows",
            file=sys.stderr,
        )
        return 2
    try:
        if args.command == "doctor":
            payload = doctor()
            _emit(payload, args.as_json)
            return 0 if payload["ready"] else 1
        if args.command == "demo":
            payload = run_demo(args.state_root)
            _emit(payload, args.as_json)
            return 0 if payload["status"] == "complete" else 1
        if args.command == "run":
            payload = run_project(
                project=args.project,
                goal_file=args.goal_file,
                backend=args.backend,
                run_root=args.run_root,
                run_id=args.run_id,
                learning_root=args.learning_root,
                lesson_trial_id=args.lesson_trial,
                lesson_expected=args.lesson_expected,
            )
            _emit(payload, args.as_json)
            return 0 if payload["status"] == "complete" else 1
        if args.command == "status":
            payload = status_payload(args.run_root, verbose=args.verbose)
            _emit(payload, args.as_json)
            return 0
        if args.command == "stop":
            store = StateStore(args.run_root)
            _require_state(store)
            store.request_stop()
            print(f"Stop requested for {Path(args.run_root).resolve()}")
            return 0
        if args.command == "resume":
            store = StateStore(args.run_root)
            _require_state(store)
            if args.continue_run or args.repair_review or args.repair_final:
                config = load_config(DEFAULT_CONFIG)
                learning = LearningStore(_default_learning_root())
                if args.backend == "codex":
                    codex_name = "codex.cmd" if os.name == "nt" else "codex"
                    executable = shutil.which(codex_name) or shutil.which("codex")
                    if not executable:
                        raise RuntimeError("Codex CLI is not available; run `mochicode doctor`")
                    provider: Any = CodexRoleProvider(
                        CodexCliBackend(executable, config),
                        run_root=Path(args.run_root).resolve(),
                        plugin_root=PLUGIN_ROOT,
                        reuse_existing=True,
                        learning_store=learning,
                    )
                else:
                    provider = StubRoleProvider()
                controller = MochiController(config, provider, learning)
                if args.repair_final:
                    result = controller.repeat_final_review(run_root=args.run_root)
                elif args.repair_review:
                    result = controller.reaudit_parked_verify_packet(
                        run_root=args.run_root,
                        packet_id=args.repair_review,
                    )
                else:
                    result = controller.resume_existing(run_root=args.run_root)
                payload = _result_payload(
                    result.state,
                    result.run_root,
                    result.integration.branch,
                    result.final_review,
                )
                _emit(payload, args.as_json)
                return 0 if payload["status"] == "complete" else 1
            with exclusive_run_lease(Path(args.run_root).resolve()):
                store.resume()
                state = store.load()
                state.stop_requested = False
                if state.status == "stopped":
                    state.status = "running"
                store.save(state)
            print(f"Run resumed from verified state at {Path(args.run_root).resolve()}")
            return 0
        if args.command == "lessons":
            store = LearningStore(args.learning_root or _default_learning_root())
            if args.lesson_action == "list":
                values = [
                    {
                        "lesson_id": lesson.lesson_id,
                        "role": lesson.role,
                        "scope": lesson.scope,
                        "text": lesson.text,
                        "status": lesson.status,
                        "tags": list(lesson.tags),
                        "evidence_refs": list(lesson.evidence_refs),
                        "retirement_reason": lesson.retirement_reason,
                    }
                    for lesson in sorted(
                        store.current_lessons().values(),
                        key=lambda item: item.lesson_id,
                    )
                ]
                payload = {"verified": store.verify()[0], "lessons": values}
                _emit(payload, args.as_json)
                return 0
            if args.lesson_action == "promote":
                lesson = store.promote(
                    args.lesson_id,
                    verification_refs=tuple(args.evidence),
                    negative_control_refs=tuple(args.negative_control_evidence),
                    human_approved=args.human_approved,
                )
                print(f"Promoted {lesson.lesson_id}")
                return 0
            if args.lesson_action == "retire":
                lesson = store.retire(args.lesson_id, reason=args.reason)
                print(f"Retired {lesson.lesson_id}")
                return 0
            if args.lesson_action == "export":
                output = Path(args.output).resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(store.redacted_export(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(f"Wrote redacted lessons to {output}")
                return 0
        if args.command == "child-receipt" and args.receipt_action == "validate":
            receipt_path = Path(args.file).resolve()
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            validate_child_receipt(
                payload,
                allowed_paths=tuple(args.allowed_path),
                required_criteria=tuple(args.criterion),
            )
            result = {
                "valid": True,
                "status": payload["status"],
                "role": payload["role"],
                "model": payload["model"],
                "effort": payload["effort"],
                "receipt": str(receipt_path),
            }
            _emit(result, args.as_json)
            return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"MochiCode refused: {error}", file=sys.stderr)
        return 2
    return 2


def doctor() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add(
        "platform",
        os.name == "nt",
        "Windows" if os.name == "nt" else "unsupported platform: release commands require Windows",
    )
    add(
        "python",
        sys.version_info >= (3, 13),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    git_executable = shutil.which("git")
    add("git", bool(git_executable), git_executable or "not found")
    codex_name = "codex.cmd" if os.name == "nt" else "codex"
    codex_executable = shutil.which(codex_name) or shutil.which("codex")
    add("codex", bool(codex_executable), codex_executable or "not found")
    if codex_executable:
        version = _read_command([codex_executable, "--version"])
        add("codex_version", version[0] == 0, version[1] or version[2])
        login = _read_command([codex_executable, "login", "status"])
        logged_in = login[0] == 0 and "logged in" in (login[1] + login[2]).lower()
        add("codex_login", logged_in, "ChatGPT login available" if logged_in else "not logged in")
    try:
        load_config(DEFAULT_CONFIG)
        add("configuration", True, str(DEFAULT_CONFIG))
    except (OSError, ValueError) as error:
        add("configuration", False, str(error))
    schema_files = sorted((PLUGIN_ROOT / "schemas").glob("*.json"))
    schemas_ok = bool(schema_files)
    schema_detail = f"{len(schema_files)} schemas"
    for path in schema_files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            schemas_ok = schemas_ok and isinstance(value, dict) and value.get("type") == "object"
        except (OSError, json.JSONDecodeError):
            schemas_ok = False
            schema_detail = f"invalid schema: {path.name}"
            break
    add("schemas", schemas_ok, schema_detail)
    adaptive = _doctor_adaptive_capabilities(codex_executable)
    add(
        "adaptive_capabilities",
        bool(adaptive.get("available") and adaptive.get("catalog_available")),
        _adaptive_capability_detail(adaptive),
    )
    return {
        "ready": all(check["ok"] for check in checks),
        "checks": checks,
        "adaptive_capabilities": adaptive,
    }


def _doctor_adaptive_capabilities(
    codex_executable: str | None,
) -> dict[str, Any]:
    if not codex_executable:
        return {
            "available": False,
            "catalog_available": False,
            "warnings": ["Codex executable not found."],
        }
    try:
        return audit_capabilities(codex_executable, probe_agent_defaults=True)
    except (OSError, RuntimeError, ValueError):
        return {
            "available": False,
            "catalog_available": False,
            "warnings": ["Adaptive capability probe failed."],
        }


def _adaptive_capability_detail(capabilities: dict[str, Any]) -> str:
    catalog = capabilities.get("model_catalog")
    model_count = len(catalog) if isinstance(catalog, list) else 0
    warnings = capabilities.get("warnings")
    if isinstance(warnings, list) and warnings:
        return f"{model_count} models; {warnings[0]}"
    return f"{model_count} models; runtime capability catalog available"


def _platform_only_doctor_payload() -> dict[str, Any]:
    return {
        "ready": False,
        "checks": [
            {
                "name": "platform",
                "ok": False,
                "detail": "unsupported platform: release commands require Windows",
            }
        ],
    }


def run_demo(state_root: Path | None) -> dict[str, Any]:
    base = (state_root or (_default_state_root() / "demos" / uuid.uuid4().hex[:12])).resolve()
    source = base / "source"
    run_root = base / "run"
    source.mkdir(parents=True, exist_ok=False)
    _git(source, "init")
    (source / "README.md").write_text("MochiCode zero-cost demo\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(
        source,
        "-c",
        "user.name=MochiCode",
        "-c",
        "user.email=mochicode@local.invalid",
        "commit",
        "-m",
        "demo baseline",
    )
    result = MochiController(
        load_config(DEFAULT_CONFIG),
        StubRoleProvider(),
        LearningStore(base / "learning"),
    ).run_new(
        goal="Demonstrate the complete zero-cost MochiCode state machine.",
        project=source,
        run_root=run_root,
        run_id="demo-" + uuid.uuid4().hex[:8],
    )
    if result.state.status != "complete":
        raise RuntimeError(
            f"demo did not complete before audit: {result.state.status}"
        )
    ledger_ok, ledger_detail = EvidenceLedger(
        result.run_root / "evidence.jsonl"
    ).verify()
    if not ledger_ok:
        raise RuntimeError("demo evidence ledger is invalid: " + ledger_detail)
    model_calls = MochiController._finished_model_call_count(result.run_root)
    if model_calls != 0:
        raise RuntimeError(
            f"zero-cost demo used {model_calls} model calls"
        )
    payload = _result_payload(
        result.state,
        result.run_root,
        result.integration.branch,
        result.final_review,
    )
    payload.update(
        {
            "backend": "stub",
            "fresh": True,
            "cost_usd": 0,
            "ledger_ok": True,
            "model_calls": model_calls,
            "accepted": sum(
                packet.status.value == "accepted"
                for packet in result.state.packets
            ),
            "total": len(result.state.packets),
        }
    )
    return payload


def run_project(
    *,
    project: Path,
    goal_file: str,
    backend: str,
    run_root: Path | None,
    run_id: str | None,
    learning_root: Path | None = None,
    lesson_trial_id: str | None = None,
    lesson_expected: str | None = None,
) -> dict[str, Any]:
    if goal_file == "-":
        goal = sys.stdin.read()
    else:
        goal = Path(goal_file).read_text(encoding="utf-8")
    actual_run_id = run_id or uuid.uuid4().hex[:12]
    actual_root = (run_root or (_default_state_root() / "runs" / actual_run_id)).resolve()
    config = load_config(DEFAULT_CONFIG)
    learning = LearningStore((learning_root or _default_learning_root()).resolve())
    if (lesson_trial_id is None) != (lesson_expected is None):
        raise ValueError("--lesson-trial and --lesson-expected must be supplied together")
    lesson_trial = (
        None
        if lesson_trial_id is None
        else learning.candidate_trial(
            lesson_trial_id,
            expected=lesson_expected == "true",
        )
    )
    if backend == "codex":
        codex_name = "codex.cmd" if os.name == "nt" else "codex"
        executable = shutil.which(codex_name) or shutil.which("codex")
        if not executable:
            raise RuntimeError("Codex CLI is not available; run `mochicode doctor`")
        provider: Any = CodexRoleProvider(
            CodexCliBackend(executable, config),
            run_root=actual_root,
            plugin_root=PLUGIN_ROOT,
            learning_store=learning,
            lesson_trial=lesson_trial,
        )
    else:
        provider = StubRoleProvider(lesson_trial)
    result = MochiController(config, provider, learning).run_new(
        goal=goal,
        project=project,
        run_root=actual_root,
        run_id=actual_run_id,
    )
    return _result_payload(result.state, result.run_root, result.integration.branch, result.final_review)


def status_payload(run_root: Path, *, verbose: bool) -> dict[str, Any]:
    store = StateStore(Path(run_root).resolve())
    _require_state(store)
    state = store.load()
    store.apply_stop_state(state)
    ledger_ok, ledger_detail = EvidenceLedger(store.root / "evidence.jsonl").verify()
    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "status": state.status,
        "goal": state.goal,
        "stop_requested": state.stop_requested,
        "accepted": sum(packet.status.value == "accepted" for packet in state.packets),
        "total": len(state.packets),
        "ledger_ok": ledger_ok,
        "ledger_detail": ledger_detail,
    }
    if verbose:
        payload["model_calls"] = state.model_calls
        payload["rounds"] = state.rounds
        payload["packets"] = [
            {
                "id": packet.packet_id,
                "status": packet.status.value,
                "attempts": packet.attempts,
                "last_failure": packet.last_failure,
                "fingerprints": list(packet.fingerprints),
            }
            for packet in state.packets
        ]
    return payload


def _result_payload(state: Any, run_root: Path, branch: str, final_review: Any) -> dict[str, Any]:
    return {
        "run_id": state.run_id,
        "status": state.status,
        "run_root": str(run_root),
        "integration_branch": branch,
        "model_calls": state.model_calls,
        "rounds": state.rounds,
        "final_review": final_review,
        "packets": [
            {"id": packet.packet_id, "status": packet.status.value, "attempts": packet.attempts}
            for packet in state.packets
        ],
    }


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if "ready" in payload:
        print("MochiCode is ready." if payload["ready"] else "MochiCode needs attention.")
        for check in payload["checks"]:
            marker = "PASS" if check["ok"] else "FAIL"
            print(f"{marker} {check['name']}: {check['detail']}")
        return
    print(f"MochiCode {payload.get('status', 'unknown')}.")
    if payload.get("run_root"):
        print(f"Review state: {payload['run_root']}")
    if payload.get("integration_branch"):
        print(f"Integration branch: {payload['integration_branch']}")


def _default_state_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "MochiCode"
    return Path.home() / ".local" / "state" / "mochicode"


def _default_learning_root() -> Path:
    return _default_state_root() / "learning"


def _read_command(argv: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=20,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def _require_state(store: StateStore) -> None:
    if not store.state_path.is_file():
        raise ValueError(f"no MochiCode state found at {store.state_path}")
