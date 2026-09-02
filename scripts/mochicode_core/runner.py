from __future__ import annotations

from dataclasses import dataclass
from dataclasses import asdict
from dataclasses import replace
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Protocol

from .config import ControllerConfig
from .contracts import (
    ExecutionMode,
    PacketContract,
    contract_from_dict,
    plan_from_dict,
    validate_final_review,
    validate_review,
)
from .evidence import EvidenceLedger
from .gitops import GitOperationError, GitWorkspaceManager, IntegrationWorkspace, PacketWorkspace
from .learning import LearningStore
from .models import PacketState, PacketStatus, RunBudget, RunState
from .protection import (
    InvalidProtectedPattern,
    ProtectedInputChanged,
    assert_protected_unchanged,
    attempt_fingerprint,
    expand_protected_pattern,
    hash_protected,
)
from .scheduler import DecisionKind, next_decision, record_attempt
from .state import StateStore, exclusive_run_lease
from .verification import (
    BaselineVerdict,
    CommandResult,
    classify_baseline,
    final_verification_passed,
    run_command,
)


class RoleProvider(Protocol):
    def plan(self, goal: str, workspace: Path) -> dict[str, Any]: ...

    def contract(self, packet: PacketState, workspace: Path) -> dict[str, Any]: ...

    def execute(
        self,
        packet: PacketState,
        contract: PacketContract,
        workspace: Path,
        attempt: int,
    ) -> dict[str, Any]: ...

    def review(
        self,
        packet: PacketState,
        contract: PacketContract,
        workspace: Path,
        review_bundle: dict[str, Any],
    ) -> dict[str, Any]: ...

    def final_review(
        self,
        goal: str,
        state: RunState,
        workspace: Path,
        final_bundle: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RunResult:
    state: RunState
    run_root: Path
    integration: IntegrationWorkspace
    final_review: dict[str, Any] | None


class StubRoleProvider:
    def plan(self, goal: str, workspace: Path) -> dict[str, Any]:
        return {
            "summary": "Create one runnable path, one independent support path, then integrate.",
            "packets": [
                {
                    "id": "vertical",
                    "title": "Runnable vertical slice",
                    "goal": "Create the ordinary runnable artifact.",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["app.txt contains the runnable result"],
                    "verification_hints": ["execute the generated focused check"],
                },
                {
                    "id": "support",
                    "title": "Independent support path",
                    "goal": "Create independent support behavior.",
                    "wave": 1,
                    "priority": 2,
                    "vertical_slice": False,
                    "dependencies": [],
                    "acceptance_criteria": ["support.txt contains the support result"],
                    "verification_hints": ["execute the generated focused check"],
                },
                {
                    "id": "integrate",
                    "title": "Integrated user path",
                    "goal": "Join the accepted vertical and support paths.",
                    "wave": 2,
                    "priority": 1,
                    "vertical_slice": False,
                    "dependencies": ["vertical", "support"],
                    "acceptance_criteria": ["integration.txt proves the two paths work together"],
                    "verification_hints": ["execute the generated end-to-end check"],
                },
            ],
        }

    def contract(self, packet: PacketState, workspace: Path) -> dict[str, Any]:
        targets = {
            "vertical": ("app.txt", "runnable\n"),
            "support": ("support.txt", "support\n"),
            "integrate": ("integration.txt", "runnable + support\n"),
        }
        target, expected = targets[packet.packet_id]
        checks = workspace / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        check_path = checks / f"{packet.packet_id}_check.py"
        check_path.write_text(
            "from pathlib import Path\n"
            f"assert Path({target!r}).read_text(encoding='utf-8') == {expected!r}\n",
            encoding="utf-8",
        )
        command = [sys.executable, str(check_path)]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal or packet.title,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": [f"checks/{packet.packet_id}_check.py"],
            "allowed_paths": [target],
            "evidence_requirements": ["raw command result", "protected file hashes"],
        }

    def execute(
        self,
        packet: PacketState,
        contract: PacketContract,
        workspace: Path,
        attempt: int,
    ) -> dict[str, Any]:
        if packet.packet_id == "vertical" and attempt == 1:
            return {
                "summary": "Intentional first-attempt failure used to prove queue rotation.",
                "changed_files": [],
                "commands_run": [],
                "remaining_assumptions": [],
            }
        outputs = {
            "vertical": ("app.txt", "runnable\n"),
            "support": ("support.txt", "support\n"),
            "integrate": ("integration.txt", "runnable + support\n"),
        }
        target, content = outputs[packet.packet_id]
        (workspace / target).write_text(content, encoding="utf-8")
        return {
            "summary": f"Created {target}",
            "changed_files": [target],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(
        self,
        packet: PacketState,
        contract: PacketContract,
        workspace: Path,
        review_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "verdict": "GREEN",
            "findings": [],
            "evidence_summary": "Focused verification passed and protected checks were unchanged.",
        }

    def final_review(
        self,
        goal: str,
        state: RunState,
        workspace: Path,
        final_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        required = {
            "app.txt": "runnable\n",
            "support.txt": "support\n",
            "integration.txt": "runnable + support\n",
        }
        passed = all(
            (workspace / path).is_file()
            and (workspace / path).read_text(encoding="utf-8") == expected
            for path, expected in required.items()
        )
        return {
            "verdict": "MERGE" if passed else "DO_NOT_MERGE",
            "criteria": [
                {
                    "criterion": criterion,
                    "status": "PASS" if passed else "FAIL",
                    "evidence": "integration worktree files",
                }
                for packet in state.packets
                for criterion in packet.acceptance_criteria
            ],
            "remaining_risks": [],
            "merge_recommendation": (
                "Human may merge the integration branch."
                if passed
                else "Do not merge the integration branch."
            ),
        }


class MochiController:
    def __init__(
        self,
        config: ControllerConfig,
        provider: RoleProvider,
        learning_store: LearningStore | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.learning_store = learning_store

    def run_new(
        self,
        *,
        goal: str,
        project: Path,
        run_root: Path,
        run_id: str,
    ) -> RunResult:
        run_root = Path(run_root).resolve()
        with exclusive_run_lease(run_root):
            return self._run_new_owned(
                goal=goal,
                project=project,
                run_root=run_root,
                run_id=run_id,
            )

    def _run_new_owned(
        self,
        *,
        goal: str,
        project: Path,
        run_root: Path,
        run_id: str,
    ) -> RunResult:
        if not goal.strip():
            raise ValueError("goal must not be empty")
        run_root = Path(run_root).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        store = StateStore(run_root)
        ledger = EvidenceLedger(run_root / "evidence.jsonl")
        git = GitWorkspaceManager()
        integration = git.create_integration(project, run_root, run_id)
        started = time.time()
        plan_data = self.provider.plan(goal, integration.path)
        budget = RunBudget(
            max_model_calls=self.config.max_model_calls,
            max_rounds=self.config.max_rounds,
            max_attempts_per_packet=self.config.max_attempts_per_packet,
            max_wall_seconds=self.config.max_wall_seconds,
        )
        state = plan_from_dict(
            plan_data,
            run_id=run_id,
            goal=goal,
            project_root=str(integration.source_root),
            budget=budget,
            started_at=started,
            source_head=integration.source_head,
            source_branch=integration.source_branch,
            integration_head=git.head(integration.path),
        )
        state.model_calls = 1
        plan_receipt = self._write_json_receipt(
            run_root,
            run_root / "plan.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
                "project_root": str(integration.source_root),
                "source_head": integration.source_head,
                "source_branch": integration.source_branch,
                "initial_integration_head": state.integration_head,
                "started_at": started,
                "budget": asdict(budget),
                "plan": plan_data,
            },
        )
        store.save(state)
        ledger.append(
            {
                "event": "plan_created",
                "run_id": run_id,
                "role": "sol_plan",
                "packet_count": len(state.packets),
                "receipts": [plan_receipt],
            }
        )
        return self._drive(
            state=state,
            integration=integration,
            run_root=run_root,
            store=store,
            ledger=ledger,
            git=git,
        )

    def resume_existing(self, *, run_root: Path) -> RunResult:
        run_root = Path(run_root).resolve()
        with exclusive_run_lease(run_root):
            return self._resume_existing_owned(run_root=run_root)

    def _resume_existing_owned(self, *, run_root: Path) -> RunResult:
        run_root = Path(run_root).resolve()
        store = StateStore(run_root)
        state = store.load()
        ledger = EvidenceLedger(run_root / "evidence.jsonl")
        ledger_ok, ledger_reason = ledger.verify()
        if not ledger_ok:
            raise ValueError(f"cannot resume an invalid evidence chain: {ledger_reason}")
        expected_integration_head = self._validate_persisted_run(
            state=state,
            run_root=run_root,
            ledger=ledger,
            allow_lagging_contract_refusal=True,
        )
        if not state.source_head or not state.source_branch or not expected_integration_head:
            raise GitOperationError("run has no pinned source baseline; start a new run")
        git = GitWorkspaceManager()
        integration = git.open_integration(
            Path(state.project_root),
            run_root,
            state.run_id,
            expected_source_head=state.source_head,
            expected_source_branch=state.source_branch,
            expected_integration_head=expected_integration_head,
        )
        contract_recovery_performed = self._recover_lagging_terminal_attempt(
            state=state,
            store=store,
            ledger=ledger,
        )
        contract_recovery_performed = (
            self._recover_interrupted_contract_attempt(
                state=state,
                store=store,
                ledger=ledger,
            )
            or contract_recovery_performed
        )
        self._recover_interrupted_implementation_attempts(
            state=state,
            store=store,
            ledger=ledger,
        )
        self._validate_persisted_run(
            state=state,
            run_root=run_root,
            ledger=ledger,
        )
        if contract_recovery_performed:
            self._assert_complete_pending_queue(state)
        store.resume()
        state.stop_requested = False
        for packet in state.packets:
            if packet.status in {PacketStatus.RUNNING, PacketStatus.REVIEWING}:
                packet.status = PacketStatus.PENDING
            if packet.status == PacketStatus.PENDING and packet.packet_id not in state.queue:
                if contract_recovery_performed:
                    raise ValueError(
                        "pending queue changed after authorized Terra contract recovery"
                    )
                state.queue.append(packet.packet_id)
        state.status = "running"
        state.model_calls = max(
            state.model_calls,
            self._finished_model_call_count(run_root),
        )
        state.updated_at = time.time()
        store.save(state)
        ledger.append(
            {
                "event": "run_resumed",
                "run_id": state.run_id,
                "model_calls": state.model_calls,
                "rounds": state.rounds,
            }
        )
        return self._drive(
            state=state,
            integration=integration,
            run_root=run_root,
            store=store,
            ledger=ledger,
            git=git,
        )

    def reaudit_parked_verify_packet(
        self,
        *,
        run_root: Path,
        packet_id: str,
    ) -> RunResult:
        run_root = Path(run_root).resolve()
        with exclusive_run_lease(run_root):
            return self._reaudit_parked_verify_packet_owned(
                run_root=run_root,
                packet_id=packet_id,
            )

    def _reaudit_parked_verify_packet_owned(
        self,
        *,
        run_root: Path,
        packet_id: str,
    ) -> RunResult:
        run_root = Path(run_root).resolve()
        store = StateStore(run_root)
        state = store.load()
        packet = state.packet(packet_id)
        if packet.status != PacketStatus.PARKED:
            raise ValueError(f"packet {packet_id!r} is not parked")
        if packet.last_failure != "verify-only review was not GREEN":
            raise ValueError("packet was not parked by a repairable verify-only review gap")
        ledger = EvidenceLedger(run_root / "evidence.jsonl")
        ok, reason = ledger.verify()
        if not ok:
            raise ValueError(f"cannot re-audit an invalid evidence chain: {reason}")
        self._validate_persisted_run(
            state=state,
            run_root=run_root,
            ledger=ledger,
        )
        verified_receipts = self._verify_ledger_receipts(run_root, ledger)
        records = ledger.records()
        git = GitWorkspaceManager()
        integration = git.open_integration(
            Path(state.project_root),
            run_root,
            state.run_id,
            expected_source_head=state.source_head,
            expected_source_branch=state.source_branch,
            expected_integration_head=state.integration_head,
        )
        store.resume()
        state.stop_requested = False
        state.model_calls = max(
            state.model_calls,
            self._finished_model_call_count(run_root),
        )
        contract_receipt = self._latest_contract_receipt(
            packet.packet_id,
            records,
            verified_receipts,
        )
        contract_data = self._read_json_receipt(run_root, contract_receipt)
        contract = contract_from_dict(contract_data, packet)
        if contract.execution_mode != ExecutionMode.VERIFY_ONLY:
            raise ValueError("review repair is limited to verify-only packets")

        repair_root = run_root / "review-repairs" / packet.packet_id
        repair_root.mkdir(parents=True, exist_ok=True)
        protected_before = hash_protected(integration.path, contract.protected_patterns)
        if not protected_before:
            raise ValueError("review repair contract protects no existing measurement input")
        verifier_inputs: list[tuple[str, ...]] = []
        contract_violation = self._contract_workspace_violation(
            contract,
            integration.path,
            set(protected_before),
            verifier_inputs=verifier_inputs,
        )
        if contract_violation:
            raise ValueError("re-audit contract refused: " + contract_violation)
        integration_snapshot = git.workspace_fingerprint(integration.path, "HEAD")
        results: list[CommandResult] = []
        verification_receipts: list[dict[str, Any]] = []
        for index, argv in enumerate(contract.final_argvs, start=1):
            workspace_fingerprint_before = git.workspace_fingerprint(
                integration.path,
                "HEAD",
            )
            if workspace_fingerprint_before != integration_snapshot:
                raise ValueError("re-audit verifier modified integration artifacts")
            result = run_command(argv, cwd=integration.path, timeout_seconds=300)
            results.append(result)
            workspace_fingerprint_after = git.workspace_fingerprint(
                integration.path,
                "HEAD",
            )
            verification_receipts.append(
                self._write_command_result(
                    run_root,
                    repair_root / f"verification-{index}.json",
                    result,
                    protected_verifier_inputs=verifier_inputs[index],
                    contract_argv=contract.final_argvs[index - 1],
                    workspace_fingerprint_before=workspace_fingerprint_before,
                    workspace_fingerprint_after=workspace_fingerprint_after,
                )
            )
            protected_after = hash_protected(
                integration.path,
                contract.protected_patterns,
            )
            assert_protected_unchanged(protected_before, protected_after)
            if workspace_fingerprint_after != integration_snapshot:
                raise ValueError("re-audit verifier modified integration artifacts")
            if not final_verification_passed(result):
                raise ValueError(
                    f"re-audit verifier failed with exit {result.returncode}: {argv}"
                )
        protected_after = hash_protected(integration.path, contract.protected_patterns)
        assert_protected_unchanged(protected_before, protected_after)
        if git.workspace_fingerprint(integration.path, "HEAD") != integration_snapshot:
            raise ValueError("re-audit verifier modified integration artifacts")
        changed_paths = git.changed_paths_between(
            integration.path,
            integration.source_head,
        )
        integration_diff = git.diff_between(
            integration.path,
            integration.source_head,
        )
        artifact_hashes = hash_protected(integration.path, tuple(changed_paths))
        if not self._reserve_model_call(state):
            raise ValueError("model-call budget exhausted before review repair")
        review = self.provider.review(
            packet,
            contract,
            integration.path,
            {
                "goal": state.goal,
                "contract": contract_data,
                "diff": integration_diff,
                "changed_paths": list(changed_paths),
                "artifact_hashes": artifact_hashes,
                "source_head": integration.source_head,
                "integration_head": git.head(integration.path),
                "protected_before": protected_before,
                "protected_after": protected_after,
                "verification": [asdict(item) for item in results],
                "repair_context": (
                    "Prior reviews were RED only because the controller omitted the committed "
                    "integration diff. No project artifact changed for this re-audit."
                ),
            },
        )
        review_receipt = self._write_json_receipt(
            run_root,
            repair_root / "review.json",
            review,
        )
        review_error = ""
        try:
            validate_review(review)
        except ValueError as error:
            review_error = str(error)
        if review_error or review.get("verdict") != "GREEN":
            ledger.append(
                {
                    "event": "review_repair_failed",
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "verdict": review.get("verdict"),
                    "reason": review_error or "review verdict was not GREEN",
                    "prior_attempts_preserved": packet.attempts,
                    "protected_before": protected_before,
                    "protected_after": protected_after,
                    "receipts": [
                        contract_receipt,
                        *verification_receipts,
                        review_receipt,
                    ],
                }
            )
            store.save(state)
            return RunResult(state, run_root, integration, None)

        if git.workspace_fingerprint(integration.path, "HEAD") != integration_snapshot:
            raise ValueError("re-audit review modified integration artifacts")
        protected_after = hash_protected(integration.path, contract.protected_patterns)
        assert_protected_unchanged(protected_before, protected_after)
        packet.status = PacketStatus.ACCEPTED
        packet.last_failure = None
        if packet.packet_id in state.queue:
            state.queue.remove(packet.packet_id)
        state.status = "running"
        state.updated_at = time.time()
        ledger.append(
            {
                "event": "review_repaired",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "verdict": "GREEN",
                "prior_attempts_preserved": packet.attempts,
                "source_head": integration.source_head,
                "integration_head": git.head(integration.path),
                "changed_paths": list(changed_paths),
                "artifact_hashes": artifact_hashes,
                "protected_before": protected_before,
                "protected_after": protected_after,
                "workspace_fingerprint": integration_snapshot,
                "receipts": [
                    contract_receipt,
                    *verification_receipts,
                    review_receipt,
                ],
            }
        )
        store.save(state)
        return self._drive(
            state=state,
            integration=integration,
            run_root=run_root,
            store=store,
            ledger=ledger,
            git=git,
        )

    def _drive(
        self,
        *,
        state: RunState,
        integration: IntegrationWorkspace,
        run_root: Path,
        store: StateStore,
        ledger: EvidenceLedger,
        git: GitWorkspaceManager,
    ) -> RunResult:
        final_review: dict[str, Any] | None = None
        while True:
            store.apply_stop_state(state)
            decision = next_decision(state, now=time.time())
            if decision.kind == DecisionKind.RUN and decision.packet_id is not None:
                self._run_packet(
                    state=state,
                    packet=state.packet(decision.packet_id),
                    integration=integration,
                    run_root=run_root,
                    store=store,
                    ledger=ledger,
                    git=git,
                )
                continue
            if decision.kind == DecisionKind.DONE:
                if state.model_calls >= state.budget.max_model_calls:
                    state.status = "budget_exhausted"
                    break
                verified_identity = self._run_final_integration_verification(
                    state=state,
                    run_root=run_root,
                    store=store,
                    ledger=ledger,
                    integration=integration,
                    git=git,
                )
                if verified_identity is None:
                    break
                if self._stop_at_boundary(state=state, store=store):
                    break
                state.model_calls += 1
                final_bundle = self._build_final_bundle(
                    state=state,
                    run_root=run_root,
                    ledger=ledger,
                    integration=integration,
                    git=git,
                )
                final_bundle["verified_integration_identity"] = verified_identity
                final_review = self.provider.final_review(
                    state.goal,
                    state,
                    integration.path,
                    final_bundle,
                )
                final_review_receipt = self._write_json_receipt(
                    run_root,
                    run_root / "final-reviews" / f"{state.model_calls:03d}.json",
                    final_review,
                )
                final_review_error = ""
                if (
                    git.head(integration.path) != verified_identity["head"]
                    or bool(git.working_status(integration.path))
                    or git.workspace_fingerprint(integration.path, "HEAD")
                    != verified_identity["fingerprint"]
                ):
                    final_review_error = (
                        "integration identity changed between final verification and Sol review"
                    )
                try:
                    validate_final_review(
                        final_review,
                        expected_criteria=tuple(
                            criterion
                            for packet in state.packets
                            for criterion in packet.acceptance_criteria
                        ),
                    )
                except ValueError as error:
                    final_review_error = str(error)
                if final_review_error:
                    ledger.append(
                        {
                            "event": "final_review_invalid",
                            "run_id": state.run_id,
                            "role": "sol_final",
                            "reason": final_review_error,
                            "receipts": [final_review_receipt],
                        }
                    )
                    state.status = "review_failed"
                    break
                ledger.append(
                    {
                        "event": "final_review",
                        "run_id": state.run_id,
                        "role": "sol_final",
                        "verdict": final_review.get("verdict"),
                        "integration_head": verified_identity["head"],
                        "integration_fingerprint": verified_identity["fingerprint"],
                        "receipts": [final_review_receipt],
                    }
                )
                if final_review.get("verdict") != "MERGE":
                    state.status = "review_failed"
                break
            if decision.kind == DecisionKind.REPLAN:
                state.replans += 1
                state.status = "blocked"
                ledger.append(
                    {
                        "event": "replan_required",
                        "run_id": state.run_id,
                        "reason": decision.reason,
                    }
                )
                break
            state.status = "stopped" if decision.kind == DecisionKind.STOP else "blocked"
            ledger.append(
                {
                    "event": "run_stopped",
                    "run_id": state.run_id,
                    "reason": decision.reason,
                    "status": state.status,
                }
            )
            break

        state.updated_at = time.time()
        store.save(state)
        return RunResult(
            state=state,
            run_root=run_root,
            integration=integration,
            final_review=final_review,
        )

    def repeat_final_review(self, *, run_root: Path) -> RunResult:
        run_root = Path(run_root).resolve()
        with exclusive_run_lease(run_root):
            return self._repeat_final_review_owned(run_root=run_root)

    def _repeat_final_review_owned(self, *, run_root: Path) -> RunResult:
        run_root = Path(run_root).resolve()
        store = StateStore(run_root)
        state = store.load()
        if not all(
            packet.status in {PacketStatus.ACCEPTED, PacketStatus.ALREADY_SATISFIED}
            for packet in state.packets
        ):
            raise ValueError("final review can repeat only after every packet is accepted")
        ledger = EvidenceLedger(run_root / "evidence.jsonl")
        ok, reason = ledger.verify()
        if not ok:
            raise ValueError(f"cannot repeat final review with an invalid ledger: {reason}")
        self._validate_persisted_run(
            state=state,
            run_root=run_root,
            ledger=ledger,
        )
        git = GitWorkspaceManager()
        integration = git.open_integration(
            Path(state.project_root),
            run_root,
            state.run_id,
            expected_source_head=state.source_head,
            expected_source_branch=state.source_branch,
            expected_integration_head=state.integration_head,
        )
        store.resume()
        state.stop_requested = False
        state.model_calls = max(
            state.model_calls,
            self._finished_model_call_count(run_root),
        )
        if state.model_calls >= state.budget.max_model_calls:
            raise ValueError("model-call budget exhausted before repeated final review")
        verified_identity = self._run_final_integration_verification(
            state=state,
            run_root=run_root,
            store=store,
            ledger=ledger,
            integration=integration,
            git=git,
        )
        if verified_identity is None:
            return RunResult(state, run_root, integration, None)
        if self._stop_at_boundary(state=state, store=store):
            return RunResult(state, run_root, integration, None)
        if not self._reserve_model_call(state):
            raise ValueError("model-call budget exhausted before repeated final review")
        bundle = self._build_final_bundle(
            state=state,
            run_root=run_root,
            ledger=ledger,
            integration=integration,
            git=git,
        )
        bundle["verified_integration_identity"] = verified_identity
        review = self.provider.final_review(
            state.goal,
            state,
            integration.path,
            bundle,
        )
        final_review_receipt = self._write_json_receipt(
            run_root,
            run_root / "final-reviews" / f"{state.model_calls:03d}.json",
            review,
        )
        review_error = ""
        if (
            git.head(integration.path) != verified_identity["head"]
            or bool(git.working_status(integration.path))
            or git.workspace_fingerprint(integration.path, "HEAD")
            != verified_identity["fingerprint"]
        ):
            review_error = (
                "integration identity changed between final verification and Sol review"
            )
        try:
            validate_final_review(
                review,
                expected_criteria=tuple(
                    criterion
                    for packet in state.packets
                    for criterion in packet.acceptance_criteria
                ),
            )
        except ValueError as error:
            review_error = str(error)
        if review_error:
            ledger.append(
                {
                    "event": "final_review_invalid",
                    "run_id": state.run_id,
                    "role": "sol_final",
                    "reason": review_error,
                    "integration_head": git.head(integration.path),
                    "receipts": [final_review_receipt],
                }
            )
            state.status = "review_failed"
            state.updated_at = time.time()
            store.save(state)
            return RunResult(state, run_root, integration, review)
        ledger.append(
            {
                "event": "final_review_repeated",
                "run_id": state.run_id,
                "role": "sol_final",
                "verdict": review.get("verdict"),
                    "reason": "prior final review lacked itemized evidence receipts",
                    "integration_head": verified_identity["head"],
                    "integration_fingerprint": verified_identity["fingerprint"],
                "receipts": [final_review_receipt],
            }
        )
        state.status = "complete" if review.get("verdict") == "MERGE" else "review_failed"
        state.updated_at = time.time()
        store.save(state)
        return RunResult(state, run_root, integration, review)

    def _run_packet(
        self,
        *,
        state: RunState,
        packet: PacketState,
        integration: IntegrationWorkspace,
        run_root: Path,
        store: StateStore,
        ledger: EvidenceLedger,
        git: GitWorkspaceManager,
    ) -> None:
        attempt = packet.attempts + 1
        workspace = git.create_packet(
            integration,
            run_root,
            f"{packet.packet_id}-a{attempt}",
        )
        attempt_root = run_root / "attempts" / packet.packet_id / workspace.path.name
        attempt_root.mkdir(parents=True, exist_ok=False)

        if self._stop_at_boundary(state=state, store=store, packet=packet):
            return
        if not self._reserve_model_call(state):
            packet.status = PacketStatus.BLOCKED
            packet.last_failure = "model-call budget exhausted before Terra contract"
            store.save(state)
            return
        packet_statuses_reserved = [
            {"packet_id": item.packet_id, "status": item.status.value}
            for item in state.packets
        ]
        packet_statuses_before = [
            {
                "packet_id": item.packet_id,
                "status": (
                    PacketStatus.PENDING.value
                    if item.packet_id == packet.packet_id
                    else item.status.value
                ),
            }
            for item in state.packets
        ]
        contract_reservation = ledger.append(
            {
                "event": "contract_attempt_reserved",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "packet_attempts_before": packet.attempts,
                "packet_fingerprints_before": list(packet.fingerprints),
                "rounds_before": state.rounds,
                "packet_status_before": PacketStatus.PENDING.value,
                "packet_status_reserved": packet.status.value,
                "model_calls_before": state.model_calls - 1,
                "model_calls_reserved": state.model_calls,
                "queue_before": list(state.queue),
                "packet_statuses_before": packet_statuses_before,
                "packet_statuses_reserved": packet_statuses_reserved,
            }
        )
        store.save(state)
        contract_data = self.provider.contract(packet, workspace.path)
        if bool(getattr(self.provider, "last_call_reused", False)):
            state.model_calls -= 1
        contract = contract_from_dict(contract_data, packet)
        contract_path = attempt_root / "contract.json"
        contract_receipt = self._write_json_receipt(
            run_root,
            contract_path,
            contract_data,
        )
        terra_workspace = workspace
        initial_path_statuses = git.changed_path_statuses_since(
            workspace,
            integration.branch,
        )
        git.stage_all(terra_workspace)
        staged_path_statuses = git.staged_path_statuses_since(
            terra_workspace,
            integration.branch,
        )
        staged_diff = git.staged_diff_text(terra_workspace, integration.branch)
        post_stage_path_statuses = git.unstaged_path_statuses(terra_workspace)
        staged_contract_receipt = self._write_json_receipt(
            run_root,
            attempt_root / "contract-staged.json",
            {
                "schema_version": 1,
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "integration_parent": integration.branch,
                "initial_path_statuses": self._path_status_rows(initial_path_statuses),
                "staged_path_statuses": self._path_status_rows(staged_path_statuses),
                "staged_diff": staged_diff,
                "staged_diff_sha256": hashlib.sha256(
                    staged_diff.encode("utf-8")
                ).hexdigest(),
                "post_stage_path_statuses": self._path_status_rows(
                    post_stage_path_statuses
                ),
            },
        )
        terra_changed_paths = tuple(
            sorted(
                {
                    path
                    for _, path in (*staged_path_statuses, *post_stage_path_statuses)
                }
            )
        )
        terra_disallowed = [
            path
            for _, path in staged_path_statuses
            if not self._terra_contract_path_allowed(path)
        ]
        terra_non_additive = [
            path for status, path in staged_path_statuses if status != "A"
        ]
        if terra_disallowed or terra_non_additive or post_stage_path_statuses:
            reasons = []
            if terra_disallowed:
                reasons.append(
                    "modified non-check paths: " + ", ".join(terra_disallowed)
                )
            if terra_non_additive:
                reasons.append(
                    "modified or deleted existing checks: "
                    + ", ".join(terra_non_additive)
                )
            if post_stage_path_statuses:
                reasons.append(
                    "worktree changed after staging: "
                    + ", ".join(path for _, path in post_stage_path_statuses)
                )
            failure_reason = "Terra contract " + "; ".join(reasons)
            failure_fingerprint = attempt_fingerprint(
                staged_diff,
                1,
                failure_reason + "\nchanged paths: " + ", ".join(terra_changed_paths),
            )
            record_attempt(
                state,
                packet.packet_id,
                success=False,
                fingerprint=failure_fingerprint,
                failure_reason=failure_reason,
            )
            ledger.append(
                {
                    "event": "contract_refused",
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "attempt": attempt,
                    "fingerprint": failure_fingerprint,
                    "reason": failure_reason,
                    "refusal_kind": "unsafe-terra-write",
                    "contract_reservation_hash": contract_reservation["record_hash"],
                    "changed_paths": list(terra_changed_paths),
                    "path_statuses": [
                        {"status": status, "path": path}
                        for status, path in staged_path_statuses
                    ],
                    "disallowed_paths": terra_disallowed,
                    "non_additive_check_paths": terra_non_additive,
                    "post_stage_path_statuses": [
                        {"status": status, "path": path}
                        for status, path in post_stage_path_statuses
                    ],
                    "receipts": [contract_receipt, staged_contract_receipt],
                }
            )
            ledger.append(
                {
                    "event": "attempt_finished",
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "attempt": attempt,
                    "success": False,
                    "fingerprint": failure_fingerprint,
                    "reason": failure_reason,
                    "changed_paths": list(terra_changed_paths),
                    "contract_refused": True,
                    "refusal_kind": "unsafe-terra-write",
                    "contract_reservation_hash": contract_reservation["record_hash"],
                    "model_calls": state.model_calls,
                    "receipts": [contract_receipt, staged_contract_receipt],
                }
            )
            store.save(state)
            return
        contract_head, contract_changed = git.commit_staged(
            terra_workspace,
            f"contract: {packet.packet_id} attempt {attempt}",
        )
        committed_diff = git.diff_between(
            terra_workspace.path,
            integration.branch,
            contract_head,
        )
        committed_path_statuses = git.path_statuses_between(
            terra_workspace.path,
            integration.branch,
            contract_head,
        )
        post_commit_staged_statuses = git.staged_path_statuses_since(
            terra_workspace,
            "HEAD",
        )
        post_commit_unstaged_statuses = git.unstaged_path_statuses(terra_workspace)
        contract_diff_matches = committed_diff == staged_diff
        contract_path_statuses_match = committed_path_statuses == staged_path_statuses
        contract_commit_receipt = self._write_json_receipt(
            run_root,
            attempt_root / "contract-commit.json",
            {
                "schema_version": 1,
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "integration_parent": integration.branch,
                "contract_head": contract_head,
                "contract_changed": contract_changed,
                "staged_receipt_sha256": staged_contract_receipt["sha256"],
                "staged_diff_sha256": hashlib.sha256(
                    staged_diff.encode("utf-8")
                ).hexdigest(),
                "committed_diff": committed_diff,
                "committed_diff_sha256": hashlib.sha256(
                    committed_diff.encode("utf-8")
                ).hexdigest(),
                "staged_path_statuses": self._path_status_rows(staged_path_statuses),
                "committed_path_statuses": self._path_status_rows(
                    committed_path_statuses
                ),
                "diff_matches": contract_diff_matches,
                "path_statuses_match": contract_path_statuses_match,
                "post_commit_staged_path_statuses": self._path_status_rows(
                    post_commit_staged_statuses
                ),
                "post_commit_unstaged_path_statuses": self._path_status_rows(
                    post_commit_unstaged_statuses
                ),
            },
        )
        post_commit_statuses = tuple(
            sorted(
                {
                    *post_commit_staged_statuses,
                    *post_commit_unstaged_statuses,
                },
                key=lambda item: item[1],
            )
        )
        if (
            not contract_diff_matches
            or not contract_path_statuses_match
            or post_commit_statuses
        ):
            reasons = []
            if not contract_diff_matches:
                reasons.append("committed contract diff did not match staged diff")
            if not contract_path_statuses_match:
                reasons.append("committed contract paths did not match staged paths")
            if post_commit_statuses:
                reasons.append(
                    "worktree changed after contract commit: "
                    + ", ".join(path for _, path in post_commit_statuses)
                )
            failure_reason = "Terra contract " + "; ".join(reasons)
            changed_paths = tuple(
                sorted(
                    {
                        path
                        for _, path in (
                            *staged_path_statuses,
                            *post_commit_statuses,
                        )
                    }
                )
            )
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason=failure_reason,
                refusal_kind="unsafe-terra-write",
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(contract_commit_receipt["sha256"]),
                changed_paths=changed_paths,
                receipts=[
                    contract_receipt,
                    staged_contract_receipt,
                    contract_commit_receipt,
                ],
                store=store,
                ledger=ledger,
            )
            return
        workspace = git.create_implementation(
            terra_workspace,
            attempt_root / "implementation",
            contract_head,
        )
        contract_receipts = [
            contract_receipt,
            staged_contract_receipt,
            contract_commit_receipt,
        ]
        protected_pattern_failures: list[str] = []
        for pattern in contract.protected_patterns:
            try:
                matches = expand_protected_pattern(workspace.path, pattern)
            except InvalidProtectedPattern as error:
                protected_pattern_failures.append(str(error))
                continue
            if not matches:
                protected_pattern_failures.append(
                    f"protected pattern matches no existing file: {pattern}"
                )
        if protected_pattern_failures:
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason="Terra contract refused: "
                + "; ".join(protected_pattern_failures),
                refusal_kind="empty-protection",
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(contract_receipt["sha256"]),
                changed_paths=(),
                receipts=contract_receipts,
                store=store,
                ledger=ledger,
            )
            return
        protected_before = hash_protected(workspace.path, contract.protected_patterns)
        if not protected_before:
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason="Terra contract protects no existing measurement input",
                refusal_kind="empty-protection",
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(contract_receipt["sha256"]),
                changed_paths=(),
                receipts=contract_receipts,
                store=store,
                ledger=ledger,
            )
            return
        baseline_argv = self._rebase_workspace_argv(
            contract.baseline_argv,
            source_root=terra_workspace.path,
            target_root=workspace.path,
        )
        final_argvs = tuple(
            self._rebase_workspace_argv(
                argv,
                source_root=terra_workspace.path,
                target_root=workspace.path,
            )
            for argv in contract.final_argvs
        )
        verifier_contract = replace(
            contract,
            baseline_argv=baseline_argv,
            final_argvs=final_argvs,
        )
        verifier_inputs: list[tuple[str, ...]] = []
        contract_violation = self._contract_workspace_violation(
            verifier_contract,
            workspace.path,
            set(protected_before),
            verifier_inputs=verifier_inputs,
        )
        if contract_violation:
            refusal_kind = (
                "write-overlap"
                if contract_violation.startswith(
                    "protected measurement inputs overlap Luna write paths"
                )
                else "workspace-contract"
            )
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason="Terra contract refused: " + contract_violation,
                refusal_kind=refusal_kind,
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(contract_receipt["sha256"]),
                changed_paths=(),
                receipts=contract_receipts,
                store=store,
                ledger=ledger,
            )
            return
        if self._stop_at_boundary(state=state, store=store, packet=packet):
            return
        baseline_snapshot = git.workspace_fingerprint(workspace.path, contract_head)
        baseline = run_command(
            baseline_argv,
            cwd=workspace.path,
            timeout_seconds=300,
        )
        baseline_after_snapshot = git.workspace_fingerprint(
            workspace.path,
            contract_head,
        )
        baseline_receipt = self._write_command_result(
            run_root,
            attempt_root / "baseline.json",
            baseline,
            protected_verifier_inputs=verifier_inputs[0],
            contract_argv=contract.baseline_argv,
            workspace_fingerprint_before=baseline_snapshot,
            workspace_fingerprint_after=baseline_after_snapshot,
        )
        baseline_verdict = classify_baseline(
            baseline,
            expected_failure_codes=contract.expected_failure_codes,
        )
        baseline_failure = ""
        if baseline_after_snapshot != baseline_snapshot:
            baseline_failure = "baseline verifier modified workspace artifacts"
            baseline_verdict = BaselineVerdict.REFUSED
        protected_after_baseline = hash_protected(
            workspace.path,
            contract.protected_patterns,
        )
        try:
            assert_protected_unchanged(protected_before, protected_after_baseline)
        except ProtectedInputChanged as error:
            baseline_failure = str(error)
            baseline_verdict = BaselineVerdict.REFUSED
        ledger.append(
            {
                "event": "baseline",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "verdict": baseline_verdict.value,
                "exit_code": baseline.returncode,
                "reason": baseline_failure,
                "receipts": [*contract_receipts, baseline_receipt],
            }
        )
        if self._stop_at_boundary(state=state, store=store, packet=packet):
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason="Terra contract stopped after baseline verification",
                refusal_kind="stopped-after-baseline",
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(baseline_receipt["sha256"]),
                changed_paths=(),
                receipts=[*contract_receipts, baseline_receipt],
                store=store,
                ledger=ledger,
            )
            return
        if contract.execution_mode == ExecutionMode.VERIFY_ONLY:
            self._run_verify_only_packet(
                state=state,
                packet=packet,
                contract=contract,
                contract_data=contract_data,
                run_root=run_root,
                workspace=workspace,
                attempt=attempt,
                attempt_root=attempt_root,
                protected_before=protected_before,
                contract_head=contract_head,
                baseline_workspace_fingerprint=baseline_after_snapshot,
                baseline=baseline,
                baseline_verdict=baseline_verdict,
                contract_changed=contract_changed,
                integration=integration,
                terminal_status=PacketStatus.ACCEPTED,
                contract_receipt=contract_receipt,
                contract_diff_receipts=contract_receipts[1:],
                baseline_receipt=baseline_receipt,
                baseline_argv=baseline_argv,
                final_argvs=final_argvs,
                verifier_inputs=tuple(verifier_inputs),
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                store=store,
                ledger=ledger,
                git=git,
            )
            return
        if baseline_verdict == BaselineVerdict.ALREADY_SATISFIED:
            self._run_verify_only_packet(
                state=state,
                packet=packet,
                contract=contract,
                contract_data=contract_data,
                run_root=run_root,
                workspace=workspace,
                attempt=attempt,
                attempt_root=attempt_root,
                protected_before=protected_before,
                contract_head=contract_head,
                baseline_workspace_fingerprint=baseline_after_snapshot,
                baseline=baseline,
                baseline_verdict=baseline_verdict,
                contract_changed=contract_changed,
                integration=integration,
                terminal_status=PacketStatus.ALREADY_SATISFIED,
                contract_receipt=contract_receipt,
                contract_diff_receipts=contract_receipts[1:],
                baseline_receipt=baseline_receipt,
                baseline_argv=baseline_argv,
                final_argvs=final_argvs,
                verifier_inputs=tuple(verifier_inputs),
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                store=store,
                ledger=ledger,
                git=git,
            )
            return
        if baseline_verdict == BaselineVerdict.REFUSED:
            baseline_reason = (
                baseline_failure
                or "baseline verifier did not produce a valid failure"
            )
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason="Terra contract baseline refused: " + baseline_reason,
                refusal_kind="baseline",
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(baseline_receipt["sha256"]),
                changed_paths=(),
                receipts=[*contract_receipts, baseline_receipt],
                store=store,
                ledger=ledger,
            )
            return

        if not self._reserve_model_call(state):
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason="Terra contract model-call budget exhausted before Luna execution",
                refusal_kind="pre-luna-model-budget",
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(contract_receipt["sha256"]),
                changed_paths=(),
                receipts=[*contract_receipts, baseline_receipt],
                store=store,
                ledger=ledger,
            )
            return
        if packet.implementation_attempts >= state.budget.max_attempts_per_packet:
            self._finish_contract_refusal(
                state=state,
                packet=packet,
                attempt=attempt,
                reason=(
                    "Terra contract implementation-attempt budget exhausted before "
                    "Luna execution"
                ),
                refusal_kind="pre-luna-implementation-cap",
                contract_reservation_hash=str(contract_reservation["record_hash"]),
                fingerprint_seed=str(contract_receipt["sha256"]),
                changed_paths=(),
                receipts=[*contract_receipts, baseline_receipt],
                store=store,
                ledger=ledger,
            )
            return
        packet.implementation_attempts += 1
        packet.active_implementation_attempt = attempt
        ledger.append(
            {
                "event": "implementation_attempt_reserved",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "implementation_attempts": packet.implementation_attempts,
                "active_implementation_attempt": packet.active_implementation_attempt,
                "packet_attempts_before": packet.attempts,
                "packet_fingerprints_before": list(packet.fingerprints),
                "rounds_before": state.rounds,
                "model_calls_reserved": state.model_calls,
                "queue_before": list(state.queue),
                "packet_statuses_reserved": [
                    {"packet_id": item.packet_id, "status": item.status.value}
                    for item in state.packets
                ],
                "contract_reservation_hash": contract_reservation["record_hash"],
            }
        )
        store.save(state)
        implementation = self.provider.execute(
            packet,
            contract,
            workspace.path,
            attempt,
        )
        implementation_receipt = self._write_json_receipt(
            run_root,
            attempt_root / "implementation.json",
            implementation,
        )

        changed_paths = git.changed_paths_since(workspace, contract_head)
        pytest_contract = self._contract_uses_pytest(contract)
        implementation_snapshot = git.workspace_fingerprint(
            workspace.path,
            contract_head,
        )
        disallowed = [
            path
            for path in changed_paths
            if self._python_startup_hook_path(path)
            or (pytest_contract and self._pytest_harness_path(path))
            or not any(fnmatch.fnmatch(path, pattern) for pattern in contract.allowed_paths)
        ]
        verification_results: list[CommandResult] = []
        verification_receipts: list[dict[str, Any]] = []
        protected_error = ""
        try:
            protected_after = hash_protected(workspace.path, contract.protected_patterns)
            assert_protected_unchanged(protected_before, protected_after)
        except ProtectedInputChanged as error:
            protected_after = hash_protected(workspace.path, contract.protected_patterns)
            protected_error = str(error)

        if not disallowed and not protected_error:
            for index, argv in enumerate(final_argvs, start=1):
                verification_workspace_fingerprint_before = git.workspace_fingerprint(
                    workspace.path,
                    contract_head,
                )
                if verification_workspace_fingerprint_before != implementation_snapshot:
                    protected_error = "packet workspace changed before verification command"
                    break
                result = run_command(argv, cwd=workspace.path, timeout_seconds=300)
                verification_results.append(result)
                verification_workspace_fingerprint_after = git.workspace_fingerprint(
                    workspace.path,
                    contract_head,
                )
                verification_receipts.append(self._write_command_result(
                    run_root,
                    attempt_root / f"verification-{index}.json",
                    result,
                    protected_verifier_inputs=verifier_inputs[index],
                    contract_argv=contract.final_argvs[index - 1],
                    workspace_fingerprint_before=verification_workspace_fingerprint_before,
                    workspace_fingerprint_after=verification_workspace_fingerprint_after,
                ))
                protected_after = hash_protected(
                    workspace.path,
                    contract.protected_patterns,
                )
                try:
                    assert_protected_unchanged(protected_before, protected_after)
                except ProtectedInputChanged as error:
                    protected_error = str(error)
                    break
                if verification_workspace_fingerprint_after != implementation_snapshot:
                    protected_error = "verification command modified packet artifacts"
                    break
                if self._stop_at_boundary(state=state, store=store, packet=packet):
                    protected_error = "stop requested after verification command"
                    break
                if not final_verification_passed(result):
                    break

        success = (
            bool(verification_results)
            and all(final_verification_passed(result) for result in verification_results)
            and not disallowed
            and not protected_error
        )
        verifier_output = "\n".join(
            f"exit={result.returncode}\n{result.stdout}\n{result.stderr}"
            for result in verification_results
        )
        if disallowed:
            verifier_output += "\ndisallowed paths: " + ", ".join(disallowed)
        if protected_error:
            verifier_output += "\n" + protected_error

        implementation_staged_receipt: dict[str, Any] | None = None
        implementation_commit_receipt: dict[str, Any] | None = None
        reviewed_head: str | None = None
        reviewed_snapshot: str | None = None
        reviewed_protected: dict[str, str] | None = None
        diff_text = git.diff_text(workspace, integration.branch)
        staged_diff = ""
        staged_path_statuses: tuple[tuple[str, str], ...] = ()

        if success:
            pre_stage_snapshot = git.workspace_fingerprint(
                workspace.path,
                contract_head,
            )
            pre_stage_protected = hash_protected(
                workspace.path,
                contract.protected_patterns,
            )
            pre_stage_protected_error = ""
            try:
                assert_protected_unchanged(protected_before, pre_stage_protected)
            except ProtectedInputChanged as error:
                pre_stage_protected_error = str(error)
            if pre_stage_snapshot != implementation_snapshot:
                success = False
                verifier_output += (
                    "\nimplementation workspace changed after hard verification"
                )
            if pre_stage_protected_error:
                success = False
                verifier_output += "\n" + pre_stage_protected_error

        if success:
            git.stage_all(workspace)
            staged_diff = git.staged_diff_text(workspace, contract_head)
            staged_path_statuses = git.staged_path_statuses_since(
                workspace,
                contract_head,
            )
            staged_paths = tuple(path for _, path in staged_path_statuses)
            staged_disallowed = [
                path
                for path in staged_paths
                if self._python_startup_hook_path(path)
                or (pytest_contract and self._pytest_harness_path(path))
                or not any(
                    fnmatch.fnmatch(path, pattern)
                    for pattern in contract.allowed_paths
                )
            ]
            staged_protected = git.staged_file_hashes(
                workspace,
                tuple(protected_before),
            )
            expected_staged_protected = git.ref_file_hashes(
                workspace,
                contract_head,
                tuple(protected_before),
            )
            staged_protected_error = ""
            try:
                assert_protected_unchanged(
                    expected_staged_protected,
                    staged_protected,
                )
            except ProtectedInputChanged as error:
                staged_protected_error = str(error)
            post_stage_unstaged = git.unstaged_path_statuses(workspace)
            post_stage_protected = hash_protected(
                workspace.path,
                contract.protected_patterns,
            )
            post_stage_protected_error = ""
            try:
                assert_protected_unchanged(protected_before, post_stage_protected)
            except ProtectedInputChanged as error:
                post_stage_protected_error = str(error)
            staged_workspace_fingerprint = git.workspace_fingerprint(
                workspace.path,
                contract_head,
            )
            pre_commit_workspace_fingerprint = git.workspace_fingerprint(
                workspace.path,
                contract_head,
            )
            staged_fingerprint_matches = (
                pre_commit_workspace_fingerprint == staged_workspace_fingerprint
            )
            implementation_staged_receipt = self._write_json_receipt(
                run_root,
                attempt_root / "implementation-staged.json",
                {
                    "schema_version": 1,
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "attempt": attempt,
                    "contract_head": contract_head,
                    "verified_workspace_fingerprint": implementation_snapshot,
                    "pre_stage_workspace_fingerprint": pre_stage_snapshot,
                    "pre_stage_fingerprint_matches": (
                        pre_stage_snapshot == implementation_snapshot
                    ),
                    "staged_diff": staged_diff,
                    "staged_diff_sha256": hashlib.sha256(
                        staged_diff.encode("utf-8")
                    ).hexdigest(),
                    "staged_path_statuses": self._path_status_rows(
                        staged_path_statuses
                    ),
                    "disallowed_paths": staged_disallowed,
                    "expected_staged_protected_hashes": (
                        expected_staged_protected
                    ),
                    "staged_protected_hashes": staged_protected,
                    "protected_hashes_match": not staged_protected_error,
                    "post_stage_unstaged_path_statuses": self._path_status_rows(
                        post_stage_unstaged
                    ),
                    "post_stage_protected_hashes": post_stage_protected,
                    "post_stage_protected_hashes_match": (
                        not post_stage_protected_error
                    ),
                    "staged_workspace_fingerprint": staged_workspace_fingerprint,
                    "pre_commit_workspace_fingerprint": (
                        pre_commit_workspace_fingerprint
                    ),
                    "workspace_fingerprint_matches": staged_fingerprint_matches,
                },
            )
            changed_paths = tuple(
                sorted(
                    {
                        *staged_paths,
                        *(path for _, path in post_stage_unstaged),
                    }
                )
            )
            disallowed = staged_disallowed
            protected_after = post_stage_protected
            diff_text = staged_diff
            if staged_disallowed:
                success = False
                verifier_output += (
                    "\ndisallowed staged implementation paths: "
                    + ", ".join(staged_disallowed)
                )
            if staged_protected_error:
                success = False
                verifier_output += "\n" + staged_protected_error
            if post_stage_unstaged:
                success = False
                verifier_output += (
                    "\nunstaged changes after implementation staging: "
                    + ", ".join(path for _, path in post_stage_unstaged)
                )
            if post_stage_protected_error:
                success = False
                verifier_output += "\n" + post_stage_protected_error
            if not staged_fingerprint_matches:
                success = False
                verifier_output += (
                    "\nimplementation workspace changed after staged validation"
                )

        if success:
            implementation_head: str | None = None
            implementation_changed = False
            try:
                implementation_head, implementation_changed = git.commit_staged(
                    workspace,
                    f"{packet.packet_id}: verified implementation",
                )
            except GitOperationError as error:
                success = False
                verifier_output += f"\nstaged implementation commit refused: {error}"
            if success and not implementation_changed:
                success = False
                verifier_output += (
                    "\nimplementation produced no change after a failing baseline"
                )
            if success and implementation_head is not None:
                committed_diff = git.diff_between(
                    workspace.path,
                    contract_head,
                    implementation_head,
                )
                committed_path_statuses = git.path_statuses_between(
                    workspace.path,
                    contract_head,
                    implementation_head,
                )
                committed_protected = hash_protected(
                    workspace.path,
                    contract.protected_patterns,
                )
                committed_protected_error = ""
                try:
                    assert_protected_unchanged(
                        protected_before,
                        committed_protected,
                    )
                except ProtectedInputChanged as error:
                    committed_protected_error = str(error)
                committed_head_matches = (
                    git.head(workspace.path) == implementation_head
                )
                committed_status = git.working_status(workspace.path)
                committed_workspace_fingerprint = git.workspace_fingerprint(
                    workspace.path,
                    implementation_head,
                )
                expected_clean_fingerprint = hashlib.sha256().hexdigest()
                committed_clean = (
                    committed_head_matches
                    and not committed_status
                    and committed_workspace_fingerprint
                    == expected_clean_fingerprint
                )
                committed_diff_matches = committed_diff == staged_diff
                committed_path_statuses_match = (
                    committed_path_statuses == staged_path_statuses
                )
                implementation_commit_receipt = self._write_json_receipt(
                    run_root,
                    attempt_root / "implementation-commit.json",
                    {
                        "schema_version": 1,
                        "run_id": state.run_id,
                        "packet_id": packet.packet_id,
                        "attempt": attempt,
                        "contract_head": contract_head,
                        "implementation_head": implementation_head,
                        "staged_receipt_sha256": (
                            implementation_staged_receipt["sha256"]
                            if implementation_staged_receipt is not None
                            else ""
                        ),
                        "committed_diff": committed_diff,
                        "committed_diff_sha256": hashlib.sha256(
                            committed_diff.encode("utf-8")
                        ).hexdigest(),
                        "committed_path_statuses": self._path_status_rows(
                            committed_path_statuses
                        ),
                        "committed_protected_hashes": committed_protected,
                        "diff_matches": committed_diff_matches,
                        "path_statuses_match": committed_path_statuses_match,
                        "protected_hashes_match": not committed_protected_error,
                        "head_matches": committed_head_matches,
                        "working_status": committed_status,
                        "workspace_fingerprint": committed_workspace_fingerprint,
                        "expected_clean_fingerprint": expected_clean_fingerprint,
                        "clean": committed_clean,
                    },
                )
                if not committed_diff_matches:
                    success = False
                    verifier_output += (
                        "\ncommitted implementation diff did not match staged diff"
                    )
                if not committed_path_statuses_match:
                    success = False
                    verifier_output += (
                        "\ncommitted implementation paths did not match staged paths"
                    )
                if committed_protected_error:
                    success = False
                    verifier_output += "\n" + committed_protected_error
                if not committed_clean:
                    success = False
                    verifier_output += (
                        "\ncommitted implementation identity or cleanliness changed"
                    )
                if success:
                    reviewed_head = implementation_head
                    reviewed_snapshot = committed_workspace_fingerprint
                    reviewed_protected = committed_protected
                    protected_after = committed_protected
                    diff_text = committed_diff

        fingerprint = attempt_fingerprint(
            diff_text,
            0 if success else 1,
            verifier_output,
        )

        review: dict[str, Any] | None = None
        review_receipt: dict[str, Any] | None = None
        if success:
            if self._stop_at_boundary(state=state, store=store, packet=packet):
                success = False
                verifier_output += "\nstop requested before Terra review"
            elif not self._reserve_model_call(state):
                success = False
                verifier_output += "\nmodel-call budget exhausted before Terra review"
            else:
                packet.status = PacketStatus.REVIEWING
                review_bundle = {
                    "goal": state.goal,
                    "contract": contract_data,
                    "diff": diff_text,
                    "protected_before": protected_before,
                    "protected_after": protected_after,
                    "packet_head": reviewed_head,
                    "verification": [asdict(item) for item in verification_results],
                }
                review = self.provider.review(
                    packet,
                    contract,
                    workspace.path,
                    review_bundle,
                )
                review_receipt = self._write_json_receipt(
                    run_root,
                    attempt_root / "review.json",
                    review,
                )
                try:
                    validate_review(review)
                except ValueError as error:
                    success = False
                    verifier_output += f"\ninvalid Terra review: {error}"
                if success and review.get("verdict") != "GREEN":
                    success = False
                    verifier_output += "\nTerra review was not GREEN"

        if success:
            identity_changed = (
                reviewed_head is None
                or reviewed_snapshot is None
                or reviewed_protected is None
                or git.head(workspace.path) != reviewed_head
                or bool(git.working_status(workspace.path))
                or git.workspace_fingerprint(workspace.path, reviewed_head)
                != reviewed_snapshot
                or hash_protected(workspace.path, contract.protected_patterns)
                != reviewed_protected
            )
            if identity_changed:
                success = False
                verifier_output += "\npacket identity changed after Terra review"

        attempt_receipts = [
            contract_receipt,
            staged_contract_receipt,
            contract_commit_receipt,
            implementation_receipt,
            *verification_receipts,
            *(
                [implementation_staged_receipt]
                if implementation_staged_receipt is not None
                else []
            ),
            *(
                [implementation_commit_receipt]
                if implementation_commit_receipt is not None
                else []
            ),
            *([review_receipt] if review_receipt is not None else []),
        ]
        integration_head: str | None = None
        if success:
            try:
                integration_head = git.integrate_packet(
                    integration,
                    workspace,
                    reviewed_head=str(reviewed_head),
                    reviewed_fingerprint=str(reviewed_snapshot),
                    expected_integration_branch=integration.branch,
                    expected_integration_head=state.integration_head,
                )
            except GitOperationError as error:
                success = False
                verifier_output += f"\nmerge identity refused: {error}"
                fingerprint = attempt_fingerprint(
                    diff_text,
                    1,
                    verifier_output,
                )
        if success and integration_head is not None:
            state.integration_head = integration_head
            ledger.append(
                {
                    "event": "packet_integrated",
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "attempt": attempt,
                    "contract_head": contract_head,
                    "integration_head": integration_head,
                    "fingerprint": fingerprint,
                    "review_verdict": review.get("verdict") if review else None,
                    "protected_before": protected_before,
                    "protected_after": protected_after,
                    "contract_reservation_hash": contract_reservation["record_hash"],
                    "model_calls": state.model_calls,
                    "receipts": attempt_receipts,
                }
            )
        attempt_record = ledger.append(
            {
                "event": "attempt_finished",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "contract_head": contract_head,
                "success": success,
                "fingerprint": fingerprint,
                "reason": "" if success else verifier_output.strip(),
                "changed_paths": list(changed_paths),
                "execution_mode": "luna",
                "contract_reservation_hash": contract_reservation["record_hash"],
                "model_calls": state.model_calls,
                "receipts": attempt_receipts,
            }
        )
        if success:
            record_attempt(
                state,
                packet.packet_id,
                success=True,
                fingerprint=fingerprint,
            )
        else:
            record_attempt(
                state,
                packet.packet_id,
                success=False,
                fingerprint=fingerprint,
                failure_reason=verifier_output.strip() or "verification failed",
            )
        packet.active_implementation_attempt = None
        store.save(state)
        self._record_learning_outcome(
            state=state,
            packet=packet,
            success=success,
            fingerprint=fingerprint,
            failure_summary="" if success else verifier_output,
            evidence_ref=str(attempt_record["record_hash"]),
            role="luna_execute",
        )

    def _run_verify_only_packet(
        self,
        *,
        state: RunState,
        packet: PacketState,
        contract: PacketContract,
        contract_data: dict[str, Any],
        run_root: Path,
        workspace: PacketWorkspace,
        attempt: int,
        attempt_root: Path,
        protected_before: dict[str, str],
        contract_head: str,
        baseline_workspace_fingerprint: str,
        baseline: CommandResult,
        baseline_verdict: BaselineVerdict,
        contract_changed: bool,
        integration: IntegrationWorkspace,
        terminal_status: PacketStatus,
        contract_receipt: dict[str, Any],
        contract_diff_receipts: list[dict[str, Any]],
        baseline_receipt: dict[str, Any],
        baseline_argv: tuple[str, ...],
        final_argvs: tuple[tuple[str, ...], ...],
        verifier_inputs: tuple[tuple[str, ...], ...],
        contract_reservation_hash: str,
        store: StateStore,
        ledger: EvidenceLedger,
        git: GitWorkspaceManager,
    ) -> None:
        results: list[CommandResult] = [baseline]
        verification_receipts: list[dict[str, Any]] = [baseline_receipt]
        failure = ""
        protected_after = hash_protected(workspace.path, contract.protected_patterns)
        workspace_fingerprint_after_verification = baseline_workspace_fingerprint
        if contract_changed:
            failure = "verify-only contract modified the packet worktree"
        elif baseline_verdict != BaselineVerdict.ALREADY_SATISFIED:
            failure = "verify-only baseline was not already green"
        else:
            for index, argv in enumerate(final_argvs, start=1):
                workspace_fingerprint_before = git.workspace_fingerprint(
                    workspace.path,
                    contract_head,
                )
                if workspace_fingerprint_before != baseline_workspace_fingerprint:
                    failure = (
                        "verify-only workspace fingerprint changed after baseline verification"
                    )
                    break
                result = run_command(argv, cwd=workspace.path, timeout_seconds=300)
                results.append(result)
                workspace_fingerprint_after = git.workspace_fingerprint(
                    workspace.path,
                    contract_head,
                )
                workspace_fingerprint_after_verification = workspace_fingerprint_after
                verification_receipts.append(self._write_command_result(
                    run_root,
                    attempt_root / f"verification-{index}.json",
                    result,
                    protected_verifier_inputs=verifier_inputs[index],
                    contract_argv=contract.final_argvs[index - 1],
                    workspace_fingerprint_before=workspace_fingerprint_before,
                    workspace_fingerprint_after=workspace_fingerprint_after,
                ))
                protected_after = hash_protected(
                    workspace.path,
                    contract.protected_patterns,
                )
                try:
                    assert_protected_unchanged(protected_before, protected_after)
                except ProtectedInputChanged as error:
                    failure = str(error)
                    break
                if workspace_fingerprint_after != baseline_workspace_fingerprint:
                    failure = f"verify-only command {index} modified workspace artifacts"
                    break
                if not final_verification_passed(result):
                    failure = f"verify-only command {index} failed with exit {result.returncode}"
                    break
                if self._stop_at_boundary(state=state, store=store, packet=packet):
                    failure = "stop requested after verify-only command"
                    break
        protected_after = hash_protected(workspace.path, contract.protected_patterns)
        workspace_fingerprint_after_verification = git.workspace_fingerprint(
            workspace.path,
            contract_head,
        )
        try:
            assert_protected_unchanged(protected_before, protected_after)
        except ProtectedInputChanged as error:
            failure = str(error)
        if (
            not failure
            and workspace_fingerprint_after_verification != baseline_workspace_fingerprint
        ):
            failure = "verify-only workspace fingerprint changed after final verification"

        review: dict[str, Any] | None = None
        review_receipt: dict[str, Any] | None = None
        if not failure:
            if self._stop_at_boundary(state=state, store=store, packet=packet):
                failure = "stop requested before verify-only review"
            elif not self._reserve_model_call(state):
                failure = "model-call budget exhausted before verify-only review"
            else:
                workspace_fingerprint_before_review = git.workspace_fingerprint(
                    workspace.path,
                    contract_head,
                )
                protected_before_review = hash_protected(
                    workspace.path,
                    contract.protected_patterns,
                )
                try:
                    assert_protected_unchanged(protected_before, protected_before_review)
                except ProtectedInputChanged as error:
                    failure = str(error)
                if (
                    not failure
                    and workspace_fingerprint_before_review
                    != baseline_workspace_fingerprint
                ):
                    failure = "verify-only workspace fingerprint changed before Terra review"
                protected_after = protected_before_review
                workspace_fingerprint_after_verification = workspace_fingerprint_before_review
                review_bundle = {
                    "goal": state.goal,
                    "contract": contract_data,
                    "diff": git.diff_between(
                        integration.path,
                        integration.source_head,
                    ),
                    "changed_paths": list(
                        git.changed_paths_between(
                            integration.path,
                            integration.source_head,
                        )
                    ),
                    "source_head": integration.source_head,
                    "integration_head": git.head(integration.path),
                    "protected_before": protected_before,
                    "protected_after": protected_after,
                    "workspace_fingerprint": workspace_fingerprint_before_review,
                    "verification": [asdict(item) for item in results],
                }
                if not failure:
                    review = self.provider.review(
                        packet,
                        contract,
                        workspace.path,
                        review_bundle,
                    )
                    review_receipt = self._write_json_receipt(
                        run_root,
                        attempt_root / "review.json",
                        review,
                    )
                    try:
                        validate_review(review)
                    except ValueError as error:
                        failure = f"invalid verify-only review: {error}"
                    if not failure and review.get("verdict") != "GREEN":
                        failure = "verify-only review was not GREEN"
                    protected_after = hash_protected(
                        workspace.path,
                        contract.protected_patterns,
                    )
                    workspace_fingerprint_after_verification = git.workspace_fingerprint(
                        workspace.path,
                        contract_head,
                    )
                    try:
                        assert_protected_unchanged(protected_before, protected_after)
                    except ProtectedInputChanged as error:
                        failure = str(error)
                    if (
                        not failure
                        and workspace_fingerprint_after_verification
                        != baseline_workspace_fingerprint
                    ):
                        failure = "verify-only workspace fingerprint changed before acceptance"

        verifier_output = "\n".join(
            f"exit={result.returncode}\n{result.stdout}\n{result.stderr}"
            for result in results
        )
        if failure:
            verifier_output += "\n" + failure
        fingerprint = attempt_fingerprint("", 0 if not failure else 1, verifier_output)
        success = not failure
        execution_mode = (
            "verify_only"
            if terminal_status == PacketStatus.ACCEPTED
            else "already_satisfied"
        )
        attempt_receipts = [
            contract_receipt,
            *contract_diff_receipts,
            *verification_receipts,
            *([review_receipt] if review_receipt is not None else []),
        ]
        if success:
            ledger.append(
                {
                    "event": "verification_packet_accepted",
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "attempt": attempt,
                    "fingerprint": fingerprint,
                    "protected_before": protected_before,
                    "protected_after": protected_after,
                    "workspace_fingerprint": workspace_fingerprint_after_verification,
                    "execution_mode": execution_mode,
                    "contract_reservation_hash": contract_reservation_hash,
                    "model_calls": state.model_calls,
                    "receipts": attempt_receipts,
                }
            )
        attempt_record = ledger.append(
            {
                "event": "attempt_finished",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "success": success,
                "fingerprint": fingerprint,
                "reason": failure,
                "changed_paths": [],
                "workspace_fingerprint": workspace_fingerprint_after_verification,
                "execution_mode": execution_mode,
                "review_verdict": review.get("verdict") if review else None,
                "contract_reservation_hash": contract_reservation_hash,
                "model_calls": state.model_calls,
                "receipts": attempt_receipts,
            }
        )
        record_attempt(
            state,
            packet.packet_id,
            success=success,
            fingerprint=fingerprint,
            failure_reason=failure,
        )
        if success:
            packet.status = terminal_status
        store.save(state)
        self._record_learning_outcome(
            state=state,
            packet=packet,
            success=success,
            fingerprint=fingerprint,
            failure_summary=failure,
            evidence_ref=str(attempt_record["record_hash"]),
            role="terra_review",
        )

    @staticmethod
    def _finish_contract_refusal(
        *,
        state: RunState,
        packet: PacketState,
        attempt: int,
        reason: str,
        refusal_kind: str,
        contract_reservation_hash: str,
        fingerprint_seed: str,
        changed_paths: tuple[str, ...],
        receipts: list[dict[str, Any]],
        store: StateStore,
        ledger: EvidenceLedger,
    ) -> str:
        fingerprint = attempt_fingerprint(fingerprint_seed, 1, reason)
        ledger.append(
            {
                "event": "contract_refused",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "fingerprint": fingerprint,
                "reason": reason,
                "refusal_kind": refusal_kind,
                "contract_reservation_hash": contract_reservation_hash,
                "changed_paths": list(changed_paths),
                "disallowed_paths": [],
                "non_additive_check_paths": [],
                "receipts": receipts,
            }
        )
        record_attempt(
            state,
            packet.packet_id,
            success=False,
            fingerprint=fingerprint,
            failure_reason=reason,
        )
        ledger.append(
            {
                "event": "attempt_finished",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "success": False,
                "fingerprint": fingerprint,
                "reason": reason,
                "refusal_kind": refusal_kind,
                "changed_paths": list(changed_paths),
                "contract_refused": True,
                "contract_reservation_hash": contract_reservation_hash,
                "model_calls": state.model_calls,
                "receipts": receipts,
            }
        )
        store.save(state)
        return fingerprint

    @staticmethod
    def _reserve_model_call(state: RunState) -> bool:
        if state.model_calls >= state.budget.max_model_calls:
            state.status = "budget_exhausted"
            return False
        state.model_calls += 1
        return True

    @staticmethod
    def _stop_at_boundary(
        *,
        state: RunState,
        store: StateStore,
        packet: PacketState | None = None,
    ) -> bool:
        store.apply_stop_state(state)
        if not state.stop_requested:
            return False
        if (
            packet is not None
            and packet.active_implementation_attempt is None
            and packet.status in {PacketStatus.RUNNING, PacketStatus.REVIEWING}
        ):
            packet.status = PacketStatus.PENDING
            if packet.packet_id not in state.queue:
                state.queue.append(packet.packet_id)
        state.status = "stopped"
        state.updated_at = time.time()
        store.save(state)
        return True

    @staticmethod
    def _finished_model_call_count(run_root: Path) -> int:
        path = Path(run_root) / "model-processes.jsonl"
        if not path.is_file():
            return 0
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("event") == "finished":
                count += 1
        return count

    def _record_learning_outcome(
        self,
        *,
        state: RunState,
        packet: PacketState,
        success: bool,
        fingerprint: str,
        failure_summary: str,
        evidence_ref: str,
        role: str,
    ) -> None:
        if self.learning_store is None:
            return
        failure_class = None if success else self._failure_class(failure_summary)
        outcome = self.learning_store.record_outcome(
            run_id=state.run_id,
            packet_id=packet.packet_id,
            role=role,
            success=success,
            failure_class=failure_class,
            fingerprint=fingerprint,
            goal_hash=hashlib.sha256(state.goal.encode("utf-8")).hexdigest(),
            evidence_ref=evidence_ref,
        )
        if not success:
            return
        prior = self.learning_store.latest_failed_outcome(
            run_id=state.run_id,
            packet_id=packet.packet_id,
        )
        if prior is None:
            return
        prior_class = str(prior.get("failure_class") or "verifier_failed")
        self.learning_store.propose_recovery_lesson(
            role=role,
            scope=f"{role}:{prior_class}",
            failure_class=prior_class,
            tags=(role, prior_class),
            failure_evidence=str(prior["record_hash"]),
            success_evidence=str(outcome["record_hash"]),
        )

    @staticmethod
    def _failure_class(summary: str) -> str:
        lowered = summary.lower()
        if "protected" in lowered and "changed" in lowered:
            return "protected_input_changed"
        if "read-only" in lowered or "sandbox" in lowered or "permission" in lowered:
            return "permission_read_only"
        if "review" in lowered and ("evidence" in lowered or "green" in lowered):
            return "review_missing_evidence"
        return "verifier_failed"

    @staticmethod
    def _terra_contract_path_allowed(path: str) -> bool:
        normalized = path.replace("\\", "/")
        patterns = (
            "tests/**",
            "test/**",
            "testing/**",
            "spec/**",
            "specs/**",
            "checks/**",
            "**/test_*.py",
            "**/*_test.py",
            "**/*.spec.js",
            "**/*.spec.ts",
            "**/*.test.js",
            "**/*.test.ts",
        )
        return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)

    @staticmethod
    def _python_startup_hook_path(path: str) -> bool:
        basename = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
        return basename in {"sitecustomize.py", "usercustomize.py"} or basename.endswith(
            ".pth"
        )

    @classmethod
    def _allowed_pattern_targets_python_startup_hook(cls, pattern: str) -> bool:
        component = str(pattern).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        lowered = component.lower()
        if any(
            fnmatch.fnmatch(name, lowered)
            for name in ("sitecustomize.py", "usercustomize.py")
        ):
            return True
        if not any(character in component for character in "*?["):
            return lowered.endswith(".pth")
        if "." in lowered:
            extension = lowered.rsplit(".", 1)[-1]
            if not any(character in extension for character in "*?["):
                return extension == "pth"
        return True

    @staticmethod
    def _executable_basename(raw: str) -> str:
        basename = str(raw).replace("\\", "/").rsplit("/", 1)[-1].lower()
        return basename[:-4] if basename.endswith(".exe") else basename

    @classmethod
    def _is_python_executable(cls, raw: str) -> bool:
        basename = cls._executable_basename(raw)
        return basename.startswith(("python", "pythonw", "pypy")) or re.fullmatch(
            r"pyw?(?:[0-9]+(?:\.[0-9]+)?)?",
            basename,
        ) is not None

    @classmethod
    def _pytest_verifier_argv(cls, argv: tuple[str, ...]) -> bool:
        executable = cls._executable_basename(argv[0])
        if executable in {"pytest", "py.test"}:
            return True
        if not cls._is_python_executable(argv[0]):
            return False
        try:
            module_index = argv.index("-m", 1)
        except ValueError:
            return False
        return (
            module_index + 1 < len(argv)
            and str(argv[module_index + 1]).lower() in {"pytest", "py.test"}
        )

    @classmethod
    def _contract_uses_pytest(cls, contract: PacketContract) -> bool:
        return any(
            cls._pytest_verifier_argv(argv)
            for argv in (contract.baseline_argv, *contract.final_argvs)
        )

    @staticmethod
    def _pytest_harness_path(path: str) -> bool:
        basename = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
        return basename in {
            "conftest.py",
            "pytest.ini",
            ".pytest.ini",
            "pyproject.toml",
            "tox.ini",
            "setup.cfg",
        }

    @classmethod
    def _allowed_pattern_targets_pytest_harness(cls, pattern: str) -> bool:
        component = str(pattern).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        lowered = component.lower()
        return any(
            fnmatch.fnmatch(name, lowered)
            for name in (
                "conftest.py",
                "pytest.ini",
                ".pytest.ini",
                "pyproject.toml",
                "tox.ini",
                "setup.cfg",
            )
        )

    @classmethod
    def _contract_workspace_violation(
        cls,
        contract: PacketContract,
        workspace: Path,
        protected_files: set[str],
        *,
        verifier_inputs: list[tuple[str, ...]] | None = None,
    ) -> str:
        root = Path(workspace).resolve()
        startup_write_patterns = sorted(
            pattern
            for pattern in contract.allowed_paths
            if MochiController._allowed_pattern_targets_python_startup_hook(pattern)
        )
        if startup_write_patterns:
            return (
                "Python startup hook paths cannot be writable: "
                + ", ".join(startup_write_patterns)
            )
        if MochiController._contract_uses_pytest(contract):
            pytest_write_patterns = sorted(
                pattern
                for pattern in contract.allowed_paths
                if MochiController._allowed_pattern_targets_pytest_harness(pattern)
            )
            if pytest_write_patterns:
                return (
                    "Pytest harness paths cannot be writable: "
                    + ", ".join(pytest_write_patterns)
                )
        allowed_existing: set[str] = set()
        for pattern in contract.allowed_paths:
            for path in root.glob(pattern):
                if path.is_file():
                    allowed_existing.add(path.resolve().relative_to(root).as_posix())
        overlap = sorted(protected_files & allowed_existing)
        if overlap:
            return "protected measurement inputs overlap Luna write paths: " + ", ".join(overlap)

        verifier_files: set[str] = set()
        command_verifier_inputs: list[set[str]] = []
        interpreter_names = {
            "python",
            "python3",
            "py",
            "node",
            "bun",
            "deno",
        }
        shell_names = {"bash", "cmd", "fish", "powershell", "pwsh", "sh", "zsh"}
        inline_flags = {"-c", "-command", "-e", "--eval", "/c"}
        test_runner_names = {"pytest", "py.test", "unittest", "nose", "tox"}
        pytest_runner_names = {"pytest", "py.test"}
        external_python_modules = {"pytest", "py.test", "unittest", "nose", "tox"}
        pytest_root_configs = (
            "pytest.ini",
            ".pytest.ini",
            "pyproject.toml",
            "tox.ini",
            "setup.cfg",
        )
        check_parts = {
            "test",
            "tests",
            "testing",
            "check",
            "checks",
            "spec",
            "specs",
        }

        def repository_path(raw: str) -> Path | None:
            candidate = Path(raw)
            resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            if not resolved.is_relative_to(root) or not resolved.exists():
                return None
            return resolved

        command_files: set[str] | None = None

        def add_path(path: Path) -> None:
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                verifier_files.add(relative)
                if command_files is not None:
                    command_files.add(relative)
            elif path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        relative = child.resolve().relative_to(root).as_posix()
                        verifier_files.add(relative)
                        if command_files is not None:
                            command_files.add(relative)

        def executable_basename(raw: str) -> str:
            return MochiController._executable_basename(raw)

        def is_python_executable(raw: str) -> bool:
            return MochiController._is_python_executable(raw)

        def repository_python_module_exists(module_name: str) -> bool:
            if re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
                module_name,
            ) is None:
                return False
            parts = tuple(module_name.split("."))
            module_base = root.joinpath(*parts)
            module_file = module_base.with_suffix(".py")
            return module_file.is_file() or module_base.is_dir()

        def python_inline_code_requested(args: tuple[str, ...]) -> bool:
            if "-m" not in args:
                return any(value.lower().startswith("-c") for value in args)
            module_index = args.index("-m")
            before_module = args[:module_index]
            module_name = args[module_index + 1] if module_index + 1 < len(args) else ""
            return any(
                value.lower().startswith("-c") for value in before_module
            ) or module_name.lower().startswith("-c")

        def pytest_option_values(
            args: tuple[str, ...],
            option: str,
        ) -> tuple[str | None, ...]:
            values: list[str | None] = []
            index = 0
            while index < len(args):
                value = args[index]
                if value == option:
                    values.append(args[index + 1] if index + 1 < len(args) else None)
                    index += 2
                    continue
                if value.startswith(option + "="):
                    values.append(value[len(option) + 1 :])
                elif value.startswith(option) and not value.startswith("--"):
                    values.append(value[len(option) :])
                index += 1
            return tuple(values)

        def pytest_nonexecution_flag(args: tuple[str, ...]) -> str:
            long_flags = (
                "--collect-only",
                "--co",
                "--setup-plan",
                "--setup-only",
                "--fixtures",
                "--fixtures-per-test",
                "--markers",
                "--trace-config",
                "--help",
                "--version",
            )
            for value in args:
                lowered = value.lower()
                if any(
                    lowered == flag or lowered.startswith(flag + "=")
                    for flag in long_flags
                ):
                    return value
                if value.startswith("-h") or value.startswith("-V"):
                    return value
            return ""

        def repository_check_roots() -> tuple[Path, ...]:
            roots: set[Path] = set()
            for candidate in root.rglob("*"):
                if candidate.is_dir() and candidate.name.lower() in check_parts:
                    resolved = candidate.resolve()
                    if resolved.is_relative_to(root):
                        roots.add(resolved)
            return tuple(sorted(roots))

        def add_conftest_ancestors(path: Path) -> None:
            current = path if path.is_dir() else path.parent
            while current.is_relative_to(root):
                conftest = current / "conftest.py"
                if conftest.is_file():
                    add_path(conftest)
                if current == root:
                    break
                current = current.parent

        def is_check_input(path: Path) -> bool:
            relative = path.relative_to(root)
            relative_parts = {part.lower() for part in relative.parts}
            name = path.name.lower()
            return bool(relative_parts & check_parts) or any(
                fnmatch.fnmatch(name, pattern)
                for pattern in (
                    "test_*.py",
                    "*_test.py",
                    "*.spec.js",
                    "*.spec.ts",
                    "*.test.js",
                    "*.test.ts",
                )
            )

        def add_git_object_inputs(argv: tuple[str, ...]) -> None:
            executable = executable_basename(argv[0])
            if executable != "git":
                return
            args = tuple(str(value) for value in argv[1:])
            subcommand_index = next(
                (
                    index
                    for index, value in enumerate(args)
                    if value in {"cat-file", "show"}
                ),
                None,
            )
            if subcommand_index is None:
                return
            for raw in args[subcommand_index + 1 :]:
                value = str(raw)
                if ":" not in value:
                    continue
                _, relative = value.split(":", 1)
                if not relative or relative.startswith("/"):
                    continue
                path = repository_path(relative)
                if path is not None:
                    add_path(path)

        for argv in (contract.baseline_argv, *contract.final_argvs):
            command_files = set()
            command_verifier_inputs.append(command_files)
            executable_stem = executable_basename(argv[0])
            python_executable = is_python_executable(argv[0])
            explicit_executable_path = any(
                separator in str(argv[0]) for separator in ("/", "\\")
            ) or Path(argv[0]).is_absolute()
            if explicit_executable_path or (
                not python_executable
                and executable_stem
                not in interpreter_names | shell_names | test_runner_names | {"git"}
            ):
                executable_path = repository_path(argv[0])
                if executable_path is not None:
                    add_path(executable_path)
            if executable_stem in shell_names:
                return f"shell-based verifier commands are forbidden: {argv[0]}"
            command_args = tuple(str(value) for value in argv[1:])
            if python_executable:
                for candidate in root.rglob("*"):
                    if candidate.is_file() and MochiController._python_startup_hook_path(
                        candidate.name
                    ):
                        add_path(candidate)
            if python_executable and python_inline_code_requested(command_args):
                return f"inline interpreter verifier commands are forbidden: {argv[0]}"
            if (
                not python_executable
                and executable_stem in interpreter_names
                and any(value.lower() in inline_flags for value in command_args)
            ):
                return f"inline interpreter verifier commands are forbidden: {argv[0]}"
            if python_executable and "-" in command_args:
                return f"stdin Python verifier commands are forbidden: {argv[0]}"
            add_git_object_inputs(argv)
            script_selected = False
            module_name = ""
            module_argument_index: int | None = None
            selected_check_paths: set[Path] = set()
            for index, raw in enumerate(argv[1:], start=1):
                if raw == "-c":
                    break
                if raw == "-m" and index + 1 < len(argv):
                    module_name = str(argv[index + 1]).strip()
                    module_argument_index = index + 1
                    continue
                if module_argument_index == index:
                    continue
                path = repository_path(raw)
                if path is None and "::" in str(raw):
                    path = repository_path(str(raw).split("::", 1)[0])
                if path is None:
                    continue
                is_check_path = is_check_input(path)
                if (
                    is_check_path
                    or (
                        (python_executable or executable_stem in interpreter_names)
                        and not script_selected
                        and path.is_file()
                    )
                ):
                    add_path(path)
                    if is_check_path:
                        selected_check_paths.add(path.resolve())
                    if path.is_file():
                        script_selected = True
            if python_executable and "-m" in command_args:
                if not module_name or module_name.startswith("-"):
                    return "python -m verifier command is missing a module name"
                if repository_python_module_exists(module_name):
                    return (
                        "repository-local python -m verifier modules are forbidden; "
                        "use a protected direct check file: "
                        + module_name
                    )
                if module_name.lower() not in external_python_modules:
                    return (
                        "python -m verifier module cannot be proven external or protected: "
                        + module_name
                    )
            elif python_executable and (
                not command_args
                or not any(
                    value and not value.startswith("-")
                    for value in command_args
                )
            ):
                return f"stdin Python verifier commands are forbidden: {argv[0]}"
            pytest_runner = (
                module_name.lower() in pytest_runner_names
                or executable_stem in pytest_runner_names
            )
            if pytest_runner:
                if module_name.lower() in pytest_runner_names:
                    module_option_index = command_args.index("-m")
                    pytest_args = command_args[module_option_index + 2 :]
                else:
                    pytest_args = command_args
                diagnostic_flag = pytest_nonexecution_flag(pytest_args)
                if diagnostic_flag:
                    return (
                        "pytest verifier does not execute tests: "
                        + diagnostic_flag
                    )
                for candidate in root.rglob("*"):
                    if candidate.is_file() and candidate.name in pytest_root_configs:
                        add_path(candidate)
                for conftest in root.rglob("conftest.py"):
                    if conftest.is_file():
                        add_path(conftest)
                for config_value in pytest_option_values(pytest_args, "-c"):
                    if not config_value or config_value.startswith("-"):
                        return "pytest -c requires a repository-local config path"
                    config_path = repository_path(config_value)
                    if config_path is None or not config_path.is_file():
                        return (
                            "pytest explicit config is missing or outside repository: "
                            + config_value
                        )
                    add_path(config_path)
                for plugin_name in pytest_option_values(pytest_args, "-p"):
                    if not plugin_name:
                        return "pytest -p requires a plugin name"
                    if plugin_name.startswith("no:"):
                        continue
                    if repository_python_module_exists(plugin_name):
                        return (
                            "repository-local pytest plugin is forbidden: "
                            + plugin_name
                        )
                check_roots = repository_check_roots()
                for selected_path in selected_check_paths:
                    add_conftest_ancestors(selected_path)
                if not selected_check_paths:
                    for check_root in check_roots:
                        add_path(check_root)
            elif module_name.lower() in test_runner_names or executable_stem in test_runner_names:
                for directory in sorted(check_parts):
                    candidate = root / directory
                    if candidate.is_dir():
                        add_path(candidate)

            if not command_files:
                return (
                    "verifier command does not exercise protected repository input: "
                    + " ".join(argv)
                )

        missing = sorted(verifier_files - protected_files)
        if missing:
            return "repository verifier inputs are not protected: " + ", ".join(missing)
        if verifier_inputs is not None:
            verifier_inputs.extend(
                tuple(sorted(files & protected_files))
                for files in command_verifier_inputs
            )
        return ""

    @classmethod
    def _write_command_result(
        cls,
        run_root: Path,
        path: Path,
        result: CommandResult,
        *,
        protected_verifier_inputs: tuple[str, ...],
        contract_argv: tuple[str, ...],
        workspace_fingerprint_before: str,
        workspace_fingerprint_after: str,
    ) -> dict[str, Any]:
        receipt = asdict(result)
        receipt["protected_verifier_inputs"] = list(protected_verifier_inputs)
        receipt["contract_argv"] = list(contract_argv)
        receipt["workspace_fingerprint_before"] = workspace_fingerprint_before
        receipt["workspace_fingerprint_after"] = workspace_fingerprint_after
        return cls._write_json_receipt(run_root, path, receipt)

    @staticmethod
    def _path_status_rows(
        statuses: tuple[tuple[str, str], ...],
    ) -> list[dict[str, str]]:
        return [
            {"status": status, "path": path}
            for status, path in statuses
        ]

    @classmethod
    def _write_json_receipt(
        cls,
        run_root: Path,
        path: Path,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return cls._file_receipt(run_root, path)

    @staticmethod
    def _file_receipt(run_root: Path, path: Path) -> dict[str, Any]:
        root = Path(run_root).resolve()
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"receipt path escapes run root: {resolved}")
        data = resolved.read_bytes()
        return {
            "path": resolved.relative_to(root).as_posix(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    @staticmethod
    def _verify_ledger_receipts(
        run_root: Path,
        ledger: EvidenceLedger,
    ) -> dict[str, dict[str, Any]]:
        root = Path(run_root).resolve()
        verified: dict[str, dict[str, Any]] = {}
        for record in ledger.records():
            raw_receipts = record.get("receipts", [])
            if not isinstance(raw_receipts, list):
                raise ValueError(f"ledger record {record.get('seq')} has invalid receipts")
            for receipt in raw_receipts:
                if not isinstance(receipt, dict):
                    raise ValueError(f"ledger record {record.get('seq')} has a malformed receipt")
                relative = str(receipt.get("path", ""))
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError(f"receipt path is missing or escapes run root: {relative}")
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest != receipt.get("sha256") or len(data) != receipt.get("bytes"):
                    raise ValueError(f"receipt hash mismatch: {relative}")
                verified[relative] = receipt
        return verified

    def _validate_persisted_run(
        self,
        *,
        state: RunState,
        run_root: Path,
        ledger: EvidenceLedger,
        allow_lagging_contract_refusal: bool = False,
    ) -> str:
        verified_receipts = self._verify_ledger_receipts(run_root, ledger)
        records = list(ledger.records())
        plan_record = next(
            (record for record in records if record.get("event") == "plan_created"),
            None,
        )
        if plan_record is None:
            raise ValueError("persisted state has no signed plan evidence")
        plan_receipt = next(
            (
                receipt
                for receipt in plan_record.get("receipts", [])
                if str(receipt.get("path", "")) == "plan.json"
                and str(receipt.get("path", "")) in verified_receipts
            ),
            None,
        )
        if plan_receipt is None:
            raise ValueError("persisted state has no verified signed plan receipt")
        artifact = self._read_json_receipt(run_root, plan_receipt)
        if artifact.get("schema_version") != 1 or not isinstance(artifact.get("plan"), dict):
            raise ValueError("signed plan artifact is malformed")
        if hashlib.sha256(state.goal.encode("utf-8")).hexdigest() != artifact.get("goal_sha256"):
            raise ValueError("persisted state goal does not match the signed plan")
        raw_budget = artifact.get("budget")
        if not isinstance(raw_budget, dict):
            raise ValueError("signed plan budget is malformed")
        expected_budget = RunBudget(
            max_model_calls=int(raw_budget["max_model_calls"]),
            max_rounds=int(raw_budget["max_rounds"]),
            max_attempts_per_packet=int(raw_budget["max_attempts_per_packet"]),
            max_wall_seconds=int(raw_budget["max_wall_seconds"]),
        )
        expected = plan_from_dict(
            artifact["plan"],
            run_id=str(artifact["run_id"]),
            goal=state.goal,
            project_root=str(artifact["project_root"]),
            budget=expected_budget,
            started_at=float(artifact["started_at"]),
            source_head=str(artifact["source_head"]),
            source_branch=str(artifact["source_branch"]),
            integration_head=str(artifact["initial_integration_head"]),
        )

        def packet_spec(packet: PacketState) -> dict[str, Any]:
            return {
                "packet_id": packet.packet_id,
                "title": packet.title,
                "wave": packet.wave,
                "goal": packet.goal,
                "priority": packet.priority,
                "dependencies": list(packet.dependencies),
                "vertical_slice": packet.vertical_slice,
                "acceptance_criteria": list(packet.acceptance_criteria),
                "verification_commands": list(packet.verification_commands),
            }

        immutable_matches = (
            state.run_id == expected.run_id
            and state.project_root == expected.project_root
            and state.source_head == expected.source_head
            and state.source_branch == expected.source_branch
            and state.started_at == expected.started_at
            and asdict(state.budget) == asdict(expected.budget)
            and [packet_spec(packet) for packet in state.packets]
            == [packet_spec(packet) for packet in expected.packets]
        )
        if not immutable_matches:
            raise ValueError("persisted state does not match the signed plan")
        if len(state.queue) != len(set(state.queue)) or any(
            packet_id not in {packet.packet_id for packet in state.packets}
            for packet_id in state.queue
        ):
            raise ValueError("persisted state queue does not match the signed plan")

        attempts: dict[str, list[str]] = {
            packet.packet_id: [] for packet in state.packets
        }
        reservations: dict[str, list[tuple[int, int]]] = {
            packet.packet_id: [] for packet in state.packets
        }
        accepted_events: dict[str, dict[str, Any]] = {}
        expected_integration_head = str(artifact["initial_integration_head"])
        for record in records:
            packet_id = str(record.get("packet_id", ""))
            if record.get("event") == "attempt_finished":
                if packet_id not in attempts:
                    raise ValueError("evidence contains an attempt outside the signed plan")
                attempts[packet_id].append(str(record.get("fingerprint", "")))
            if record.get("event") == "implementation_attempt_reserved":
                if packet_id not in reservations:
                    raise ValueError("evidence reserves an attempt outside the signed plan")
                try:
                    packet_attempt = int(record.get("attempt", 0))
                    implementation_count = int(record.get("implementation_attempts", 0))
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "implementation reservation count is malformed"
                    ) from error
                reservations[packet_id].append(
                    (packet_attempt, implementation_count)
                )
            if record.get("event") in {
                "packet_integrated",
                "verification_packet_accepted",
                "review_repaired",
            }:
                if packet_id not in attempts:
                    raise ValueError("evidence accepts a packet outside the signed plan")
                accepted_events[packet_id] = record
            if record.get("event") == "packet_integrated":
                integration_head = record.get("integration_head")
                if not isinstance(integration_head, str) or not integration_head:
                    raise ValueError("packet integration evidence has no valid HEAD")
                expected_integration_head = integration_head

        lagging_contract_terminal = (
            self._lagging_terminal_attempt(
                state=state,
                records=records,
                verified_receipts=verified_receipts,
            )
            if allow_lagging_contract_refusal
            else None
        )
        has_active_reservation = any(
            packet.active_implementation_attempt is not None
            for packet in state.packets
        )
        if state.integration_head != expected_integration_head and not has_active_reservation:
            raise ValueError("persisted integration head does not match signed evidence")
        expected_rounds = sum(len(values) for values in attempts.values())
        if (
            state.rounds != expected_rounds
            and not (has_active_reservation and state.rounds <= expected_rounds)
            and lagging_contract_terminal is None
        ):
            raise ValueError("persisted round count does not match signed evidence")
        if state.replans != sum(
            1 for record in records if record.get("event") == "replan_required"
        ):
            raise ValueError("persisted replan count does not match signed evidence")
        for packet in state.packets:
            fingerprints = attempts[packet.packet_id]
            active = packet.active_implementation_attempt
            attempts_match = (
                packet.attempts == len(fingerprints)
                and packet.fingerprints == fingerprints
            )
            lagging_active_state = (
                active is not None
                and packet.attempts <= len(fingerprints)
                and packet.fingerprints == fingerprints[: len(packet.fingerprints)]
            )
            lagging_contract_state = (
                lagging_contract_terminal is not None
                and packet.packet_id
                == lagging_contract_terminal["packet"].packet_id
            )
            if (
                not attempts_match
                and not lagging_active_state
                and not lagging_contract_state
            ):
                raise ValueError(
                    f"persisted attempts for {packet.packet_id!r} do not match signed evidence"
                )
            signed_implementation_reservations = reservations[packet.packet_id]
            signed_counts = [
                count for _, count in signed_implementation_reservations
            ]
            if signed_counts != list(range(1, len(signed_counts) + 1)):
                raise ValueError(
                    f"implementation attempt sequence for {packet.packet_id!r} is not contiguous"
                )
            if packet.implementation_attempts != len(
                signed_implementation_reservations
            ):
                raise ValueError(
                    f"implementation attempt count for {packet.packet_id!r} lacks signed evidence"
                )
            if active is not None and (
                not signed_implementation_reservations
                or signed_implementation_reservations[-1][0] != active
            ):
                raise ValueError(
                    f"active implementation attempt for {packet.packet_id!r} is invalid"
                )
            accepted = accepted_events.get(packet.packet_id)
            if packet.status in {PacketStatus.ACCEPTED, PacketStatus.ALREADY_SATISFIED}:
                if accepted is None:
                    raise ValueError(
                        f"persisted acceptance for {packet.packet_id!r} lacks signed evidence"
                    )
                finished = next(
                    (
                        record
                        for record in reversed(records)
                        if record.get("event") == "attempt_finished"
                        and record.get("packet_id") == packet.packet_id
                    ),
                    {},
                )
                expected_status = (
                    PacketStatus.ALREADY_SATISFIED
                    if finished.get("execution_mode") == "already_satisfied"
                    else PacketStatus.ACCEPTED
                )
                if packet.status != expected_status:
                    raise ValueError(
                        f"persisted status for {packet.packet_id!r} does not match signed evidence"
                    )
            elif (
                accepted is not None
                and active is None
                and not (
                    lagging_contract_terminal is not None
                    and packet.packet_id
                    == lagging_contract_terminal["packet"].packet_id
                )
            ):
                raise ValueError(
                    f"persisted status for {packet.packet_id!r} discards signed acceptance"
                )
        return expected_integration_head

    @staticmethod
    def _contract_attempt_reservations(
        *,
        state: RunState,
        records: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        known_packets = {packet.packet_id for packet in state.packets}
        packet_order = [packet.packet_id for packet in state.packets]
        valid_statuses = {status.value for status in PacketStatus}

        def status_snapshot(value: Any) -> dict[str, str] | None:
            if not isinstance(value, list) or len(value) != len(packet_order):
                return None
            result: dict[str, str] = {}
            order: list[str] = []
            for item in value:
                if not isinstance(item, dict) or set(item) != {"packet_id", "status"}:
                    return None
                packet_id = item.get("packet_id")
                status = item.get("status")
                if (
                    not isinstance(packet_id, str)
                    or packet_id in result
                    or not isinstance(status, str)
                    or status not in valid_statuses
                ):
                    return None
                order.append(packet_id)
                result[packet_id] = status
            return result if order == packet_order else None

        seen: set[tuple[str, int]] = set()
        reservations: list[tuple[int, dict[str, Any]]] = []
        for index, record in enumerate(records):
            if record.get("event") != "contract_attempt_reserved":
                continue
            try:
                packet_id = str(record["packet_id"])
                attempt = int(record["attempt"])
                attempts_before = int(record["packet_attempts_before"])
                rounds_before = int(record["rounds_before"])
                model_calls_before = int(record["model_calls_before"])
                model_calls_reserved = int(record["model_calls_reserved"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("malformed Terra contract attempt reservation") from error
            fingerprints = record.get("packet_fingerprints_before")
            queue_before = record.get("queue_before")
            statuses_before = status_snapshot(record.get("packet_statuses_before"))
            statuses_reserved = status_snapshot(record.get("packet_statuses_reserved"))
            queue_valid = (
                isinstance(queue_before, list)
                and all(isinstance(item, str) and item for item in queue_before)
                and len(queue_before) == len(set(queue_before))
                and set(queue_before).issubset(known_packets)
                and queue_before.count(packet_id) == 1
            )
            statuses_valid = False
            if queue_valid and statuses_before is not None and statuses_reserved is not None:
                queue_members = set(queue_before)
                before_pending = {
                    item
                    for item, status in statuses_before.items()
                    if status == PacketStatus.PENDING.value
                }
                reserved_runnable = {
                    item
                    for item, status in statuses_reserved.items()
                    if status in {PacketStatus.PENDING.value, PacketStatus.RUNNING.value}
                }
                statuses_valid = (
                    statuses_before.get(packet_id) == PacketStatus.PENDING.value
                    and statuses_reserved.get(packet_id) == PacketStatus.RUNNING.value
                    and sum(
                        status == PacketStatus.RUNNING.value
                        for status in statuses_reserved.values()
                    )
                    == 1
                    and all(
                        statuses_before[item] == statuses_reserved[item]
                        for item in known_packets - {packet_id}
                    )
                    and before_pending == queue_members
                    and reserved_runnable == queue_members
                )
            valid = (
                record.get("run_id") == state.run_id
                and packet_id in known_packets
                and attempt == attempts_before + 1
                and attempts_before >= 0
                and rounds_before >= 0
                and isinstance(fingerprints, list)
                and len(fingerprints) == attempts_before
                and all(isinstance(item, str) and item for item in fingerprints)
                and record.get("packet_status_before") == PacketStatus.PENDING.value
                and record.get("packet_status_reserved") == PacketStatus.RUNNING.value
                and model_calls_before >= 0
                and model_calls_reserved == model_calls_before + 1
                and model_calls_reserved <= state.budget.max_model_calls
                and isinstance(record.get("record_hash"), str)
                and bool(record.get("record_hash"))
                and queue_valid
                and statuses_valid
            )
            if not valid:
                raise ValueError("malformed Terra contract attempt reservation")
            key = (packet_id, attempt)
            if key in seen:
                raise ValueError("multiple Terra contract reservations for one packet attempt")
            seen.add(key)
            reservations.append((index, record))
        return reservations

    @staticmethod
    def _contract_reservation_matches_state(
        *,
        state: RunState,
        packet: PacketState,
        reservation: dict[str, Any],
    ) -> bool:
        current_statuses = tuple(
            (item.packet_id, item.status.value)
            for item in state.packets
        )
        return (
            packet.attempts == int(reservation["packet_attempts_before"])
            and packet.fingerprints == reservation["packet_fingerprints_before"]
            and state.rounds == int(reservation["rounds_before"])
            and packet.status.value
            in {
                str(reservation["packet_status_before"]),
                str(reservation["packet_status_reserved"]),
            }
            and state.model_calls
            in {
                int(reservation["model_calls_before"]),
                int(reservation["model_calls_reserved"]),
            }
            and state.queue == reservation["queue_before"]
            and current_statuses
            in {
                tuple(
                    (item["packet_id"], item["status"])
                    for item in reservation["packet_statuses_before"]
                ),
                tuple(
                    (item["packet_id"], item["status"])
                    for item in reservation["packet_statuses_reserved"]
                ),
            }
            and packet.active_implementation_attempt is None
        )

    @staticmethod
    def _implementation_attempt_reservations(
        *,
        state: RunState,
        records: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
        contract_reservations = MochiController._contract_attempt_reservations(
            state=state,
            records=records,
        )
        contract_by_attempt = {
            (str(record["packet_id"]), int(record["attempt"])): (index, record)
            for index, record in contract_reservations
        }
        packet_order = [packet.packet_id for packet in state.packets]
        known_packets = set(packet_order)
        valid_statuses = {status.value for status in PacketStatus}
        seen: set[tuple[str, int]] = set()
        next_implementation_count = {packet_id: 1 for packet_id in packet_order}
        result: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for index, record in enumerate(records):
            if record.get("event") != "implementation_attempt_reserved":
                continue
            try:
                packet_id = str(record["packet_id"])
                attempt = int(record["attempt"])
                implementation_attempts = int(record["implementation_attempts"])
                active_attempt = int(record["active_implementation_attempt"])
                packet_attempts = int(record["packet_attempts_before"])
                rounds = int(record["rounds_before"])
                model_calls = int(record["model_calls_reserved"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("malformed implementation attempt reservation") from error
            fingerprints = record.get("packet_fingerprints_before")
            queue = record.get("queue_before")
            raw_statuses = record.get("packet_statuses_reserved")
            statuses: list[tuple[str, str]] = []
            if isinstance(raw_statuses, list):
                for item in raw_statuses:
                    if (
                        not isinstance(item, dict)
                        or set(item) != {"packet_id", "status"}
                        or not isinstance(item.get("packet_id"), str)
                        or not isinstance(item.get("status"), str)
                    ):
                        statuses = []
                        break
                    statuses.append((item["packet_id"], item["status"]))
            status_map = dict(statuses)
            queue_valid = (
                isinstance(queue, list)
                and all(isinstance(item, str) and item for item in queue)
                and len(queue) == len(set(queue))
                and set(queue).issubset(known_packets)
                and queue.count(packet_id) == 1
            )
            statuses_valid = (
                len(statuses) == len(packet_order)
                and [item for item, _ in statuses] == packet_order
                and len(status_map) == len(packet_order)
                and all(status in valid_statuses for status in status_map.values())
                and status_map.get(packet_id) == PacketStatus.RUNNING.value
                and sum(
                    status == PacketStatus.RUNNING.value
                    for status in status_map.values()
                )
                == 1
                and {
                    item
                    for item, status in status_map.items()
                    if status in {PacketStatus.PENDING.value, PacketStatus.RUNNING.value}
                }
                == (set(queue) if isinstance(queue, list) else set())
            )
            contract_entry = contract_by_attempt.get((packet_id, attempt))
            contract_reservation = contract_entry[1] if contract_entry is not None else None
            if contract_entry is not None and contract_entry[0] >= index:
                raise ValueError(
                    "Terra contract reservation must precede implementation reservation"
                )
            valid = (
                record.get("run_id") == state.run_id
                and packet_id in known_packets
                and attempt == packet_attempts + 1
                and active_attempt == attempt
                and implementation_attempts
                == next_implementation_count.get(packet_id, 0)
                and packet_attempts >= 0
                and rounds >= 0
                and isinstance(fingerprints, list)
                and len(fingerprints) == packet_attempts
                and all(isinstance(item, str) and item for item in fingerprints)
                and model_calls > 0
                and model_calls <= state.budget.max_model_calls
                and queue_valid
                and statuses_valid
                and contract_reservation is not None
                and contract_entry is not None
                and record.get("contract_reservation_hash")
                == contract_reservation.get("record_hash")
                and queue == contract_reservation.get("queue_before")
                and raw_statuses
                == contract_reservation.get("packet_statuses_reserved")
                and packet_attempts
                == int(contract_reservation["packet_attempts_before"])
                and fingerprints
                == contract_reservation.get("packet_fingerprints_before")
                and rounds == int(contract_reservation["rounds_before"])
                and model_calls
                == int(contract_reservation["model_calls_reserved"]) + 1
            )
            if not valid:
                raise ValueError("malformed implementation attempt reservation")
            key = (packet_id, attempt)
            if key in seen:
                raise ValueError(
                    "multiple implementation reservations for one packet attempt"
                )
            seen.add(key)
            result.append((index, record, contract_reservation))
            next_implementation_count[packet_id] += 1
        return result

    @staticmethod
    def _implementation_reservation_matches_state(
        *,
        state: RunState,
        packet: PacketState,
        reservation: dict[str, Any],
    ) -> bool:
        current_statuses = [
            {"packet_id": item.packet_id, "status": item.status.value}
            for item in state.packets
        ]
        return (
            packet.attempts == int(reservation["packet_attempts_before"])
            and packet.fingerprints == reservation["packet_fingerprints_before"]
            and state.rounds == int(reservation["rounds_before"])
            and state.model_calls == int(reservation["model_calls_reserved"])
            and state.queue == reservation["queue_before"]
            and current_statuses == reservation["packet_statuses_reserved"]
            and packet.active_implementation_attempt
            == int(reservation["active_implementation_attempt"])
            and packet.implementation_attempts
            == int(reservation["implementation_attempts"])
        )

    @staticmethod
    def _assert_complete_pending_queue(state: RunState) -> None:
        pending = {
            packet.packet_id
            for packet in state.packets
            if packet.status == PacketStatus.PENDING
        }
        if (
            len(state.queue) != len(set(state.queue))
            or set(state.queue) != pending
        ):
            raise ValueError(
                "pending queue changed after authorized Terra contract recovery"
            )

    @staticmethod
    def _lagging_terminal_attempt(
        *,
        state: RunState,
        records: list[dict[str, Any]],
        verified_receipts: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        reservations = MochiController._contract_attempt_reservations(
            state=state,
            records=records,
        )
        implementation_reservations = (
            MochiController._implementation_attempt_reservations(
                state=state,
                records=records,
            )
        )
        finished_by_packet: dict[str, list[tuple[int, dict[str, Any]]]] = {
            packet.packet_id: [] for packet in state.packets
        }
        for index, record in enumerate(records):
            if record.get("event") != "attempt_finished":
                continue
            packet_id = str(record.get("packet_id", ""))
            if packet_id not in finished_by_packet:
                return None
            finished_by_packet[packet_id].append((index, record))

        lagging: list[tuple[PacketState, int, dict[str, Any]]] = []
        for packet in state.packets:
            rows = finished_by_packet[packet.packet_id]
            fingerprints = [str(record.get("fingerprint", "")) for _, record in rows]
            if packet.attempts != len(packet.fingerprints):
                return None
            if packet.attempts > len(rows):
                return None
            if packet.fingerprints != fingerprints[: packet.attempts]:
                return None
            lagging.extend(
                (packet, index, record)
                for index, record in rows[packet.attempts :]
            )
        if len(lagging) != 1:
            return None

        packet, terminal_index, terminal = lagging[0]
        if terminal_index != len(records) - 1:
            return None
        attempt = packet.attempts + 1
        if (
            int(terminal.get("attempt", 0)) != attempt
            or terminal.get("packet_id") != packet.packet_id
            or terminal.get("success") not in {True, False}
            or not isinstance(terminal.get("fingerprint"), str)
            or not terminal.get("fingerprint")
            or not isinstance(terminal.get("reason"), str)
            or state.rounds != sum(item.attempts for item in state.packets)
        ):
            return None
        matching_reservations = [
            reservation
            for reservation_index, reservation in reservations
            if reservation_index < terminal_index
            and reservation.get("packet_id") == packet.packet_id
            and int(reservation.get("attempt", 0)) == attempt
        ]
        if len(matching_reservations) != 1:
            return None
        reservation = matching_reservations[0]
        reservation_hash = str(reservation["record_hash"])
        if terminal.get("contract_reservation_hash") != reservation_hash:
            return None

        current_statuses = tuple(
            (item.packet_id, item.status.value) for item in state.packets
        )
        signed_statuses = {
            tuple(
                (item["packet_id"], item["status"])
                for item in reservation["packet_statuses_before"]
            ),
            tuple(
                (item["packet_id"], item["status"])
                for item in reservation["packet_statuses_reserved"]
            ),
        }
        try:
            terminal_model_calls = int(terminal["model_calls"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            packet.attempts != int(reservation["packet_attempts_before"])
            or packet.fingerprints != reservation["packet_fingerprints_before"]
            or state.rounds != int(reservation["rounds_before"])
            or state.queue != reservation["queue_before"]
            or current_statuses not in signed_statuses
            or state.model_calls > terminal_model_calls
            or state.model_calls < int(reservation["model_calls_reserved"])
            or terminal_model_calls > state.budget.max_model_calls
        ):
            return None

        handoffs = [
            (index, record, contract_reservation)
            for index, record, contract_reservation in implementation_reservations
            if index < terminal_index
            and record.get("packet_id") == packet.packet_id
            and int(record.get("attempt", 0)) == attempt
        ]
        refusals = [
            (index, record)
            for index, record in enumerate(records[:terminal_index])
            if record.get("event") == "contract_refused"
            and record.get("packet_id") == packet.packet_id
            and int(record.get("attempt", 0)) == attempt
        ]
        acceptances = [
            (index, record)
            for index, record in enumerate(records[:terminal_index])
            if record.get("event")
            in {"packet_integrated", "verification_packet_accepted"}
            and record.get("packet_id") == packet.packet_id
            and int(record.get("attempt", 0)) == attempt
        ]
        success = bool(terminal["success"])
        execution_mode = str(terminal.get("execution_mode", ""))
        shape = ""
        acceptance: dict[str, Any] | None = None

        if terminal.get("contract_interrupted") is True:
            if (
                handoffs
                or refusals
                or acceptances
                or packet.active_implementation_attempt is not None
                or not MochiController._contract_interruption_matches_reservation(
                    finished=terminal,
                    reservation=reservation,
                )
            ):
                return None
            shape = "contract_interrupted"
        elif terminal.get("contract_refused") is True:
            if (
                handoffs
                or acceptances
                or len(refusals) != 1
                or refusals[0][0] != terminal_index - 1
                or packet.active_implementation_attempt is not None
            ):
                return None
            refusal = refusals[0][1]
            refusal_kind = refusal.get("refusal_kind")
            valid_refusal_kinds = {
                "unsafe-terra-write",
                "empty-protection",
                "write-overlap",
                "workspace-contract",
                "baseline",
                "stopped-after-baseline",
                "pre-luna-model-budget",
                "pre-luna-implementation-cap",
            }
            refusal_receipts = terminal.get("receipts")
            if (
                refusal.get("contract_reservation_hash") != reservation_hash
                or refusal.get("fingerprint") != terminal.get("fingerprint")
                or refusal.get("reason") != terminal.get("reason")
                or refusal.get("refusal_kind") != terminal.get("refusal_kind")
                or refusal.get("changed_paths") != terminal.get("changed_paths")
                or refusal.get("receipts") != terminal.get("receipts")
                or refusal_kind not in valid_refusal_kinds
                or not isinstance(refusal_receipts, list)
                or not refusal_receipts
                or any(
                    not isinstance(receipt, dict)
                    or str(receipt.get("path", "")) not in verified_receipts
                    for receipt in refusal_receipts
                )
                or (
                    refusal_kind == "unsafe-terra-write"
                    and not (
                        refusal.get("disallowed_paths")
                        or refusal.get("non_additive_check_paths")
                    )
                )
            ):
                return None
            shape = "contract_refused"
        elif execution_mode == "luna":
            if (
                len(handoffs) != 1
                or handoffs[0][1].get("contract_reservation_hash") != reservation_hash
                or refusals
                or packet.active_implementation_attempt != attempt
                or not MochiController._implementation_reservation_matches_state(
                    state=state,
                    packet=packet,
                    reservation=handoffs[0][1],
                )
            ):
                return None
            shape = "luna"
        elif execution_mode in {"verify_only", "already_satisfied"}:
            if handoffs or refusals or packet.active_implementation_attempt is not None:
                return None
            shape = execution_mode
        else:
            return None

        luna_interrupted = (
            shape == "luna"
            and not success
            and terminal.get("recovered_interruption") is True
        )
        if luna_interrupted:
            interruption_reason = (
                "controller interrupted after durable Luna attempt reservation"
            )
            if (
                terminal.get("reason") != interruption_reason
                or terminal.get("fingerprint")
                != attempt_fingerprint("", 1, interruption_reason)
                or terminal.get("receipts") != []
            ):
                return None
        elif shape not in {"contract_interrupted", "contract_refused"}:
            receipts = terminal.get("receipts")
            if (
                not isinstance(receipts, list)
                or not receipts
                or any(
                    not isinstance(receipt, dict)
                    or str(receipt.get("path", "")) not in verified_receipts
                    for receipt in receipts
                )
            ):
                return None
        if success:
            expected_event = (
                "packet_integrated" if shape == "luna" else "verification_packet_accepted"
            )
            if (
                shape in {"contract_interrupted", "contract_refused"}
                or len(acceptances) != 1
                or acceptances[0][0] != terminal_index - 1
                or acceptances[0][1].get("event") != expected_event
            ):
                return None
            acceptance = acceptances[0][1]
            try:
                acceptance_model_calls = int(acceptance["model_calls"])
            except (KeyError, TypeError, ValueError):
                return None
            if (
                acceptance.get("contract_reservation_hash") != reservation_hash
                or acceptance.get("fingerprint") != terminal.get("fingerprint")
                or acceptance.get("receipts") != terminal.get("receipts")
                or acceptance_model_calls != terminal_model_calls
                or (
                    shape == "luna"
                    and acceptance_model_calls
                    != int(handoffs[0][1]["model_calls_reserved"]) + 1
                )
            ):
                return None
        elif acceptances:
            return None

        return {
            "packet": packet,
            "terminal": terminal,
            "reservation": reservation,
            "shape": shape,
            "acceptance": acceptance,
            "model_calls": terminal_model_calls,
        }

    @staticmethod
    def _contract_interruption_matches_reservation(
        *,
        finished: dict[str, Any],
        reservation: dict[str, Any],
    ) -> bool:
        reservation_hash = str(reservation["record_hash"])
        reason = "controller interrupted after durable Terra contract reservation"
        expected_fingerprint = attempt_fingerprint(
            reservation_hash,
            1,
            reason + "\nreservation=" + reservation_hash,
        )
        return (
            finished.get("event") == "attempt_finished"
            and finished.get("run_id") == reservation.get("run_id")
            and finished.get("packet_id") == reservation.get("packet_id")
            and int(finished.get("attempt", 0)) == int(reservation["attempt"])
            and finished.get("success") is False
            and finished.get("contract_interrupted") is True
            and finished.get("contract_refused") is not True
            and finished.get("contract_reservation_hash") == reservation_hash
            and finished.get("reason") == reason
            and finished.get("fingerprint") == expected_fingerprint
            and finished.get("changed_paths") == []
            and finished.get("receipts") == []
        )

    @staticmethod
    def _lagging_contract_refusal(
        *,
        state: RunState,
        records: list[dict[str, Any]],
        verified_receipts: dict[str, dict[str, Any]],
    ) -> tuple[PacketState, dict[str, Any]] | None:
        reservations = MochiController._contract_attempt_reservations(
            state=state,
            records=records,
        )
        if any(
            packet.active_implementation_attempt is not None
            for packet in state.packets
        ):
            return None

        finished_by_packet: dict[str, list[tuple[int, dict[str, Any]]]] = {
            packet.packet_id: [] for packet in state.packets
        }
        for index, record in enumerate(records):
            if record.get("event") != "attempt_finished":
                continue
            packet_id = str(record.get("packet_id", ""))
            if packet_id not in finished_by_packet:
                return None
            finished_by_packet[packet_id].append((index, record))

        lagging: list[tuple[PacketState, int, dict[str, Any]]] = []
        for packet in state.packets:
            finished_rows = finished_by_packet[packet.packet_id]
            evidence_fingerprints = [
                str(record.get("fingerprint", ""))
                for _, record in finished_rows
            ]
            if packet.attempts != len(packet.fingerprints):
                return None
            if packet.attempts > len(finished_rows):
                return None
            if packet.fingerprints != evidence_fingerprints[: packet.attempts]:
                return None
            lagging.extend(
                (packet, index, record)
                for index, record in finished_rows[packet.attempts :]
            )

        if len(lagging) != 1:
            return None
        packet, index, finished = lagging[0]
        if index != len(records) - 1:
            return None
        if state.rounds != sum(item.attempts for item in state.packets):
            return None
        if int(finished.get("attempt", 0)) != packet.attempts + 1:
            return None
        fingerprint = finished.get("fingerprint")
        reason = finished.get("reason")
        if (
            finished.get("success") is not False
            or finished.get("contract_refused") is not True
            or not isinstance(fingerprint, str)
            or not fingerprint
            or not isinstance(reason, str)
            or not reason.startswith("Terra contract ")
        ):
            return None
        if index == 0:
            return None

        refused = records[index - 1]
        refusal_kind = refused.get("refusal_kind")
        matching_fields = (
            refused.get("event") == "contract_refused"
            and refused.get("packet_id") == packet.packet_id
            and int(refused.get("attempt", 0)) == int(finished["attempt"])
            and refused.get("fingerprint") == fingerprint
            and refused.get("reason") == reason
            and refused.get("changed_paths") == finished.get("changed_paths")
            and finished.get("refusal_kind") == refusal_kind
        )
        if not matching_fields:
            return None
        matching_reservations = [
            reservation
            for reservation_index, reservation in reservations
            if reservation_index < index - 1
            and reservation.get("packet_id") == packet.packet_id
            and int(reservation.get("attempt", 0)) == int(finished["attempt"])
        ]
        if len(matching_reservations) != 1:
            return None
        reservation = matching_reservations[0]
        reservation_hash = reservation["record_hash"]
        if (
            refused.get("contract_reservation_hash") != reservation_hash
            or finished.get("contract_reservation_hash") != reservation_hash
            or not MochiController._contract_reservation_matches_state(
                state=state,
                packet=packet,
                reservation=reservation,
            )
        ):
            return None
        disallowed = refused.get("disallowed_paths")
        non_additive = refused.get("non_additive_check_paths")
        valid_refusal_kinds = {
            "unsafe-terra-write",
            "empty-protection",
            "write-overlap",
            "workspace-contract",
            "baseline",
            "stopped-after-baseline",
            "pre-luna-model-budget",
            "pre-luna-implementation-cap",
        }
        if (
            refusal_kind not in valid_refusal_kinds
            or not isinstance(disallowed, list)
            or not isinstance(non_additive, list)
            or (
                refusal_kind == "unsafe-terra-write"
                and not (disallowed or non_additive)
            )
        ):
            return None
        receipts = finished.get("receipts")
        if (
            not isinstance(receipts, list)
            or not receipts
            or receipts != refused.get("receipts")
            or not any(
                isinstance(receipt, dict)
                and str(receipt.get("path", "")).endswith("/contract.json")
                and str(receipt.get("path", "")) in verified_receipts
                for receipt in receipts
            )
        ):
            return None
        if any(
            record.get("packet_id") == packet.packet_id
            and record.get("event")
            in {"packet_integrated", "verification_packet_accepted", "review_repaired"}
            for record in records
        ):
            return None
        return packet, finished

    @staticmethod
    def _lagging_interrupted_contract_attempt(
        *,
        state: RunState,
        records: list[dict[str, Any]],
    ) -> tuple[PacketState, dict[str, Any], dict[str, Any]] | None:
        reservations = MochiController._contract_attempt_reservations(
            state=state,
            records=records,
        )
        if any(
            packet.active_implementation_attempt is not None
            for packet in state.packets
        ):
            return None

        finished_by_packet: dict[str, list[tuple[int, dict[str, Any]]]] = {
            packet.packet_id: [] for packet in state.packets
        }
        for index, record in enumerate(records):
            if record.get("event") != "attempt_finished":
                continue
            packet_id = str(record.get("packet_id", ""))
            if packet_id not in finished_by_packet:
                return None
            finished_by_packet[packet_id].append((index, record))

        lagging: list[tuple[PacketState, int, dict[str, Any]]] = []
        for packet in state.packets:
            finished_rows = finished_by_packet[packet.packet_id]
            fingerprints = [
                str(record.get("fingerprint", ""))
                for _, record in finished_rows
            ]
            if packet.attempts != len(packet.fingerprints):
                return None
            if packet.attempts > len(finished_rows):
                return None
            if packet.fingerprints != fingerprints[: packet.attempts]:
                return None
            lagging.extend(
                (packet, index, record)
                for index, record in finished_rows[packet.attempts :]
            )

        if len(lagging) != 1:
            return None
        packet, index, finished = lagging[0]
        if index != len(records) - 1:
            return None
        if state.rounds != sum(item.attempts for item in state.packets):
            return None
        if int(finished.get("attempt", 0)) != packet.attempts + 1:
            return None
        matching_reservations = [
            reservation
            for reservation_index, reservation in reservations
            if reservation_index < index
            and reservation.get("packet_id") == packet.packet_id
            and int(reservation.get("attempt", 0)) == int(finished["attempt"])
        ]
        if len(matching_reservations) != 1:
            return None
        reservation = matching_reservations[0]
        if (
            not MochiController._contract_interruption_matches_reservation(
                finished=finished,
                reservation=reservation,
            )
            or not MochiController._contract_reservation_matches_state(
                state=state,
                packet=packet,
                reservation=reservation,
            )
        ):
            return None
        if any(
            record.get("packet_id") == packet.packet_id
            and int(record.get("attempt", 0)) == int(finished["attempt"])
            and record.get("event")
            in {
                "contract_refused",
                "implementation_attempt_reserved",
                "packet_integrated",
                "verification_packet_accepted",
                "review_repaired",
            }
            for record in records
        ):
            return None
        return packet, finished, reservation

    def _recover_lagging_terminal_attempt(
        self,
        *,
        state: RunState,
        store: StateStore,
        ledger: EvidenceLedger,
    ) -> bool:
        recovery = self._lagging_terminal_attempt(
            state=state,
            records=list(ledger.records()),
            verified_receipts=self._verify_ledger_receipts(store.root, ledger),
        )
        if recovery is None:
            return False
        packet = recovery["packet"]
        terminal = recovery["terminal"]
        acceptance = recovery["acceptance"]
        shape = str(recovery["shape"])
        state.model_calls = int(recovery["model_calls"])
        success = bool(terminal["success"])
        record_attempt(
            state,
            packet.packet_id,
            success=success,
            fingerprint=str(terminal["fingerprint"]),
            failure_reason=str(terminal["reason"]),
        )
        if success and shape == "luna":
            packet.status = PacketStatus.ACCEPTED
            state.integration_head = str(acceptance["integration_head"])
        elif success and shape == "already_satisfied":
            packet.status = PacketStatus.ALREADY_SATISFIED
        elif success and shape == "verify_only":
            packet.status = PacketStatus.ACCEPTED
        if shape == "luna":
            packet.active_implementation_attempt = None
        store.save(state)
        return True

    def _recover_lagging_contract_refusal(
        self,
        *,
        state: RunState,
        store: StateStore,
        ledger: EvidenceLedger,
    ) -> bool:
        recovery = self._lagging_contract_refusal(
            state=state,
            records=list(ledger.records()),
            verified_receipts=self._verify_ledger_receipts(store.root, ledger),
        )
        if recovery is None:
            return False
        packet, finished = recovery
        record_attempt(
            state,
            packet.packet_id,
            success=False,
            fingerprint=str(finished["fingerprint"]),
            failure_reason=str(finished["reason"]),
        )
        store.save(state)
        return True

    def _recover_lagging_interrupted_contract_attempt(
        self,
        *,
        state: RunState,
        store: StateStore,
        ledger: EvidenceLedger,
    ) -> bool:
        recovery = self._lagging_interrupted_contract_attempt(
            state=state,
            records=list(ledger.records()),
        )
        if recovery is None:
            return False
        packet, finished, reservation = recovery
        state.model_calls = int(reservation["model_calls_reserved"])
        record_attempt(
            state,
            packet.packet_id,
            success=False,
            fingerprint=str(finished["fingerprint"]),
            failure_reason=str(finished["reason"]),
        )
        store.save(state)
        return True

    def _recover_interrupted_contract_attempt(
        self,
        *,
        state: RunState,
        store: StateStore,
        ledger: EvidenceLedger,
    ) -> bool:
        records = list(ledger.records())
        reservations = self._contract_attempt_reservations(
            state=state,
            records=records,
        )
        unmatched: list[tuple[int, dict[str, Any]]] = []
        for index, reservation in reservations:
            packet_id = str(reservation["packet_id"])
            attempt = int(reservation["attempt"])
            finished = [
                record
                for record in records
                if record.get("event") == "attempt_finished"
                and record.get("packet_id") == packet_id
                and int(record.get("attempt", 0)) == attempt
            ]
            if len(finished) > 1:
                raise ValueError("multiple terminal attempts for one Terra reservation")
            implementation_reservations = [
                record
                for record in records
                if record.get("event") == "implementation_attempt_reserved"
                and record.get("packet_id") == packet_id
                and int(record.get("attempt", 0)) == attempt
            ]
            if len(implementation_reservations) > 1:
                raise ValueError("multiple Luna reservations for one Terra reservation")
            if implementation_reservations:
                if (
                    implementation_reservations[0].get("contract_reservation_hash")
                    != reservation.get("record_hash")
                ):
                    raise ValueError(
                        "Luna reservation is not bound to its Terra reservation"
                    )
                continue
            if finished:
                terminal = finished[0]
                if terminal.get("contract_interrupted") is True:
                    if not self._contract_interruption_matches_reservation(
                        finished=terminal,
                        reservation=reservation,
                    ):
                        raise ValueError(
                            "synthetic Terra terminal is not bound to its reservation"
                        )
                elif terminal.get("contract_refused") is True:
                    refused = [
                        record
                        for record in records
                        if record.get("event") == "contract_refused"
                        and record.get("packet_id") == packet_id
                        and int(record.get("attempt", 0)) == attempt
                    ]
                    if len(refused) != 1:
                        raise ValueError(
                            "Terra refusal terminal lacks exactly one refusal record"
                        )
                    refusal = refused[0]
                    if (
                        refusal.get("contract_reservation_hash")
                        != reservation.get("record_hash")
                        or terminal.get("contract_reservation_hash")
                        != reservation.get("record_hash")
                        or refusal.get("fingerprint") != terminal.get("fingerprint")
                        or refusal.get("reason") != terminal.get("reason")
                        or refusal.get("refusal_kind")
                        != terminal.get("refusal_kind")
                        or refusal.get("changed_paths")
                        != terminal.get("changed_paths")
                        or refusal.get("receipts") != terminal.get("receipts")
                    ):
                        raise ValueError(
                            "Terra refusal terminal is not bound to its reservation"
                        )
                elif (
                    terminal.get("contract_reservation_hash")
                    != reservation.get("record_hash")
                ):
                    raise ValueError(
                        "Terra terminal attempt is not bound to its reservation"
                    )
                continue
            packet_state = state.packet(packet_id)
            if packet_state.active_implementation_attempt == attempt:
                continue
            refused = [
                record
                for record in records
                if record.get("event") == "contract_refused"
                and record.get("packet_id") == packet_id
                and int(record.get("attempt", 0)) == attempt
            ]
            if refused:
                raise ValueError("orphan contract refusal lacks terminal attempt evidence")
            unmatched.append((index, reservation))

        if not unmatched:
            return False
        if len(unmatched) != 1:
            raise ValueError("multiple unmatched Terra contract attempt reservations")
        index, reservation = unmatched[0]
        if index != len(records) - 1:
            raise ValueError("unmatched Terra contract reservation is not final evidence")

        packet = state.packet(str(reservation["packet_id"]))
        if not self._contract_reservation_matches_state(
            state=state,
            packet=packet,
            reservation=reservation,
        ):
            raise ValueError("persisted state is inconsistent with Terra contract reservation")
        attempt = int(reservation["attempt"])
        if attempt != packet.attempts + 1:
            raise ValueError("Terra contract reservation attempt is inconsistent with state")
        if any(
            record.get("packet_id") == packet.packet_id
            and int(record.get("attempt", 0)) == attempt
            and record.get("event")
            in {
                "implementation_attempt_reserved",
                "packet_integrated",
                "verification_packet_accepted",
                "review_repaired",
            }
            for record in records
        ):
            raise ValueError("Terra contract reservation conflicts with later attempt evidence")

        reservation_hash = str(reservation["record_hash"])
        reason = "controller interrupted after durable Terra contract reservation"
        fingerprint = attempt_fingerprint(
            reservation_hash,
            1,
            reason + "\nreservation=" + reservation_hash,
        )
        ledger.append(
            {
                "event": "attempt_finished",
                "run_id": state.run_id,
                "packet_id": packet.packet_id,
                "attempt": attempt,
                "success": False,
                "fingerprint": fingerprint,
                "reason": reason,
                "changed_paths": [],
                "contract_interrupted": True,
                "contract_reservation_hash": reservation_hash,
                "model_calls": int(reservation["model_calls_reserved"]),
                "receipts": [],
            }
        )
        state.model_calls = int(reservation["model_calls_reserved"])
        record_attempt(
            state,
            packet.packet_id,
            success=False,
            fingerprint=fingerprint,
            failure_reason=reason,
        )
        store.save(state)
        return True

    def _recover_interrupted_implementation_attempts(
        self,
        *,
        state: RunState,
        store: StateStore,
        ledger: EvidenceLedger,
    ) -> None:
        for packet in state.packets:
            attempt = packet.active_implementation_attempt
            if attempt is None:
                continue
            records = list(ledger.records())
            implementation_reservations = [
                reservation
                for _, reservation, _ in self._implementation_attempt_reservations(
                    state=state,
                    records=records,
                )
                if reservation.get("packet_id") == packet.packet_id
                and int(reservation.get("attempt", 0)) == attempt
            ]
            if len(implementation_reservations) != 1:
                raise ValueError(
                    "active Luna attempt lacks one signed implementation reservation"
                )
            reservation = implementation_reservations[0]
            if not self._implementation_reservation_matches_state(
                state=state,
                packet=packet,
                reservation=reservation,
            ):
                raise ValueError(
                    "active Luna state does not match signed implementation reservation"
                )
            finished = next(
                (
                    record
                    for record in records
                    if record.get("event") == "attempt_finished"
                    and record.get("packet_id") == packet.packet_id
                    and int(record.get("attempt", 0)) == attempt
                ),
                None,
            )
            if finished is not None:
                raise ValueError(
                    "active Luna state has a terminal that failed canonical lag replay"
                )

            accepted_rows = [
                record
                for record in records
                if record.get("packet_id") == packet.packet_id
                and int(record.get("attempt", 0)) == attempt
                and record.get("event") == "packet_integrated"
            ]
            if len(accepted_rows) > 1:
                raise ValueError("active Luna attempt has duplicate integration evidence")
            accepted = accepted_rows[0] if accepted_rows else None
            if accepted is not None:
                verified_receipts = self._verify_ledger_receipts(store.root, ledger)
                receipts = accepted.get("receipts")
                try:
                    accepted_model_calls = int(accepted["model_calls"])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(
                        "active Luna integration evidence has invalid model-call count"
                    ) from error
                if (
                    accepted.get("contract_reservation_hash")
                    != reservation.get("contract_reservation_hash")
                    or not isinstance(accepted.get("fingerprint"), str)
                    or not accepted.get("fingerprint")
                    or accepted_model_calls
                    != int(reservation["model_calls_reserved"]) + 1
                    or accepted_model_calls > state.budget.max_model_calls
                    or not isinstance(receipts, list)
                    or not receipts
                    or any(
                        not isinstance(receipt, dict)
                        or str(receipt.get("path", "")) not in verified_receipts
                        for receipt in receipts
                    )
                ):
                    raise ValueError(
                        "active Luna integration evidence is not reservation-bound"
                    )
            success = accepted is not None
            reason = (
                ""
                if success
                else "controller interrupted after durable Luna attempt reservation"
            )
            fingerprint = (
                str(accepted.get("fingerprint", ""))
                if accepted is not None
                else attempt_fingerprint("", 1, reason)
            )
            terminal_receipts = (
                list(accepted.get("receipts", [])) if accepted is not None else []
            )
            terminal_model_calls = int(
                accepted.get("model_calls", reservation["model_calls_reserved"])
                if accepted is not None
                else reservation["model_calls_reserved"]
            )
            ledger.append(
                {
                    "event": "attempt_finished",
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "attempt": attempt,
                    "success": success,
                    "fingerprint": fingerprint,
                    "reason": reason,
                    "changed_paths": [],
                    "execution_mode": "luna",
                    "recovered_interruption": accepted is None,
                    "contract_reservation_hash": reservation[
                        "contract_reservation_hash"
                    ],
                    "model_calls": terminal_model_calls,
                    "receipts": terminal_receipts,
                }
            )
            state.model_calls = terminal_model_calls
            record_attempt(
                state,
                packet.packet_id,
                success=success,
                fingerprint=fingerprint,
                failure_reason=reason,
            )
            packet.active_implementation_attempt = None
            if accepted is not None and accepted.get("event") == "packet_integrated":
                state.integration_head = str(accepted.get("integration_head", ""))
            store.save(state)

    @staticmethod
    def _latest_contract_receipt(
        packet_id: str,
        records: list[dict[str, Any]],
        verified_receipts: dict[str, dict[str, Any]],
        *,
        through_index: int | None = None,
    ) -> dict[str, Any]:
        start = len(records) - 1 if through_index is None else through_index
        for index in range(start, -1, -1):
            record = records[index]
            if record.get("packet_id") != packet_id:
                continue
            for receipt in reversed(record.get("receipts", [])):
                relative = str(receipt.get("path", ""))
                if relative.endswith("/contract.json") and relative in verified_receipts:
                    return receipt
        raise ValueError(f"packet {packet_id!r} has no verified contract receipt")

    @staticmethod
    def _accepted_packet_evidence(
        packet: PacketState,
        records: list[dict[str, Any]],
        verified_receipts: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        accepted_events = {
            "packet_integrated",
            "verification_packet_accepted",
            "review_repaired",
        }
        accepted_index = -1
        accepted_record: dict[str, Any] | None = None
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if (
                record.get("packet_id") == packet.packet_id
                and record.get("event") in accepted_events
            ):
                accepted_index = index
                accepted_record = record
                break
        if accepted_record is None:
            raise ValueError(f"accepted packet {packet.packet_id!r} has no acceptance evidence")

        return accepted_record, MochiController._latest_contract_receipt(
            packet.packet_id,
            records,
            verified_receipts,
            through_index=accepted_index,
        )

    @staticmethod
    def _read_json_receipt(run_root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
        relative = str(receipt.get("path", ""))
        path = (Path(run_root).resolve() / relative).resolve()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"receipt is not a JSON object: {relative}")
        return value

    @staticmethod
    def _accepted_command_receipt(
        accepted_record: dict[str, Any],
        verified_receipts: dict[str, dict[str, Any]],
        contract: PacketContract,
        index: int,
    ) -> dict[str, Any]:
        suffix = f"/verification-{index}.json"
        for receipt in accepted_record.get("receipts", []):
            if not isinstance(receipt, dict):
                continue
            relative = str(receipt.get("path", ""))
            if relative.endswith(suffix) and relative in verified_receipts:
                return receipt
        raise ValueError(
            f"accepted packet {contract.packet_id!r} has no verifier receipt for command "
            f"{index}"
        )

    @staticmethod
    def _rebase_workspace_argv(
        argv: tuple[str, ...],
        *,
        source_root: Path,
        target_root: Path,
    ) -> tuple[str, ...]:
        resolved_source = Path(source_root).resolve()
        resolved_target = Path(target_root).resolve()
        rebased: list[str] = []
        for raw in argv:
            candidate = Path(raw)
            replacement = raw
            if candidate.is_absolute():
                resolved = candidate.resolve()
                if resolved.is_relative_to(resolved_source):
                    relative = resolved.relative_to(resolved_source)
                    target = (resolved_target / relative).resolve()
                    if not target.is_relative_to(resolved_target):
                        raise ValueError(f"rebased verifier path escapes implementation: {raw}")
                    if not target.exists():
                        raise ValueError(f"rebased verifier path is missing: {target}")
                    replacement = str(target)
            rebased.append(replacement)
        return tuple(rebased)

    @staticmethod
    def _integration_argv(
        argv: tuple[str, ...],
        *,
        run_root: Path,
        integration_root: Path,
    ) -> tuple[str, ...]:
        packet_root = Path(run_root).resolve() / "packets"
        resolved_integration = Path(integration_root).resolve()
        rebased: list[str] = []
        for raw in argv:
            candidate = Path(raw)
            replacement = raw
            if candidate.is_absolute():
                resolved = candidate.resolve()
                if resolved.is_relative_to(packet_root):
                    packet_relative = resolved.relative_to(packet_root)
                    if len(packet_relative.parts) < 2:
                        raise ValueError(f"verifier path does not identify a packet file: {raw}")
                    relative = Path(*packet_relative.parts[1:])
                    target = (resolved_integration / relative).resolve()
                    if not target.is_relative_to(resolved_integration):
                        raise ValueError(f"rebased verifier path escapes integration: {raw}")
                    if not target.exists():
                        raise ValueError(f"rebased verifier path is missing: {target}")
                    replacement = str(target)
            rebased.append(replacement)
        return tuple(rebased)

    def _run_final_integration_verification(
        self,
        *,
        state: RunState,
        run_root: Path,
        store: StateStore,
        ledger: EvidenceLedger,
        integration: IntegrationWorkspace,
        git: GitWorkspaceManager,
    ) -> dict[str, str] | None:
        verified_receipts = self._verify_ledger_receipts(run_root, ledger)
        records = ledger.records()
        integration_head = git.head(integration.path)
        if integration_head != state.integration_head:
            raise GitOperationError(
                "integration HEAD drifted outside the controller before final verification"
            )
        if git.working_status(integration.path):
            raise GitOperationError("integration worktree is dirty before final verification")
        integration_snapshot = git.workspace_fingerprint(integration.path, "HEAD")
        pass_number = 1 + sum(
            1
            for record in records
            if record.get("event") == "final_integration_verification_started"
        )
        pass_root = run_root / "final-integration-verification" / f"pass-{pass_number:03d}"
        ledger.append(
            {
                "event": "final_integration_verification_started",
                "run_id": state.run_id,
                "integration_head": integration_head,
                "pass": pass_number,
            }
        )

        for packet in state.packets:
            accepted_record, contract_receipt = self._accepted_packet_evidence(
                packet,
                records,
                verified_receipts,
            )
            contract_data = self._read_json_receipt(run_root, contract_receipt)
            contract = contract_from_dict(contract_data, packet)
            verify_only = contract.execution_mode == ExecutionMode.VERIFY_ONLY
            raw_expected = accepted_record.get("protected_after")
            if not isinstance(raw_expected, dict):
                raise ValueError(
                    f"accepted packet {packet.packet_id!r} has no protected-hash evidence"
                )
            expected_protected = {
                str(path): str(digest)
                for path, digest in raw_expected.items()
            }
            if not expected_protected:
                raise ValueError(
                    f"accepted packet {packet.packet_id!r} protected no measurement input"
                )
            expected_workspace_fingerprint: str | None = None
            if verify_only:
                raw_workspace_fingerprint = accepted_record.get("workspace_fingerprint")
                if not isinstance(raw_workspace_fingerprint, str) or not raw_workspace_fingerprint:
                    raise ValueError(
                        f"accepted packet {packet.packet_id!r} has no workspace-fingerprint evidence"
                    )
                expected_workspace_fingerprint = raw_workspace_fingerprint
            protected_before = hash_protected(
                integration.path,
                contract.protected_patterns,
            )
            results: list[CommandResult] = []
            result_receipts: list[dict[str, Any]] = []
            failure = ""
            try:
                assert_protected_unchanged(expected_protected, protected_before)
            except ProtectedInputChanged as error:
                failure = f"protected inputs drifted after packet acceptance: {error}"

            packet_root = pass_root / packet.packet_id
            if not failure:
                for index, argv in enumerate(contract.final_argvs, start=1):
                    rebased_argv = self._integration_argv(
                        argv,
                        run_root=run_root,
                        integration_root=integration.path,
                    )
                    try:
                        command_receipt = self._accepted_command_receipt(
                            accepted_record,
                            verified_receipts,
                            contract,
                            index,
                        )
                        command_data = self._read_json_receipt(
                            run_root,
                            command_receipt,
                        )
                        raw_contract_argv = command_data.get("contract_argv")
                        raw_inputs = command_data.get("protected_verifier_inputs")
                        if (
                            not isinstance(raw_contract_argv, list)
                            or any(not isinstance(value, str) for value in raw_contract_argv)
                            or tuple(raw_contract_argv) != argv
                        ):
                            failure = (
                                f"final integration verifier {index} command receipt is "
                                "not bound to the accepted contract"
                            )
                        elif (
                            not isinstance(raw_inputs, list)
                            or not raw_inputs
                            or any(not isinstance(value, str) for value in raw_inputs)
                            or tuple(raw_inputs) != tuple(sorted(set(raw_inputs)))
                        ):
                            failure = (
                                f"final integration verifier {index} has no valid "
                                    "protected input binding"
                            )
                        elif verify_only and (
                            not isinstance(command_data.get("workspace_fingerprint_before"), str)
                            or not isinstance(command_data.get("workspace_fingerprint_after"), str)
                            or not command_data.get("workspace_fingerprint_before")
                            or not command_data.get("workspace_fingerprint_after")
                        ):
                            failure = (
                                f"final integration verifier {index} has no valid "
                                "workspace fingerprint binding"
                            )
                        else:
                            expected_inputs = tuple(raw_inputs)
                            if not set(expected_inputs).issubset(expected_protected):
                                failure = (
                                    f"final integration verifier {index} protected input "
                                    "binding is outside the accepted protected set"
                                )
                            replay_contract = replace(
                                contract,
                                baseline_argv=rebased_argv,
                                final_argvs=(rebased_argv,),
                            )
                            replay_inputs: list[tuple[str, ...]] = []
                            replay_violation = self._contract_workspace_violation(
                                replay_contract,
                                integration.path,
                                set(protected_before),
                                verifier_inputs=replay_inputs,
                            )
                            if replay_violation:
                                failure = (
                                    f"final integration verifier {index} is not bound to "
                                    "a protected repository input: "
                                    + replay_violation
                                )
                            elif (
                                len(replay_inputs) != 2
                                or replay_inputs[0] != expected_inputs
                            ):
                                failure = (
                                    f"final integration verifier {index} protected input "
                                    "binding does not match the accepted command"
                                )
                    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                        failure = (
                            f"final integration verifier {index} command receipt could not "
                            f"be validated: {error}"
                        )
                    if failure:
                        break
                    workspace_fingerprint_before = git.workspace_fingerprint(
                        integration.path,
                        "HEAD",
                    )
                    if verify_only and (
                        workspace_fingerprint_before
                        != command_data["workspace_fingerprint_before"]
                        or workspace_fingerprint_before != expected_workspace_fingerprint
                    ):
                        failure = (
                            f"final integration verifier {index} workspace fingerprint "
                            "before command does not match the accepted receipt"
                        )
                        break
                    result = run_command(
                        rebased_argv,
                        cwd=integration.path,
                        timeout_seconds=300,
                    )
                    results.append(result)
                    workspace_fingerprint_after = git.workspace_fingerprint(
                        integration.path,
                        "HEAD",
                    )
                    result_receipts.append(
                        self._write_command_result(
                            run_root,
                            packet_root / f"verification-{index}.json",
                            result,
                            protected_verifier_inputs=expected_inputs,
                            contract_argv=argv,
                            workspace_fingerprint_before=workspace_fingerprint_before,
                            workspace_fingerprint_after=workspace_fingerprint_after,
                        )
                    )
                    protected_after_command = hash_protected(
                        integration.path,
                        contract.protected_patterns,
                    )
                    try:
                        assert_protected_unchanged(
                            protected_before,
                            protected_after_command,
                        )
                    except ProtectedInputChanged as error:
                        failure = str(error)
                        break
                    if verify_only and (
                        workspace_fingerprint_after
                        != command_data["workspace_fingerprint_after"]
                        or workspace_fingerprint_after != expected_workspace_fingerprint
                    ):
                        failure = (
                            f"final integration verifier {index} workspace fingerprint "
                            "after command does not match the accepted receipt"
                        )
                        break
                    if workspace_fingerprint_after != integration_snapshot:
                        failure = "verification command modified integration artifacts"
                        break
                    if self._stop_at_boundary(state=state, store=store):
                        failure = "stop requested after final integration verifier"
                        break
                    if not final_verification_passed(result):
                        failure = (
                            f"final integration verifier {index} failed with "
                            f"exit {result.returncode}"
                        )
                        break

            protected_after = hash_protected(
                integration.path,
                contract.protected_patterns,
            )
            if not failure:
                try:
                    assert_protected_unchanged(protected_before, protected_after)
                except ProtectedInputChanged as error:
                    failure = str(error)
            context_receipt = self._write_json_receipt(
                run_root,
                packet_root / "context.json",
                {
                    "packet_id": packet.packet_id,
                    "integration_head": integration_head,
                    "acceptance_record_hash": accepted_record.get("record_hash"),
                    "contract_receipt_hash": contract_receipt.get("sha256"),
                    "expected_protected": expected_protected,
                    "protected_before": protected_before,
                    "protected_after": protected_after,
                    "expected_workspace_fingerprint": expected_workspace_fingerprint,
                    "integration_snapshot": integration_snapshot,
                    "failure": failure,
                },
            )
            event = (
                "final_integration_verification_failed"
                if failure
                else "final_integration_verification_passed"
            )
            ledger.append(
                {
                    "event": event,
                    "run_id": state.run_id,
                    "packet_id": packet.packet_id,
                    "integration_head": integration_head,
                    "pass": pass_number,
                    "reason": failure,
                    "protected_before": protected_before,
                    "protected_after": protected_after,
                    "receipts": [
                        contract_receipt,
                        *result_receipts,
                        context_receipt,
                    ],
                }
            )
            if failure:
                if not state.stop_requested:
                    state.status = "verification_failed"
                state.updated_at = time.time()
                store.save(state)
                return None

        ledger.append(
            {
                "event": "final_integration_verification_complete",
                "run_id": state.run_id,
                "integration_head": integration_head,
                "integration_fingerprint": integration_snapshot,
                "pass": pass_number,
            }
        )
        return {
            "head": integration_head,
            "fingerprint": integration_snapshot,
        }

    @staticmethod
    def _state_summary(state: RunState) -> dict[str, Any]:
        return {
            "status": state.status,
            "model_calls": state.model_calls,
            "rounds": state.rounds,
            "replans": state.replans,
            "packets": [
                {
                    "id": packet.packet_id,
                    "status": packet.status.value,
                    "attempts": packet.attempts,
                    "last_failure": packet.last_failure,
                    "acceptance_criteria": list(packet.acceptance_criteria),
                }
                for packet in state.packets
            ],
        }

    def _build_final_bundle(
        self,
        *,
        state: RunState,
        run_root: Path,
        ledger: EvidenceLedger,
        integration: IntegrationWorkspace,
        git: GitWorkspaceManager,
    ) -> dict[str, Any]:
        verified_receipts = self._verify_ledger_receipts(run_root, ledger)
        evidence_records = []
        keep_fields = {
            "seq",
            "event",
            "packet_id",
            "attempt",
            "role",
            "verdict",
            "success",
            "exit_code",
            "review_verdict",
            "execution_mode",
            "changed_paths",
            "fingerprint",
            "source_head",
            "integration_head",
            "record_hash",
            "previous_hash",
            "reason",
            "prior_attempts_preserved",
            "protected_before",
            "protected_after",
            "pass",
            "receipts",
        }
        for record in ledger.records():
            evidence_records.append(
                {key: value for key, value in record.items() if key in keep_fields}
            )

        terra_reviews = []
        verification_receipts = []
        assumptions: list[str] = []
        for relative in sorted(verified_receipts):
            path = run_root / relative
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            normalized = relative.replace("\\", "/")
            if path.name == "review.json" and (
                normalized.startswith("attempts/")
                or normalized.startswith("review-repairs/")
            ):
                terra_reviews.append(
                    {
                        "receipt": relative,
                        "receipt_hash": verified_receipts[relative]["sha256"],
                        "verdict": value.get("verdict"),
                        "findings": value.get("findings", []),
                        "evidence_summary": value.get("evidence_summary"),
                        **(
                            {"disposition": "repairs earlier missing-diff review findings"}
                            if normalized.startswith("review-repairs/")
                            else {}
                        ),
                    }
                )
            if path.name == "baseline.json" or path.name.startswith("verification-"):
                verification_receipts.append(
                    {
                        "receipt": relative,
                        "receipt_hash": verified_receipts[relative]["sha256"],
                        "argv": value.get("argv"),
                        "contract_argv": value.get("contract_argv"),
                        "protected_verifier_inputs": value.get(
                            "protected_verifier_inputs"
                        ),
                        "returncode": value.get("returncode"),
                        "timed_out": value.get("timed_out"),
                        "stdout": self._bounded_text(str(value.get("stdout", ""))),
                        "stderr": self._bounded_text(str(value.get("stderr", ""))),
                    }
                )
            if path.name == "implementation.json":
                for assumption in value.get("remaining_assumptions", []):
                    text = str(assumption).strip()
                    if text and text not in assumptions:
                        assumptions.append(text)

        changed_paths = git.changed_paths_between(
            integration.path,
            integration.source_head,
        )
        return {
            "goal": state.goal,
            "state": self._state_summary(state),
            "evidence_chain": {
                "verification": ledger.verify()[1],
                "records": evidence_records,
                "receipt_index": list(verified_receipts.values()),
            },
            "terra_reviews": terra_reviews,
            "review_disposition": (
                "Historical Terra findings remain in the evidence chain. Only packet attempts "
                "with a GREEN Terra review were eligible for integration, and any explicit "
                "review-repair record preserves the earlier verdict rather than erasing it."
            ),
            "verification_receipts": verification_receipts,
            "assumptions": assumptions,
            "source": {
                "head": integration.source_head,
                "status": git.status(integration.source_root),
            },
            "integration": {
                "branch": integration.branch,
                "head": git.head(integration.path),
                "status": git.status(integration.path),
                "changed_paths": list(changed_paths),
                "diff": git.diff_between(
                    integration.path,
                    integration.source_head,
                ),
                "artifact_hashes": hash_protected(
                    integration.path,
                    tuple(changed_paths),
                ),
            },
        }

    @staticmethod
    def _bounded_text(value: str, limit: int = 4000) -> str:
        if len(value) <= limit:
            return value
        return value[:limit] + f"\n...[truncated {len(value) - limit} characters]"
