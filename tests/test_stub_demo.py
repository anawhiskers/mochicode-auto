from __future__ import annotations

import json
from dataclasses import replace
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.config import load_config
from mochicode_core.evidence import EvidenceLedger
from mochicode_core.gitops import GitOperationError, GitWorkspaceManager
from mochicode_core.learning import LearningStore
from mochicode_core.models import PacketStatus
from mochicode_core.runner import MochiController, StubRoleProvider
from mochicode_core.state import StateLockError, StateStore


class VerifyOnlyProvider(StubRoleProvider):
    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "Verify the already-integrated path",
            "packets": [
                {
                    "id": "verify",
                    "title": "End-to-end verification",
                    "goal": "Prove the ordinary repository path is readable",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["README is present"],
                    "verification_hints": ["check README without writing"],
                }
            ],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        command = ["git", "cat-file", "-e", "HEAD:README.md"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "verify_only",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [],
            "protected_patterns": ["README.md"],
            "allowed_paths": [],
            "evidence_requirements": ["raw verification output"],
        }

    def execute(self, *args, **kwargs):
        raise AssertionError("verify-only packets must not invoke Luna")

    def final_review(self, goal, state, workspace, final_bundle):
        return {
            "verdict": "MERGE",
            "criteria": [{"criterion": "README is present", "status": "PASS", "evidence": "verification"}],
            "remaining_risks": [],
            "merge_recommendation": "human may merge",
        }


class TerraProductionEditProvider(StubRoleProvider):
    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "one packet",
            "packets": [{
                "id": "unsafe",
                "title": "Unsafe contract",
                "goal": "change production",
                "wave": 1,
                "priority": 1,
                "vertical_slice": True,
                "dependencies": [],
                "acceptance_criteria": ["production works"],
                "verification_hints": ["focused check"],
            }],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        (workspace / "production.py").write_text("IMPLEMENTED_BY_TERRA = True\n", encoding="utf-8")
        command = [sys.executable, "-c", "raise SystemExit(1)"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["tests/**"],
            "allowed_paths": ["production.py"],
            "evidence_requirements": ["output"],
        }

    def execute(self, *args, **kwargs):
        raise AssertionError("Luna must not run after Terra changes production")


class CrashWindowTerraProvider(TerraProductionEditProvider):
    def __init__(self) -> None:
        self.contract_calls = 0
        self.execute_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        self.contract_calls += 1
        if self.contract_calls > 1:
            raise AssertionError("the consumed unsafe contract must not be rerun")
        return super().contract(packet, workspace)

    def execute(self, *args, **kwargs):
        self.execute_calls += 1
        raise AssertionError("Luna must not run for a consumed unsafe contract")


class TerraExistingTestEditProvider(TerraProductionEditProvider):
    def contract(self, packet, workspace: Path) -> dict[str, object]:
        existing = workspace / "tests" / "test_existing.py"
        existing.write_text("assert True  # weakened by Terra\n", encoding="utf-8")
        command = [sys.executable, "tests/test_existing.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["tests/test_existing.py"],
            "allowed_paths": ["production.py"],
            "evidence_requirements": ["original test remains unchanged"],
        }


class TerraContractRecoveryProvider(StubRoleProvider):
    def __init__(self) -> None:
        self.contract_calls: list[tuple[str, int, str | None, str]] = []
        self.execute_calls: list[tuple[str, int]] = []

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "recover an unsafe contract while rotating independent work",
            "packets": [
                {
                    "id": "primary",
                    "title": "Primary packet",
                    "goal": "create the primary result",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["primary.txt contains the primary result"],
                    "verification_hints": ["run the primary focused check"],
                },
                {
                    "id": "independent",
                    "title": "Independent packet",
                    "goal": "create the independent result",
                    "wave": 1,
                    "priority": 2,
                    "vertical_slice": False,
                    "dependencies": [],
                    "acceptance_criteria": ["independent.txt contains the independent result"],
                    "verification_hints": ["run the independent focused check"],
                },
            ],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        prior_calls = [
            call
            for call in self.contract_calls
            if call[0] == packet.packet_id
        ]
        self.contract_calls.append(
            (packet.packet_id, packet.attempts, packet.last_failure, workspace.name)
        )
        targets = {
            "primary": ("primary.txt", "primary\n"),
            "independent": ("independent.txt", "independent\n"),
        }
        target, expected = targets[packet.packet_id]
        checks = workspace / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        check = checks / f"{packet.packet_id}_check.py"
        check.write_text(
            "from pathlib import Path\n"
            f"assert Path({target!r}).read_text(encoding='utf-8') == {expected!r}\n",
            encoding="utf-8",
        )
        if packet.packet_id == "primary" and not prior_calls:
            (workspace / "tests" / "test_existing.py").write_text(
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
        command = [sys.executable, str(check)]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": [f"checks/{packet.packet_id}_check.py"],
            "allowed_paths": [target],
            "evidence_requirements": ["focused check and protected hashes"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls.append((packet.packet_id, attempt))
        targets = {
            "primary": ("primary.txt", "primary\n"),
            "independent": ("independent.txt", "independent\n"),
        }
        target, content = targets[packet.packet_id]
        (workspace / target).write_text(content, encoding="utf-8")
        return {
            "summary": f"Created {target}",
            "changed_files": [target],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {
            "verdict": "GREEN",
            "findings": [],
            "evidence_summary": "focused verification passed",
        }

    def final_review(self, goal, state, workspace, final_bundle):
        return {
            "verdict": "MERGE",
            "criteria": [
                {
                    "criterion": criterion,
                    "status": "PASS",
                    "evidence": "final integration verification",
                }
                for packet in state.packets
                for criterion in packet.acceptance_criteria
            ],
            "remaining_risks": [],
            "merge_recommendation": "human may merge",
        }


class TerraThenInterruptingLunaProvider(TerraContractRecoveryProvider):
    def execute(self, packet, contract, workspace: Path, attempt: int):
        if packet.packet_id == "primary" and attempt == 2:
            self.execute_calls.append((packet.packet_id, attempt))
            (workspace / "primary.txt").write_text(
                "interrupted primary\n",
                encoding="utf-8",
            )
            raise RuntimeError("simulated Luna interruption on packet attempt two")
        return super().execute(packet, contract, workspace, attempt)


class VerifierTamperProvider(StubRoleProvider):
    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "one packet",
            "packets": [{
                "id": "vertical",
                "title": "Tamper check",
                "goal": "create app while preserving checks",
                "wave": 1,
                "priority": 1,
                "vertical_slice": True,
                "dependencies": [],
                "acceptance_criteria": ["app works and checks stay unchanged"],
                "verification_hints": ["focused protected check"],
            }],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        checks = workspace / "checks"
        checks.mkdir()
        check = checks / "check.py"
        check.write_text(
            "from pathlib import Path\nassert Path('app.txt').read_text(encoding='utf-8') == 'runnable\\n'\n",
            encoding="utf-8",
        )
        baseline = [sys.executable, str(check)]
        tamper_script = checks / "tamper.py"
        tamper_script.write_text(
            "from pathlib import Path\n"
            "Path('checks/check.py').write_text('raise SystemExit(0)\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        tamper = [sys.executable, "checks/tamper.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": baseline,
            "final_argvs": [baseline, tamper],
            "expected_failure_codes": [1],
            "protected_patterns": ["checks/**"],
            "allowed_paths": ["app.txt"],
            "evidence_requirements": ["checks unchanged"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        (workspace / "app.txt").write_text("runnable\n", encoding="utf-8")
        return {"summary": "app", "changed_files": ["app.txt"], "commands_run": [], "remaining_assumptions": []}


class VerifierImplementsProvider(StubRoleProvider):
    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "one verifier-immutability packet",
            "packets": [{
                "id": "hard-red",
                "title": "Verifier must not implement",
                "goal": "implement app only through Luna",
                "wave": 1,
                "priority": 1,
                "vertical_slice": True,
                "dependencies": [],
                "acceptance_criteria": ["app is implemented by Luna"],
                "verification_hints": ["verifier is read-only"],
            }],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        check = checks / "artifact_check.py"
        check.write_text(
            "from pathlib import Path\n"
            "assert Path('app.txt').read_text(encoding='utf-8') == 'fixed\\n'\n",
            encoding="utf-8",
        )
        fixer_script = checks / "fixer.py"
        fixer_script.write_text(
            "from pathlib import Path\n"
            "Path('app.txt').write_text('fixed\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        baseline = [sys.executable, "checks/artifact_check.py"]
        fixer = [sys.executable, "checks/fixer.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": baseline,
            "final_argvs": [fixer, baseline],
            "expected_failure_codes": [1],
            "protected_patterns": ["checks/artifact_check.py", "checks/fixer.py"],
            "allowed_paths": ["app.txt"],
            "evidence_requirements": ["verifiers do not implement"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        (workspace / "app.txt").write_text("wrong\n", encoding="utf-8")
        return {
            "summary": "wrote wrong value",
            "changed_files": ["app.txt"],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {"verdict": "GREEN", "findings": [], "evidence_summary": "commands passed"}


class AlreadySatisfiedProvider(StubRoleProvider):
    review_calls = 0

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "already satisfied",
            "packets": [{
                "id": "existing",
                "title": "Existing behavior",
                "goal": "prove README exists",
                "wave": 1,
                "priority": 1,
                "vertical_slice": True,
                "dependencies": [],
                "acceptance_criteria": ["README exists"],
                "verification_hints": ["check README"],
            }],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        command = ["git", "cat-file", "-e", "HEAD:README.md"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["README.md"],
            "allowed_paths": ["unused.txt"],
            "evidence_requirements": ["review receipt"],
        }

    def execute(self, *args, **kwargs):
        raise AssertionError("already-satisfied packets must not invoke Luna")

    def review(self, packet, contract, workspace, review_bundle):
        self.review_calls += 1
        return {"verdict": "GREEN", "findings": [], "evidence_summary": "reviewed"}

    def final_review(self, goal, state, workspace, final_bundle):
        return {
            "verdict": "MERGE",
            "criteria": [{"criterion": "README exists", "status": "PASS", "evidence": "verification"}],
            "remaining_risks": [],
            "merge_recommendation": "merge",
        }


class CountingVerifyOnlyProvider(VerifyOnlyProvider):
    def __init__(self) -> None:
        self.contract_calls = 0
        self.review_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        self.contract_calls += 1
        return super().contract(packet, workspace)

    def review(self, packet, contract, workspace, review_bundle):
        self.review_calls += 1
        return super().review(packet, contract, workspace, review_bundle)


class CountingAlreadySatisfiedProvider(AlreadySatisfiedProvider):
    def __init__(self) -> None:
        self.contract_calls = 0
        self.review_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        self.contract_calls += 1
        return super().contract(packet, workspace)


class HardReviewRedProvider(StubRoleProvider):
    def __init__(self) -> None:
        self.final_review_calls = 0

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "one hard-verification packet",
            "packets": [{
                "id": "hard-red",
                "title": "Hard verifier with RED review",
                "goal": "implement app only if Terra approves",
                "wave": 1,
                "priority": 1,
                "vertical_slice": True,
                "dependencies": [],
                "acceptance_criteria": ["app is reviewed"],
                "verification_hints": ["focused check and Terra review"],
            }],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        check = checks / "hard_review.py"
        check.write_text(
            "from pathlib import Path\n"
            "assert Path('app.txt').read_text(encoding='utf-8') == 'reviewed\\n'\n",
            encoding="utf-8",
        )
        command = [sys.executable, "checks/hard_review.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["checks/hard_review.py"],
            "allowed_paths": ["app.txt"],
            "evidence_requirements": ["GREEN Terra review"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        (workspace / "app.txt").write_text("reviewed\n", encoding="utf-8")
        return {
            "summary": "implemented app",
            "changed_files": ["app.txt"],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {
            "verdict": "RED",
            "findings": [{"severity": "P1", "title": "reject", "evidence": "test", "correction": "fix"}],
            "evidence_summary": "Terra rejected the implementation",
        }

    def final_review(self, goal, state, workspace, final_bundle):
        self.final_review_calls += 1
        return {"verdict": "MERGE", "criteria": [], "remaining_risks": [], "merge_recommendation": "merge"}


class SuccessfulLunaAtomicProvider(HardReviewRedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.contract_calls = 0
        self.execute_calls = 0
        self.review_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        self.contract_calls += 1
        return super().contract(packet, workspace)

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls += 1
        return super().execute(packet, contract, workspace, attempt)

    def review(self, packet, contract, workspace, review_bundle):
        self.review_calls += 1
        return {
            "verdict": "GREEN",
            "findings": [],
            "evidence_summary": "normal Luna implementation is green",
        }

    def final_review(self, goal, state, workspace, final_bundle):
        raise AssertionError("model-call budget must stop before final review")


class RecordingLunaCommitProvider(SuccessfulLunaAtomicProvider):
    def __init__(self) -> None:
        super().__init__()
        self.review_bundle: dict[str, object] | None = None
        self.review_workspace_head: str | None = None

    def review(self, packet, contract, workspace, review_bundle):
        self.review_bundle = dict(review_bundle)
        self.review_workspace_head = git(workspace, "rev-parse", "HEAD")
        return super().review(packet, contract, workspace, review_bundle)


class InterruptingLunaProvider(HardReviewRedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls += 1
        (workspace / "app.txt").write_text(
            f"interrupted attempt {self.execute_calls}\n",
            encoding="utf-8",
        )
        raise RuntimeError("simulated controller interruption after Luna write")


class ActiveLunaPeerProvider(StubRoleProvider):
    def __init__(self) -> None:
        self.contract_calls: list[str] = []
        self.execute_calls: list[str] = []
        self.review_calls = 0

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "one active Luna packet with two same-wave peers",
            "packets": [
                {
                    "id": "active",
                    "title": "Interrupted active packet",
                    "goal": "create active.txt",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["active.txt contains active"],
                    "verification_hints": ["run active check"],
                },
                {
                    "id": "peer-a",
                    "title": "Peer A",
                    "goal": "create peer-a.txt",
                    "wave": 1,
                    "priority": 2,
                    "vertical_slice": False,
                    "dependencies": [],
                    "acceptance_criteria": ["peer-a.txt contains peer-a"],
                    "verification_hints": ["run peer-a check"],
                },
                {
                    "id": "peer-b",
                    "title": "Peer B",
                    "goal": "create peer-b.txt",
                    "wave": 1,
                    "priority": 3,
                    "vertical_slice": False,
                    "dependencies": [],
                    "acceptance_criteria": ["peer-b.txt contains peer-b"],
                    "verification_hints": ["run peer-b check"],
                },
            ],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        self.contract_calls.append(packet.packet_id)
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        check = checks / f"{packet.packet_id}.py"
        target = f"{packet.packet_id}.txt"
        check.write_text(
            "from pathlib import Path\n"
            f"assert Path({target!r}).read_text(encoding='utf-8') "
            f"== {packet.packet_id + chr(10)!r}\n",
            encoding="utf-8",
        )
        command = [sys.executable, f"checks/{packet.packet_id}.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": [f"checks/{packet.packet_id}.py"],
            "allowed_paths": [target],
            "evidence_requirements": ["active Luna reservation state"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls.append(packet.packet_id)
        if packet.packet_id != "active":
            raise AssertionError("tampered resume reached an independent peer")
        (workspace / "active.txt").write_text("interrupted\n", encoding="utf-8")
        raise RuntimeError("simulated active Luna interruption")

    def review(self, packet, contract, workspace, review_bundle):
        self.review_calls += 1
        raise AssertionError("tampered resume reached Terra review")


class LunaVerifierRewriteProvider(HardReviewRedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        verifier = checks / "verifier.py"
        verifier.write_text(
            "from pathlib import Path\n"
            "assert Path('app.txt').read_text(encoding='utf-8') == 'reviewed\\n'\n",
            encoding="utf-8",
        )
        command = [sys.executable, "checks/verifier.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["README.md"],
            "allowed_paths": ["app.txt", "checks/verifier.py"],
            "evidence_requirements": ["verifier is protected"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls += 1
        (workspace / "app.txt").write_text("wrong\n", encoding="utf-8")
        (workspace / "checks" / "verifier.py").write_text(
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        return {
            "summary": "rewrote verifier",
            "changed_files": ["app.txt", "checks/verifier.py"],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {"verdict": "GREEN", "findings": [], "evidence_summary": "rewritten verifier passed"}


class BaselineImplementsProvider(HardReviewRedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        baseline = checks / "baseline.py"
        baseline.write_text(
            "from pathlib import Path\n"
            "target = Path('app.txt')\n"
            "if target.is_file():\n"
            "    raise SystemExit(0)\n"
            "target.write_text('reviewed\\n', encoding='utf-8')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        command = [sys.executable, "checks/baseline.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["checks/baseline.py"],
            "allowed_paths": ["app.txt"],
            "evidence_requirements": ["baseline is read-only"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls += 1
        return {
            "summary": "no-op Luna",
            "changed_files": [],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {"verdict": "GREEN", "findings": [], "evidence_summary": "baseline-created app passed"}


class StopAfterContractProvider(HardReviewRedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        contract = super().contract(packet, workspace)
        (workspace.parent.parent / "STOP").write_text("stop after Terra\n", encoding="utf-8")
        return contract

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls += 1
        return super().execute(packet, contract, workspace, attempt)


class PostReviewMutationProvider(HardReviewRedProvider):
    def review(self, packet, contract, workspace: Path, review_bundle):
        (workspace / "app.txt").write_text("mutated after Terra review\n", encoding="utf-8")
        return {"verdict": "GREEN", "findings": [], "evidence_summary": "claimed green"}


class InlineVerifierProvider(HardReviewRedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        contract = super().contract(packet, workspace)
        contract["final_argvs"] = [
            *contract["final_argvs"],
            [sys.executable, "-c", "raise SystemExit(0)"],
        ]
        return contract

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls += 1
        return super().execute(packet, contract, workspace, attempt)


class MutatingSolProvider(VerifyOnlyProvider):
    def final_review(self, goal, state, workspace: Path, final_bundle):
        (workspace / "unreviewed-sol-write.txt").write_text(
            "changed after final verification\n",
            encoding="utf-8",
        )
        return {
            "verdict": "MERGE",
            "criteria": [
                {
                    "criterion": "README is present",
                    "status": "PASS",
                    "evidence": "pre-mutation verification",
                }
            ],
            "remaining_risks": [],
            "merge_recommendation": "merge",
        }


class StaleIntegrationProvider(StubRoleProvider):
    def __init__(self) -> None:
        self.final_review_calls = 0

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "later work invalidates an earlier accepted behavior",
            "packets": [
                {
                    "id": "producer",
                    "title": "Produce good behavior",
                    "goal": "write the good value",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["shared value is good"],
                    "verification_hints": ["check the good value"],
                },
                {
                    "id": "breaker",
                    "title": "Replace the shared behavior",
                    "goal": "write a later value",
                    "wave": 2,
                    "priority": 1,
                    "vertical_slice": False,
                    "dependencies": ["producer"],
                    "acceptance_criteria": ["shared value is bad"],
                    "verification_hints": ["check the later value"],
                },
            ],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        expected = "good" if packet.packet_id == "producer" else "bad"
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        check = checks / f"{packet.packet_id}.py"
        check.write_text(
            "from pathlib import Path\n"
            f"assert Path('shared.txt').read_text(encoding='utf-8') == {expected + chr(10)!r}\n",
            encoding="utf-8",
        )
        command = [sys.executable, f"checks/{packet.packet_id}.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": [f"checks/{packet.packet_id}.py"],
            "allowed_paths": ["shared.txt"],
            "evidence_requirements": ["current integration verification"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        value = "good\n" if packet.packet_id == "producer" else "bad\n"
        (workspace / "shared.txt").write_text(value, encoding="utf-8")
        return {
            "summary": f"wrote {value.strip()}",
            "changed_files": ["shared.txt"],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {"verdict": "GREEN", "findings": [], "evidence_summary": "packet is locally green"}

    def final_review(self, goal, state, workspace, final_bundle):
        self.final_review_calls += 1
        return {"verdict": "MERGE", "criteria": [], "remaining_risks": [], "merge_recommendation": "merge"}


class EmptyProtectedProvider(HardReviewRedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        check = checks / "actual.py"
        check.write_text(
            "from pathlib import Path\n"
            "assert Path('app.txt').read_text(encoding='utf-8') == 'reviewed\\n'\n",
            encoding="utf-8",
        )
        command = [sys.executable, "checks/actual.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["checks/does-not-exist.py"],
            "allowed_paths": ["app.txt"],
            "evidence_requirements": ["a real protected measurement input"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls += 1
        return super().execute(packet, contract, workspace, attempt)

    def review(self, packet, contract, workspace, review_bundle):
        return {"verdict": "GREEN", "findings": [], "evidence_summary": "locally green"}


class LegitimateContractRefusalProvider(StubRoleProvider):
    def __init__(self, refusal_mode: str) -> None:
        self.refusal_mode = refusal_mode
        self.contract_calls: list[str] = []
        self.execute_calls: list[str] = []

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "one refused packet and one independent packet",
            "packets": [
                {
                    "id": "refused",
                    "title": "Legitimate refusal",
                    "goal": "exercise a safe contract refusal",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["unsafe or invalid contract is refused"],
                    "verification_hints": ["never invoke Luna for the refused packet"],
                },
                {
                    "id": "independent",
                    "title": "Independent work",
                    "goal": "create independent.txt",
                    "wave": 1,
                    "priority": 2,
                    "vertical_slice": False,
                    "dependencies": [],
                    "acceptance_criteria": ["independent.txt contains independent"],
                    "verification_hints": ["run the independent focused check"],
                },
            ],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        self.contract_calls.append(packet.packet_id)
        checks = workspace / "checks"
        checks.mkdir(exist_ok=True)
        check = checks / f"{packet.packet_id}.py"
        if packet.packet_id == "independent":
            check.write_text(
                "from pathlib import Path\n"
                "assert Path('independent.txt').read_text(encoding='utf-8') "
                "== 'independent\\n'\n",
                encoding="utf-8",
            )
            protected_patterns = ["checks/independent.py"]
            allowed_paths = ["independent.txt"]
            expected_failure_codes = [1]
        else:
            check.write_text(
                "raise SystemExit(4)\n"
                if self.refusal_mode == "baseline"
                else "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            protected_patterns = (
                ["checks/does-not-exist.py"]
                if self.refusal_mode == "empty-protection"
                else (
                    ["checks/*.py"]
                    if self.refusal_mode == "write-overlap"
                    else ["checks/refused.py"]
                )
            )
            allowed_paths = (
                ["checks/refused.py"]
                if self.refusal_mode == "write-overlap"
                else ["refused.txt"]
            )
            expected_failure_codes = [1]
        command = [sys.executable, f"checks/{packet.packet_id}.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": expected_failure_codes,
            "protected_patterns": protected_patterns,
            "allowed_paths": allowed_paths,
            "evidence_requirements": ["reservation-bound terminal evidence"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        self.execute_calls.append(packet.packet_id)
        if packet.packet_id == "refused":
            raise AssertionError("Luna must not run for a refused contract")
        (workspace / "independent.txt").write_text("independent\n", encoding="utf-8")
        return {
            "summary": "created independent.txt",
            "changed_files": ["independent.txt"],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {
            "verdict": "GREEN",
            "findings": [],
            "evidence_summary": "independent verification passed",
        }


class MalformedPacketReviewProvider(HardReviewRedProvider):
    def review(self, packet, contract, workspace, review_bundle):
        return {"verdict": "GREEN"}


class MalformedFinalReviewProvider(VerifyOnlyProvider):
    def final_review(self, goal, state, workspace, final_bundle):
        return {"verdict": "MERGE"}


class SecretLessonProvider(StubRoleProvider):
    secret = "ULTRAPRIVATEGOALZXQ"

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        plan = super().plan(goal, workspace)
        first = plan["packets"][0]
        first["title"] = self.secret
        first["goal"] = self.secret
        return plan


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    git(source, "init")
    (source / "README.md").write_text("stub project\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    return source


class StubDemoTests(unittest.TestCase):
    def test_premerge_identity_drift_records_failure_without_packet_integration(self) -> None:
        for drift in (
            "packet-branch-ref",
            "packet-detached",
            "integration-branch",
            "integration-head",
        ):
            with self.subTest(drift=drift):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    provider = SuccessfulLunaAtomicProvider()
                    config = replace(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        max_model_calls=4,
                        max_attempts_per_packet=1,
                    )
                    original = GitWorkspaceManager.integrate_packet

                    def drift_before_merge(manager, integration, packet, *args, **kwargs):
                        reviewed_head = str(
                            kwargs.get("reviewed_head") or manager.head(packet.path)
                        )
                        if drift == "packet-branch-ref":
                            parent = git(packet.path, "rev-parse", f"{reviewed_head}^")
                            git(
                                packet.path,
                                "update-ref",
                                f"refs/heads/{packet.branch}",
                                parent,
                            )
                        elif drift == "packet-detached":
                            git(packet.path, "switch", "--detach", reviewed_head)
                        elif drift == "integration-branch":
                            git(
                                integration.path,
                                "switch",
                                "-c",
                                "unexpected-integration",
                            )
                        else:
                            git(
                                integration.path,
                                "-c",
                                "user.name=Test",
                                "-c",
                                "user.email=test@example.invalid",
                                "commit",
                                "--allow-empty",
                                "-m",
                                "external integration drift",
                            )
                        return original(
                            manager,
                            integration,
                            packet,
                            *args,
                            **kwargs,
                        )

                    with mock.patch.object(
                        GitWorkspaceManager,
                        "integrate_packet",
                        new=drift_before_merge,
                    ):
                        result = MochiController(config, provider).run_new(
                            goal=f"Refuse premerge identity drift {drift}",
                            project=source,
                            run_root=root / "run",
                            run_id=f"premerge-{drift}",
                        )

                    packet = result.state.packet("hard-red")
                    rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
                    finished = next(
                        row
                        for row in rows
                        if row.get("event") == "attempt_finished"
                        and row.get("packet_id") == "hard-red"
                    )
                    self.assertEqual(packet.status, PacketStatus.PARKED)
                    self.assertEqual(provider.review_calls, 1)
                    self.assertFalse(finished["success"])
                    self.assertIn("merge identity refused", finished["reason"])
                    self.assertFalse(
                        any(row.get("event") == "packet_integrated" for row in rows)
                    )

    def _run_luna_late_mutation(
        self,
        root: Path,
        *,
        boundary: str,
        mutation: str,
    ):
        source = make_repo(root)
        run_root = root / "run"
        config = replace(
            load_config(PLUGIN_ROOT / "config" / "default.toml"),
            max_model_calls=4,
            max_attempts_per_packet=1,
        )
        provider = SuccessfulLunaAtomicProvider()
        implementation_paths: list[Path] = []
        heads_before_mutation: list[str] = []

        def inject(manager: GitWorkspaceManager, packet) -> None:
            implementation_paths.append(packet.path)
            heads_before_mutation.append(manager.head(packet.path))
            if mutation == "disallowed":
                (packet.path / "late-production.py").write_text(
                    "LATE = True\n",
                    encoding="utf-8",
                )
                return
            verifier = packet.path / "checks" / "hard_review.py"
            verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")

        if boundary == "before-stage":
            original = GitWorkspaceManager.stage_all

            def before_stage(manager, packet):
                if packet.path.name == "implementation":
                    inject(manager, packet)
                return original(manager, packet)

            patcher = mock.patch.object(
                GitWorkspaceManager,
                "stage_all",
                new=before_stage,
            )
        elif boundary == "after-stage":
            original = GitWorkspaceManager.stage_all

            def after_stage(manager, packet):
                result = original(manager, packet)
                if packet.path.name == "implementation":
                    inject(manager, packet)
                return result

            patcher = mock.patch.object(
                GitWorkspaceManager,
                "stage_all",
                new=after_stage,
            )
        elif boundary == "before-commit":
            original = GitWorkspaceManager.commit_staged

            def before_commit(manager, packet, message):
                if packet.path.name == "implementation":
                    inject(manager, packet)
                return original(manager, packet, message)

            patcher = mock.patch.object(
                GitWorkspaceManager,
                "commit_staged",
                new=before_commit,
            )
        else:
            raise AssertionError(f"unknown Luna mutation boundary: {boundary}")

        with patcher:
            result = MochiController(config, provider).run_new(
                goal=f"Refuse {mutation} Luna mutation {boundary}",
                project=source,
                run_root=run_root,
                run_id=f"luna-{boundary}-{mutation}",
            )
        return provider, result, implementation_paths, heads_before_mutation

    def test_luna_late_disallowed_and_protected_mutations_never_commit_or_integrate(self) -> None:
        for boundary in ("before-stage", "after-stage", "before-commit"):
            for mutation in ("disallowed", "protected"):
                with self.subTest(boundary=boundary, mutation=mutation):
                    with tempfile.TemporaryDirectory() as raw:
                        provider, result, implementation_paths, heads_before = (
                            self._run_luna_late_mutation(
                                Path(raw),
                                boundary=boundary,
                                mutation=mutation,
                            )
                        )

                        packet = result.state.packet("hard-red")
                        rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
                        finished = next(
                            row
                            for row in rows
                            if row.get("event") == "attempt_finished"
                            and row.get("packet_id") == "hard-red"
                        )
                        self.assertEqual(packet.status, PacketStatus.PARKED)
                        self.assertEqual(packet.implementation_attempts, 1)
                        self.assertEqual(provider.review_calls, 0)
                        self.assertEqual(len(implementation_paths), 1)
                        self.assertEqual(
                            git(implementation_paths[0], "rev-parse", "HEAD"),
                            heads_before[0],
                        )
                        self.assertFalse((result.integration.path / "app.txt").exists())
                        self.assertFalse(
                            (result.integration.path / "late-production.py").exists()
                        )
                        self.assertFalse(
                            any(row.get("event") == "packet_integrated" for row in rows)
                        )
                        self.assertTrue(
                            any(
                                str(receipt.get("path", "")).endswith(
                                    "/implementation-staged.json"
                                )
                                for receipt in finished["receipts"]
                            )
                        )

    def test_terra_reviews_the_exact_committed_luna_staged_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = RecordingLunaCommitProvider()
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_model_calls=4,
            )
            result = MochiController(config, provider).run_new(
                goal="Bind Terra to the staged Luna commit",
                project=source,
                run_root=root / "run",
                run_id="luna-staged-commit",
            )

            rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
            integrated = next(row for row in rows if row.get("event") == "packet_integrated")
            staged_ref = next(
                receipt
                for receipt in integrated["receipts"]
                if str(receipt.get("path", "")).endswith("/implementation-staged.json")
            )
            commit_ref = next(
                receipt
                for receipt in integrated["receipts"]
                if str(receipt.get("path", "")).endswith("/implementation-commit.json")
            )
            staged = json.loads(
                (result.run_root / staged_ref["path"]).read_text(encoding="utf-8")
            )
            committed = json.loads(
                (result.run_root / commit_ref["path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(provider.review_calls, 1)
            self.assertIsNotNone(provider.review_bundle)
            self.assertTrue(committed["diff_matches"])
            self.assertTrue(committed["path_statuses_match"])
            self.assertTrue(committed["protected_hashes_match"])
            self.assertTrue(committed["clean"])
            self.assertEqual(
                staged["staged_diff_sha256"],
                committed["committed_diff_sha256"],
            )
            self.assertEqual(
                committed["implementation_head"],
                provider.review_workspace_head,
            )
            self.assertEqual(
                committed["implementation_head"],
                provider.review_bundle["packet_head"],
            )
            self.assertEqual(
                committed["committed_diff"],
                provider.review_bundle["diff"],
            )

    def test_complete_stub_state_machine_is_runnable_first_and_source_safe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            source_head = git(source, "rev-parse", "HEAD")
            learning = LearningStore(root / "learning")
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                StubRoleProvider(),
                learning,
            )

            result = controller.run_new(
                goal="Build the zero-cost vertical slice",
                project=source,
                run_root=root / "run",
                run_id="stubdemo",
            )

            self.assertEqual(result.state.status, "complete")
            self.assertEqual(result.state.packet("vertical").attempts, 2)
            self.assertEqual(result.state.packet("support").attempts, 1)
            self.assertEqual(result.state.packet("integrate").attempts, 1)
            self.assertTrue(
                all(packet.status == PacketStatus.ACCEPTED for packet in result.state.packets)
            )
            self.assertEqual(git(source, "rev-parse", "HEAD"), source_head)
            self.assertFalse((source / "app.txt").exists())
            self.assertEqual(
                (result.integration.path / "integration.txt").read_text(encoding="utf-8"),
                "runnable + support\n",
            )
            ledger = EvidenceLedger(result.run_root / "evidence.jsonl")
            self.assertTrue(ledger.verify()[0])
            rows = [
                json.loads(line)
                for line in (result.run_root / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            attempt_order = [
                (row["packet_id"], row["success"])
                for row in rows
                if row.get("event") == "attempt_finished"
            ]
            self.assertEqual(
                attempt_order,
                [
                    ("vertical", False),
                    ("support", True),
                    ("vertical", True),
                    ("integrate", True),
                ],
            )
            self.assertEqual(result.final_review["verdict"], "MERGE")
            candidates = learning.retrieve(
                "exact verifier",
                role="sol_plan",
                include_candidates=True,
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].status, "candidate")

    def test_verify_only_packet_reviews_without_invoking_luna(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                VerifyOnlyProvider(),
            ).run_new(
                goal="Verify it",
                project=source,
                run_root=root / "run",
                run_id="verifyonly",
            )

            self.assertEqual(result.state.status, "complete")
            self.assertEqual(result.state.packet("verify").status, PacketStatus.ACCEPTED)
            self.assertEqual(result.state.model_calls, 4)

    def test_terra_contract_cannot_smuggle_production_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                TerraProductionEditProvider(),
            ).run_new(
                goal="Do not let Terra implement",
                project=source,
                run_root=root / "run",
                run_id="terraguard",
            )

            self.assertEqual(result.state.packet("unsafe").status, PacketStatus.PARKED)
            self.assertEqual(result.state.packet("unsafe").attempts, 2)
            self.assertEqual(result.state.packet("unsafe").implementation_attempts, 0)
            self.assertFalse((result.integration.path / "production.py").exists())
            self.assertFalse((source / "production.py").exists())
            rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
            refusals = [row for row in rows if row.get("event") == "contract_refused"]
            self.assertEqual(len(refusals), 2)
            self.assertEqual(len({row["fingerprint"] for row in refusals}), 1)
            self.assertEqual(
                [
                    (row["packet_id"], row["success"])
                    for row in rows
                    if row.get("event") == "attempt_finished"
                ],
                [("unsafe", False), ("unsafe", False)],
            )
            self.assertFalse(
                any(row.get("event") == "implementation_attempt_reserved" for row in rows)
            )

    def test_resume_repairs_state_lagging_one_signed_contract_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            run_root = root / "run"
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            provider = CrashWindowTerraProvider()
            controller = MochiController(config, provider)
            original_save = StateStore.save

            def crash_before_refusal_state_save(store, state):
                packet = state.packet("unsafe")
                if packet.attempts == 1 and packet.last_failure:
                    raise RuntimeError("simulated crash before refusal state save")
                return original_save(store, state)

            with mock.patch.object(StateStore, "save", new=crash_before_refusal_state_save):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash before refusal state save",
                ):
                    controller.run_new(
                        goal="Recover the signed unsafe contract refusal",
                        project=source,
                        run_root=run_root,
                        run_id="contractrefusalcrash",
                    )

            persisted = StateStore(run_root).load()
            self.assertEqual(persisted.packet("unsafe").status, PacketStatus.RUNNING)
            self.assertEqual(persisted.packet("unsafe").attempts, 0)
            self.assertEqual(persisted.packet("unsafe").fingerprints, [])
            self.assertEqual(persisted.queue, ["unsafe"])
            self.assertEqual(persisted.rounds, 0)

            ledger = EvidenceLedger(run_root / "evidence.jsonl")
            self.assertTrue(ledger.verify()[0])
            rows = ledger.records()
            refusal = next(row for row in rows if row.get("event") == "contract_refused")
            finished = next(row for row in rows if row.get("event") == "attempt_finished")
            self.assertEqual(finished["fingerprint"], refusal["fingerprint"])
            self.assertEqual(finished["reason"], refusal["reason"])
            self.assertTrue(finished["contract_refused"])

            result = controller.resume_existing(run_root=run_root)
            packet = result.state.packet("unsafe")

            self.assertEqual(result.state.status, "blocked")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.attempts, 1)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertEqual(packet.fingerprints, [finished["fingerprint"]])
            self.assertEqual(packet.last_failure, finished["reason"])
            self.assertEqual(result.state.queue, [])
            self.assertEqual(result.state.rounds, 1)
            self.assertEqual(provider.contract_calls, 1)
            self.assertEqual(provider.execute_calls, 0)
            self.assertFalse((result.integration.path / "production.py").exists())
            self.assertFalse((source / "production.py").exists())
            self.assertFalse(
                any(
                    row.get("event") == "implementation_attempt_reserved"
                    for row in EvidenceLedger(run_root / "evidence.jsonl").records()
                )
            )

    def test_provider_return_crash_consumes_attempt_then_retries_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            existing = source / "tests" / "test_existing.py"
            existing.parent.mkdir()
            existing.write_text("raise SystemExit(1)\n", encoding="utf-8")
            git(source, "add", "tests/test_existing.py")
            git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "add existing test",
            )
            run_root = root / "run"
            provider = TerraContractRecoveryProvider()
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            )

            with mock.patch(
                "mochicode_core.runner.contract_from_dict",
                side_effect=RuntimeError("simulated crash after Terra returned"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash after Terra returned",
                ):
                    controller.run_new(
                        goal="Consume the interrupted Terra contract attempt",
                        project=source,
                        run_root=run_root,
                        run_id="terracontractreturncrash",
                    )

            before_resume = EvidenceLedger(run_root / "evidence.jsonl").records()
            reservation = next(
                row
                for row in before_resume
                if row.get("event") == "contract_attempt_reserved"
            )
            self.assertFalse(
                any(row.get("event") == "attempt_finished" for row in before_resume)
            )

            result = controller.resume_existing(run_root=run_root)
            packet = result.state.packet("primary")
            rows = EvidenceLedger(run_root / "evidence.jsonl").records()
            recovered = next(
                row
                for row in rows
                if row.get("event") == "attempt_finished"
                and row.get("contract_interrupted") is True
            )

            self.assertEqual(result.state.status, "complete")
            self.assertEqual(packet.status, PacketStatus.ACCEPTED)
            self.assertEqual(packet.attempts, 2)
            self.assertEqual(packet.implementation_attempts, 1)
            self.assertEqual(
                recovered["contract_reservation_hash"],
                reservation["record_hash"],
            )
            self.assertEqual(
                provider.execute_calls,
                [("independent", 1), ("primary", 2)],
            )
            self.assertEqual(
                [call[0] for call in provider.contract_calls],
                ["primary", "independent", "primary"],
            )

    def test_max_one_attempt_cannot_be_bypassed_after_provider_return_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            run_root = root / "run"
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            provider = CrashWindowTerraProvider()
            controller = MochiController(config, provider)

            with mock.patch(
                "mochicode_core.runner.contract_from_dict",
                side_effect=RuntimeError("simulated crash after Terra returned"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash after Terra returned",
                ):
                    controller.run_new(
                        goal="Do not bypass the Terra attempt cap",
                        project=source,
                        run_root=run_root,
                        run_id="terracontractcapcrash",
                    )

            result = controller.resume_existing(run_root=run_root)
            packet = result.state.packet("unsafe")

            self.assertEqual(result.state.status, "blocked")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.attempts, 1)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertEqual(provider.contract_calls, 1)
            self.assertEqual(provider.execute_calls, 0)
            self.assertFalse((result.integration.path / "production.py").exists())
            self.assertFalse((source / "production.py").exists())

    def test_orphan_contract_refusal_without_terminal_attempt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            run_root = root / "run"
            provider = CrashWindowTerraProvider()
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            )
            original_append = EvidenceLedger.append

            def crash_before_terminal_attempt(ledger, value):
                if value.get("event") == "attempt_finished":
                    raise RuntimeError("simulated crash before terminal attempt evidence")
                return original_append(ledger, value)

            with mock.patch.object(
                EvidenceLedger,
                "append",
                new=crash_before_terminal_attempt,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash before terminal attempt evidence",
                ):
                    controller.run_new(
                        goal="Reject orphan Terra refusal evidence",
                        project=source,
                        run_root=run_root,
                        run_id="orphancontractrefusal",
                    )

            rows = EvidenceLedger(run_root / "evidence.jsonl").records()
            self.assertTrue(
                any(row.get("event") == "contract_attempt_reserved" for row in rows)
            )
            self.assertTrue(any(row.get("event") == "contract_refused" for row in rows))
            self.assertFalse(any(row.get("event") == "attempt_finished" for row in rows))
            with self.assertRaisesRegex(ValueError, "orphan contract refusal"):
                controller.resume_existing(run_root=run_root)
            self.assertEqual(provider.contract_calls, 1)
            self.assertEqual(provider.execute_calls, 0)

    def test_synthetic_contract_interruption_is_idempotent_after_second_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            run_root = root / "run"
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            provider = CrashWindowTerraProvider()
            controller = MochiController(config, provider)

            with mock.patch(
                "mochicode_core.runner.contract_from_dict",
                side_effect=RuntimeError("simulated crash after Terra returned"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash after Terra returned",
                ):
                    controller.run_new(
                        goal="Recover one synthetic Terra terminal exactly once",
                        project=source,
                        run_root=run_root,
                        run_id="terrasyntheticsecondcrash",
                    )

            original_save = StateStore.save

            def crash_after_synthetic_terminal(store, state):
                packet = state.packet("unsafe")
                if (
                    packet.attempts == 1
                    and packet.last_failure
                    == "controller interrupted after durable Terra contract reservation"
                ):
                    raise RuntimeError("simulated second crash before recovery state save")
                return original_save(store, state)

            with mock.patch.object(
                StateStore,
                "save",
                new=crash_after_synthetic_terminal,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated second crash before recovery state save",
                ):
                    controller.resume_existing(run_root=run_root)

            rows_after_second_crash = EvidenceLedger(
                run_root / "evidence.jsonl"
            ).records()
            synthetic = [
                row
                for row in rows_after_second_crash
                if row.get("event") == "attempt_finished"
                and row.get("contract_interrupted") is True
            ]
            self.assertEqual(len(synthetic), 1)
            persisted = StateStore(run_root).load()
            self.assertEqual(persisted.packet("unsafe").attempts, 0)

            result = controller.resume_existing(run_root=run_root)
            repeated = controller.resume_existing(run_root=run_root)
            packet = repeated.state.packet("unsafe")
            final_rows = EvidenceLedger(run_root / "evidence.jsonl").records()

            self.assertEqual(result.state.status, "blocked")
            self.assertEqual(repeated.state.status, "blocked")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.attempts, 1)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertEqual(repeated.state.model_calls, 2)
            self.assertEqual(provider.contract_calls, 1)
            self.assertEqual(provider.execute_calls, 0)
            self.assertEqual(
                sum(
                    row.get("event") == "attempt_finished"
                    and row.get("contract_interrupted") is True
                    for row in final_rows
                ),
                1,
            )
            self.assertFalse(
                any(
                    row.get("event") == "implementation_attempt_reserved"
                    for row in final_rows
                )
            )

    def test_normal_luna_terminal_is_evidence_first_and_replayed_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            run_root = root / "run"
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_model_calls=4,
            )
            provider = SuccessfulLunaAtomicProvider()
            controller = MochiController(config, provider)
            original_append = EvidenceLedger.append

            def crash_at_terminal_append(ledger, value):
                if (
                    value.get("event") == "attempt_finished"
                    and value.get("packet_id") == "hard-red"
                ):
                    raise RuntimeError("simulated crash at normal Luna terminal append")
                return original_append(ledger, value)

            with mock.patch.object(
                EvidenceLedger,
                "append",
                new=crash_at_terminal_append,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash at normal Luna terminal append",
                ):
                    controller.run_new(
                        goal="Make normal Luna terminal persistence atomic",
                        project=source,
                        run_root=run_root,
                        run_id="normalterminalatomicity",
                    )

            before = EvidenceLedger(run_root / "evidence.jsonl").records()
            self.assertEqual(
                sum(row.get("event") == "packet_integrated" for row in before),
                1,
            )
            self.assertFalse(any(row.get("event") == "attempt_finished" for row in before))

            first = controller.resume_existing(run_root=run_root)
            integration_head = first.state.integration_head
            second = controller.resume_existing(run_root=run_root)
            rows = EvidenceLedger(run_root / "evidence.jsonl").records()
            packet = second.state.packet("hard-red")

            self.assertEqual(first.state.status, "stopped")
            self.assertEqual(second.state.status, "stopped")
            self.assertEqual(packet.status, PacketStatus.ACCEPTED)
            self.assertEqual(packet.attempts, 1)
            self.assertEqual(packet.implementation_attempts, 1)
            self.assertIsNone(packet.active_implementation_attempt)
            self.assertEqual(second.state.integration_head, integration_head)
            self.assertEqual(second.state.model_calls, 4)
            self.assertEqual(provider.contract_calls, 1)
            self.assertEqual(provider.execute_calls, 1)
            self.assertEqual(provider.review_calls, 1)
            self.assertEqual(
                sum(row.get("event") == "packet_integrated" for row in rows),
                1,
            )
            self.assertEqual(
                sum(row.get("event") == "attempt_finished" for row in rows),
                1,
            )
            self.assertEqual(
                (second.integration.path / "app.txt").read_text(encoding="utf-8"),
                "reviewed\n",
            )

    def test_verify_only_terminals_replay_once_after_state_save_crash(self) -> None:
        cases = (
            ("verify-only", CountingVerifyOnlyProvider, "verify", PacketStatus.ACCEPTED),
            (
                "already-satisfied",
                CountingAlreadySatisfiedProvider,
                "existing",
                PacketStatus.ALREADY_SATISFIED,
            ),
        )
        for name, provider_type, packet_id, expected_status in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    run_root = root / "run"
                    config = replace(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        max_model_calls=3,
                    )
                    provider = provider_type()
                    controller = MochiController(config, provider)
                    original_save = StateStore.save

                    def crash_after_terminal_evidence(store, state):
                        packet = state.packet(packet_id)
                        if packet.attempts == 1 and packet.status == expected_status:
                            raise RuntimeError(
                                "simulated crash before verify-only state save"
                            )
                        return original_save(store, state)

                    with mock.patch.object(
                        StateStore,
                        "save",
                        new=crash_after_terminal_evidence,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "simulated crash before verify-only state save",
                        ):
                            controller.run_new(
                                goal="Make verify-only terminal persistence atomic",
                                project=source,
                                run_root=run_root,
                                run_id=f"verifyterminal-{name}",
                            )

                    before = EvidenceLedger(run_root / "evidence.jsonl").records()
                    self.assertEqual(
                        sum(
                            row.get("event") == "verification_packet_accepted"
                            for row in before
                        ),
                        1,
                    )
                    self.assertEqual(
                        sum(row.get("event") == "attempt_finished" for row in before),
                        1,
                    )

                    first = controller.resume_existing(run_root=run_root)
                    second = controller.resume_existing(run_root=run_root)
                    rows = EvidenceLedger(run_root / "evidence.jsonl").records()
                    packet = second.state.packet(packet_id)

                    self.assertEqual(first.state.status, "stopped")
                    self.assertEqual(second.state.status, "stopped")
                    self.assertEqual(packet.status, expected_status)
                    self.assertEqual(packet.attempts, 1)
                    self.assertEqual(packet.implementation_attempts, 0)
                    self.assertEqual(second.state.model_calls, 3)
                    self.assertEqual(provider.contract_calls, 1)
                    self.assertEqual(provider.review_calls, 1)
                    self.assertEqual(
                        sum(
                            row.get("event") == "verification_packet_accepted"
                            for row in rows
                        ),
                        1,
                    )
                    self.assertEqual(
                        sum(row.get("event") == "attempt_finished" for row in rows),
                        1,
                    )

    def test_contract_refusal_recovery_rejects_omitted_or_reordered_queue(self) -> None:
        cases = (
            ("omitted", ["primary"], None),
            ("reordered", ["independent", "primary"], None),
            ("added-unknown", ["primary", "independent", "unknown"], None),
            ("duplicate", ["primary", "independent", "primary"], None),
            ("status-drift", ["primary", "independent"], PacketStatus.BLOCKED),
        )
        for name, altered_queue, independent_status in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    existing = source / "tests" / "test_existing.py"
                    existing.parent.mkdir()
                    existing.write_text("raise SystemExit(1)\n", encoding="utf-8")
                    git(source, "add", "tests/test_existing.py")
                    git(
                        source,
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-m",
                        "add existing test",
                    )
                    run_root = root / "run"
                    provider = TerraContractRecoveryProvider()
                    controller = MochiController(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        provider,
                    )
                    original_save = StateStore.save

                    def crash_after_terminal_evidence(store, state):
                        packet = state.packet("primary")
                        if packet.attempts == 1 and packet.last_failure:
                            raise RuntimeError("simulated crash before refusal state save")
                        return original_save(store, state)

                    with mock.patch.object(
                        StateStore,
                        "save",
                        new=crash_after_terminal_evidence,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "simulated crash before refusal state save",
                        ):
                            controller.run_new(
                                goal="Bind recovery to the exact pending queue",
                                project=source,
                                run_root=run_root,
                                run_id="contractqueuecrash",
                            )

                    persisted = StateStore(run_root).load()
                    persisted.queue = list(altered_queue)
                    if independent_status is not None:
                        persisted.packet("independent").status = independent_status
                    StateStore(run_root).save(persisted)
                    with self.assertRaisesRegex(
                        ValueError,
                        "queue|status|signed evidence|reservation",
                    ):
                        controller.resume_existing(run_root=run_root)
                    self.assertEqual(len(provider.contract_calls), 1)
                    self.assertEqual(provider.execute_calls, [])

    def test_terra_contract_cannot_modify_an_existing_test_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            existing = source / "tests" / "test_existing.py"
            existing.parent.mkdir()
            existing.write_text("raise SystemExit(1)\n", encoding="utf-8")
            git(source, "add", "tests/test_existing.py")
            git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "add existing test",
            )
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                TerraExistingTestEditProvider(),
            ).run_new(
                goal="Preserve existing tests",
                project=source,
                run_root=root / "run",
                run_id="terraexistingtest",
            )

            self.assertEqual(result.state.packet("unsafe").status, PacketStatus.PARKED)
            self.assertEqual(result.state.packet("unsafe").attempts, 2)
            self.assertEqual(result.state.packet("unsafe").implementation_attempts, 0)
            self.assertEqual(
                (result.integration.path / "tests" / "test_existing.py").read_text(encoding="utf-8"),
                "raise SystemExit(1)\n",
            )

    def test_unsafe_terra_contract_rotates_and_retries_in_fresh_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            existing = source / "tests" / "test_existing.py"
            existing.parent.mkdir()
            existing.write_text("raise SystemExit(1)\n", encoding="utf-8")
            git(source, "add", "tests/test_existing.py")
            git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "add existing test",
            )
            provider = TerraContractRecoveryProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Recover unsafe Terra contracts",
                project=source,
                run_root=root / "run",
                run_id="terracontractrecovery",
            )

            self.assertEqual(result.state.status, "complete")
            self.assertEqual(result.state.packet("primary").status, PacketStatus.ACCEPTED)
            self.assertEqual(result.state.packet("primary").attempts, 2)
            self.assertEqual(result.state.packet("primary").implementation_attempts, 1)
            self.assertEqual(
                provider.contract_calls,
                [
                    ("primary", 0, None, "primary-a1"),
                    ("independent", 0, None, "independent-a1"),
                    (
                        "primary",
                        1,
                        "Terra contract modified or deleted existing checks: tests/test_existing.py",
                        "primary-a2",
                    ),
                ],
            )
            self.assertEqual(provider.execute_calls, [("independent", 1), ("primary", 2)])
            self.assertEqual(
                (result.integration.path / "tests" / "test_existing.py").read_text(
                    encoding="utf-8"
                ),
                "raise SystemExit(1)\n",
            )
            self.assertEqual(
                (source / "tests" / "test_existing.py").read_text(encoding="utf-8"),
                "raise SystemExit(1)\n",
            )
            rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
            refusal = next(row for row in rows if row.get("event") == "contract_refused")
            self.assertEqual(refusal["packet_id"], "primary")
            self.assertEqual(refusal["attempt"], 1)
            self.assertTrue(refusal["fingerprint"])
            self.assertEqual(
                [
                    (row["packet_id"], row["success"])
                    for row in rows
                    if row.get("event") == "attempt_finished"
                ],
                [("primary", False), ("independent", True), ("primary", True)],
            )

    def test_final_verifier_cannot_modify_protected_inputs_and_still_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            result = MochiController(config, VerifierTamperProvider()).run_new(
                goal="Reject verifier tampering",
                project=source,
                run_root=root / "run",
                run_id="verifiertamper",
            )

            self.assertEqual(result.state.packet("vertical").status, PacketStatus.PARKED)
            self.assertFalse((result.integration.path / "app.txt").exists())

    def test_verifier_cannot_modify_an_allowed_artifact_and_become_the_implementer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            result = MochiController(config, VerifierImplementsProvider()).run_new(
                goal="Keep verification read-only",
                project=source,
                run_root=root / "run",
                run_id="verifierimplements",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertFalse((result.integration.path / "app.txt").exists())

    def test_already_satisfied_packet_still_gets_final_verification_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = AlreadySatisfiedProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Prove existing behavior",
                project=source,
                run_root=root / "run",
                run_id="alreadysatisfied",
            )

            packet = result.state.packet("existing")
            self.assertEqual(packet.status, PacketStatus.ALREADY_SATISFIED)
            self.assertEqual(packet.attempts, 1)
            self.assertEqual(provider.review_calls, 1)
            self.assertEqual(result.state.status, "complete")

    def test_tampered_raw_verifier_receipt_blocks_later_final_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                VerifyOnlyProvider(),
            )
            result = controller.run_new(
                goal="Verify receipt integrity",
                project=source,
                run_root=root / "run",
                run_id="receipttamper",
            )
            ledger = EvidenceLedger(result.run_root / "evidence.jsonl")
            baseline_receipt = next(
                receipt
                for record in ledger.records()
                for receipt in record.get("receipts", [])
                if receipt["path"].endswith("baseline.json")
            )
            path = result.run_root / baseline_receipt["path"]
            path.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "receipt hash mismatch"):
                controller.repeat_final_review(run_root=result.run_root)

    def test_red_terra_review_blocks_hard_verification_packet_integration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = HardReviewRedProvider()
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            result = MochiController(config, provider).run_new(
                goal="Require Terra approval",
                project=source,
                run_root=root / "run",
                run_id="hardreviewred",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertFalse((result.integration.path / "app.txt").exists())
            self.assertEqual(provider.final_review_calls, 0)
            events = [row.get("event") for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()]
            self.assertNotIn("packet_integrated", events)

    def test_final_review_requires_current_integration_verification_for_every_packet(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = StaleIntegrationProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Reject stale packet evidence",
                project=source,
                run_root=root / "run",
                run_id="staleintegration",
            )

            self.assertEqual(result.state.status, "verification_failed")
            self.assertIsNone(result.final_review)
            self.assertEqual(provider.final_review_calls, 0)
            events = [row.get("event") for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()]
            self.assertIn("final_integration_verification_failed", events)

    def test_contract_with_no_protected_matches_is_refused_before_luna(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = EmptyProtectedProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Reject empty protection",
                project=source,
                run_root=root / "run",
                run_id="emptyprotected",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertEqual(result.state.packet("hard-red").attempts, 2)
            self.assertEqual(result.state.packet("hard-red").implementation_attempts, 0)
            self.assertEqual(provider.execute_calls, 0)
            self.assertEqual(provider.final_review_calls, 0)
            self.assertFalse((result.integration.path / "app.txt").exists())

    def test_incomplete_green_review_cannot_accept_or_integrate_a_packet(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = MalformedPacketReviewProvider()
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            result = MochiController(config, provider).run_new(
                goal="Reject malformed GREEN",
                project=source,
                run_root=root / "run",
                run_id="malformedgreen",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertFalse((result.integration.path / "app.txt").exists())
            self.assertEqual(provider.final_review_calls, 0)

    def test_incomplete_merge_review_cannot_complete_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                MalformedFinalReviewProvider(),
            ).run_new(
                goal="Reject malformed MERGE",
                project=source,
                run_root=root / "run",
                run_id="malformedmerge",
            )

            self.assertEqual(result.state.status, "review_failed")
            events = [row.get("event") for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()]
            self.assertIn("final_review_invalid", events)

    def test_packet_goal_tokens_never_enter_learning_state_or_cloud_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            learning = LearningStore(root / "learning")
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                SecretLessonProvider(),
                learning,
            ).run_new(
                goal=SecretLessonProvider.secret,
                project=source,
                run_root=root / "run",
                run_id="privacylesson",
            )

            self.assertEqual(result.state.status, "complete")
            persisted = (learning.root / "lessons.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(SecretLessonProvider.secret.lower(), persisted.lower())
            self.assertNotIn(
                SecretLessonProvider.secret.lower(),
                json.dumps(learning.redacted_export()).lower(),
            )

    def test_live_run_lease_blocks_a_second_resume_before_any_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                VerifyOnlyProvider(),
            )
            result = controller.run_new(
                goal="Create a resumable run",
                project=source,
                run_root=root / "run",
                run_id="runlease",
            )
            before_processes = list(result.run_root.glob("model-processes.jsonl"))
            lease = result.run_root / ".run.lease.json"
            lease.write_text(
                json.dumps({"pid": os.getpid(), "token": "held-by-test"}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StateLockError, "live PID"):
                controller.resume_existing(run_root=result.run_root)

            self.assertEqual(list(result.run_root.glob("model-processes.jsonl")), before_processes)
            self.assertFalse((result.run_root / "packets" / "verify-a2").exists())

    def test_resume_refuses_when_the_source_branch_head_has_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                VerifyOnlyProvider(),
            )
            result = controller.run_new(
                goal="Pin the source baseline",
                project=source,
                run_root=root / "run",
                run_id="sourcepin",
            )
            evidence_before = (result.run_root / "evidence.jsonl").read_bytes()
            (source / "README.md").write_text("advanced source\n", encoding="utf-8")
            git(source, "add", "README.md")
            git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "advance source",
            )

            with self.assertRaisesRegex(GitOperationError, "source HEAD drifted"):
                controller.resume_existing(run_root=result.run_root)

            self.assertEqual((result.run_root / "evidence.jsonl").read_bytes(), evidence_before)

    def test_resume_refuses_an_unreviewed_integration_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                VerifyOnlyProvider(),
            )
            result = controller.run_new(
                goal="Pin the integration head",
                project=source,
                run_root=root / "run",
                run_id="integrationpin",
            )
            evidence_before = (result.run_root / "evidence.jsonl").read_bytes()
            (result.integration.path / "unreviewed.txt").write_text("outside controller\n", encoding="utf-8")
            git(result.integration.path, "add", "unreviewed.txt")
            git(
                result.integration.path,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "unreviewed integration change",
            )

            with self.assertRaisesRegex(GitOperationError, "integration HEAD drifted"):
                controller.resume_existing(run_root=result.run_root)

            self.assertEqual((result.run_root / "evidence.jsonl").read_bytes(), evidence_before)

    def test_resume_refuses_state_that_prunes_the_signed_packet_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                VerifyOnlyProvider(),
            )
            result = controller.run_new(
                goal="Sign the packet graph",
                project=source,
                run_root=root / "run",
                run_id="signedplan",
            )
            state_path = result.run_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["packets"] = []
            state["queue"] = []
            state["status"] = "running"
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            evidence_before = (result.run_root / "evidence.jsonl").read_bytes()

            with self.assertRaisesRegex(ValueError, "signed plan"):
                controller.resume_existing(run_root=result.run_root)

            self.assertEqual((result.run_root / "evidence.jsonl").read_bytes(), evidence_before)

    def test_two_interrupted_luna_reservations_park_before_a_third_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = InterruptingLunaProvider()
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            )
            run_root = root / "run"

            with self.assertRaisesRegex(RuntimeError, "simulated controller interruption"):
                controller.run_new(
                    goal="Consume interrupted Luna attempts",
                    project=source,
                    run_root=run_root,
                    run_id="interruptedluna",
                )
            with self.assertRaisesRegex(RuntimeError, "simulated controller interruption"):
                controller.resume_existing(run_root=run_root)

            result = controller.resume_existing(run_root=run_root)

            packet = result.state.packet("hard-red")
            self.assertEqual(provider.execute_calls, 2)
            self.assertEqual(packet.attempts, 2)
            self.assertEqual(packet.status, PacketStatus.PARKED)
            reservations = [
                row
                for row in EvidenceLedger(run_root / "evidence.jsonl").records()
                if row.get("event") == "implementation_attempt_reserved"
            ]
            self.assertEqual(len(reservations), 2)

    def test_active_luna_recovery_rejects_signed_state_drift(self) -> None:
        cases = (
            "omit-peer",
            "reorder-peers",
            "status-drift",
            "model-calls-lower",
            "model-calls-higher",
            "attempt-drift",
            "round-drift",
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    run_root = root / "run"
                    config = replace(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        max_attempts_per_packet=1,
                    )
                    provider = ActiveLunaPeerProvider()
                    controller = MochiController(config, provider)

                    with self.assertRaisesRegex(
                        RuntimeError,
                        "simulated active Luna interruption",
                    ):
                        controller.run_new(
                            goal="Bind active Luna recovery to signed controller state",
                            project=source,
                            run_root=run_root,
                            run_id=f"activelunatamper-{case}",
                        )

                    state = StateStore(run_root).load()
                    packet = state.packet("active")
                    self.assertEqual(packet.active_implementation_attempt, 1)
                    self.assertEqual(packet.implementation_attempts, 1)
                    evidence_before = (run_root / "evidence.jsonl").read_bytes()
                    integration_head_before = state.integration_head

                    if case == "omit-peer":
                        state.queue = ["active", "peer-a"]
                    elif case == "reorder-peers":
                        state.queue = ["active", "peer-b", "peer-a"]
                    elif case == "status-drift":
                        state.packet("peer-a").status = PacketStatus.BLOCKED
                    elif case == "model-calls-lower":
                        state.model_calls -= 1
                    elif case == "model-calls-higher":
                        state.model_calls += 1
                    elif case == "attempt-drift":
                        packet.attempts = 1
                        packet.fingerprints = ["tampered-fingerprint"]
                    elif case == "round-drift":
                        state.rounds += 1
                    StateStore(run_root).save(state)

                    with self.assertRaisesRegex(
                        ValueError,
                        "active Luna|implementation reservation|signed evidence|round|attempt",
                    ):
                        controller.resume_existing(run_root=run_root)

                    persisted = StateStore(run_root).load()
                    self.assertEqual(
                        (run_root / "evidence.jsonl").read_bytes(),
                        evidence_before,
                    )
                    self.assertEqual(persisted.integration_head, integration_head_before)
                    self.assertEqual(provider.contract_calls, ["active"])
                    self.assertEqual(provider.execute_calls, ["active"])
                    self.assertEqual(provider.review_calls, 0)
                    records = EvidenceLedger(run_root / "evidence.jsonl").records()
                    self.assertFalse(
                        any(row.get("event") == "attempt_finished" for row in records)
                    )
                    self.assertFalse(
                        any(row.get("event") == "packet_integrated" for row in records)
                    )

    def test_implementation_count_is_independent_from_packet_attempt_ordinal(self) -> None:
        def source_with_existing_test(root: Path) -> Path:
            source = make_repo(root)
            existing = source / "tests" / "test_existing.py"
            existing.parent.mkdir()
            existing.write_text("raise SystemExit(1)\n", encoding="utf-8")
            git(source, "add", "tests/test_existing.py")
            git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "add existing test",
            )
            return source

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = source_with_existing_test(root)
            run_root = root / "run"
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_model_calls=9,
            )
            provider = TerraContractRecoveryProvider()
            controller = MochiController(config, provider)
            completed = controller.run_new(
                goal="Refuse Terra attempt one, then complete Luna attempt two",
                project=source,
                run_root=run_root,
                run_id="implementationcountcomplete",
            )
            resumed = controller.resume_existing(run_root=run_root)
            packet = resumed.state.packet("primary")

            self.assertEqual(completed.state.status, "complete")
            self.assertEqual(resumed.state.status, "stopped")
            self.assertEqual(packet.attempts, 2)
            self.assertEqual(packet.implementation_attempts, 1)
            self.assertIsNone(packet.active_implementation_attempt)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = source_with_existing_test(root)
            run_root = root / "run"
            provider = TerraThenInterruptingLunaProvider()
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "simulated Luna interruption on packet attempt two",
            ):
                controller.run_new(
                    goal="Recover Luna reservation one at packet attempt two",
                    project=source,
                    run_root=run_root,
                    run_id="implementationcountactive",
                )
            active = StateStore(run_root).load().packet("primary")
            self.assertEqual(active.attempts, 1)
            self.assertEqual(active.implementation_attempts, 1)
            self.assertEqual(active.active_implementation_attempt, 2)

            recovered = controller.resume_existing(run_root=run_root)
            packet = recovered.state.packet("primary")
            self.assertEqual(packet.attempts, 2)
            self.assertEqual(packet.implementation_attempts, 1)
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertIsNone(packet.active_implementation_attempt)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = source_with_existing_test(root)
            run_root = root / "run"
            provider = TerraThenInterruptingLunaProvider()
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            )
            with self.assertRaisesRegex(RuntimeError, "packet attempt two"):
                controller.run_new(
                    goal="Reject tampered implementation count",
                    project=source,
                    run_root=run_root,
                    run_id="implementationcounttamper",
                )
            state = StateStore(run_root).load()
            state.packet("primary").implementation_attempts = 2
            StateStore(run_root).save(state)
            evidence_before = (run_root / "evidence.jsonl").read_bytes()
            with self.assertRaisesRegex(
                ValueError,
                "implementation attempt|active Luna|signed evidence",
            ):
                controller.resume_existing(run_root=run_root)
            self.assertEqual((run_root / "evidence.jsonl").read_bytes(), evidence_before)

    def test_unrecorded_integration_merge_fails_before_recovery_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            run_root = root / "run"
            provider = SuccessfulLunaAtomicProvider()
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            )
            original_append = EvidenceLedger.append

            def crash_before_integration_evidence(ledger, value):
                if value.get("event") == "packet_integrated":
                    raise RuntimeError("simulated crash before packet_integrated")
                return original_append(ledger, value)

            with mock.patch.object(
                EvidenceLedger,
                "append",
                new=crash_before_integration_evidence,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash before packet_integrated",
                ):
                    controller.run_new(
                        goal="Reject an unrecorded integration merge",
                        project=source,
                        run_root=run_root,
                        run_id="unrecordedintegration",
                    )

            state_before = (run_root / "state.json").read_bytes()
            evidence_before = (run_root / "evidence.jsonl").read_bytes()
            with self.assertRaisesRegex(
                GitOperationError,
                "integration HEAD drifted",
            ):
                controller.resume_existing(run_root=run_root)
            self.assertEqual((run_root / "state.json").read_bytes(), state_before)
            self.assertEqual((run_root / "evidence.jsonl").read_bytes(), evidence_before)
            records = list(EvidenceLedger(run_root / "evidence.jsonl").records())
            self.assertFalse(any(row.get("event") == "attempt_finished" for row in records))
            self.assertFalse(any(row.get("event") == "packet_integrated" for row in records))
            self.assertEqual(provider.contract_calls, 1)
            self.assertEqual(provider.execute_calls, 1)
            self.assertEqual(provider.review_calls, 1)

    def test_luna_integration_and_terminal_model_calls_must_match(self) -> None:
        for tampered_event in ("packet_integrated", "attempt_finished"):
            with self.subTest(tampered_event=tampered_event):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    run_root = root / "run"
                    provider = SuccessfulLunaAtomicProvider()
                    controller = MochiController(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        provider,
                    )
                    original_append = EvidenceLedger.append
                    original_save = StateStore.save

                    def append_with_tampered_count(ledger, value):
                        if value.get("event") == tampered_event:
                            value = dict(value)
                            value["model_calls"] = int(value["model_calls"]) + 1
                        return original_append(ledger, value)

                    def crash_before_terminal_state_save(store, state):
                        packet = state.packet("hard-red")
                        if packet.attempts == 1 and packet.status == PacketStatus.ACCEPTED:
                            raise RuntimeError("simulated crash before Luna terminal state save")
                        return original_save(store, state)

                    with mock.patch.object(
                        EvidenceLedger,
                        "append",
                        new=append_with_tampered_count,
                    ), mock.patch.object(
                        StateStore,
                        "save",
                        new=crash_before_terminal_state_save,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "terminal state save"):
                            controller.run_new(
                                goal="Reject mismatched integration model calls",
                                project=source,
                                run_root=run_root,
                                run_id=f"modelcalltamper-{tampered_event}",
                            )

                    state_before = (run_root / "state.json").read_bytes()
                    evidence_before = (run_root / "evidence.jsonl").read_bytes()
                    with self.assertRaisesRegex(
                        ValueError,
                        "model-call|canonical lag replay|signed evidence",
                    ):
                        controller.resume_existing(run_root=run_root)
                    self.assertEqual((run_root / "state.json").read_bytes(), state_before)
                    self.assertEqual(
                        (run_root / "evidence.jsonl").read_bytes(),
                        evidence_before,
                    )
                    self.assertEqual(provider.contract_calls, 1)
                    self.assertEqual(provider.execute_calls, 1)
                    self.assertEqual(provider.review_calls, 1)

    def test_implementation_reservation_rejects_out_of_order_contract_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            run_root = root / "run"
            provider = ActiveLunaPeerProvider()
            controller = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            )
            with self.assertRaisesRegex(RuntimeError, "active Luna interruption"):
                controller.run_new(
                    goal="Reject out-of-order reservation evidence",
                    project=source,
                    run_root=run_root,
                    run_id="implementationreservationorder",
            )
            state = StateStore(run_root).load()
            records = list(EvidenceLedger(run_root / "evidence.jsonl").records())
            contract_index = next(
                index
                for index, row in enumerate(records)
                if row.get("event") == "contract_attempt_reserved"
            )
            implementation_index = next(
                index
                for index, row in enumerate(records)
                if row.get("event") == "implementation_attempt_reserved"
            )
            records[contract_index], records[implementation_index] = (
                records[implementation_index],
                records[contract_index],
            )
            with self.assertRaisesRegex(ValueError, "order|precede"):
                MochiController._implementation_attempt_reservations(
                    state=state,
                    records=records,
                )

    def test_luna_cannot_rewrite_an_unprotected_final_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = LunaVerifierRewriteProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Protect the verifier itself",
                project=source,
                run_root=root / "run",
                run_id="protectverifier",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertEqual(result.state.packet("hard-red").attempts, 2)
            self.assertEqual(result.state.packet("hard-red").implementation_attempts, 0)
            self.assertEqual(provider.execute_calls, 0)
            self.assertFalse((result.integration.path / "app.txt").exists())

    def test_baseline_cannot_write_an_allowed_artifact_before_luna(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = BaselineImplementsProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Keep the red baseline read-only",
                project=source,
                run_root=root / "run",
                run_id="baselineimmutable",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertEqual(result.state.packet("hard-red").attempts, 2)
            self.assertEqual(result.state.packet("hard-red").implementation_attempts, 0)
            self.assertEqual(provider.execute_calls, 0)
            self.assertFalse((result.integration.path / "app.txt").exists())

    def test_stop_after_terra_contract_prevents_any_luna_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = StopAfterContractProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Stop at the Terra boundary",
                project=source,
                run_root=root / "run",
                run_id="stopafterterra",
            )

            self.assertEqual(result.state.status, "stopped")
            self.assertEqual(provider.execute_calls, 0)
            self.assertFalse((result.integration.path / "app.txt").exists())

    def test_packet_mutation_after_terra_review_cannot_be_merged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            result = MochiController(config, PostReviewMutationProvider()).run_new(
                goal="Bind Terra review to one packet commit",
                project=source,
                run_root=root / "run",
                run_id="postreviewmutation",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertFalse((result.integration.path / "app.txt").exists())

    def test_inline_interpreter_verifier_is_refused_before_luna(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = InlineVerifierProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Reject inline verifier code",
                project=source,
                run_root=root / "run",
                run_id="inlineverifier",
            )

            self.assertEqual(result.state.packet("hard-red").status, PacketStatus.PARKED)
            self.assertEqual(result.state.packet("hard-red").attempts, 2)
            self.assertEqual(result.state.packet("hard-red").implementation_attempts, 0)
            self.assertEqual(provider.execute_calls, 0)

    def test_legitimate_contract_refusals_are_terminal_and_resumable(self) -> None:
        for refusal_mode in ("empty-protection", "write-overlap", "baseline"):
            with self.subTest(refusal_mode=refusal_mode):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    run_root = root / "run"
                    config = replace(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        max_attempts_per_packet=1,
                    )
                    provider = LegitimateContractRefusalProvider(refusal_mode)
                    controller = MochiController(config, provider)

                    first = controller.run_new(
                        goal="Refuse packet A while packet B remains independent",
                        project=source,
                        run_root=run_root,
                        run_id=f"legitimaterefusal-{refusal_mode}",
                    )
                    resumed = controller.resume_existing(run_root=run_root)
                    packet = resumed.state.packet("refused")
                    rows = EvidenceLedger(run_root / "evidence.jsonl").records()
                    reservation = next(
                        row
                        for row in rows
                        if row.get("event") == "contract_attempt_reserved"
                        and row.get("packet_id") == "refused"
                    )
                    refusal = next(
                        row
                        for row in rows
                        if row.get("event") == "contract_refused"
                        and row.get("packet_id") == "refused"
                    )
                    terminal = next(
                        row
                        for row in rows
                        if row.get("event") == "attempt_finished"
                        and row.get("packet_id") == "refused"
                    )

                    self.assertEqual(first.state.status, "blocked")
                    self.assertEqual(resumed.state.status, "blocked")
                    self.assertEqual(packet.status, PacketStatus.PARKED)
                    self.assertEqual(packet.attempts, 1)
                    self.assertEqual(packet.implementation_attempts, 0)
                    self.assertEqual(
                        provider.contract_calls,
                        ["refused", "independent"],
                    )
                    self.assertEqual(provider.execute_calls, ["independent"])
                    self.assertEqual(refusal["refusal_kind"], refusal_mode)
                    self.assertEqual(terminal["refusal_kind"], refusal_mode)
                    self.assertEqual(
                        refusal["contract_reservation_hash"],
                        reservation["record_hash"],
                    )
                    self.assertEqual(
                        terminal["contract_reservation_hash"],
                        reservation["record_hash"],
                    )
                    self.assertEqual(terminal["fingerprint"], refusal["fingerprint"])
                    self.assertEqual(terminal["reason"], refusal["reason"])
                    self.assertFalse(
                        any(
                            row.get("event") == "implementation_attempt_reserved"
                            and row.get("packet_id") == "refused"
                            for row in rows
                        )
                    )
                    self.assertEqual(
                        (resumed.integration.path / "independent.txt").read_text(
                            encoding="utf-8"
                        ),
                        "independent\n",
                    )

    def test_sol_merge_cannot_survive_integration_mutation_after_final_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                MutatingSolProvider(),
            ).run_new(
                goal="Bind Sol to the verified integration identity",
                project=source,
                run_root=root / "run",
                run_id="solidentity",
            )

            self.assertEqual(result.state.status, "review_failed")
            events = [
                row.get("event")
                for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()
            ]
            self.assertIn("final_review_invalid", events)


if __name__ == "__main__":
    unittest.main()
