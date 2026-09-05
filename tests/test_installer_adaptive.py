from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
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
            entries.append(f"F\0{relative}\0{len(payload)}\0{hashlib.sha256(payload).hexdigest()}\n".encode("utf-8"))
    return hashlib.sha256(b"".join(sorted(entries))).hexdigest()


def _run_install(
    powershell: str,
    source: Path,
    fake_home: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(source / "install.ps1"),
            "-Source",
            str(source),
            "-UserHome",
            str(fake_home),
            *arguments,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )


def _write_canary_receipt(user_root: Path, path: Path) -> None:
    version = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]
    source_hash = _source_manifest_hash()
    evidence_path = user_root / ".codex" / "mochicode-auto-canary-evidence.json"
    evidence = {
        "schema_version": 1,
        "success_marker": "mochicode-auto.fresh-natural-prompt-activation.success",
        "plugin_name": "mochicode-auto",
        "plugin_version": version,
        "source_manifest_hash": source_hash,
        "fresh_task_id": TEST_FRESH_TASK_ID,
        "natural_prompt_sha256": TEST_NATURAL_PROMPT_SHA256,
        "workspace_tree_before_sha256": TEST_WORKSPACE_TREE_SHA256,
        "workspace_tree_after_sha256": TEST_WORKSPACE_TREE_SHA256,
    }
    evidence_bytes = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(evidence_bytes)
    evidence_hash = hashlib.sha256(evidence_bytes).hexdigest()
    receipt = {
        "schema_version": 2,
        "plugin_name": "mochicode-auto",
        "source_plugin_version": version,
        "cache_plugin_version": version,
        "source_manifest_hash": source_hash,
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
            "path": str(evidence_path),
            "bytes": len(evidence_bytes),
            "sha256": evidence_hash,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _write_fake_codex(
    directory: Path,
    calls: Path,
    *,
    fail_load: bool = False,
    include_astra: bool = False,
) -> Path:
    models = [
        {
            "slug": "gpt-5.6-sol",
            "context_window": 272000,
            "max_context_window": 872000,
            "supported_reasoning_levels": [
                {"effort": effort} for effort in ("high", "max", "ultra")
            ],
            "service_tiers": [{"id": "priority", "name": "Fast"}],
        }
    ]
    if include_astra:
        models.append(
            {
                "slug": "gpt-6-astra",
                "context_window": 1050000,
                "max_context_window": 1050000,
                "supported_reasoning_levels": [
                    {"effort": effort}
                    for effort in ("low", "medium", "high", "xhigh", "max")
                ],
            }
        )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fake_codex.py").write_text(
        "from pathlib import Path\n"
        "import json\n"
        "import sys\n"
        "\n"
        f"CALLS = Path({json.dumps(str(calls))})\n"
        f"FAIL_LOAD = {fail_load!r}\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('codex-cli 0.144.0')\n"
        "elif args == ['debug', 'models']:\n"
        f"    print(json.dumps({{'models': {models!r}}}))\n"
        "elif args == ['features', 'list']:\n"
        "    count = int(CALLS.read_text(encoding='utf-8')) if CALLS.exists() else 0\n"
        "    count += 1\n"
        "    CALLS.write_text(str(count), encoding='utf-8')\n"
        "    if FAIL_LOAD and count >= 5:\n"
        "        raise SystemExit(17)\n"
        "    print('multi_agent stable true')\n"
        "    print('fast_mode stable true')\n"
        "else:\n"
        "    raise SystemExit(0)\n",
        encoding="utf-8",
    )
    shim = directory / "codex.cmd"
    shim.write_text(
        "@echo off\r\n"
        "python \"%~dp0fake_codex.py\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="ascii",
    )
    return shim


class AdaptiveInstallerTests(unittest.TestCase):
    def _powershell(self) -> str:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        return str(powershell)

    def test_clean_install_contains_catalog_and_plugin_only_upgrader_with_byte_preserved_marker_context(self) -> None:
        powershell = self._powershell()
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            agents = fake_home / ".codex" / "AGENTS.md"
            begin = b"<!-- MOCHICODE-AUTO:BEGIN -->"
            end = b"<!-- MOCHICODE-AUTO:END -->"
            prefix = b"# machine-specific prefix\r\nnon_ascii = \xc3\xa9\r\n\r\n"
            old_block = begin + b"\r\nold routing\r\n" + end
            suffix = b"\r\n# machine-specific suffix\r\n"
            agents.parent.mkdir(parents=True)
            agents.write_bytes(prefix + old_block + suffix)

            install = _run_install(
                powershell,
                PLUGIN_ROOT,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-ConfirmInstall",
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            updated = agents.read_bytes()
            begin_index = updated.index(begin)
            end_index = updated.index(end, begin_index) + len(end)
            self.assertEqual(updated[:begin_index], prefix)
            self.assertEqual(updated[end_index:], suffix)
            plugin = fake_home / "plugins" / "mochicode-auto"
            self.assertTrue((plugin / "config" / "role-dispositions.json").is_file())
            self.assertTrue((plugin / "skills" / "repository-workflow-upgrader" / "SKILL.md").is_file())
            self.assertFalse(
                (fake_home / ".agents" / "skills" / "repository-workflow-upgrader").exists()
            )
            installed_roles = {
                path.name for path in (fake_home / ".codex" / "agents").glob("*.toml")
            }
            source_roles = {path.name for path in (PLUGIN_ROOT / "config" / "agents").glob("*.toml")}
            self.assertEqual(installed_roles, source_roles)

    def test_astra_first_refuses_without_live_catalog_and_applies_when_supported(self) -> None:
        powershell = self._powershell()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original = (
                b'model = "gpt-5.6-sol"\r\n'
                b"model_context_window = 1000000\r\n"
                b"model_auto_compact_token_limit = 850000\r\n"
            )

            unavailable_home = root / "unavailable-profile"
            unavailable_config = unavailable_home / ".codex" / "config.toml"
            unavailable_config.parent.mkdir(parents=True)
            unavailable_config.write_bytes(original)
            unavailable_shim = _write_fake_codex(
                root / "unavailable-shim", root / "unavailable-calls"
            )
            unavailable = _run_install(
                powershell,
                PLUGIN_ROOT,
                unavailable_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-AstraFirst",
                "-ConfirmInstall",
                environment={
                    "PATH": str(unavailable_shim.parent)
                    + os.pathsep
                    + os.environ.get("PATH", "")
                },
            )
            self.assertNotEqual(unavailable.returncode, 0)
            self.assertEqual(unavailable_config.read_bytes(), original)
            self.assertIn("gpt-6-astra", unavailable.stdout + unavailable.stderr)

            available_home = root / "available-profile"
            available_config = available_home / ".codex" / "config.toml"
            available_config.parent.mkdir(parents=True)
            available_config.write_bytes(original)
            available_shim = _write_fake_codex(
                root / "available-shim",
                root / "available-calls",
                include_astra=True,
            )
            available = _run_install(
                powershell,
                PLUGIN_ROOT,
                available_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-AstraFirst",
                "-ConfirmInstall",
                environment={
                    "PATH": str(available_shim.parent)
                    + os.pathsep
                    + os.environ.get("PATH", "")
                },
            )
            self.assertEqual(available.returncode, 0, available.stderr)
            updated = available_config.read_text(encoding="utf-8")
            self.assertIn('model = "gpt-6-astra"', updated)
            self.assertIn('model_reasoning_effort = "high"', updated)
            self.assertIn("model_context_window = 1000000", updated)
            self.assertIn("model_auto_compact_token_limit = 850000", updated)
            self.assertIn('review_model = "gpt-5.6-sol"', updated)

    def test_direct_source_install_excludes_unmeasured_git_cache_and_bytecode(self) -> None:
        powershell = self._powershell()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            shutil.copytree(
                PLUGIN_ROOT,
                source,
                ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc"),
            )
            (source / ".git" / "objects").mkdir(parents=True)
            (source / ".git" / "objects" / "private").write_text("not copied", encoding="utf-8")
            (source / ".pytest_cache").mkdir()
            (source / ".pytest_cache" / "state").write_text("not copied", encoding="utf-8")
            (source / "scripts" / "__pycache__").mkdir(exist_ok=True)
            (source / "scripts" / "__pycache__" / "stale.pyc").write_bytes(b"not copied")
            (source / ".agent-workflow-backups" / "old").mkdir(parents=True)
            (source / ".agent-workflow-backups" / "old" / "private.md").write_text("not copied", encoding="utf-8")
            (source / "local-output.jsonl").write_text("not copied", encoding="utf-8")
            (source / "notes.md.bak").write_text("not copied", encoding="utf-8")
            fake_home = root / "profile"

            install = _run_install(
                powershell,
                source,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-ConfirmInstall",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            plugin = fake_home / "plugins" / "mochicode-auto"
            self.assertFalse((plugin / ".git").exists())
            self.assertFalse((plugin / ".pytest_cache").exists())
            self.assertFalse((plugin / "scripts" / "__pycache__").exists())
            self.assertFalse((plugin / ".agent-workflow-backups").exists())
            self.assertFalse((plugin / "local-output.jsonl").exists())
            self.assertFalse((plugin / "notes.md.bak").exists())

    def test_canary_cleanup_disables_only_named_mcp_servers_removes_only_stale_agents_and_restores(self) -> None:
        powershell = self._powershell()
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw) / "profile"
            stage_one = _run_install(
                powershell,
                PLUGIN_ROOT,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)

            config = fake_home / ".codex" / "config.toml"
            original_config = (
                b"model = 'machine-model'\r\n"
                b"[mcp_servers.\"windows-mcp\"]\r\n"
                b"command = 'keep-windows-endpoint'\r\n"
                b"args = ['--private']\r\n"
                b"enabled = true\r\n"
                b"[mcp_servers.filesystem]\r\n"
                b"root = 'C:/machine-data'\r\n"
                b"enabled = true\r\n"
                b"[mcp_servers.obsidian]\r\n"
                b"command = 'legacy-obsidian-endpoint'\r\n"
                b"enabled = true\r\n"
                b"[mcp_servers.agent-swarm-remote]\r\n"
                b"command = 'preserve-swarm'\r\n"
                b"enabled = true\r\n"
                b"[mcp_servers.node_repl]\r\n"
                b"command = 'preserve-node'\r\n"
                b"enabled = true\r\n"
                b"[mcp_servers.\"obsidian-anaminipc\"]\r\n"
                b"command = 'preserve-obsidian'\r\n"
                b"enabled = true\r\n"
                b"[mcp_servers.mobile-mcp]\r\n"
                b"command = 'preserve-mobile'\r\n"
                b"enabled = true\r\n"
                b"[mcp_servers.serial]\r\n"
                b"command = 'preserve-serial'\r\n"
                b"enabled = true\r\n"
            )
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_bytes(original_config)

            agent_dir = fake_home / ".codex" / "agents"
            stale_files = {
                "explore-cheap.toml": b"stale explore\r\n",
                "worker-sonnet.toml": b"stale worker\r\n",
            }
            for name, payload in stale_files.items():
                (agent_dir / name).write_bytes(payload)
            unrelated_agent = agent_dir / "keep-me.toml"
            unrelated_agent.write_bytes(b"unrelated agent\r\n")

            policy_bytes: dict[str, bytes] = {}
            for name in ("coder", "master-status", "obsidian-second-brain", "claude-state-bridge"):
                policy = fake_home / ".agents" / "skills" / name / "agents" / "openai.yaml"
                policy.parent.mkdir(parents=True, exist_ok=True)
                policy_bytes[name] = b"policy:\r\n  allow_implicit_invocation: true\r\n"
                policy.write_bytes(policy_bytes[name])
                (policy.parent.parent / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")

            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)
            codex_calls = Path(raw) / "cleanup-codex-calls"
            codex_shim = _write_fake_codex(Path(raw) / "cleanup-shim", codex_calls)
            cleanup = _run_install(
                powershell,
                PLUGIN_ROOT,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
                "-DisableStaleMcp",
                "-ConfirmInstall",
                environment={
                    "PATH": str(codex_shim.parent) + os.pathsep + os.environ.get("PATH", ""),
                },
            )

            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            text = config.read_text(encoding="utf-8-sig")
            for endpoint in (
                "keep-windows-endpoint",
                "C:/machine-data",
                "legacy-obsidian-endpoint",
                "preserve-swarm",
                "preserve-node",
                "preserve-obsidian",
                "preserve-mobile",
                "preserve-serial",
            ):
                self.assertIn(endpoint, text)
            for name in ("windows-mcp", "filesystem", "obsidian"):
                start = text.index(f"[mcp_servers.{name if name != 'windows-mcp' else chr(34) + name + chr(34)}]")
                end = text.find("\n[", start + 1)
                block = text[start:] if end < 0 else text[start:end]
                self.assertIn("enabled = false", block, name)
            self.assertIn('[mcp_servers.agent-swarm-remote]', text)
            self.assertIn('[mcp_servers."obsidian-anaminipc"]', text)
            self.assertIn("enabled = true", text[text.index('[mcp_servers.agent-swarm-remote]') :])
            for name in stale_files:
                self.assertFalse((agent_dir / name).exists())
            self.assertEqual(unrelated_agent.read_bytes(), b"unrelated agent\r\n")
            self.assertEqual(
                (fake_home / ".agents" / "skills" / "claude-state-bridge" / "agents" / "openai.yaml").read_bytes(),
                policy_bytes["claude-state-bridge"],
            )
            self.assertIn(
                "allow_implicit_invocation: false",
                (fake_home / ".agents" / "skills" / "master-status" / "agents" / "openai.yaml").read_text(
                    encoding="utf-8-sig"
                ),
            )
            receipt = json.loads(
                (fake_home / ".codex" / "mochicode-auto-install.json").read_text(encoding="utf-8-sig")
            )
            manifest_path = Path(receipt["backup_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            manifest_paths = {Path(entry["path"]).resolve() for entry in manifest["entries"]}
            self.assertIn(config.resolve(), manifest_paths)
            for name in stale_files:
                self.assertIn((agent_dir / name).resolve(), manifest_paths)

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
                timeout=90,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(config.read_bytes(), original_config)
            for name, payload in stale_files.items():
                self.assertEqual((agent_dir / name).read_bytes(), payload)

    def test_routing_cleanup_failure_restores_missing_skill_agents_tree(self) -> None:
        powershell = self._powershell()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "profile"
            skill = fake_home / ".agents" / "skills" / "coder"
            skill.mkdir(parents=True)
            skill_bytes = b"---\nname: coder\n---\n"
            (skill / "SKILL.md").write_bytes(skill_bytes)

            stage_one = _run_install(
                powershell,
                PLUGIN_ROOT,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-ConfirmInstall",
            )
            self.assertEqual(stage_one.returncode, 0, stage_one.stderr)
            config = fake_home / ".codex" / "config.toml"
            original_config = b'model = "gpt-5.6-sol"\r\n'
            config.write_bytes(original_config)
            canary = fake_home / ".codex" / "canary.json"
            _write_canary_receipt(fake_home, canary)
            calls = root / "failing-codex-calls"
            shim = _write_fake_codex(root / "failing-shim", calls, fail_load=True)
            environment = {
                "PATH": str(shim.parent) + os.pathsep + os.environ.get("PATH", ""),
            }

            failed = _run_install(
                powershell,
                PLUGIN_ROOT,
                fake_home,
                "-RoutingCleanupOnly",
                "-CanaryReceipt",
                str(canary),
                "-DisableStaleMcp",
                "-ConfirmInstall",
                environment=environment,
            )

            self.assertNotEqual(failed.returncode, 0)
            failure_output = " ".join((failed.stdout + failed.stderr).split()).replace(
                " | ", " "
            )
            self.assertIn("Codex rejected the adaptive config candidate during load validation", failure_output)
            self.assertIn("Automatic rollback completed.", failure_output)
            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual(
                {path.relative_to(skill).as_posix(): path.read_bytes() for path in skill.rglob("*") if path.is_file()},
                {"SKILL.md": skill_bytes},
            )
            self.assertFalse((skill / "agents").exists())

    def test_adaptive_audit_merge_is_default_safe_and_opt_in_context_removal_rolls_back(self) -> None:
        powershell = self._powershell()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls = root / "success-codex-calls"
            shim = _write_fake_codex(root / "success-shim", calls)
            environment = {"PATH": str(shim.parent) + os.pathsep + os.environ.get("PATH", "")}

            fake_home = root / "clean-profile"
            config = fake_home / ".codex" / "config.toml"
            original = (
                b'model = "gpt-5.6-sol"\r\n'
                b"model_context_window = 1000000\r\n"
                b"model_auto_compact_token_limit = 900000\r\n"
                b'machine_path = "C:/machine-only"\r\n'
            )
            config.parent.mkdir(parents=True)
            config.write_bytes(original)
            clean = _run_install(
                powershell,
                PLUGIN_ROOT,
                fake_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-RemoveStaleContext",
                "-ConfirmInstall",
                environment=environment,
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            updated = config.read_bytes()
            self.assertNotIn(b"model_context_window", updated)
            self.assertNotIn(b"model_auto_compact_token_limit", updated)
            self.assertIn(b'machine_path = "C:/machine-only"\r\n', updated)
            receipt = json.loads(
                (fake_home / ".codex" / "mochicode-auto-install.json").read_text(encoding="utf-8-sig")
            )
            self.assertTrue(receipt["confirm_install"])
            manifest = json.loads(Path(receipt["backup_manifest"]).read_text(encoding="utf-8-sig"))
            self.assertTrue(manifest["confirm_install"])
            self.assertTrue(manifest["adaptive_config"]["attempted"])
            self.assertEqual(int(calls.read_text(encoding="utf-8")), 5)
            backup_root = Path(manifest["backup_root"])
            self.assertEqual(list(backup_root.glob("*.candidate.toml")), [])
            self.assertEqual(list(backup_root.glob("*.report.json")), [])
            self.assertEqual(list(backup_root.glob(".adaptive-config-validation-*")), [])

            update = _run_install(
                powershell,
                PLUGIN_ROOT,
                fake_home,
                "-UpdateExisting",
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-ConfirmInstall",
                environment=environment,
            )
            self.assertEqual(update.returncode, 0, update.stderr)
            self.assertIn(b'machine_path = "C:/machine-only"\r\n', config.read_bytes())

            failing_home = root / "failing-profile"
            failing_config = failing_home / ".codex" / "config.toml"
            failing_config.parent.mkdir(parents=True)
            failing_config.write_bytes(original)
            failing_calls = root / "failing-codex-calls"
            failing_shim = _write_fake_codex(root / "failing-shim", failing_calls, fail_load=True)
            failed = _run_install(
                powershell,
                PLUGIN_ROOT,
                failing_home,
                "-SkipPluginCommand",
                "-SkipRoutingCleanup",
                "-RemoveStaleContext",
                "-ConfirmInstall",
                environment={
                    "PATH": str(failing_shim.parent) + os.pathsep + os.environ.get("PATH", ""),
                },
            )
            self.assertNotEqual(failed.returncode, 0)
            failure_output = " ".join((failed.stdout + failed.stderr).split()).replace(
                " | ", " "
            )
            self.assertIn("Codex rejected the adaptive config candidate during load validation", failure_output)
            self.assertIn("Automatic rollback completed.", failure_output)
            self.assertEqual(failing_config.read_bytes(), original)
            self.assertFalse((failing_home / "plugins" / "mochicode-auto").exists())
            self.assertEqual(int(failing_calls.read_text(encoding="utf-8")), 5)
            failed_manifests = list((failing_home / ".codex" / "backups").glob("mochicode-auto-*/manifest.json"))
            self.assertEqual(len(failed_manifests), 1)
            failed_manifest = json.loads(failed_manifests[0].read_text(encoding="utf-8-sig"))
            failed_backup_root = Path(failed_manifest["backup_root"])
            self.assertEqual(list(failed_backup_root.glob("*.candidate.toml")), [])
            self.assertEqual(list(failed_backup_root.glob("*.report.json")), [])


if __name__ == "__main__":
    unittest.main()
