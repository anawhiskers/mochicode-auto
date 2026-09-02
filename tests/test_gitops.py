from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.gitops import GitOperationError, GitWorkspaceManager


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
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", "base.txt")
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


class GitWorkspaceTests(unittest.TestCase):
    def _reviewed_packet(self, root: Path, run_id: str):
        source = make_repo(root)
        manager = GitWorkspaceManager()
        run_root = root / "run"
        integration = manager.create_integration(source, run_root, run_id)
        packet = manager.create_packet(integration, run_root, "reviewed")
        (packet.path / "feature.txt").write_text("reviewed\n", encoding="utf-8")
        manager.stage_all(packet)
        reviewed_head, changed = manager.commit_staged(packet, "reviewed implementation")
        self.assertTrue(changed)
        reviewed_fingerprint = manager.workspace_fingerprint(
            packet.path,
            reviewed_head,
        )
        integration_head = manager.head(integration.path)
        return manager, integration, packet, reviewed_head, reviewed_fingerprint, integration_head

    def _integrate_reviewed(
        self,
        manager,
        integration,
        packet,
        reviewed_head,
        reviewed_fingerprint,
        integration_head,
    ):
        return manager.integrate_packet(
            integration,
            packet,
            reviewed_head=reviewed_head,
            reviewed_fingerprint=reviewed_fingerprint,
            expected_integration_branch=integration.branch,
            expected_integration_head=integration_head,
        )

    def test_merge_refuses_packet_branch_ref_moved_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager, integration, packet, reviewed_head, fingerprint, integration_head = (
                self._reviewed_packet(Path(raw), "branch-ref-drift")
            )
            parent = git(packet.path, "rev-parse", f"{reviewed_head}^")
            git(packet.path, "update-ref", f"refs/heads/{packet.branch}", parent)

            with self.assertRaisesRegex(GitOperationError, "branch ref"):
                self._integrate_reviewed(
                    manager,
                    integration,
                    packet,
                    reviewed_head,
                    fingerprint,
                    integration_head,
                )

    def test_merge_refuses_implementation_worktree_detached_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager, integration, packet, reviewed_head, fingerprint, integration_head = (
                self._reviewed_packet(Path(raw), "packet-detached")
            )
            git(packet.path, "switch", "--detach", reviewed_head)

            with self.assertRaisesRegex(GitOperationError, "packet branch"):
                self._integrate_reviewed(
                    manager,
                    integration,
                    packet,
                    reviewed_head,
                    fingerprint,
                    integration_head,
                )

    def test_merge_refuses_integration_branch_switch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager, integration, packet, reviewed_head, fingerprint, integration_head = (
                self._reviewed_packet(Path(raw), "integration-branch-drift")
            )
            git(integration.path, "switch", "-c", "unexpected-integration")

            with self.assertRaisesRegex(GitOperationError, "integration branch"):
                self._integrate_reviewed(
                    manager,
                    integration,
                    packet,
                    reviewed_head,
                    fingerprint,
                    integration_head,
                )

    def test_merge_refuses_integration_head_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager, integration, packet, reviewed_head, fingerprint, integration_head = (
                self._reviewed_packet(Path(raw), "integration-head-drift")
            )
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

            with self.assertRaisesRegex(GitOperationError, "integration HEAD"):
                self._integrate_reviewed(
                    manager,
                    integration,
                    packet,
                    reviewed_head,
                    fingerprint,
                    integration_head,
                )

    def test_valid_merge_second_parent_is_exact_reviewed_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager, integration, packet, reviewed_head, fingerprint, integration_head = (
                self._reviewed_packet(Path(raw), "reviewed-hash-merge")
            )

            merged_head = self._integrate_reviewed(
                manager,
                integration,
                packet,
                reviewed_head,
                fingerprint,
                integration_head,
            )

            self.assertEqual(
                git(integration.path, "rev-parse", f"{merged_head}^2"),
                reviewed_head,
            )

    def test_staged_contract_receipt_matches_exact_commit_without_restaging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            manager = GitWorkspaceManager()
            run_root = root / "run"
            integration = manager.create_integration(source, run_root, "staged-receipt")
            packet = manager.create_packet(integration, run_root, "contract")
            check = packet.path / "tests" / "test_contract.py"
            check.parent.mkdir()
            check.write_text("assert True\n", encoding="utf-8")

            manager.stage_all(packet)
            staged_diff = manager.staged_diff_text(packet, integration.branch)
            staged_statuses = manager.staged_path_statuses_since(packet, integration.branch)
            contract_head, changed = manager.commit_staged(
                packet,
                "contract: staged receipt",
            )

            self.assertTrue(changed)
            self.assertEqual(
                manager.diff_between(packet.path, integration.branch, contract_head),
                staged_diff,
            )
            self.assertEqual(
                manager.path_statuses_between(
                    packet.path,
                    integration.branch,
                    contract_head,
                ),
                staged_statuses,
            )
            self.assertEqual(manager.working_status(packet.path), "")

    def test_staged_contract_commit_refuses_changes_after_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            manager = GitWorkspaceManager()
            run_root = root / "run"
            integration = manager.create_integration(source, run_root, "staged-race")
            packet = manager.create_packet(integration, run_root, "contract")
            check = packet.path / "tests" / "test_contract.py"
            check.parent.mkdir()
            check.write_text("assert True\n", encoding="utf-8")
            manager.stage_all(packet)
            (packet.path / "production.py").write_text(
                "owned_by_terra = True\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(GitOperationError, "unstaged"):
                manager.commit_staged(packet, "contract: must refuse race")
            self.assertEqual(manager.head(packet.path), manager.head(integration.path))

    def test_fresh_implementation_worktree_is_clean_at_contract_head_and_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            manager = GitWorkspaceManager()
            run_root = root / "run"
            integration = manager.create_integration(source, run_root, "fresh-implementation")
            terra = manager.create_packet(integration, run_root, "contract")
            check = terra.path / "tests" / "test_contract.py"
            check.parent.mkdir()
            check.write_text("assert True\n", encoding="utf-8")
            manager.stage_all(terra)
            contract_head, changed = manager.commit_staged(terra, "contract: fresh root")
            self.assertTrue(changed)

            implementation = manager.create_implementation(
                terra,
                run_root / "implementations" / "contract-a1",
                contract_head,
            )

            self.assertEqual(implementation.branch, terra.branch)
            self.assertEqual(manager.head(implementation.path), contract_head)
            self.assertEqual(manager.working_status(implementation.path), "")
            self.assertEqual(git(terra.path, "branch", "--show-current"), "")
            (terra.path / "terra-descendant.txt").write_text(
                "must stay out of the implementation root\n",
                encoding="utf-8",
            )
            self.assertFalse((implementation.path / "terra-descendant.txt").exists())

    def test_packet_integrates_without_touching_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            source_branch = git(source, "branch", "--show-current")
            source_head = git(source, "rev-parse", "HEAD")
            manager = GitWorkspaceManager()
            run_root = root / "run"

            integration = manager.create_integration(source, run_root, "abc123")
            packet = manager.create_packet(integration, run_root, "vertical")
            (packet.path / "feature.txt").write_text("working\n", encoding="utf-8")
            manager.commit_packet(packet, "vertical: working")
            reviewed_head = manager.head(packet.path)
            manager.integrate_packet(
                integration,
                packet,
                reviewed_head=reviewed_head,
                reviewed_fingerprint=manager.workspace_fingerprint(
                    packet.path,
                    reviewed_head,
                ),
                expected_integration_branch=integration.branch,
                expected_integration_head=manager.head(integration.path),
            )

            self.assertEqual(git(source, "branch", "--show-current"), source_branch)
            self.assertEqual(git(source, "rev-parse", "HEAD"), source_head)
            self.assertFalse((source / "feature.txt").exists())
            self.assertEqual(
                (integration.path / "feature.txt").read_text(encoding="utf-8"),
                "working\n",
            )
            self.assertTrue(integration.branch.startswith("codex/mochicode-"))

    def test_dirty_tracked_and_untracked_work_is_copied_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            (source / "base.txt").write_text("user-owned dirty change\n", encoding="utf-8")
            (source / "new.txt").write_text("user-owned untracked file\n", encoding="utf-8")
            before_status = git(source, "status", "--short")

            integration = GitWorkspaceManager().create_integration(
                source,
                root / "run",
                "dirty123",
            )

            self.assertEqual(git(source, "status", "--short"), before_status)
            self.assertEqual(
                (integration.path / "base.txt").read_text(encoding="utf-8"),
                "user-owned dirty change\n",
            )
            self.assertEqual(
                (integration.path / "new.txt").read_text(encoding="utf-8"),
                "user-owned untracked file\n",
            )
            self.assertEqual(git(integration.path, "status", "--short"), "")

    def test_run_state_cannot_be_created_inside_the_source_repo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = make_repo(Path(raw))

            with self.assertRaisesRegex(ValueError, "outside the source repository"):
                GitWorkspaceManager().create_integration(
                    source,
                    source / ".mochicode",
                    "unsafe",
                )

    def test_resume_reopens_integration_and_allocates_a_fresh_packet_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_repo(root)
            manager = GitWorkspaceManager()
            run_root = root / "run"
            integration = manager.create_integration(source, run_root, "resume")
            first = manager.create_packet(integration, run_root, "packet-a1")

            reopened = manager.open_integration(
                source,
                run_root,
                "resume",
                expected_source_head=integration.source_head,
                expected_source_branch=integration.source_branch,
                expected_integration_head=manager.head(integration.path),
            )
            second = manager.create_packet(reopened, run_root, "packet-a1")

            self.assertEqual(reopened.branch, integration.branch)
            self.assertNotEqual(second.path, first.path)
            self.assertTrue(second.path.name.endswith("-r2"))


if __name__ == "__main__":
    unittest.main()
