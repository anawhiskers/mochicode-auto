from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = REPO_ROOT / "package.ps1"
VERIFY_SCRIPT = REPO_ROOT / "verify-package.ps1"
SCHEMA_PATH = REPO_ROOT / "schemas" / "package-manifest.schema.json"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
ZIP_MAX_ENTRIES = 1_024
ZIP_MAX_ENTRY_BYTES = 4 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")


def _fixture(root: Path, *, categories: tuple[str, ...] = ("docs", "templates", "chatgpt")) -> Path:
    _write(
        root / ".codex-plugin" / "plugin.json",
        json.dumps(
            {
                "name": "mochicode-auto",
                "version": "0.1.0-test",
                "description": "fixture",
            }
        ),
    )
    _write(root / "install.ps1", "param([string]$Source, [string]$UserHome)\n")
    _write(root / "restore.ps1", "param([string]$Manifest)\n")
    # Package unit fixtures test structure/integrity; release smoke tests use real source.
    for folder in ("skills", "schemas", "config"):
        shutil.copytree(REPO_ROOT / folder, root / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for relative in (
        "scripts/mochicode.py", "scripts/mochicode_core/cli.py",
        "scripts/mochicode_core/manager_state.py", "scripts/mochicode_core/capabilities.py",
        "scripts/mochicode_core/child_receipts.py", "scripts/adaptive_config.py",
        "scripts/agent_adapter.py",
        "scripts/context_trial.py", "scripts/recovery_advisor.py",
    ):
        _write(root / relative, "# Package integrity fixture, not a runnable release.\n")
    _write(root / "README.md", "Fixture plugin source.\n")

    if "docs" in categories:
        _write(root / "docs" / "portable-guide.md", "Portable documentation.\n")
    if "templates" in categories:
        _write(root / "templates" / "starter.txt", "Portable template.\n")
    if "chatgpt" in categories:
        _write(root / "chatgpt" / "CUSTOM-INSTRUCTIONS-COMPACT.txt", "Account-level text.\n")
        _write(root / "chatgpt" / "NEW-PC-HANDOFF-PROMPT.md", "Local Codex handoff.\n")
        _write(root / "chatgpt" / "APPLY-AND-VERIFY.md", "Apply and verify.\n")
    return root


def _run_ps(script: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    if POWERSHELL is None:
        raise unittest.SkipTest("PowerShell is not available")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-File", str(script), *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


class PackageTests(unittest.TestCase):
    def _assert_zip_refused_without_partial_output(
        self,
        root: Path,
        archive: Path,
        expected_text: str,
    ) -> None:
        result = _run_ps(VERIFY_SCRIPT, "-ZipPath", str(archive), "-Quiet")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected_text, result.stdout.lower() + result.stderr.lower())
        leftovers = [
            path
            for path in root.iterdir()
            if path.name.startswith(".mochicode-package-")
        ]
        self.assertEqual(leftovers, [])

    def test_builds_manifest_and_verifies_extracted_zip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source")
            destination = root / "bundle"
            archive = root / "bundle.zip"
            result = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
                "-GeneratedTimestampUtc",
                "2026-08-27T12:34:56.789Z",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(destination.is_dir())
            self.assertTrue(archive.is_file())

            manifest = json.loads((destination / "MANIFEST.json").read_text(encoding="utf-8"))
            paths = [str(entry["path"]) for entry in manifest["files"]]
            self.assertEqual(paths, sorted(paths))
            self.assertEqual(manifest["file_count"], len(paths))
            self.assertEqual(
                manifest["total_bytes"],
                sum(int(entry["bytes"]) for entry in manifest["files"]),
            )
            self.assertIn("portable/docs/portable-guide.md", paths)
            self.assertIn("portable/templates/starter.txt", paths)
            self.assertIn("portable/chatgpt/CUSTOM-INSTRUCTIONS-COMPACT.txt", paths)
            for entry in manifest["files"]:
                payload = destination / Path(str(entry["path"]))
                self.assertEqual(payload.stat().st_size, entry["bytes"])
                self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), entry["sha256"])

            names = zipfile.ZipFile(archive).namelist()
            self.assertEqual(names, sorted(names))
            self.assertIn("MANIFEST.json", names)
            self.assertNotIn("../escape.txt", names)
            self.assertTrue(all("\\" not in name for name in names))

            verify_folder = _run_ps(VERIFY_SCRIPT, "-PackageRoot", str(destination), "-Quiet")
            self.assertEqual(verify_folder.returncode, 0, verify_folder.stdout + verify_folder.stderr)
            verify_zip = _run_ps(VERIFY_SCRIPT, "-ZipPath", str(archive), "-Quiet")
            self.assertEqual(verify_zip.returncode, 0, verify_zip.stdout + verify_zip.stderr)
            # A self-consistent manifest must not make a functionally incomplete bundle valid.
            missing = "plugin/skills/mochicode-auto/SKILL.md"
            (destination / missing).unlink()
            manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != missing]
            manifest["file_count"] = len(manifest["files"])
            manifest["total_bytes"] = sum(entry["bytes"] for entry in manifest["files"])
            (destination / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            incomplete = _run_ps(VERIFY_SCRIPT, "-PackageRoot", str(destination), "-Quiet")
            self.assertNotEqual(incomplete.returncode, 0)
            self.assertIn("missing required file", incomplete.stdout + incomplete.stderr)

    def test_missing_required_asset_fails_before_creating_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source", categories=("docs", "chatgpt"))
            destination = root / "bundle"
            archive = root / "bundle.zip"
            result = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("templates", result.stdout.lower() + result.stderr.lower())
            self.assertFalse(destination.exists())
            self.assertFalse(archive.exists())

    def test_verifier_rejects_manifest_traversal_and_zip_extraction_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source")
            destination = root / "bundle"
            archive = root / "bundle.zip"
            built = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
                "-GeneratedTimestampUtc",
                "2026-08-27T12:34:56.789Z",
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

            manifest_path = destination / "MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["path"] = "../escape.txt"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            tampered = _run_ps(VERIFY_SCRIPT, "-PackageRoot", str(destination), "-Quiet")
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("traversal", tampered.stdout.lower() + tampered.stderr.lower())

            malicious = root / "malicious.zip"
            with zipfile.ZipFile(malicious, "w") as handle:
                handle.writestr("../escape.txt", b"must not extract")
            extracted_outside = root / "escape.txt"
            rejected = _run_ps(VERIFY_SCRIPT, "-ZipPath", str(malicious), "-Quiet")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(extracted_outside.exists())

    def test_onedrive_target_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source")
            one_drive = root / "OneDrive"
            one_drive.mkdir()
            destination = one_drive / "bundle"
            archive = one_drive / "bundle.zip"
            environment = os.environ.copy()
            environment["OneDrive"] = str(one_drive)
            result = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("onedrive", result.stdout.lower() + result.stderr.lower())
            self.assertFalse(destination.exists())
            self.assertFalse(archive.exists())

    def test_filters_runtime_files_and_refuses_private_endpoint_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source")
            _write(source / "cache" / "ignored.bin", b"cache")
            _write(source / "sessions" / "ignored.json", "session")
            _write(source / ".env", "TOKEN=not-packaged\n")
            destination = root / "bundle"
            archive = root / "bundle.zip"
            result = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((destination / "MANIFEST.json").read_text(encoding="utf-8"))
            paths = {str(entry["path"]) for entry in manifest["files"]}
            self.assertFalse(any(path.startswith("plugin/cache/") for path in paths))
            self.assertFalse(any(path.startswith("plugin/sessions/") for path in paths))
            self.assertNotIn("plugin/.env", paths)

            _write(source / "endpoint-note.txt", "Use https://10.20.30.40:9443 for the service.\n")
            rejected_destination = root / "rejected-bundle"
            rejected_archive = root / "rejected-bundle.zip"
            rejected = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(rejected_destination),
                "-ZipPath",
                str(rejected_archive),
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("endpoint", rejected.stdout.lower() + rejected.stderr.lower())
            self.assertFalse(rejected_destination.exists())
            self.assertFalse(rejected_archive.exists())

    def test_filters_backup_trees_and_refuses_additional_token_formats(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source")
            _write(source / ".agent-workflow-backups" / "old.md", "private backup\n")
            _write(source / "notes.md.bak", "private backup\n")
            destination = root / "bundle"
            archive = root / "bundle.zip"
            built = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            manifest = json.loads((destination / "MANIFEST.json").read_text(encoding="utf-8"))
            paths = {str(entry["path"]) for entry in manifest["files"]}
            self.assertFalse(any(".agent-workflow-backups" in path for path in paths))
            self.assertNotIn("plugin/notes.md.bak", paths)

            _write(source / "release-proof.txt", "github_pat_" + "A" * 40 + "\n")
            rejected = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(root / "rejected"),
                "-ZipPath",
                str(root / "rejected.zip"),
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("credential", rejected.stdout.lower() + rejected.stderr.lower())

    def test_directory_manifest_resource_claims_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source")
            destination = root / "bundle"
            archive = root / "bundle.zip"
            built = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            manifest_path = destination / "MANIFEST.json"
            original = json.loads(manifest_path.read_text(encoding="utf-8"))

            manifest = dict(original)
            manifest["file_count"] = ZIP_MAX_ENTRIES + 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            count_result = _run_ps(VERIFY_SCRIPT, "-PackageRoot", str(destination), "-Quiet")
            self.assertNotEqual(count_result.returncode, 0)
            self.assertIn("file_count exceeds limit", count_result.stdout.lower() + count_result.stderr.lower())

            manifest = dict(original)
            manifest["total_bytes"] = ZIP_MAX_TOTAL_BYTES + 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            size_result = _run_ps(VERIFY_SCRIPT, "-PackageRoot", str(destination), "-Quiet")
            self.assertNotEqual(size_result.returncode, 0)
            self.assertIn("total_bytes exceeds limit", size_result.stdout.lower() + size_result.stderr.lower())

    def test_binary_secret_payload_is_rejected_as_non_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = _fixture(root / "source")
            _write(
                source / "opaque-data.txt",
                b"\xff\xfeBearer abcdefghijklmnopqrstuvwxyz",
            )
            destination = root / "bundle"
            archive = root / "bundle.zip"
            result = _run_ps(
                PACKAGE_SCRIPT,
                "-Source",
                str(source),
                "-Destination",
                str(destination),
                "-ZipPath",
                str(archive),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("utf-8", result.stdout.lower() + result.stderr.lower())
            self.assertFalse(destination.exists())
            self.assertFalse(archive.exists())

    def test_zip_oversized_entry_is_refused_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "oversized-entry.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
                handle.writestr("oversized.txt", b"A" * (ZIP_MAX_ENTRY_BYTES + 1))
            self._assert_zip_refused_without_partial_output(
                root,
                archive,
                "entry exceeds uncompressed byte limit",
            )

    def test_zip_total_uncompressed_bytes_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "oversized-total.zip"
            chunk = b"B" * ZIP_MAX_ENTRY_BYTES
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
                for index in range(ZIP_MAX_TOTAL_BYTES // ZIP_MAX_ENTRY_BYTES):
                    handle.writestr(f"chunk-{index}.txt", chunk)
                handle.writestr("one-byte-over.txt", b"C")
            self._assert_zip_refused_without_partial_output(
                root,
                archive,
                "total uncompressed bytes exceed limit",
            )

    def test_zip_compression_ratio_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "high-ratio.zip"
            with zipfile.ZipFile(
                archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as handle:
                handle.writestr("high-ratio.txt", b"D" * (1024 * 1024))
            self._assert_zip_refused_without_partial_output(
                root,
                archive,
                "compression ratio exceeds limit",
            )

    def test_zip_entry_count_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "too-many-entries.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
                for index in range(ZIP_MAX_ENTRIES + 1):
                    handle.writestr(f"entries/{index:04d}.txt", b"")
            self._assert_zip_refused_without_partial_output(
                root,
                archive,
                "entry count exceeds limit",
            )

    def test_schema_and_owned_entrypoints_exist(self) -> None:
        self.assertTrue(PACKAGE_SCRIPT.is_file())
        self.assertTrue(VERIFY_SCRIPT.is_file())
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["package_name"]["const"],
            "mochicode-auto-portable",
        )
        for name in ("package-safety.ps1", "install.ps1", "update.ps1", "doctor.ps1", "restore.ps1", "easy-install.ps1", "agent-sync.ps1"):
            self.assertTrue((REPO_ROOT / "portable" / "install" / name).is_file(), name)
        wrapper = (REPO_ROOT / "portable" / "install" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("[bool]$DirectFirst = $false", wrapper)
        self.assertIn("[bool]$TerraFirst = $false", wrapper)

    def test_portable_python_launchers_select_one_executable(self) -> None:
        for name in ("doctor.ps1", "agent-sync.ps1"):
            script = (REPO_ROOT / "portable" / "install" / name).read_text(encoding="utf-8")
            self.assertIn("Select-Object -First 1", script, name)
            self.assertIn("-CommandType Application", script, name)


if __name__ == "__main__":
    unittest.main()
