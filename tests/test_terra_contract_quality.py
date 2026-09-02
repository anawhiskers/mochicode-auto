from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.config import load_config
from mochicode_core.contracts import ExecutionMode, PacketContract, VerificationClass
from mochicode_core.evidence import EvidenceLedger
from mochicode_core.gitops import GitWorkspaceManager
from mochicode_core.models import PacketStatus
from mochicode_core.runner import MochiController, StubRoleProvider
from mochicode_core.verification import CommandResult


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


class TerraTestingRootProvider(StubRoleProvider):
    def __init__(
        self,
        *,
        modify_existing: bool = False,
        protected_patterns: tuple[str, ...] | None = None,
    ) -> None:
        self.modify_existing = modify_existing
        self.protected_patterns = protected_patterns
        self.execute_calls = 0

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "one pastebin packet",
            "packets": [
                {
                    "id": "pastebin",
                    "title": "Pastebin lexer",
                    "goal": "create pastebin.txt",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["pastebin.txt contains the paste"],
                    "verification_hints": ["run the focused pytest check"],
                }
            ],
        }

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        testing = workspace / "testing"
        testing.mkdir(parents=True, exist_ok=True)
        if self.modify_existing:
            check = testing / "test_pastebin.py"
            check.write_text("raise SystemExit(0)\n", encoding="utf-8")
        else:
            check = testing / "check_pastebin_text_lexer.py"
            check.write_text(
                "from pathlib import Path\n"
                "assert Path('pastebin.txt').read_text(encoding='utf-8') == 'paste\\n'\n",
                encoding="utf-8",
            )
        relative_check = check.relative_to(workspace).as_posix()
        command = [sys.executable, relative_check]
        protected_patterns = self.protected_patterns or (relative_check,)
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": list(protected_patterns),
            "allowed_paths": ["pastebin.txt"],
            "evidence_requirements": ["focused check and protected hashes"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        if self.modify_existing:
            raise AssertionError("Luna must not run after an existing testing file edit")
        self.execute_calls += 1
        (workspace / "pastebin.txt").write_text("paste\n", encoding="utf-8")
        return {
            "summary": "created pastebin.txt",
            "changed_files": ["pastebin.txt"],
            "commands_run": [],
            "remaining_assumptions": [],
        }

    def review(self, packet, contract, workspace, review_bundle):
        return {
            "verdict": "GREEN",
            "findings": [],
            "evidence_summary": "focused testing check passed",
        }

    def final_review(self, goal, state, workspace, final_bundle):
        return {
            "verdict": "MERGE",
            "criteria": [
                {
                    "criterion": criterion,
                    "status": "PASS",
                    "evidence": "final focused check",
                }
                for packet in state.packets
                for criterion in packet.acceptance_criteria
            ],
            "remaining_risks": [],
            "merge_recommendation": "human may merge",
        }


class NoOpVerifyOnlyProvider(TerraTestingRootProvider):
    def contract(self, packet, workspace: Path) -> dict[str, object]:
        command = ["git", "status", "--short"]
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
            "evidence_requirements": ["a protected repository verifier"],
        }

    def execute(self, *args, **kwargs):
        raise AssertionError("a no-op verify-only packet must not invoke Luna")


class ProtectedVerifyOnlyProvider(StubRoleProvider):
    def __init__(self) -> None:
        self.execute_calls = 0

    def plan(self, goal: str, workspace: Path) -> dict[str, object]:
        return {
            "summary": "verify one existing repository path",
            "packets": [
                {
                    "id": "verify-readme",
                    "title": "Verify README",
                    "goal": "prove README.md exists in the repository",
                    "wave": 1,
                    "priority": 1,
                    "vertical_slice": True,
                    "dependencies": [],
                    "acceptance_criteria": ["README.md exists"],
                    "verification_hints": ["check the protected README path"],
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
            "evidence_requirements": ["separate baseline and final command receipts"],
        }

    def execute(self, *args, **kwargs):
        self.execute_calls += 1
        raise AssertionError("verify-only packets must not invoke Luna")

    def review(self, packet, contract, workspace, review_bundle):
        return {
            "verdict": "GREEN",
            "findings": [],
            "evidence_summary": "baseline and final verification passed",
        }

    def final_review(self, goal, state, workspace, final_bundle):
        return {
            "verdict": "MERGE",
            "criteria": [
                {
                    "criterion": "README.md exists",
                    "status": "PASS",
                    "evidence": "protected verifier receipts",
                }
            ],
            "remaining_risks": [],
            "merge_recommendation": "human may merge",
        }


class FingerprintVerifyOnlyProvider(ProtectedVerifyOnlyProvider):
    def __init__(self, final_argvs: tuple[tuple[str, ...], ...]) -> None:
        super().__init__()
        self.final_argvs = final_argvs
        self.review_calls = 0

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        contract = super().contract(packet, workspace)
        contract["final_argvs"] = [list(argv) for argv in self.final_argvs]
        return contract

    def review(self, packet, contract, workspace, review_bundle):
        self.review_calls += 1
        return super().review(packet, contract, workspace, review_bundle)


class DirectProjectVerifierProvider(TerraTestingRootProvider):
    def contract(self, packet, workspace: Path) -> dict[str, object]:
        command = [sys.executable, "project_verifier.py"]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": ["project_verifier.py"],
            "allowed_paths": ["pastebin.txt"],
            "evidence_requirements": ["protected direct repository verifier"],
        }


class PytestExecutionProvider(TerraTestingRootProvider):
    def __init__(
        self,
        *,
        diagnostic_args: tuple[str, ...] = (),
        extra_allowed: tuple[str, ...] = (),
        write_harness: str | None = None,
    ) -> None:
        super().__init__()
        self.diagnostic_args = diagnostic_args
        self.extra_allowed = extra_allowed
        self.write_harness = write_harness

    def contract(self, packet, workspace: Path) -> dict[str, object]:
        testing = workspace / "testing"
        testing.mkdir(parents=True, exist_ok=True)
        check = testing / "test_pastebin.py"
        check.write_text(
            "from pathlib import Path\n"
            "def test_pastebin():\n"
            "    assert Path('pastebin.txt').read_text(encoding='utf-8') == 'paste\\n'\n",
            encoding="utf-8",
        )
        relative = check.relative_to(workspace).as_posix()
        pytest_config = testing / "pytest.ini"
        pytest_config.write_text("[pytest]\n", encoding="utf-8")
        relative_config = pytest_config.relative_to(workspace).as_posix()
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:typeguard",
            "-s",
            "-c",
            relative_config,
            "--rootdir=.",
            "--confcutdir=.",
            *self.diagnostic_args,
            relative,
        ]
        return {
            "packet_id": packet.packet_id,
            "goal": packet.goal,
            "execution_mode": "implement",
            "verification_class": "hard",
            "acceptance_criteria": list(packet.acceptance_criteria),
            "baseline_argv": command,
            "final_argvs": [command],
            "expected_failure_codes": [1],
            "protected_patterns": [relative, relative_config],
            "allowed_paths": ["pastebin.txt", *self.extra_allowed],
            "evidence_requirements": ["pytest executes the protected test"],
        }

    def execute(self, packet, contract, workspace: Path, attempt: int):
        result = super().execute(packet, contract, workspace, attempt)
        if self.write_harness:
            target = workspace / self.write_harness
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# late pytest harness mutation\n", encoding="utf-8")
        return result


class TerraContractQualityTests(unittest.TestCase):
    @staticmethod
    def _verifier_contract(
        argv: tuple[str, ...],
        *,
        protected_patterns: tuple[str, ...] = ("README.md",),
        allowed_paths: tuple[str, ...] = ("artifact.txt",),
    ) -> PacketContract:
        return PacketContract(
            packet_id="module-verifier",
            goal="verify the repository through a bounded command",
            execution_mode=ExecutionMode.IMPLEMENT,
            verification_class=VerificationClass.HARD,
            acceptance_criteria=("verifier passes",),
            baseline_argv=argv,
            final_argvs=(argv,),
            expected_failure_codes=(1,),
            protected_patterns=protected_patterns,
            allowed_paths=allowed_paths,
            evidence_requirements=("protected verifier inputs",),
        )

    @staticmethod
    def _python_module_contract(
        module_name: str,
        *,
        protected_patterns: tuple[str, ...],
        allowed_paths: tuple[str, ...] = ("artifact.txt",),
        extra_args: tuple[str, ...] = (),
    ) -> PacketContract:
        argv = (sys.executable, "-m", module_name, *extra_args)
        return TerraContractQualityTests._verifier_contract(
            argv,
            protected_patterns=protected_patterns,
            allowed_paths=allowed_paths,
        )

    def test_python_m_existing_module_is_always_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "README.md").write_text("project\n", encoding="utf-8")
            (root / "project_verifier.py").write_text(
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )

            unprotected = self._python_module_contract(
                "project_verifier",
                protected_patterns=("README.md",),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    unprotected,
                    root,
                    {"README.md"},
                ),
                "repository-local python -m verifier modules are forbidden; "
                "use a protected direct check file: project_verifier",
            )

            writable = self._python_module_contract(
                "project_verifier",
                protected_patterns=("project_verifier.py",),
                allowed_paths=("project_verifier.py",),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    writable,
                    root,
                    {"project_verifier.py"},
                ),
                "protected measurement inputs overlap Luna write paths: project_verifier.py",
            )

            protected = self._python_module_contract(
                "project_verifier",
                protected_patterns=("project_verifier.py",),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    protected,
                    root,
                    {"project_verifier.py"},
                ),
                "repository-local python -m verifier modules are forbidden; "
                "use a protected direct check file: project_verifier",
            )

    def test_protected_direct_project_verifier_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            module = source / "project_verifier.py"
            module.write_text(
                "from pathlib import Path\n"
                "assert Path('pastebin.txt').read_text(encoding='utf-8') == 'paste\\n'\n",
                encoding="utf-8",
            )
            git(source, "add", "project_verifier.py")
            git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "add project module verifier",
            )
            provider = DirectProjectVerifierProvider()

            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Run a protected direct Python verifier",
                project=source,
                run_root=root / "run",
                run_id="protected-python-module",
            )

            self.assertEqual(result.state.status, "complete")
            self.assertEqual(result.state.packet("pastebin").status, PacketStatus.ACCEPTED)
            self.assertEqual(provider.execute_calls, 1)

    def test_python_m_package_helper_import_attack_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = (
                "company/__init__.py",
                "company/verifiers/__init__.py",
                "company/verifiers/project/__init__.py",
                "company/verifiers/project/__main__.py",
                "company/verifiers/project/helper.py",
            )
            for relative in inputs:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                content = (
                    "from . import helper\nassert helper.VALUE == 1\n"
                    if relative.endswith("/__main__.py")
                    else "VALUE = 1\n"
                )
                path.write_text(content, encoding="utf-8")
            contract = self._python_module_contract(
                "company.verifiers.project",
                protected_patterns=inputs,
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    set(inputs),
                ),
                "repository-local python -m verifier modules are forbidden; "
                "use a protected direct check file: company.verifiers.project",
            )

    def test_local_pytest_module_shadows_external_and_is_refused(self) -> None:
        cases = {
            "module": ("pytest.py",),
            "package": ("pytest/__init__.py", "pytest/__main__.py"),
        }
        for name, inputs in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    for relative in inputs:
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(f"# local {relative}\n", encoding="utf-8")
                    contract = self._python_module_contract(
                        "pytest",
                        protected_patterns=inputs,
                    )

                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            set(),
                        ),
                        "repository-local python -m verifier modules are forbidden; "
                        "use a protected direct check file: pytest",
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            set(inputs),
                        ),
                        "repository-local python -m verifier modules are forbidden; "
                        "use a protected direct check file: pytest",
                    )

    def test_versioned_and_path_qualified_python_reject_inline_c(self) -> None:
        executables = (
            "python",
            "python3",
            "python3.12",
            "python312",
            "py",
            "py.exe",
            r"C:\Tools\Python312\python3.12.exe",
            r"C:\Tools\Python312\python312.exe",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for executable in executables:
                with self.subTest(executable=executable):
                    contract = self._verifier_contract(
                        (executable, "-c", "raise SystemExit(0)"),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            set(),
                        ),
                        f"inline interpreter verifier commands are forbidden: {executable}",
                    )

    def test_non_python_interpreter_aliases_reject_inline_code(self) -> None:
        cases = (
            ("node", "--eval=process.exit(0)"),
            ("node", "-eprocess.exit(0)"),
            ("node", "--print=1"),
            ("node", "-p1"),
            ("bun", "-econsole.log(1)"),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "checks" / "pinned.js"
            check.parent.mkdir()
            check.write_text("process.exit(1);\n", encoding="utf-8")
            for executable, flag in cases:
                with self.subTest(executable=executable, flag=flag):
                    contract = self._verifier_contract(
                        (executable, flag, "checks/pinned.js"),
                        protected_patterns=("checks/pinned.js",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"checks/pinned.js"},
                        ),
                        f"inline interpreter verifier commands are forbidden: {executable}",
                    )

            deno = self._verifier_contract(
                ("deno", "eval", "Deno.exit(0)", "checks/pinned.js"),
                protected_patterns=("checks/pinned.js",),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    deno,
                    root,
                    {"checks/pinned.js"},
                ),
                "inline interpreter verifier commands are forbidden: deno",
            )

    def test_nonexecution_mode_cannot_be_padded_with_protected_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "checks" / "pinned.js"
            check.parent.mkdir()
            check.write_text("process.exit(1);\n", encoding="utf-8")
            contract = self._verifier_contract(
                ("node", "--version", "checks/pinned.js"),
                protected_patterns=("checks/pinned.js",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"checks/pinned.js"},
                ),
                "verifier command does not execute checks: --version",
            )

    def test_direct_node_script_is_valid_only_when_consumed_and_protected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "checks" / "direct-check.js"
            check.parent.mkdir()
            check.write_text("process.exit(0);\n", encoding="utf-8")
            contract = self._verifier_contract(
                ("node", "checks/direct-check.js"),
                protected_patterns=("checks/direct-check.js",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"checks/direct-check.js"},
                ),
                "",
            )

    def test_unknown_external_verifier_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "checks" / "pinned.txt"
            check.parent.mkdir()
            check.write_text("expected\n", encoding="utf-8")
            contract = self._verifier_contract(
                ("unknown-checker", "checks/pinned.txt"),
                protected_patterns=("checks/pinned.txt",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"checks/pinned.txt"},
                ),
                "unsupported verifier executable: unknown-checker",
            )

    def test_free_threaded_debug_and_suffixed_python_family_is_fail_closed(self) -> None:
        executables = (
            "python3.13t.exe",
            "python3.13d",
            "pythonw3.13t",
            "pypy3.11-v7.3",
            r"C:\Tools\Python313\python3.13t.exe",
            r"C:\Tools\Python313\pythonw3.13d.exe",
            r"C:\Tools\PyPy\pypy3.11-v7.3.exe",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local_module = root / "project_verifier.py"
            local_module.write_text("raise SystemExit(1)\n", encoding="utf-8")
            for executable in executables:
                with self.subTest(executable=executable, policy="inline"):
                    contract = self._verifier_contract(
                        (executable, "-c", "raise SystemExit(0)"),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            set(),
                        ),
                        f"inline interpreter verifier commands are forbidden: {executable}",
                    )
                with self.subTest(executable=executable, policy="stdin"):
                    contract = self._verifier_contract((executable,))
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            set(),
                        ),
                        f"stdin Python verifier commands are forbidden: {executable}",
                    )
                with self.subTest(executable=executable, policy="local-module"):
                    contract = self._verifier_contract(
                        (executable, "-m", "project_verifier"),
                        protected_patterns=("project_verifier.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"project_verifier.py"},
                        ),
                        "repository-local python -m verifier modules are forbidden; "
                        "use a protected direct check file: project_verifier",
                    )

    def test_recognized_python_rejects_missing_module_and_stdin_forms(self) -> None:
        executables = (
            "python3.12",
            "python312",
            "py.exe",
            r"C:\Tools\Python312\python3.12.exe",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for executable in executables:
                for args in (("-m",), ("-m", ""), ("-I", "-m")):
                    with self.subTest(executable=executable, args=args):
                        contract = self._verifier_contract((executable, *args))
                        self.assertEqual(
                            MochiController._contract_workspace_violation(
                                contract,
                                root,
                                set(),
                            ),
                            "python -m verifier command is missing a module name",
                        )
                for args in ((), ("-",), ("-I", "-"), ("-B",)):
                    with self.subTest(executable=executable, args=args):
                        contract = self._verifier_contract((executable, *args))
                        self.assertEqual(
                            MochiController._contract_workspace_violation(
                                contract,
                                root,
                                set(),
                            ),
                            f"stdin Python verifier commands are forbidden: {executable}",
                        )

    def test_pythonw_pyw_pypy_family_uses_same_verifier_policy(self) -> None:
        executables = (
            "pythonw.exe",
            "pyw.exe",
            "pythonw3.12.exe",
            "pypy3.10",
            "pythonw",
            "pyw",
            "pypy",
            "pypy310.exe",
            r"C:\Tools\Python312\pythonw3.12.exe",
            r"C:\Tools\PyPy\pypy3.10.exe",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local_module = root / "project_verifier.py"
            local_module.write_text("raise SystemExit(1)\n", encoding="utf-8")
            external_test = root / "testing" / "test_sample.py"
            external_test.parent.mkdir()
            external_test.write_text("def test_ok(): assert True\n", encoding="utf-8")
            direct_check = root / "checks" / "direct_check.py"
            direct_check.parent.mkdir()
            direct_check.write_text("raise SystemExit(1)\n", encoding="utf-8")

            for executable in executables:
                with self.subTest(executable=executable, policy="inline-c"):
                    contract = self._verifier_contract(
                        (executable, "-c", "raise SystemExit(0)"),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            set(),
                        ),
                        f"inline interpreter verifier commands are forbidden: {executable}",
                    )

                for args in ((), ("-",), ("-I",)):
                    with self.subTest(
                        executable=executable,
                        policy="stdin",
                        args=args,
                    ):
                        contract = self._verifier_contract((executable, *args))
                        self.assertEqual(
                            MochiController._contract_workspace_violation(
                                contract,
                                root,
                                set(),
                            ),
                            f"stdin Python verifier commands are forbidden: {executable}",
                        )

                for args in (("-m",), ("-m", "")):
                    with self.subTest(
                        executable=executable,
                        policy="malformed-m",
                        args=args,
                    ):
                        contract = self._verifier_contract((executable, *args))
                        self.assertEqual(
                            MochiController._contract_workspace_violation(
                                contract,
                                root,
                                set(),
                            ),
                            "python -m verifier command is missing a module name",
                        )

                with self.subTest(executable=executable, policy="inline-m-c"):
                    contract = self._verifier_contract((executable, "-m", "-c"))
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            set(),
                        ),
                        f"inline interpreter verifier commands are forbidden: {executable}",
                    )

                with self.subTest(executable=executable, policy="local-m"):
                    contract = self._verifier_contract(
                        (executable, "-m", "project_verifier"),
                        protected_patterns=("project_verifier.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"project_verifier.py"},
                        ),
                        "repository-local python -m verifier modules are forbidden; "
                        "use a protected direct check file: project_verifier",
                    )

                with self.subTest(executable=executable, policy="external-m"):
                    contract = self._verifier_contract(
                        (
                            executable,
                            "-m",
                            "pytest",
                            "testing/test_sample.py",
                        ),
                        protected_patterns=("testing/test_sample.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "",
                    )

                with self.subTest(executable=executable, policy="direct-script"):
                    contract = self._verifier_contract(
                        (executable, "checks/direct_check.py"),
                        protected_patterns=("checks/direct_check.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"checks/direct_check.py"},
                        ),
                        "",
                    )

    def test_unknown_unresolved_python_module_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            contract = self._python_module_contract(
                "private_project_verifier",
                protected_patterns=("README.md",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    set(),
                ),
                "python -m verifier module cannot be proven external or protected: "
                "private_project_verifier",
            )

    def test_external_pytest_remains_valid_with_protected_repository_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "testing" / "test_external_pytest.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")
            contract = self._python_module_contract(
                "pytest",
                protected_patterns=("testing/test_external_pytest.py",),
                extra_args=("testing/test_external_pytest.py",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"testing/test_external_pytest.py"},
                ),
                "",
            )

    def test_noop_git_verifier_is_refused_without_a_protected_repository_check(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            contract = self._verifier_contract(
                ("git", "status", "--short"),
                protected_patterns=("README.md",),
            )

            violation = MochiController._contract_workspace_violation(
                contract,
                root,
                {"README.md"},
            )

            self.assertIn("does not exercise protected repository input", violation)
            self.assertIn("git status --short", violation)

    def test_git_metadata_commands_are_not_repository_verifiers(self) -> None:
        for argv in (("git", "--version"), ("git", "rev-parse", "--is-inside-work-tree")):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                contract = self._verifier_contract(
                    argv,
                    protected_patterns=("README.md",),
                )

                violation = MochiController._contract_workspace_violation(
                    contract,
                    Path(raw),
                    {"README.md"},
                )

                self.assertIn("does not exercise protected repository input", violation)
                self.assertIn("git", violation)

    def test_each_final_verifier_must_exercise_a_protected_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "checks" / "direct_check.py"
            check.parent.mkdir()
            check.write_text("raise SystemExit(0)\n", encoding="utf-8")
            baseline = (sys.executable, "checks/direct_check.py")
            contract = PacketContract(
                packet_id="module-verifier",
                goal="verify the repository through bounded commands",
                execution_mode=ExecutionMode.VERIFY_ONLY,
                verification_class=VerificationClass.HARD,
                acceptance_criteria=("verifier passes",),
                baseline_argv=baseline,
                final_argvs=(baseline, ("git", "status")),
                expected_failure_codes=(),
                protected_patterns=("checks/direct_check.py",),
                allowed_paths=(),
                evidence_requirements=("protected verifier inputs",),
            )

            violation = MochiController._contract_workspace_violation(
                contract,
                root,
                {"checks/direct_check.py"},
            )

            self.assertIn("does not exercise protected repository input", violation)
            self.assertIn("git status", violation)

    def test_verify_only_noop_packet_is_refused_before_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = NoOpVerifyOnlyProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Reject an unrelated verify-only command",
                project=source,
                run_root=root / "run",
                run_id="verify-only-no-op",
            )

            packet = result.state.packet("pastebin")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertFalse(any(
                row.get("event") == "verification_packet_accepted"
                for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()
            ))
            self.assertTrue(all(
                "does not exercise protected repository input" in row["reason"]
                for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()
                if row.get("event") == "contract_refused"
            ))

    def test_verify_only_identical_baseline_and_final_execute_separately(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = ProtectedVerifyOnlyProvider()
            calls: list[tuple[tuple[str, ...], Path]] = []

            def successful_verifier(argv, *, cwd, timeout_seconds):
                calls.append((tuple(argv), Path(cwd).resolve()))
                return CommandResult(
                    argv=tuple(argv),
                    returncode=0,
                    stdout=f"verified call {len(calls)}",
                    stderr="",
                    duration_seconds=0.01,
                )

            with mock.patch(
                "mochicode_core.runner.run_command",
                new=successful_verifier,
            ):
                result = MochiController(
                    load_config(PLUGIN_ROOT / "config" / "default.toml"),
                    provider,
                ).run_new(
                    goal="Execute baseline and identical final verifier separately",
                    project=source,
                    run_root=root / "run",
                    run_id="verify-only-identical-command",
                )

            self.assertEqual(result.state.status, "complete")
            self.assertEqual(result.state.packet("verify-readme").status, PacketStatus.ACCEPTED)
            self.assertEqual(provider.execute_calls, 0)
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0], calls[1])
            rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
            accepted = next(
                row for row in rows if row.get("event") == "verification_packet_accepted"
            )
            baseline_ref = next(
                receipt
                for receipt in accepted["receipts"]
                if str(receipt["path"]).endswith("/baseline.json")
            )
            final_ref = next(
                receipt
                for receipt in accepted["receipts"]
                if str(receipt["path"]).endswith("/verification-1.json")
            )
            baseline = json.loads(
                (result.run_root / baseline_ref["path"]).read_text(encoding="utf-8")
            )
            final = json.loads(
                (result.run_root / final_ref["path"]).read_text(encoding="utf-8")
            )
            command = ["git", "cat-file", "-e", "HEAD:README.md"]
            self.assertEqual(baseline["contract_argv"], command)
            self.assertEqual(final["contract_argv"], command)
            self.assertEqual(baseline["protected_verifier_inputs"], ["README.md"])
            self.assertEqual(final["protected_verifier_inputs"], ["README.md"])
            self.assertEqual(
                baseline["workspace_fingerprint_before"],
                baseline["workspace_fingerprint_after"],
            )
            self.assertEqual(
                final["workspace_fingerprint_before"],
                final["workspace_fingerprint_after"],
            )
            self.assertEqual(
                final["workspace_fingerprint_before"],
                baseline["workspace_fingerprint_after"],
            )
            self.assertNotEqual(baseline_ref["path"], final_ref["path"])

    def test_verify_only_protected_input_change_during_final_run_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = ProtectedVerifyOnlyProvider()
            calls = 0

            def mutating_final_verifier(argv, *, cwd, timeout_seconds):
                nonlocal calls
                calls += 1
                if calls == 2:
                    (Path(cwd) / "README.md").write_text(
                        "changed between baseline and final verification\n",
                        encoding="utf-8",
                    )
                return CommandResult(
                    argv=tuple(argv),
                    returncode=0,
                    stdout=f"verified call {calls}",
                    stderr="",
                    duration_seconds=0.01,
                )

            config = replace(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                max_attempts_per_packet=1,
            )
            with mock.patch(
                "mochicode_core.runner.run_command",
                new=mutating_final_verifier,
            ):
                result = MochiController(config, provider).run_new(
                    goal="Refuse protected input drift between verify-only runs",
                    project=source,
                    run_root=root / "run",
                    run_id="verify-only-protected-drift",
                )

            packet = result.state.packet("verify-readme")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertEqual(provider.execute_calls, 0)
            self.assertEqual(calls, 2)
            self.assertIn("protected measurement inputs changed: README.md", packet.last_failure or "")
            self.assertFalse(any(
                row.get("event") == "verification_packet_accepted"
                for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()
            ))

    def test_verify_only_final_verifier_fingerprint_refuses_unprotected_mutation(self) -> None:
        baseline = ("git", "cat-file", "-e", "HEAD:README.md")
        final_commands = (
            ("identical", (baseline,), 2, 1),
            ("distinct", (baseline, ("git", "show", "HEAD:README.md")), 3, 2),
        )
        for label, final_argvs, mutation_call, receipt_index in final_commands:
            with self.subTest(command=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = make_repo(root)
                provider = FingerprintVerifyOnlyProvider(final_argvs)
                calls = 0

                def mutating_final_verifier(argv, *, cwd, timeout_seconds):
                    nonlocal calls
                    calls += 1
                    if calls == mutation_call:
                        protected = Path(cwd) / "README.md"
                        original = protected.read_bytes()
                        protected.write_bytes(original + b"transient probe\n")
                        self.assertTrue(protected.read_bytes().endswith(b"transient probe\n"))
                        protected.write_bytes(original)
                        self.assertEqual(protected.read_bytes(), original)
                        (Path(cwd) / "unprotected.txt").write_text(
                            "final verifier mutation\n",
                            encoding="utf-8",
                        )
                    return CommandResult(
                        argv=tuple(argv),
                        returncode=0,
                        stdout=f"verified call {calls}",
                        stderr="",
                        duration_seconds=0.01,
                    )

                config = replace(
                    load_config(PLUGIN_ROOT / "config" / "default.toml"),
                    max_attempts_per_packet=1,
                )
                with mock.patch(
                    "mochicode_core.runner.run_command",
                    new=mutating_final_verifier,
                ):
                    result = MochiController(config, provider).run_new(
                        goal=f"Refuse {label} verify-only verifier workspace mutation",
                        project=source,
                        run_root=root / "run",
                        run_id=f"verify-only-workspace-drift-{label}",
                    )

                packet = result.state.packet("verify-readme")
                self.assertEqual(packet.status, PacketStatus.PARKED)
                self.assertEqual(packet.implementation_attempts, 0)
                self.assertEqual(provider.execute_calls, 0)
                self.assertEqual(provider.review_calls, 0)
                self.assertEqual(calls, mutation_call)
                rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
                self.assertFalse(any(
                    row.get("event") == "verification_packet_accepted"
                    for row in rows
                ))
                finished = next(row for row in rows if row.get("event") == "attempt_finished")
                final_ref = next(
                    receipt
                    for receipt in finished["receipts"]
                    if str(receipt["path"]).endswith(
                        f"/verification-{receipt_index}.json"
                    )
                )
                baseline_ref = next(
                    receipt
                    for receipt in finished["receipts"]
                    if str(receipt["path"]).endswith("/baseline.json")
                )
                final = json.loads(
                    (result.run_root / final_ref["path"]).read_text(encoding="utf-8")
                )
                baseline_receipt = json.loads(
                    (result.run_root / baseline_ref["path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(final["contract_argv"], list(final_argvs[receipt_index - 1]))
                self.assertEqual(final["protected_verifier_inputs"], ["README.md"])
                self.assertEqual(
                    final["workspace_fingerprint_before"],
                    baseline_receipt["workspace_fingerprint_after"],
                )
                self.assertNotEqual(
                    final["workspace_fingerprint_before"],
                    final["workspace_fingerprint_after"],
                )

    def test_verify_only_final_integration_replays_exact_workspace_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            command = ("git", "cat-file", "-e", "HEAD:README.md")
            provider = FingerprintVerifyOnlyProvider((command,))
            calls = 0

            def mutating_final_integration(argv, *, cwd, timeout_seconds):
                nonlocal calls
                calls += 1
                if calls == 3:
                    protected = Path(cwd) / "README.md"
                    original = protected.read_bytes()
                    protected.write_bytes(original + b"transient integration probe\n")
                    self.assertEqual(
                        protected.read_bytes(),
                        original + b"transient integration probe\n",
                    )
                    protected.write_bytes(original)
                    self.assertEqual(protected.read_bytes(), original)
                    (Path(cwd) / "unprotected.txt").write_text(
                        "final integration mutation\n",
                        encoding="utf-8",
                    )
                return CommandResult(
                    argv=tuple(argv),
                    returncode=0,
                    stdout=f"verified call {calls}",
                    stderr="",
                    duration_seconds=0.01,
                )

            with mock.patch(
                "mochicode_core.runner.run_command",
                new=mutating_final_integration,
            ):
                result = MochiController(
                    load_config(PLUGIN_ROOT / "config" / "default.toml"),
                    provider,
                ).run_new(
                    goal="Refuse final integration workspace drift",
                    project=source,
                    run_root=root / "run",
                    run_id="verify-only-integration-fingerprint",
                )

            self.assertEqual(result.state.status, "verification_failed")
            self.assertIsNone(result.final_review)
            self.assertEqual(provider.review_calls, 1)
            self.assertEqual(provider.execute_calls, 0)
            self.assertEqual(calls, 3)
            rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
            failure = next(
                row
                for row in rows
                if row.get("event") == "final_integration_verification_failed"
            )
            self.assertIn("workspace fingerprint after command", failure["reason"])

    def test_identical_final_command_requires_its_own_verified_receipt(self) -> None:
        command = ("git", "cat-file", "-e", "HEAD:README.md")
        contract = replace(
            self._verifier_contract(
                command,
                protected_patterns=("README.md",),
                allowed_paths=(),
            ),
            execution_mode=ExecutionMode.VERIFY_ONLY,
            expected_failure_codes=(),
        )
        baseline_ref = {
            "path": "attempts/verify-readme/a1/baseline.json",
            "bytes": 1,
            "sha256": "baseline",
        }
        final_ref = {
            "path": "attempts/verify-readme/a1/verification-1.json",
            "bytes": 1,
            "sha256": "final",
        }

        for name, receipts, verified in (
            ("missing", [], {}),
            (
                "baseline-substitution",
                [baseline_ref],
                {baseline_ref["path"]: baseline_ref},
            ),
        ):
            with self.subTest(case=name), self.assertRaisesRegex(
                ValueError,
                "no verifier receipt for command 1",
            ):
                MochiController._accepted_command_receipt(
                    {
                        "event": "verification_packet_accepted",
                        "receipts": receipts,
                    },
                    verified,
                    contract,
                    1,
                )

        selected = MochiController._accepted_command_receipt(
            {
                "event": "verification_packet_accepted",
                "receipts": [baseline_ref, final_ref],
            },
            {
                baseline_ref["path"]: baseline_ref,
                final_ref["path"]: final_ref,
            },
            contract,
            1,
        )
        self.assertIs(selected, final_ref)

    def test_pytest_root_conftest_allowed_write_bypass_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            conftest = root / "conftest.py"
            conftest.write_text("def pytest_runtest_setup(): pass\n", encoding="utf-8")
            check = root / "testing" / "test_sample.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")

            unprotected = self._python_module_contract(
                "pytest",
                protected_patterns=("testing/test_sample.py",),
                allowed_paths=("artifact.txt", "conftest.py"),
                extra_args=("testing/test_sample.py",),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    unprotected,
                    root,
                    {"testing/test_sample.py"},
                ),
                "Pytest harness paths cannot be writable: conftest.py",
            )

            protected_but_writable = self._python_module_contract(
                "pytest",
                protected_patterns=("conftest.py", "testing/test_sample.py"),
                allowed_paths=("artifact.txt", "conftest.py"),
                extra_args=("testing/test_sample.py",),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    protected_but_writable,
                    root,
                    {"conftest.py", "testing/test_sample.py"},
                ),
                "Pytest harness paths cannot be writable: conftest.py",
            )

    def test_each_pytest_root_config_is_required_and_nonwritable(self) -> None:
        for config_name in (
            "pytest.ini",
            ".pytest.ini",
            "pyproject.toml",
            "tox.ini",
            "setup.cfg",
        ):
            with self.subTest(config_name=config_name):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    (root / config_name).write_text("[pytest]\n", encoding="utf-8")
                    check = root / "testing" / "test_sample.py"
                    check.parent.mkdir()
                    check.write_text("def test_ok(): assert True\n", encoding="utf-8")

                    missing = self._python_module_contract(
                        "pytest",
                        protected_patterns=("testing/test_sample.py",),
                        extra_args=("testing/test_sample.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            missing,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "repository verifier inputs are not protected: " + config_name,
                    )

                    writable = self._python_module_contract(
                        "pytest",
                        protected_patterns=(config_name, "testing/test_sample.py"),
                        allowed_paths=("artifact.txt", config_name),
                        extra_args=("testing/test_sample.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            writable,
                            root,
                            {config_name, "testing/test_sample.py"},
                        ),
                        "Pytest harness paths cannot be writable: " + config_name,
                    )

    def test_each_pytest_config_filename_is_required_recursively(self) -> None:
        for config_name in (
            "pytest.ini",
            ".pytest.ini",
            "pyproject.toml",
            "tox.ini",
            "setup.cfg",
        ):
            with self.subTest(config_name=config_name):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    config = root / "src" / "pkg" / config_name
                    config.parent.mkdir(parents=True)
                    config.write_text("[pytest]\n", encoding="utf-8")
                    check = root / "testing" / "test_sample.py"
                    check.parent.mkdir()
                    check.write_text("def test_ok(): assert True\n", encoding="utf-8")
                    contract = self._python_module_contract(
                        "pytest",
                        protected_patterns=("testing/test_sample.py",),
                        extra_args=("testing/test_sample.py",),
                    )

                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "repository verifier inputs are not protected: "
                        f"src/pkg/{config_name}",
                    )

    def test_pytest_selected_test_requires_nested_and_ancestor_conftests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = (
                "src/conftest.py",
                "src/pkg/conftest.py",
                "src/pkg/tests/conftest.py",
                "src/pkg/tests/other/conftest.py",
                "src/pkg/tests/unit/test_sample.py",
            )
            for relative in inputs:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def test_ok(): assert True\n", encoding="utf-8")
            contract = self._python_module_contract(
                "pytest",
                protected_patterns=("src/pkg/tests/unit/test_sample.py",),
                extra_args=("src/pkg/tests/unit/test_sample.py",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"src/pkg/tests/unit/test_sample.py"},
                ),
                "repository verifier inputs are not protected: "
                "src/conftest.py, src/pkg/conftest.py, "
                "src/pkg/tests/conftest.py, src/pkg/tests/other/conftest.py",
            )

    def test_plain_pytest_requires_all_repository_conftests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for relative in (
                "src/conftest.py",
                "src/pkg/conftest.py",
                "tests/test_sample.py",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def test_ok(): assert True\n", encoding="utf-8")
            contract = self._python_module_contract(
                "pytest",
                protected_patterns=("tests/test_sample.py",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"tests/test_sample.py"},
                ),
                "repository verifier inputs are not protected: "
                "src/conftest.py, src/pkg/conftest.py",
            )

    def test_pytest_explicit_config_forms_require_existing_protected_nonwritable_file(self) -> None:
        forms = (
            ("-c", "configs/custom.ini"),
            ("-c=configs/custom.ini",),
            ("-cconfigs/custom.ini",),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "testing" / "test_sample.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")

            missing = self._python_module_contract(
                "pytest",
                protected_patterns=("testing/test_sample.py",),
                extra_args=(
                    "-c",
                    "configs/missing.ini",
                    "testing/test_sample.py",
                ),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    missing,
                    root,
                    {"testing/test_sample.py"},
                ),
                "pytest explicit config is missing or outside repository: "
                "configs/missing.ini",
            )

            config = root / "configs" / "custom.ini"
            config.parent.mkdir()
            config.write_text("[pytest]\n", encoding="utf-8")
            unprotected = self._python_module_contract(
                "pytest",
                protected_patterns=("testing/test_sample.py",),
                extra_args=(
                    "-c",
                    "configs/custom.ini",
                    "testing/test_sample.py",
                ),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    unprotected,
                    root,
                    {"testing/test_sample.py"},
                ),
                "repository verifier inputs are not protected: configs/custom.ini",
            )

            writable = self._python_module_contract(
                "pytest",
                protected_patterns=(
                    "configs/custom.ini",
                    "testing/test_sample.py",
                ),
                allowed_paths=("artifact.txt", "configs/custom.ini"),
                extra_args=(
                    "-c",
                    "configs/custom.ini",
                    "testing/test_sample.py",
                ),
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    writable,
                    root,
                    {"configs/custom.ini", "testing/test_sample.py"},
                ),
                "protected measurement inputs overlap Luna write paths: "
                "configs/custom.ini",
            )

            for form in forms:
                with self.subTest(form=form):
                    valid = self._python_module_contract(
                        "pytest",
                        protected_patterns=(
                            "configs/custom.ini",
                            "testing/test_sample.py",
                        ),
                        extra_args=(*form, "testing/test_sample.py"),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            valid,
                            root,
                            {"configs/custom.ini", "testing/test_sample.py"},
                        ),
                        "",
                    )

    def test_fully_protected_pytest_root_inputs_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = (
                "conftest.py",
                "pytest.ini",
                ".pytest.ini",
                "pyproject.toml",
                "tox.ini",
                "setup.cfg",
                "testing/conftest.py",
                "testing/unit/conftest.py",
                "testing/unit/test_sample.py",
            )
            for relative in inputs:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# protected pytest input\n", encoding="utf-8")
            contract = self._python_module_contract(
                "pytest",
                protected_patterns=inputs,
                extra_args=("testing/unit/test_sample.py",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    set(inputs),
                ),
                "",
            )

    def test_external_pytest_no_config_project_remains_valid(self) -> None:
        commands = (
            (sys.executable, "-m", "pytest", "testing/test_sample.py"),
            (sys.executable, "-m", "py.test", "testing/test_sample.py"),
            ("pytest", "testing/test_sample.py"),
            ("py.test", "testing/test_sample.py"),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "testing" / "test_sample.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")
            for argv in commands:
                with self.subTest(argv=argv):
                    contract = self._verifier_contract(
                        argv,
                        protected_patterns=("testing/test_sample.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "",
                    )

    def test_explicit_local_pytest_plugin_is_refused_but_no_and_external_remain_valid(self) -> None:
        cases = (
            (
                "module",
                "plugins/custom_plugin.py",
                ("-p", "plugins.custom_plugin"),
            ),
            (
                "package",
                "plugins/custom_plugin/__init__.py",
                ("-p=plugins.custom_plugin",),
            ),
            (
                "attached",
                "plugins/custom_plugin.py",
                ("-pplugins.custom_plugin",),
            ),
        )
        for name, plugin_path, form in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    plugin = root / plugin_path
                    plugin.parent.mkdir(parents=True, exist_ok=True)
                    plugin.write_text("VALUE = 1\n", encoding="utf-8")
                    check = root / "testing" / "test_sample.py"
                    check.parent.mkdir()
                    check.write_text("def test_ok(): assert True\n", encoding="utf-8")
                    contract = self._python_module_contract(
                        "pytest",
                        protected_patterns=("testing/test_sample.py",),
                        allowed_paths=(plugin_path,),
                        extra_args=(*form, "testing/test_sample.py"),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "repository-local pytest plugin is forbidden: "
                        "plugins.custom_plugin",
                    )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = root / "plugins" / "custom_plugin.py"
            plugin.parent.mkdir()
            plugin.write_text("VALUE = 1\n", encoding="utf-8")
            check = root / "testing" / "test_sample.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")
            for plugin_args in (
                ("-p", "no:plugins.custom_plugin"),
                ("-p", "pytest_cov"),
            ):
                with self.subTest(plugin_args=plugin_args):
                    contract = self._python_module_contract(
                        "pytest",
                        protected_patterns=("testing/test_sample.py",),
                        allowed_paths=("plugins/custom_plugin.py",),
                        extra_args=(*plugin_args, "testing/test_sample.py"),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "",
                    )

    def test_pytest_nonexecution_and_diagnostic_flags_are_refused(self) -> None:
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
        flags = (
            *long_flags,
            *(flag + "=attached" for flag in long_flags),
            "-h",
            "-hattached",
            "-V",
            "-Vattached",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "testing" / "test_sample.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")
            for flag in flags:
                with self.subTest(flag=flag):
                    contract = self._python_module_contract(
                        "pytest",
                        protected_patterns=("testing/test_sample.py",),
                        extra_args=(flag, "testing/test_sample.py"),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "pytest verifier does not execute tests: " + flag,
                    )

    def test_pytest_false_green_diagnostics_refuse_before_baseline_or_luna(self) -> None:
        for flag in ("--collect-only", "--setup-plan"):
            with self.subTest(flag=flag):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    provider = PytestExecutionProvider(diagnostic_args=(flag,))
                    config = replace(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        max_attempts_per_packet=1,
                    )
                    result = MochiController(config, provider).run_new(
                        goal=f"Reject false-green pytest flag {flag}",
                        project=source,
                        run_root=root / "run",
                        run_id="pytest-diagnostic-" + flag.lstrip("-").replace("-", "_"),
                    )
                    rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()

                    self.assertEqual(result.state.packet("pastebin").status, PacketStatus.PARKED)
                    self.assertEqual(provider.execute_calls, 0)
                    self.assertFalse(any(row.get("event") == "baseline" for row in rows))

    def test_pytest_harness_write_patterns_are_forbidden_even_when_absent(self) -> None:
        patterns = (
            "generated/conftest.py",
            "generated/pytest.ini",
            "generated/.pytest.ini",
            "generated/pyproject.toml",
            "generated/tox.ini",
            "generated/setup.cfg",
            "**/conftest.py",
            "**/*.ini",
            "**/*.toml",
            "**/*.cfg",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "testing" / "test_sample.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")
            for pattern in patterns:
                with self.subTest(pattern=pattern):
                    contract = self._python_module_contract(
                        "pytest",
                        protected_patterns=("testing/test_sample.py",),
                        allowed_paths=("artifact.txt", pattern),
                        extra_args=("testing/test_sample.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"testing/test_sample.py"},
                        ),
                        "Pytest harness paths cannot be writable: " + pattern,
                    )

    def test_pytest_harness_actual_and_staged_late_mutations_are_refused(self) -> None:
        cases = (
            ("actual", "generated/conftest.py"),
            ("staged", "generated/pytest.ini"),
        )
        for boundary, relative in cases:
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = make_repo(root)
                    provider = PytestExecutionProvider(
                        extra_allowed=(relative,),
                        write_harness=relative if boundary == "actual" else None,
                    )
                    config = replace(
                        load_config(PLUGIN_ROOT / "config" / "default.toml"),
                        max_attempts_per_packet=1,
                    )
                    original_stage = GitWorkspaceManager.stage_all

                    def fake_pytest(argv, *, cwd, timeout_seconds):
                        implemented = (Path(cwd) / "pastebin.txt").is_file()
                        return CommandResult(
                            argv=tuple(argv),
                            returncode=0 if implemented else 1,
                            stdout="simulated protected pytest execution",
                            stderr="",
                            duration_seconds=0.01,
                        )

                    def stage_with_late_harness(manager, packet):
                        if boundary == "staged" and packet.path.name == "implementation":
                            target = packet.path / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text("# staged late harness\n", encoding="utf-8")
                        return original_stage(manager, packet)

                    with (
                        mock.patch.object(
                            MochiController,
                            "_allowed_pattern_targets_pytest_harness",
                            return_value=False,
                        ),
                        mock.patch.object(
                            GitWorkspaceManager,
                            "stage_all",
                            new=stage_with_late_harness,
                        ),
                        mock.patch(
                            "mochicode_core.runner.run_command",
                            new=fake_pytest,
                        ),
                    ):
                        result = MochiController(config, provider).run_new(
                            goal=f"Refuse {boundary} pytest harness mutation",
                            project=source,
                            run_root=root / "run",
                            run_id=f"pytest-harness-{boundary}",
                        )
                    rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()

                    packet = result.state.packet("pastebin")
                    self.assertEqual(packet.status, PacketStatus.PARKED)
                    self.assertEqual(provider.execute_calls, 1)
                    self.assertIn(relative, packet.last_failure or "")
                    self.assertFalse(
                        any(row.get("event") == "packet_integrated" for row in rows)
                    )

    def test_pytest_without_harness_write_paths_executes_normally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "testing" / "test_sample.py"
            check.parent.mkdir()
            check.write_text("def test_ok(): assert True\n", encoding="utf-8")
            contract = self._python_module_contract(
                "pytest",
                protected_patterns=("testing/test_sample.py",),
                extra_args=("testing/test_sample.py",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"testing/test_sample.py"},
                ),
                "",
            )

    def test_existing_python_startup_hooks_are_required_and_nonwritable(self) -> None:
        for relative in ("sitecustomize.py", "usercustomize.py", "startup.pth"):
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    hook = root / relative
                    hook.write_text("# startup hook\n", encoding="utf-8")
                    check = root / "checks" / "direct_check.py"
                    check.parent.mkdir()
                    check.write_text("raise SystemExit(1)\n", encoding="utf-8")

                    unprotected = self._verifier_contract(
                        (sys.executable, "checks/direct_check.py"),
                        protected_patterns=("checks/direct_check.py",),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            unprotected,
                            root,
                            {"checks/direct_check.py"},
                        ),
                        "repository verifier inputs are not protected: " + relative,
                    )

                    writable = self._verifier_contract(
                        (sys.executable, "checks/direct_check.py"),
                        protected_patterns=(relative, "checks/direct_check.py"),
                        allowed_paths=("artifact.txt", relative),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            writable,
                            root,
                            {relative, "checks/direct_check.py"},
                        ),
                        "Python startup hook paths cannot be writable: " + relative,
                    )

    def test_nested_python_startup_hooks_and_pth_are_all_protected_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = (
                "src/pkg/sitecustomize.py",
                "src/pkg/usercustomize.py",
                "src/pkg/hooks/custom_startup.pth",
                "checks/direct_check.py",
            )
            for relative in inputs:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# protected input\n", encoding="utf-8")
            contract = self._verifier_contract(
                (sys.executable, "checks/direct_check.py"),
                protected_patterns=inputs,
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"checks/direct_check.py"},
                ),
                "repository verifier inputs are not protected: "
                "src/pkg/hooks/custom_startup.pth, src/pkg/sitecustomize.py, "
                "src/pkg/usercustomize.py",
            )
            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    set(inputs),
                ),
                "",
            )

    def test_absent_python_startup_hook_cannot_be_authorized_for_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "checks" / "direct_check.py"
            check.parent.mkdir()
            check.write_text("raise SystemExit(1)\n", encoding="utf-8")
            for relative in (
                "generated/sitecustomize.py",
                "generated/usercustomize.py",
                "generated/startup.pth",
            ):
                with self.subTest(relative=relative):
                    contract = self._verifier_contract(
                        (sys.executable, "checks/direct_check.py"),
                        protected_patterns=("checks/direct_check.py",),
                        allowed_paths=("artifact.txt", relative),
                    )
                    self.assertEqual(
                        MochiController._contract_workspace_violation(
                            contract,
                            root,
                            {"checks/direct_check.py"},
                        ),
                        "Python startup hook paths cannot be writable: " + relative,
                    )

    def test_python_startup_hook_changed_paths_are_reserved(self) -> None:
        for relative in (
            "sitecustomize.py",
            "nested/usercustomize.py",
            "nested/hooks/custom.pth",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(MochiController._python_startup_hook_path(relative))
        self.assertFalse(MochiController._python_startup_hook_path("src/application.py"))

    def test_python_verifier_without_startup_hooks_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "checks" / "direct_check.py"
            check.parent.mkdir()
            check.write_text("raise SystemExit(1)\n", encoding="utf-8")
            contract = self._verifier_contract(
                (sys.executable, "checks/direct_check.py"),
                protected_patterns=("checks/direct_check.py",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(
                    contract,
                    root,
                    {"checks/direct_check.py"},
                ),
                "",
            )

    def _run_terra_mutation_at_boundary(
        self,
        root: Path,
        *,
        boundary: str,
        target: str,
        run_id: str,
    ):
        source = make_repo(root)
        original_existing = "raise SystemExit(1)\n"
        if target == "existing-test":
            existing = source / "tests" / "test_existing.py"
            existing.parent.mkdir()
            existing.write_text(original_existing, encoding="utf-8")
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

        provider = TerraTestingRootProvider()

        def mutate(workspace: Path) -> None:
            if target == "production":
                path = workspace / "production.py"
                path.write_text("written_after_terra_boundary = True\n", encoding="utf-8")
                return
            path = workspace / "tests" / "test_existing.py"
            path.write_text("raise SystemExit(0)  # changed after Terra boundary\n", encoding="utf-8")

        if boundary == "initial-sample":
            original = GitWorkspaceManager.changed_path_statuses_since

            def after_initial_sample(manager, packet, base_ref):
                result = original(manager, packet, base_ref)
                mutate(packet.path)
                return result

            patcher = mock.patch.object(
                GitWorkspaceManager,
                "changed_path_statuses_since",
                new=after_initial_sample,
            )
        elif boundary == "staging":
            original = GitWorkspaceManager.stage_all

            def after_staging(manager, packet):
                result = original(manager, packet)
                mutate(packet.path)
                return result

            patcher = mock.patch.object(
                GitWorkspaceManager,
                "stage_all",
                new=after_staging,
            )
        elif boundary == "commit":
            original = GitWorkspaceManager.commit_staged

            def after_commit(manager, packet, message):
                result = original(manager, packet, message)
                mutate(packet.path)
                return result

            patcher = mock.patch.object(
                GitWorkspaceManager,
                "commit_staged",
                new=after_commit,
            )
        else:
            raise AssertionError(f"unknown Terra mutation boundary: {boundary}")

        with patcher:
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal=f"Refuse Terra {target} mutation at {boundary}",
                project=source,
                run_root=root / "run",
                run_id=run_id,
            )
        return source, provider, result

    def test_exact_staged_contract_receipt_matches_committed_contract_diff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            provider = TerraTestingRootProvider()
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                provider,
            ).run_new(
                goal="Record the exact Terra contract tree",
                project=source,
                run_root=root / "run",
                run_id="exact-contract-diff",
            )

            rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
            integrated = next(row for row in rows if row.get("event") == "packet_integrated")
            staged_ref = next(
                receipt
                for receipt in integrated["receipts"]
                if str(receipt["path"]).endswith("/contract-staged.json")
            )
            commit_ref = next(
                receipt
                for receipt in integrated["receipts"]
                if str(receipt["path"]).endswith("/contract-commit.json")
            )
            staged = json.loads((result.run_root / staged_ref["path"]).read_text(encoding="utf-8"))
            committed = json.loads((result.run_root / commit_ref["path"]).read_text(encoding="utf-8"))

            self.assertEqual(
                staged["staged_path_statuses"],
                [{"status": "A", "path": "testing/check_pastebin_text_lexer.py"}],
            )
            self.assertEqual(
                staged["staged_diff_sha256"],
                hashlib.sha256(staged["staged_diff"].encode("utf-8")).hexdigest(),
            )
            self.assertTrue(committed["diff_matches"])
            self.assertTrue(committed["path_statuses_match"])
            self.assertEqual(
                committed["staged_diff_sha256"],
                committed["committed_diff_sha256"],
            )
            self.assertEqual(integrated["contract_head"], committed["contract_head"])

    def test_verifier_receipts_bind_contract_commands_to_protected_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                TerraTestingRootProvider(),
            ).run_new(
                goal="Bind verifier evidence to the contract",
                project=source,
                run_root=root / "run",
                run_id="verifier-receipt-binding",
            )

            rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
            integrated = next(row for row in rows if row.get("event") == "packet_integrated")
            verification_ref = next(
                receipt
                for receipt in integrated["receipts"]
                if str(receipt["path"]).endswith("/verification-1.json")
            )
            verification = json.loads(
                (result.run_root / verification_ref["path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(
                verification["contract_argv"],
                [sys.executable, "testing/check_pastebin_text_lexer.py"],
            )
            self.assertEqual(
                verification["protected_verifier_inputs"],
                ["testing/check_pastebin_text_lexer.py"],
            )

    def test_production_and_existing_test_mutations_are_refused_after_each_terra_boundary(self) -> None:
        for boundary in ("initial-sample", "staging", "commit"):
            for target in ("production", "existing-test"):
                with self.subTest(boundary=boundary, target=target):
                    with tempfile.TemporaryDirectory() as raw:
                        source, provider, result = self._run_terra_mutation_at_boundary(
                            Path(raw),
                            boundary=boundary,
                            target=target,
                            run_id=f"race-{boundary}-{target}",
                        )

                        packet = result.state.packet("pastebin")
                        self.assertEqual(packet.status, PacketStatus.PARKED)
                        self.assertEqual(packet.implementation_attempts, 0)
                        self.assertEqual(provider.execute_calls, 0)
                        self.assertFalse((result.integration.path / "pastebin.txt").exists())
                        self.assertFalse((source / "production.py").exists())
                        if target == "existing-test":
                            self.assertEqual(
                                (source / "tests" / "test_existing.py").read_text(
                                    encoding="utf-8"
                                ),
                                "raise SystemExit(1)\n",
                            )
                        rows = EvidenceLedger(result.run_root / "evidence.jsonl").records()
                        self.assertFalse(any(row.get("event") == "baseline" for row in rows))
                        self.assertFalse(
                            any(row.get("event") == "implementation_attempt_reserved" for row in rows)
                        )

    def _run_pattern_contract(
        self,
        root: Path,
        protected_patterns: tuple[str, ...],
        *,
        run_id: str,
    ):
        source = make_repo(root)
        provider = TerraTestingRootProvider(protected_patterns=protected_patterns)
        result = MochiController(
            load_config(PLUGIN_ROOT / "config" / "default.toml"),
            provider,
        ).run_new(
            goal="Validate every protected pattern independently",
            project=source,
            run_root=root / "run",
            run_id=run_id,
        )
        return provider, result

    def test_terra_prompt_requires_filesystem_only_existing_protected_patterns(self) -> None:
        prompt = " ".join(
            (PLUGIN_ROOT / "prompts" / "terra-contract.md")
            .read_text(encoding="utf-8")
            .lower()
            .split()
        )

        for phrase in (
            "filesystem paths or repository-relative filesystem globs only",
            "never use test node ids",
            "content selectors or prose",
            "every pattern must match an existing file after terra's additive check stage",
            "terra may add new focused check/spec files only",
            "terra must never modify or delete any existing file",
            "an existing test modification belongs neither to terra nor to protected check authoring",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_top_level_testing_check_is_accepted_as_an_additive_terra_root(self) -> None:
        self.assertTrue(
            MochiController._terra_contract_path_allowed(
                "testing/check_pastebin_text_lexer.py"
            )
        )

    def test_pytest_inputs_under_testing_are_treated_as_repository_verifiers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            check = root / "testing" / "check_pastebin_text_lexer.py"
            check.parent.mkdir()
            check.write_text("raise SystemExit(1)\n", encoding="utf-8")

            value = PacketContract(
                packet_id="pastebin",
                goal="create pastebin.txt",
                execution_mode=ExecutionMode.IMPLEMENT,
                verification_class=VerificationClass.HARD,
                acceptance_criteria=("pastebin.txt contains the paste",),
                baseline_argv=("pytest", "testing/check_pastebin_text_lexer.py"),
                final_argvs=(("pytest", "testing/check_pastebin_text_lexer.py"),),
                expected_failure_codes=(1,),
                protected_patterns=("testing/check_pastebin_text_lexer.py",),
                allowed_paths=("pastebin.txt",),
                evidence_requirements=("focused check",),
            )

            self.assertEqual(
                MochiController._contract_workspace_violation(value, root, set()),
                "repository verifier inputs are not protected: testing/check_pastebin_text_lexer.py",
            )

    def test_valid_testing_glob_reaches_luna_and_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provider, result = self._run_pattern_contract(
                Path(raw),
                ("testing/check_*.py",),
                run_id="terra-valid-testing-glob",
            )

            packet = result.state.packet("pastebin")
            self.assertEqual(result.state.status, "complete")
            self.assertEqual(packet.status, PacketStatus.ACCEPTED)
            self.assertEqual(packet.attempts, 1)
            self.assertEqual(packet.implementation_attempts, 1)
            self.assertEqual(provider.execute_calls, 1)
            self.assertEqual(
                (result.integration.path / "pastebin.txt").read_text(encoding="utf-8"),
                "paste\n",
            )

    def test_valid_plus_unmatched_pattern_is_refused_before_luna(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provider, result = self._run_pattern_contract(
                Path(raw),
                ("testing/check_*.py", "testing/does-not-exist-*.py"),
                run_id="terra-mixed-unmatched",
            )

            packet = result.state.packet("pastebin")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.attempts, 2)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertEqual(provider.execute_calls, 0)
            refusals = [
                row
                for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()
                if row.get("event") == "contract_refused"
            ]
            self.assertEqual(len(refusals), 2)
            self.assertTrue(
                all(
                    "protected pattern matches no existing file: "
                    "testing/does-not-exist-*.py" in row["reason"]
                    for row in refusals
                )
            )

    def test_valid_plus_node_id_pattern_is_explicitly_refused_before_luna(self) -> None:
        node_id = (
            "testing/check_pastebin_text_lexer.py"
            "::TestPaste::test_create_new_paste"
        )
        with tempfile.TemporaryDirectory() as raw:
            provider, result = self._run_pattern_contract(
                Path(raw),
                ("testing/check_*.py", node_id),
                run_id="terra-mixed-node-id",
            )

            packet = result.state.packet("pastebin")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.attempts, 2)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertEqual(provider.execute_calls, 0)
            refusals = [
                row
                for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()
                if row.get("event") == "contract_refused"
            ]
            self.assertEqual(len(refusals), 2)
            self.assertTrue(
                all(
                    f"protected pattern is a test node selector: {node_id}"
                    in row["reason"]
                    for row in refusals
                )
            )

    def test_existing_testing_file_modification_is_refused_as_non_additive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            existing = source / "testing" / "test_pastebin.py"
            existing.parent.mkdir()
            existing.write_text("raise SystemExit(1)\n", encoding="utf-8")
            git(source, "add", "testing/test_pastebin.py")
            git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "add existing testing file",
            )

            result = MochiController(
                load_config(PLUGIN_ROOT / "config" / "default.toml"),
                TerraTestingRootProvider(modify_existing=True),
            ).run_new(
                goal="Preserve existing testing files",
                project=source,
                run_root=root / "run",
                run_id="terra-testing-existing",
            )

            packet = result.state.packet("pastebin")
            self.assertEqual(packet.status, PacketStatus.PARKED)
            self.assertEqual(packet.attempts, 2)
            self.assertEqual(packet.implementation_attempts, 0)
            self.assertEqual(
                existing.read_text(encoding="utf-8"),
                "raise SystemExit(1)\n",
            )
            self.assertEqual(
                (result.integration.path / "testing" / "test_pastebin.py").read_text(
                    encoding="utf-8"
                ),
                "raise SystemExit(1)\n",
            )

            refusals = [
                row
                for row in EvidenceLedger(result.run_root / "evidence.jsonl").records()
                if row.get("event") == "contract_refused"
            ]
            self.assertEqual(len(refusals), 2)
            self.assertEqual(
                {
                    row["reason"] for row in refusals
                },
                {"Terra contract modified or deleted existing checks: testing/test_pastebin.py"},
            )


if __name__ == "__main__":
    unittest.main()
