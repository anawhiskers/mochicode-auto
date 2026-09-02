from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = PLUGIN_ROOT / "package.ps1"
TEST_NATURAL_PROMPT_SHA256 = "a" * 64
TEST_WORKSPACE_TREE_SHA256 = "b" * 64
TEST_FRESH_TASK_ID = "fresh-task-20260826-0001"


def _source_manifest_hash(root: Path = PLUGIN_ROOT) -> str:
    entries: list[bytes] = []
    transient_directories = {".git", ".pytest_cache", "__pycache__"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(part in transient_directories for part in Path(relative).parts) or path.suffix == ".pyc":
            continue
        if path.is_dir():
            entries.append(f"D\0{relative}\n".encode("utf-8"))
        elif path.is_file():
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            entries.append(f"F\0{relative}\0{len(payload)}\0{digest}\n".encode("utf-8"))
    entries.sort()
    return hashlib.sha256(b"".join(entries)).hexdigest()


def _merge_dict(target: dict[str, object], overrides: dict[str, object]) -> None:
    for key, value in overrides.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_dict(current, value)
        else:
            target[key] = value


def _plugin_version() -> str:
    plugin = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return str(plugin["version"])


def _write_canary_receipt(
    user_root: Path,
    path: Path,
    *,
    evidence_path: Path | None = None,
    receipt_overrides: dict[str, object] | None = None,
    evidence_overrides: dict[str, object] | None = None,
) -> Path:
    version = _plugin_version()
    source_manifest_hash = _source_manifest_hash()
    evidence_file = evidence_path or user_root / ".codex" / "mochicode-auto-canary-evidence.json"
    evidence: dict[str, object] = {
        "schema_version": 1,
        "success_marker": "mochicode-auto.fresh-natural-prompt-activation.success",
        "plugin_name": "mochicode-auto",
        "plugin_version": version,
        "source_manifest_hash": source_manifest_hash,
        "fresh_task_id": TEST_FRESH_TASK_ID,
        "natural_prompt_sha256": TEST_NATURAL_PROMPT_SHA256,
        "workspace_tree_before_sha256": TEST_WORKSPACE_TREE_SHA256,
        "workspace_tree_after_sha256": TEST_WORKSPACE_TREE_SHA256,
    }
    if evidence_overrides:
        _merge_dict(evidence, evidence_overrides)
    evidence_bytes = (
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    evidence_file.write_bytes(evidence_bytes)
    evidence_hash = hashlib.sha256(evidence_bytes).hexdigest()
    receipt: dict[str, object] = {
        "schema_version": 2,
        "plugin_name": "mochicode-auto",
        "source_plugin_version": version,
        "cache_plugin_version": version,
        "source_manifest_hash": source_manifest_hash,
        "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "command_process": {
            "fresh_task_id": TEST_FRESH_TASK_ID,
            "natural_prompt_sha256": TEST_NATURAL_PROMPT_SHA256,
            "model_output_bytes": len(evidence_bytes),
            "model_output_sha256": evidence_hash,
            "exit_code": 0,
            "timed_out": False,
        },
        "workspace_tree": {
            "before_sha256": TEST_WORKSPACE_TREE_SHA256,
            "after_sha256": TEST_WORKSPACE_TREE_SHA256,
        },
        "evidence": {
            "path": str(evidence_file),
            "bytes": len(evidence_bytes),
            "sha256": evidence_hash,
        },
    }
    if receipt_overrides:
        _merge_dict(receipt, receipt_overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return evidence_file


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
        and path.suffix != ".pyc"
    }


def _run_install(powershell: str, fake_home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(PLUGIN_ROOT / "install.ps1"),
            "-Source",
            str(PLUGIN_ROOT),
            "-UserHome",
            str(fake_home),
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


class InstallerTests(unittest.TestCase):
    def test_normal_install_and_obsolete_skip_leave_competing_policy_unchanged(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        for kind, arguments in (("default", ()), ("obsolete-skip", ("-SkipRoutingCleanup",))):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                fake_home = Path(raw) / "profile"
                coder = fake_home / ".agents" / "skills" / "coder"
                (coder / "agents").mkdir(parents=True)
                (coder / "SKILL.md").write_text(
                    "---\nname: coder\ndescription: test\n---\n",
                    encoding="utf-8",
                )
                policy = coder / "agents" / "openai.yaml"
                original_policy = b"policy:\r\n  allow_implicit_invocation: true\r\n"
                policy.write_bytes(original_policy)

                install = _run_install(
                    powershell,
                    fake_home,
                    "-SkipPluginCommand",
                    "-ConfirmInstall",
                    *arguments,
                )

                self.assertEqual(install.returncode, 0, install.stderr)
                self.assertEqual(policy.read_bytes(), original_policy)
                self.assertTrue((fake_home / "plugins" / "mochicode-auto").is_dir())

    def test_unconfirmed_root_install_is_a_zero_mutation_preview(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            before = _tree_snapshot(root)

            install = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertIn("No changes made", install.stdout)
            self.assertIn("-ConfirmInstall", install.stdout)
            self.assertEqual(_tree_snapshot(root), before)
            self.assertFalse(fake_home.exists())

    def test_unconfirmed_routing_cleanup_is_a_zero_mutation_preview(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            receipt_bytes = receipt.read_bytes()
            backup_roots = sorted(
                path.name for path in (fake_home / ".codex" / "backups").iterdir()
            )
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)
            before = _tree_snapshot(fake_home)

            cleanup = _run_install(
                powershell,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
            )

            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertIn("No changes made", cleanup.stdout)
            self.assertIn("-ConfirmInstall", cleanup.stdout)
            self.assertEqual(_tree_snapshot(fake_home), before)
            self.assertEqual(receipt.read_bytes(), receipt_bytes)
            self.assertEqual(
                sorted(path.name for path in (fake_home / ".codex" / "backups").iterdir()),
                backup_roots,
            )

    def test_confirmed_portable_wrappers_record_confirmation_in_manifests(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package_root = root / "package"
            package = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PACKAGE_SCRIPT),
                    "-Source",
                    str(PLUGIN_ROOT),
                    "-Destination",
                    str(package_root),
                    "-ZipPath",
                    str(root / "package.zip"),
                    "-GeneratedTimestampUtc",
                    "2026-08-27T12:34:56.789Z",
                ],
                cwd=PLUGIN_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            self.assertEqual(package.returncode, 0, package.stdout + package.stderr)

            def run_wrapper(
                wrapper_name: str,
                profile: Path,
                *arguments: str,
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-File",
                        str(package_root / "portable" / "install" / wrapper_name),
                        "-UserHome",
                        str(profile),
                        *arguments,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=180,
                )

            def assert_confirmed(profile: Path) -> None:
                receipt_path = profile / ".codex" / "mochicode-auto-install.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
                self.assertTrue(receipt["confirm_install"])
                manifest = json.loads(
                    Path(receipt["backup_manifest"]).read_text(encoding="utf-8-sig")
                )
                self.assertTrue(manifest["confirm_install"])

            install_profile = root / "install-profile"
            install_profile.mkdir()
            install = run_wrapper(
                "install.ps1",
                install_profile,
                "-ConfirmInstall",
                "-SkipPluginCommand",
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            assert_confirmed(install_profile)

            update = run_wrapper(
                "update.ps1",
                install_profile,
                "-ConfirmInstall",
                "-SkipPluginCommand",
            )
            self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
            assert_confirmed(install_profile)

            easy_profile = root / "easy-profile"
            easy_profile.mkdir()
            easy = run_wrapper(
                "easy-install.ps1",
                easy_profile,
                "-ConfirmInstall",
                "-SkipPluginCommand",
            )
            self.assertEqual(easy.returncode, 0, easy.stdout + easy.stderr)
            assert_confirmed(easy_profile)

    def test_routing_cleanup_only_requires_an_installed_exact_source_tree(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with self.subTest(kind="missing-installed-tree"), tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            coder = fake_home / ".agents" / "skills" / "coder"
            (coder / "agents").mkdir(parents=True)
            (coder / "SKILL.md").write_text("---\nname: coder\n---\n", encoding="utf-8")
            policy = coder / "agents" / "openai.yaml"
            original_policy = b"policy:\n  allow_implicit_invocation: true\n"
            policy.write_bytes(original_policy)
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)

            cleanup = _run_install(
                powershell,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
            )

            self.assertNotEqual(cleanup.returncode, 0)
            self.assertEqual(policy.read_bytes(), original_policy)
            self.assertFalse((fake_home / "plugins" / "mochicode-auto").exists())

        with self.subTest(kind="mismatched-installed-tree"), tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            coder = fake_home / ".agents" / "skills" / "coder"
            (coder / "agents").mkdir(parents=True)
            (coder / "SKILL.md").write_text("---\nname: coder\n---\n", encoding="utf-8")
            policy = coder / "agents" / "openai.yaml"
            original_policy = b"policy:\n  allow_implicit_invocation: true\n"
            policy.write_bytes(original_policy)
            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            installed_readme = fake_home / "plugins" / "mochicode-auto" / "README.md"
            installed_readme.write_bytes(installed_readme.read_bytes() + b"tampered\n")
            prior_receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            prior_receipt_bytes = prior_receipt.read_bytes()
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)

            cleanup = _run_install(
                powershell,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
            )

            self.assertNotEqual(cleanup.returncode, 0)
            self.assertEqual(policy.read_bytes(), original_policy)
            self.assertEqual(prior_receipt.read_bytes(), prior_receipt_bytes)
            self.assertTrue(installed_readme.read_bytes().endswith(b"tampered\n"))

        with self.subTest(kind="obsolete-skip-conflict"), tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            coder = fake_home / ".agents" / "skills" / "coder"
            (coder / "agents").mkdir(parents=True)
            (coder / "SKILL.md").write_text("---\nname: coder\n---\n", encoding="utf-8")
            policy = coder / "agents" / "openai.yaml"
            original_policy = b"policy:\n  allow_implicit_invocation: true\n"
            policy.write_bytes(original_policy)
            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)

            cleanup = _run_install(
                powershell,
                fake_home,
                "-RoutingCleanupOnly",
                "-SkipRoutingCleanup",
                "-CanaryReceipt",
                str(canary),
            )

            self.assertNotEqual(cleanup.returncode, 0)
            self.assertEqual(policy.read_bytes(), original_policy)

    def test_routing_cleanup_receipt_fails_closed_on_invalid_process_fields(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            coder = fake_home / ".agents" / "skills" / "coder"
            (coder / "agents").mkdir(parents=True)
            (coder / "SKILL.md").write_text("---\nname: coder\n---\n", encoding="utf-8")
            policy = coder / "agents" / "openai.yaml"
            original_policy = b"policy:\n  allow_implicit_invocation: true\n"
            policy.write_bytes(original_policy)
            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            latest_receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            latest_receipt_bytes = latest_receipt.read_bytes()
            backup_roots = set((fake_home / ".codex" / "backups").iterdir())

            missing = fake_home / ".codex" / "missing-canary.json"
            malformed = fake_home / ".codex" / "malformed-canary.json"
            malformed.write_text("{not-json", encoding="utf-8")
            for kind, canary in (("missing", missing), ("malformed", malformed)):
                with self.subTest(kind=kind):
                    cleanup = _run_install(
                        powershell,
                        fake_home,
                        "-RoutingCleanupOnly",
                        "-CanaryReceipt",
                        str(canary),
                    )
                    self.assertNotEqual(cleanup.returncode, 0)
                    self.assertEqual(policy.read_bytes(), original_policy)
                    self.assertEqual(latest_receipt.read_bytes(), latest_receipt_bytes)

            invalid_receipts: list[tuple[str, dict[str, object]]] = [
                (
                    "stale",
                    {
                        "completed_at_utc": (
                            datetime.now(timezone.utc) - timedelta(days=2)
                        ).isoformat().replace("+00:00", "Z")
                    },
                ),
                ("source-hash", {"source_manifest_hash": "0" * 64}),
                ("source-version", {"source_plugin_version": "0.0.0"}),
                ("cache-version", {"cache_plugin_version": "0.0.0"}),
                ("fresh-task", {"command_process": {"fresh_task_id": ""}}),
                ("prompt-hash", {"command_process": {"natural_prompt_sha256": "invalid"}}),
                ("output-bytes", {"command_process": {"model_output_bytes": 1}}),
                ("output-hash", {"command_process": {"model_output_sha256": "0" * 64}}),
                ("exit-code", {"command_process": {"exit_code": 9}}),
                ("timeout", {"command_process": {"timed_out": True}}),
                ("workspace-change", {"workspace_tree": {"after_sha256": "c" * 64}}),
                ("raw-prompt-field", {"raw_prompt": "<forbidden>"}),
            ]
            for kind, overrides in invalid_receipts:
                with self.subTest(kind=kind):
                    canary = fake_home / ".codex" / f"canary-{kind}.json"
                    evidence = fake_home / ".codex" / f"evidence-{kind}.json"
                    _write_canary_receipt(
                        fake_home,
                        canary,
                        evidence_path=evidence,
                        receipt_overrides=overrides,
                    )
                    cleanup = _run_install(
                        powershell,
                        fake_home,
                        "-RoutingCleanupOnly",
                        "-CanaryReceipt",
                        str(canary),
                    )
                    self.assertNotEqual(cleanup.returncode, 0)
                    self.assertEqual(policy.read_bytes(), original_policy)
                    self.assertEqual(latest_receipt.read_bytes(), latest_receipt_bytes)
                    self.assertEqual(set((fake_home / ".codex" / "backups").iterdir()), backup_roots)

    def test_routing_cleanup_rejects_missing_tampered_or_substituted_evidence(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            coder = fake_home / ".agents" / "skills" / "coder"
            (coder / "agents").mkdir(parents=True)
            (coder / "SKILL.md").write_text("---\nname: coder\n---\n", encoding="utf-8")
            policy = coder / "agents" / "openai.yaml"
            original_policy = b"policy:\n  allow_implicit_invocation: true\n"
            policy.write_bytes(original_policy)
            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            latest_receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            latest_receipt_bytes = latest_receipt.read_bytes()

            missing_canary = fake_home / ".codex" / "canary-missing-evidence.json"
            missing_evidence = fake_home / ".codex" / "missing-evidence.json"
            _write_canary_receipt(
                fake_home,
                missing_canary,
                receipt_overrides={"evidence": {"path": str(missing_evidence)}},
            )
            cases: list[tuple[str, Path]] = [("missing", missing_canary)]

            tampered_canary = fake_home / ".codex" / "canary-tampered-evidence.json"
            tampered_evidence = _write_canary_receipt(
                fake_home,
                tampered_canary,
                evidence_path=fake_home / ".codex" / "tampered-evidence.json",
            )
            tampered_evidence.write_bytes(tampered_evidence.read_bytes() + b"tampered\n")
            cases.append(("tampered", tampered_canary))

            substituted_canary = fake_home / ".codex" / "canary-substituted-evidence.json"
            _write_canary_receipt(
                fake_home,
                substituted_canary,
                evidence_path=fake_home / ".codex" / "substituted-evidence.json",
                evidence_overrides={"fresh_task_id": "fresh-task-substitution"},
            )
            cases.append(("substituted", substituted_canary))

            marker_canary = fake_home / ".codex" / "canary-wrong-marker.json"
            _write_canary_receipt(
                fake_home,
                marker_canary,
                evidence_path=fake_home / ".codex" / "wrong-marker-evidence.json",
                evidence_overrides={"success_marker": "other-plugin.success"},
            )
            cases.append(("wrong-marker", marker_canary))

            for kind, canary in cases:
                with self.subTest(kind=kind):
                    cleanup = _run_install(
                        powershell,
                        fake_home,
                        "-RoutingCleanupOnly",
                        "-CanaryReceipt",
                        str(canary),
                    )
                    self.assertNotEqual(cleanup.returncode, 0)
                    self.assertEqual(policy.read_bytes(), original_policy)
                    self.assertEqual(latest_receipt.read_bytes(), latest_receipt_bytes)

    def test_routing_cleanup_rejects_receipt_and_evidence_path_escapes(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            coder = fake_home / ".agents" / "skills" / "coder"
            (coder / "agents").mkdir(parents=True)
            (coder / "SKILL.md").write_text("---\nname: coder\n---\n", encoding="utf-8")
            policy = coder / "agents" / "openai.yaml"
            original_policy = b"policy:\n  allow_implicit_invocation: true\n"
            policy.write_bytes(original_policy)
            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            latest_receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            latest_receipt_bytes = latest_receipt.read_bytes()

            external = root / "external"
            external.mkdir()
            outside_receipt = external / "outside-canary.json"
            _write_canary_receipt(fake_home, outside_receipt)
            outside_evidence_receipt = fake_home / ".codex" / "outside-evidence-canary.json"
            _write_canary_receipt(
                fake_home,
                outside_evidence_receipt,
                evidence_path=external / "outside-evidence.json",
            )

            link = fake_home / ".codex" / "canary-link"
            link_environment = os.environ.copy()
            link_environment["MOCHICODE_TEST_LINK"] = str(link)
            link_environment["MOCHICODE_TEST_TARGET"] = str(external)
            junction = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:MOCHICODE_TEST_LINK -Target $env:MOCHICODE_TEST_TARGET | Out-Null",
                ],
                env=link_environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(junction.returncode, 0, junction.stderr)
            reparse_receipt = link / "reparse-canary.json"
            _write_canary_receipt(fake_home, external / "reparse-canary.json")
            reparse_evidence_receipt = fake_home / ".codex" / "reparse-evidence-canary.json"
            _write_canary_receipt(
                fake_home,
                reparse_evidence_receipt,
                evidence_path=link / "reparse-evidence.json",
            )

            cases = [
                ("receipt-outside", outside_receipt),
                ("evidence-outside", outside_evidence_receipt),
                ("receipt-reparse", reparse_receipt),
                ("evidence-reparse", reparse_evidence_receipt),
            ]
            for kind, canary in cases:
                with self.subTest(kind=kind):
                    cleanup = _run_install(
                        powershell,
                        fake_home,
                        "-RoutingCleanupOnly",
                        "-CanaryReceipt",
                        str(canary),
                    )
                    self.assertNotEqual(cleanup.returncode, 0)
                    self.assertEqual(policy.read_bytes(), original_policy)
                    self.assertEqual(latest_receipt.read_bytes(), latest_receipt_bytes)

    def test_installer_powershell_parser_accepts_script(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        environment = os.environ.copy()
        environment["MOCHICODE_TEST_SCRIPT"] = str(PLUGIN_ROOT / "install.ps1")
        parser = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-Command",
                "$tokens = $null\n$errors = $null\n[System.Management.Automation.Language.Parser]::ParseFile($env:MOCHICODE_TEST_SCRIPT, [ref]$tokens, [ref]$errors) | Out-Null\nif ($errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(parser.returncode, 0, parser.stdout + parser.stderr)

    def test_install_and_restore_are_reversible_in_an_isolated_profile(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            fake_home.mkdir()
            install = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "install.ps1"),
                    "-Source",
                    str(PLUGIN_ROOT),
                    "-UserHome",
                    str(fake_home),
                    "-SkipPluginCommand",
                    "-SkipRoutingCleanup",
                    "-ConfirmInstall",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertTrue(
                (fake_home / "plugins" / "mochicode-auto" / ".codex-plugin" / "plugin.json").is_file()
            )
            marketplace_path = fake_home / ".agents" / "plugins" / "marketplace.json"
            marketplace = json.loads(marketplace_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(marketplace["name"], "personal")
            self.assertEqual(marketplace["plugins"][0]["name"], "mochicode-auto")
            self.assertIn(
                "MOCHICODE-AUTO:BEGIN",
                (fake_home / ".codex" / "AGENTS.md").read_text(encoding="utf-8-sig"),
            )
            self.assertTrue((fake_home / ".codex" / "agents" / "mochicode-luna.toml").is_file())
            receipt = json.loads(
                (fake_home / ".codex" / "mochicode-auto-install.json").read_text(
                    encoding="utf-8-sig"
                )
            )

            restore = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "restore.ps1"),
                    "-Manifest",
                    receipt["backup_manifest"],
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertFalse((fake_home / "plugins" / "mochicode-auto").exists())
            self.assertFalse(marketplace_path.exists())
            self.assertFalse((fake_home / ".codex" / "AGENTS.md").exists())
            self.assertFalse((fake_home / ".codex" / "mochicode-auto-install.json").exists())

    def test_routing_cleanup_only_is_scoped_and_restores_policy_files_byte_for_byte(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            agents = fake_home / ".codex" / "AGENTS.md"
            agents.parent.mkdir(parents=True)
            agents.write_bytes(b"original global instructions\r\n")
            marketplace = fake_home / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            marketplace.write_bytes(
                b'{"name":"personal","interface":{"displayName":"Mine"},"plugins":[{"name":"existing","source":{"source":"local","path":"./plugins/existing"},"policy":{"installation":"AVAILABLE","authentication":"ON_INSTALL"},"category":"Productivity"}]}\r\n'
            )
            old_agent = fake_home / ".codex" / "agents" / "mochicode-luna.toml"
            old_agent.parent.mkdir(parents=True)
            old_agent.write_bytes(b"old agent bytes\r\n")
            coder = fake_home / ".agents" / "skills" / "coder"
            (coder / "agents").mkdir(parents=True)
            (coder / "SKILL.md").write_text(
                "---\nname: coder\ndescription: test\n---\n",
                encoding="utf-8",
            )
            old_policy = b"policy:\r\n  allow_implicit_invocation: true\r\n"
            policy = coder / "agents" / "openai.yaml"
            policy.write_bytes(old_policy)
            skill_agents = coder / "agents"
            extra_agent_file = skill_agents / "machine-specific.yaml"
            extra_agent_bytes = b"machine-specific: preserve\r\n"
            extra_agent_file.write_bytes(extra_agent_bytes)
            config = fake_home / ".codex" / "config.toml"
            config.write_bytes(b'personality = "pragmatic"\r\n')
            cache_sentinel = (
                fake_home
                / ".codex"
                / "plugins"
                / "cache"
                / "personal"
                / "mochicode-auto"
                / "sentinel.txt"
            )
            cache_sentinel.parent.mkdir(parents=True)
            cache_sentinel.write_bytes(b"cache must remain\r\n")
            old_receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            old_receipt.write_bytes(b"old receipt bytes\r\n")

            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            installed_plugin = fake_home / "plugins" / "mochicode-auto"
            self.assertEqual(_source_manifest_hash(installed_plugin), _source_manifest_hash())
            stage_one_receipt_bytes = old_receipt.read_bytes()
            untouched_files = {
                agents: agents.read_bytes(),
                marketplace: marketplace.read_bytes(),
                config: config.read_bytes(),
                cache_sentinel: cache_sentinel.read_bytes(),
            }
            plugin_snapshot = _tree_snapshot(installed_plugin)
            agent_snapshot = _tree_snapshot(fake_home / ".codex" / "agents")
            skill_agents_snapshot = _tree_snapshot(skill_agents)
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)

            cleanup = _run_install(
                powershell,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
                "-ConfirmInstall",
            )

            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertIn(
                "allow_implicit_invocation: false",
                policy.read_text(encoding="utf-8-sig"),
            )
            for path, expected in untouched_files.items():
                self.assertEqual(path.read_bytes(), expected, str(path))
            self.assertEqual(_tree_snapshot(installed_plugin), plugin_snapshot)
            self.assertEqual(_tree_snapshot(fake_home / ".codex" / "agents"), agent_snapshot)
            receipt = json.loads(
                old_receipt.read_text(encoding="utf-8-sig")
            )
            self.assertEqual(receipt["operation"], "routing_cleanup")
            manifest = json.loads(
                Path(receipt["backup_manifest"]).read_text(encoding="utf-8-sig")
            )
            self.assertEqual(len(manifest["entries"]), 2)
            for expected in (old_receipt, skill_agents):
                self.assertTrue(
                    any(os.path.samefile(entry["path"], expected) for entry in manifest["entries"]),
                    str(expected),
                )
            self.assertFalse(manifest["plugin_registration"]["attempted"])
            policy_entry = next(
                entry
                for entry in manifest["entries"]
                if os.path.samefile(entry["path"], skill_agents)
            )
            self.assertTrue(policy_entry["existed"])
            self.assertTrue(Path(policy_entry["backup"]).is_dir())
            self.assertEqual(_tree_snapshot(Path(policy_entry["backup"])), skill_agents_snapshot)
            receipt_entry = next(
                entry
                for entry in manifest["entries"]
                if os.path.samefile(entry["path"], old_receipt)
            )
            self.assertEqual(Path(receipt_entry["backup"]).read_bytes(), stage_one_receipt_bytes)
            restore = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "restore.ps1"),
                    "-Manifest",
                    receipt["backup_manifest"],
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(policy.read_bytes(), old_policy)
            self.assertEqual(extra_agent_file.read_bytes(), extra_agent_bytes)
            self.assertEqual(old_receipt.read_bytes(), stage_one_receipt_bytes)
            self.assertEqual(_tree_snapshot(skill_agents), skill_agents_snapshot)
            for path, expected in untouched_files.items():
                self.assertEqual(path.read_bytes(), expected, str(path))
            self.assertEqual(_tree_snapshot(installed_plugin), plugin_snapshot)
            self.assertEqual(_tree_snapshot(fake_home / ".codex" / "agents"), agent_snapshot)

    def test_restore_refuses_a_manifest_that_targets_the_user_root(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            fake_home.mkdir()
            install = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "install.ps1"),
                    "-Source",
                    str(PLUGIN_ROOT),
                    "-UserHome",
                    str(fake_home),
                    "-SkipPluginCommand",
                    "-SkipRoutingCleanup",
                    "-ConfirmInstall",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            receipt = json.loads(
                (fake_home / ".codex" / "mochicode-auto-install.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            manifest_path = Path(receipt["backup_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            manifest["entries"][0]["path"] = str(fake_home)
            tampered = manifest_path.parent / "tampered.json"
            tampered.write_text(json.dumps(manifest), encoding="utf-8")

            restore = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "restore.ps1"),
                    "-Manifest",
                    str(tampered),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertNotEqual(restore.returncode, 0)
            self.assertTrue(fake_home.is_dir())

    def test_routing_cleanup_restores_a_missing_skill_agents_tree_without_an_empty_directory(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            skill = fake_home / ".agents" / "skills" / "coder"
            skill.mkdir(parents=True)
            skill_bytes = b"---\nname: coder\n---\n"
            (skill / "SKILL.md").write_bytes(skill_bytes)
            original_skill_tree = {"SKILL.md": skill_bytes}

            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            skill_agents = skill / "agents"
            self.assertFalse(skill_agents.exists())
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)

            cleanup = _run_install(
                powershell,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
                "-ConfirmInstall",
            )

            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertTrue(skill_agents.is_dir())
            self.assertIn(
                "allow_implicit_invocation: false",
                (skill_agents / "openai.yaml").read_text(encoding="utf-8-sig"),
            )
            receipt = json.loads(
                (fake_home / ".codex" / "mochicode-auto-install.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            manifest = json.loads(Path(receipt["backup_manifest"]).read_text(encoding="utf-8-sig"))
            agents_entry = next(
                entry
                for entry in manifest["entries"]
                if Path(entry["path"]).resolve() == skill_agents.resolve()
            )
            self.assertFalse(agents_entry["existed"])

            restore = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "restore.ps1"),
                    "-Manifest",
                    receipt["backup_manifest"],
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(_tree_snapshot(skill), original_skill_tree)
            self.assertFalse(skill_agents.exists())

    def test_failed_install_publishes_manifest_and_rolls_back_partial_profile(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            fake_home.mkdir()
            marketplace = fake_home / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            original_marketplace = b'{"name":"unexpected","plugins":[]}\r\n'
            marketplace.write_bytes(original_marketplace)

            install = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "install.ps1"),
                    "-Source",
                    str(PLUGIN_ROOT),
                    "-UserHome",
                    str(fake_home),
                    "-SkipPluginCommand",
                    "-SkipRoutingCleanup",
                    "-ConfirmInstall",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertFalse((fake_home / "plugins" / "mochicode-auto").exists())
            self.assertEqual(marketplace.read_bytes(), original_marketplace)
            self.assertFalse((fake_home / ".codex" / "mochicode-auto-install.json").exists())
            manifests = list(
                (fake_home / ".codex" / "backups").glob("mochicode-auto-*/manifest.json")
            )
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8-sig"))
            self.assertGreaterEqual(len(manifest["entries"]), 3)
            failure_output = " ".join((install.stdout + install.stderr).split()).replace(
                " | ", " "
            )
            self.assertIn("Automatic rollback completed.", failure_output)
            self.assertIn("Rollback manifest:", failure_output)

    def test_real_plugin_registration_is_backed_up_and_automatically_reversed(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        codex = shutil.which("codex.cmd") or shutil.which("codex")
        self.assertIsNotNone(powershell)
        self.assertIsNotNone(codex)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            codex_home = fake_home / ".codex"
            codex_home.mkdir(parents=True)
            config = codex_home / "config.toml"
            original_config = b'personality = "pragmatic"\r\n'
            config.write_bytes(original_config)
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(codex_home)
            environment["USERPROFILE"] = str(fake_home)
            environment["HOME"] = str(fake_home)

            install = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "install.ps1"),
                    "-Source",
                    str(PLUGIN_ROOT),
                    "-UserHome",
                    str(fake_home),
                    "-SkipRoutingCleanup",
                    "-ConfirmInstall",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
            if (
                install.returncode != 0
                and "failed to activate plugin cache entry" in install.stderr
            ):
                self.assertEqual(config.read_bytes(), original_config)
                self.assertFalse(
                    (fake_home / "plugins" / "mochicode-auto").exists()
                )
                time.sleep(1.1)
                install = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-File",
                        str(PLUGIN_ROOT / "install.ps1"),
                        "-Source",
                        str(PLUGIN_ROOT),
                        "-UserHome",
                        str(fake_home),
                        "-SkipRoutingCleanup",
                        "-ConfirmInstall",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )
            self.assertEqual(install.returncode, 0, install.stderr)
            cache = codex_home / "plugins" / "cache" / "personal" / "mochicode-auto"
            self.assertTrue(cache.is_dir())
            self.assertNotEqual(config.read_bytes(), original_config)
            receipt = json.loads(
                (codex_home / "mochicode-auto-install.json").read_text(encoding="utf-8-sig")
            )
            manifest_path = Path(receipt["backup_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            manifest_paths = {
                str(entry["path"]).replace("/", "\\").lower()
                for entry in manifest["entries"]
            }
            self.assertTrue(any(path.endswith("\\.codex\\config.toml") for path in manifest_paths))
            self.assertTrue(
                any(
                    path.endswith("\\.codex\\plugins\\cache\\personal\\mochicode-auto")
                    for path in manifest_paths
                )
            )
            self.assertTrue(manifest["plugin_registration"]["succeeded"])

            restore = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "restore.ps1"),
                    "-Manifest",
                    str(manifest_path),
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(config.read_bytes(), original_config)
            self.assertFalse(cache.exists())
            self.assertFalse((fake_home / "plugins" / "mochicode-auto").exists())

    def test_restore_refuses_a_junction_that_redirects_an_allowed_target(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            codex_home = fake_home / ".codex"
            backup_root = codex_home / "backups" / "mochicode-auto-junction-test"
            backup_root.mkdir(parents=True)
            external = root / "external-agents"
            external.mkdir()
            sentinel = external / "mochicode-luna.toml"
            sentinel.write_text("must survive\n", encoding="utf-8")
            junction = codex_home / "agents"
            junction_environment = os.environ.copy()
            junction_environment["MOCHICODE_TEST_LINK"] = str(junction)
            junction_environment["MOCHICODE_TEST_TARGET"] = str(external)
            junction_result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:MOCHICODE_TEST_LINK -Target $env:MOCHICODE_TEST_TARGET | Out-Null",
                ],
                env=junction_environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(junction_result.returncode, 0, junction_result.stderr)
            manifest_path = backup_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "user_root": str(fake_home),
                        "backup_root": str(backup_root),
                        "entries": [
                            {
                                "path": str(junction / "mochicode-luna.toml"),
                                "existed": False,
                                "backup": str(backup_root / "unused"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            restore = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "restore.ps1"),
                    "-Manifest",
                    str(manifest_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertNotEqual(restore.returncode, 0)
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive\n")

    def test_restore_refuses_an_unrelated_specialist_skill_policy_before_any_write(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            backup_root = fake_home / ".codex" / "backups" / "mochicode-auto-scope-test"
            backup_root.mkdir(parents=True)
            receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            receipt.write_text("must remain\n", encoding="utf-8")
            specialist = (
                fake_home
                / ".agents"
                / "skills"
                / "ana-collaboration-protocol"
                / "agents"
                / "openai.yaml"
            )
            specialist.parent.mkdir(parents=True)
            specialist.write_text("policy:\n  allow_implicit_invocation: true\n", encoding="utf-8")
            manifest_path = backup_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "user_root": str(fake_home),
                        "backup_root": str(backup_root),
                        "entries": [
                            {
                                "path": str(receipt),
                                "existed": False,
                                "backup": str(backup_root / "unused-receipt"),
                            },
                            {
                                "path": str(specialist),
                                "existed": False,
                                "backup": str(backup_root / "unused-specialist"),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            restore = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "restore.ps1"),
                    "-Manifest",
                    str(manifest_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertNotEqual(restore.returncode, 0)
            self.assertEqual(receipt.read_text(encoding="utf-8"), "must remain\n")
            self.assertTrue(specialist.is_file())

    def test_install_refuses_a_redirected_workflow_policy_before_any_write(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            coder = fake_home / ".agents" / "skills" / "coder"
            coder.mkdir(parents=True)
            (coder / "SKILL.md").write_text(
                "---\nname: coder\ndescription: test\n---\n",
                encoding="utf-8",
            )
            external = root / "external-policy"
            external.mkdir()
            policy = external / "openai.yaml"
            policy.write_text("policy:\n  allow_implicit_invocation: true\n", encoding="utf-8")
            junction_environment = os.environ.copy()
            junction_environment["MOCHICODE_TEST_LINK"] = str(coder / "agents")
            junction_environment["MOCHICODE_TEST_TARGET"] = str(external)
            junction = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:MOCHICODE_TEST_LINK -Target $env:MOCHICODE_TEST_TARGET | Out-Null",
                ],
                env=junction_environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(junction.returncode, 0, junction.stderr)
            stage_one = _run_install(
                powershell,
                fake_home,
                "-SkipPluginCommand",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            installed_plugin = fake_home / "plugins" / "mochicode-auto"
            installed_snapshot = _tree_snapshot(installed_plugin)
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)

            install = _run_install(
                powershell,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertEqual(
                policy.read_text(encoding="utf-8"),
                "policy:\n  allow_implicit_invocation: true\n",
            )
            self.assertEqual(_tree_snapshot(installed_plugin), installed_snapshot)

    def test_install_refuses_a_redirected_codex_agents_file_before_any_write(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            fake_home.mkdir()
            external_codex = root / "external-codex"
            external_codex.mkdir()
            agents = external_codex / "AGENTS.md"
            agents.write_text("must remain external\n", encoding="utf-8")
            junction_environment = os.environ.copy()
            junction_environment["MOCHICODE_TEST_LINK"] = str(fake_home / ".codex")
            junction_environment["MOCHICODE_TEST_TARGET"] = str(external_codex)
            junction = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:MOCHICODE_TEST_LINK -Target $env:MOCHICODE_TEST_TARGET | Out-Null",
                ],
                env=junction_environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(junction.returncode, 0, junction.stderr)

            install = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "install.ps1"),
                    "-Source",
                    str(PLUGIN_ROOT),
                    "-UserHome",
                    str(fake_home),
                    "-SkipPluginCommand",
                    "-SkipRoutingCleanup",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertEqual(agents.read_text(encoding="utf-8"), "must remain external\n")
            self.assertFalse((fake_home / "plugins" / "mochicode-auto").exists())


    def test_update_existing_is_explicit_replaces_exact_tree_and_rolls_back_on_registration_failure(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)

        def seed_profile(fake_home: Path) -> dict[str, object]:
            plugin = fake_home / "plugins" / "mochicode-auto"
            (plugin / "old-only").mkdir(parents=True)
            (plugin / "old-only" / "stale.txt").write_bytes(b"old plugin bytes\r\n")
            cache = (
                fake_home
                / ".codex"
                / "plugins"
                / "cache"
                / "personal"
                / "mochicode-auto"
            )
            old_cache = cache / "0.0.0+old"
            (old_cache / "old-only").mkdir(parents=True)
            (old_cache / "old-only" / "stale.txt").write_bytes(b"old cache bytes\r\n")
            sibling_plugin = fake_home / "plugins" / "unrelated-sibling" / "keep.txt"
            sibling_plugin.parent.mkdir(parents=True)
            sibling_plugin.write_bytes(b"unrelated plugin bytes\r\n")
            marketplace = fake_home / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            marketplace.write_bytes(
                b'{"name":"personal","plugins":[{"name":"unrelated-sibling","source":{"source":"local","path":"./plugins/unrelated-sibling"}},{"name":"mochicode-auto","source":{"source":"local","path":"./plugins/mochicode-auto"}}]}\r\n'
            )
            agents = fake_home / ".codex" / "AGENTS.md"
            agents.parent.mkdir(parents=True, exist_ok=True)
            agents.write_bytes(b"preexisting instructions\r\n")
            config = fake_home / ".codex" / "config.toml"
            config.write_bytes(b'personality = "pragmatic"\r\ncustom = true\r\n')
            receipt = fake_home / ".codex" / "mochicode-auto-install.json"
            receipt.write_bytes(b"old receipt bytes\r\n")
            roles: list[Path] = []
            for source_role in (PLUGIN_ROOT / "config" / "agents").glob("mochicode-*.toml"):
                role = fake_home / ".codex" / "agents" / source_role.name
                role.parent.mkdir(parents=True, exist_ok=True)
                role.write_bytes(f"old {source_role.name}\r\n".encode("utf-8"))
                roles.append(role)
            skill_policy = fake_home / ".agents" / "skills" / "coder" / "agents" / "openai.yaml"
            skill_policy.parent.mkdir(parents=True)
            skill_policy.write_bytes(b"policy:\r\n  allow_implicit_invocation: true\r\n")
            return {
                "plugin": plugin,
                "cache": cache,
                "marketplace": marketplace,
                "agents": agents,
                "roles": roles,
                "config": config,
                "receipt": receipt,
                "sibling_plugin": sibling_plugin,
                "skill_policy": skill_policy,
            }

        def write_codex_shim(shim_dir: Path) -> Path:
            shim_dir.mkdir(parents=True)
            shim = shim_dir / "codex.cmd"
            shim.write_text(
                "@echo off\r\n"
                "setlocal EnableExtensions\r\n"
                "if /I \"%1\"==\"--version\" (\r\n"
                "  echo codex-cli 0.144.0\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if /I \"%1\"==\"debug\" if /I \"%2\"==\"models\" (\r\n"
                "  echo {\"models\":[{\"slug\":\"gpt-5.6-sol\",\"context_window\":272000,\"max_context_window\":872000,\"supported_reasoning_levels\":[{\"effort\":\"high\"},{\"effort\":\"max\"},{\"effort\":\"ultra\"}],\"service_tiers\":[{\"id\":\"priority\",\"name\":\"Fast\"}]}]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if /I \"%1\"==\"features\" if /I \"%2\"==\"list\" (\r\n"
                "  echo multi_agent stable true\r\n"
                "  echo fast_mode stable true\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                ">> \"%MOCHICODE_TEST_PLUGIN_LOG%\" echo %*\r\n"
                "if /I \"%1\" NEQ \"plugin\" exit /b 24\r\n"
                "if /I \"%2\"==\"list\" (\r\n"
                "  if /I \"%3\" NEQ \"--marketplace\" exit /b 24\r\n"
                "  if /I \"%4\" NEQ \"personal\" exit /b 24\r\n"
                "  if /I \"%5\" NEQ \"--json\" exit /b 24\r\n"
                "  if not \"%6\"==\"\" exit /b 24\r\n"
                "  powershell -NoProfile -Command \"$entry=[PSCustomObject]@{pluginId='mochicode-auto@personal';name='mochicode-auto';marketplaceName='personal';version=[Environment]::GetEnvironmentVariable('MOCHICODE_TEST_PLUGIN_VERSION');enabled=$true;source=[PSCustomObject]@{source='local';path=[Environment]::GetEnvironmentVariable('MOCHICODE_TEST_INSTALLED_PLUGIN')}}; [PSCustomObject]@{installed=@($entry)} | ConvertTo-Json -Depth 5 -Compress\"\r\n"
                "  exit /b %ERRORLEVEL%\r\n"
                ")\r\n"
                "if /I \"%2\" NEQ \"add\" exit /b 24\r\n"
                "if /I \"%3\" NEQ \"mochicode-auto@personal\" exit /b 24\r\n"
                "if /I \"%MOCHICODE_TEST_FAIL_PLUGIN_ADD%\"==\"1\" (\r\n"
                "  if exist \"%USERPROFILE%\\plugins\\mochicode-auto\\.codex-plugin\\plugin.json\" >> \"%MOCHICODE_TEST_PLUGIN_LOG%\" echo replacement-observed\r\n"
                "  exit /b 23\r\n"
                ")\r\n"
                "powershell -NoProfile -Command \"$src=[Environment]::GetEnvironmentVariable('MOCHICODE_TEST_SOURCE'); $metadata=Get-Content -LiteralPath (Join-Path $src '.codex-plugin\\plugin.json') -Raw | ConvertFrom-Json; $cacheRoot=Join-Path ([Environment]::GetEnvironmentVariable('CODEX_HOME')) 'plugins\\cache\\personal\\mochicode-auto'; $dst=Join-Path $cacheRoot ([string]$metadata.version); if (Test-Path -LiteralPath $cacheRoot) { Remove-Item -LiteralPath $cacheRoot -Recurse -Force }; New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null; Copy-Item -LiteralPath $src -Destination $dst -Recurse\"\r\n"
                "exit /b %ERRORLEVEL%\r\n",
                encoding="ascii",
            )
            return shim

        def run_update(
            fake_home: Path,
            environment: dict[str, str],
            *arguments: str,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(PLUGIN_ROOT / "install.ps1"),
                    "-Source",
                    str(PLUGIN_ROOT),
                    "-UserHome",
                    str(fake_home),
                    "-SkipRoutingCleanup",
                    *arguments,
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )

        def tracked_snapshot(paths: dict[str, object]) -> dict[str, object]:
            return {
                "plugin": _tree_snapshot(paths["plugin"]),
                "marketplace": paths["marketplace"].read_bytes(),
                "agents": paths["agents"].read_bytes(),
                "roles": {role: role.read_bytes() for role in paths["roles"]},
                "config": paths["config"].read_bytes(),
                "cache": _tree_snapshot(paths["cache"]),
                "receipt": paths["receipt"].read_bytes(),
                "sibling_plugin": paths["sibling_plugin"].read_bytes(),
                "skill_policy": paths["skill_policy"].read_bytes(),
            }

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            paths = seed_profile(fake_home)
            shim = write_codex_shim(root / "shim")
            log = root / "plugin-command.log"
            environment = os.environ.copy()
            environment.update(
                {
                    "CODEX_HOME": str(fake_home / ".codex"),
                    "USERPROFILE": str(fake_home),
                    "HOME": str(fake_home),
                    "PATH": str(shim.parent) + os.pathsep + environment.get("PATH", ""),
                    "MOCHICODE_TEST_SOURCE": str(PLUGIN_ROOT),
                    "MOCHICODE_TEST_PLUGIN_LOG": str(log),
                    "MOCHICODE_TEST_PLUGIN_VERSION": _plugin_version(),
                    "MOCHICODE_TEST_INSTALLED_PLUGIN": str(paths["plugin"].resolve()),
                }
            )
            before_refusal = tracked_snapshot(paths)
            refusal = run_update(fake_home, environment)
            self.assertNotEqual(refusal.returncode, 0)
            self.assertEqual(tracked_snapshot(paths), before_refusal)
            self.assertFalse(log.exists())

            success = run_update(
                fake_home,
                environment,
                "-UpdateExisting",
                "-ConfirmInstall",
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            plugin = paths["plugin"]
            cache = paths["cache"] / _plugin_version()
            self.assertEqual(_source_manifest_hash(plugin), _source_manifest_hash())
            self.assertEqual(_source_manifest_hash(cache), _source_manifest_hash())
            self.assertFalse((plugin / "old-only").exists())
            self.assertFalse((paths["cache"] / "0.0.0+old").exists())
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [
                    "plugin add mochicode-auto@personal",
                    "plugin list --marketplace personal --json",
                ],
            )
            self.assertEqual(paths["sibling_plugin"].read_bytes(), b"unrelated plugin bytes\r\n")
            self.assertEqual(
                paths["skill_policy"].read_bytes(),
                b"policy:\r\n  allow_implicit_invocation: true\r\n",
            )
            self.assertIn("custom = true", paths["config"].read_text(encoding="utf-8-sig"))
            marketplace = json.loads(paths["marketplace"].read_text(encoding="utf-8-sig"))
            self.assertIn("unrelated-sibling", [entry["name"] for entry in marketplace["plugins"]])
            receipt = json.loads(paths["receipt"].read_text(encoding="utf-8-sig"))
            manifest = json.loads(Path(receipt["backup_manifest"]).read_text(encoding="utf-8-sig"))
            backed_up = {Path(entry["path"]).resolve() for entry in manifest["entries"]}
            required = {
                paths["plugin"].resolve(),
                paths["marketplace"].resolve(),
                paths["agents"].resolve(),
                paths["config"].resolve(),
                paths["cache"].resolve(),
                paths["receipt"].resolve(),
                *(role.resolve() for role in paths["roles"]),
            }
            self.assertTrue(required.issubset(backed_up))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            paths = seed_profile(fake_home)
            shim = write_codex_shim(root / "shim")
            log = root / "plugin-command.log"
            tracked_before = tracked_snapshot(paths)
            environment = os.environ.copy()
            environment.update(
                {
                    "CODEX_HOME": str(fake_home / ".codex"),
                    "USERPROFILE": str(fake_home),
                    "HOME": str(fake_home),
                    "PATH": str(shim.parent) + os.pathsep + environment.get("PATH", ""),
                    "MOCHICODE_TEST_SOURCE": str(PLUGIN_ROOT),
                    "MOCHICODE_TEST_PLUGIN_LOG": str(log),
                    "MOCHICODE_TEST_PLUGIN_VERSION": _plugin_version(),
                    "MOCHICODE_TEST_INSTALLED_PLUGIN": str(paths["plugin"].resolve()),
                    "MOCHICODE_TEST_FAIL_PLUGIN_ADD": "1",
                }
            )
            failed = run_update(
                fake_home,
                environment,
                "-UpdateExisting",
                "-ConfirmInstall",
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("Automatic rollback completed.", failed.stdout + failed.stderr)
            self.assertIn("replacement-observed", log.read_text(encoding="utf-8"))
            self.assertEqual(_tree_snapshot(paths["plugin"]), tracked_before["plugin"])
            self.assertEqual(paths["marketplace"].read_bytes(), tracked_before["marketplace"])
            self.assertEqual(paths["agents"].read_bytes(), tracked_before["agents"])
            self.assertEqual(_tree_snapshot(paths["cache"]), tracked_before["cache"])
            self.assertEqual(paths["config"].read_bytes(), tracked_before["config"])
            self.assertEqual(paths["receipt"].read_bytes(), tracked_before["receipt"])
            self.assertEqual(paths["sibling_plugin"].read_bytes(), tracked_before["sibling_plugin"])
            self.assertEqual(paths["skill_policy"].read_bytes(), tracked_before["skill_policy"])
            for role, expected in tracked_before["roles"].items():
                self.assertEqual(role.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
