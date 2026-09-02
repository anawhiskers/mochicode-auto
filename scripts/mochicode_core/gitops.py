from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import os
import re
import shutil
import subprocess


class GitOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IntegrationWorkspace:
    source_root: Path
    path: Path
    branch: str
    source_head: str
    source_branch: str
    run_id: str


@dataclass(frozen=True, slots=True)
class PacketWorkspace:
    path: Path
    branch: str
    packet_id: str


class GitWorkspaceManager:
    def discover_root(self, project: Path) -> Path:
        project = Path(project).resolve()
        output = self._git(project, "rev-parse", "--show-toplevel")
        return Path(output).resolve()

    def create_integration(
        self,
        project: Path,
        run_root: Path,
        run_id: str,
    ) -> IntegrationWorkspace:
        source_root = self.discover_root(project)
        run_root = Path(run_root).resolve()
        if run_root == source_root or run_root.is_relative_to(source_root):
            raise ValueError("run state and worktrees must live outside the source repository")
        run_root.mkdir(parents=True, exist_ok=True)

        safe_run = self._safe_component(run_id)
        integration_path = run_root / "integration"
        if integration_path.exists():
            raise ValueError(f"integration worktree already exists: {integration_path}")
        source_head = self._git(source_root, "rev-parse", "HEAD")
        source_branch = self._git(source_root, "branch", "--show-current") or "detached"
        integration_branch = f"codex/mochicode-{safe_run}-integration"
        if self._branch_exists(source_root, integration_branch):
            raise ValueError(f"integration branch already exists: {integration_branch}")

        tracked_patch = self._git_bytes(source_root, "diff", "--binary", "HEAD")
        untracked_output = self._git_bytes(
            source_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        untracked = [
            Path(os.fsdecode(item))
            for item in untracked_output.split(b"\0")
            if item
        ]

        self._git(source_root, "worktree", "add", "--detach", str(integration_path), source_head)
        self._git(integration_path, "switch", "-c", integration_branch)

        if tracked_patch:
            self._git_with_input(
                integration_path,
                tracked_patch,
                "apply",
                "--index",
                "--binary",
                "-",
            )
        for relative in untracked:
            source = (source_root / relative).resolve()
            if not source.is_relative_to(source_root):
                raise GitOperationError(f"untracked path escapes source root: {relative}")
            destination = integration_path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        if tracked_patch or untracked:
            self._git(integration_path, "add", "-A")
            self._commit(integration_path, "mochicode: preserve source working state")

        return IntegrationWorkspace(
            source_root=source_root,
            path=integration_path,
            branch=integration_branch,
            source_head=source_head,
            source_branch=source_branch,
            run_id=safe_run,
        )

    def create_packet(
        self,
        integration: IntegrationWorkspace,
        run_root: Path,
        packet_id: str,
    ) -> PacketWorkspace:
        safe_packet = self._safe_component(packet_id)
        packet_root = Path(run_root).resolve() / "packets"
        packet_root.mkdir(parents=True, exist_ok=True)
        candidate = safe_packet
        suffix = 1
        while True:
            packet_path = packet_root / candidate
            branch = f"codex/mochicode-{integration.run_id}-packet-{candidate}"
            if not packet_path.exists() and not self._branch_exists(
                integration.source_root,
                branch,
            ):
                break
            suffix += 1
            candidate = f"{safe_packet}-r{suffix}"
        self._git(
            integration.source_root,
            "worktree",
            "add",
            "--detach",
            str(packet_path),
            integration.branch,
        )
        self._git(packet_path, "switch", "-c", branch)
        return PacketWorkspace(path=packet_path, branch=branch, packet_id=packet_id)

    def create_implementation(
        self,
        packet: PacketWorkspace,
        path: Path,
        contract_head: str,
    ) -> PacketWorkspace:
        implementation_path = Path(path).resolve()
        if implementation_path.exists():
            raise ValueError(f"implementation worktree already exists: {implementation_path}")
        source_root = self.discover_root(packet.path)
        if implementation_path == source_root or implementation_path.is_relative_to(source_root):
            raise ValueError("implementation worktree must live outside the source repository")
        branch = self._git(packet.path, "branch", "--show-current")
        if branch != packet.branch:
            raise GitOperationError(
                f"packet branch changed before implementation worktree: "
                f"expected {packet.branch}, found {branch or 'detached'}"
            )
        if self.head(packet.path) != contract_head:
            raise GitOperationError(
                "packet HEAD is not the validated contract head: "
                f"expected {contract_head}, found {self.head(packet.path)}"
            )
        dirty = self.working_status(packet.path)
        if dirty:
            raise GitOperationError(
                "Terra packet worktree is dirty before implementation isolation: "
                + dirty
            )

        implementation_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(packet.path, "switch", "--detach", contract_head)
        try:
            self._git(
                packet.path,
                "worktree",
                "add",
                str(implementation_path),
                packet.branch,
            )
        except GitOperationError:
            self._git(packet.path, "switch", packet.branch)
            raise

        implementation = PacketWorkspace(
            path=implementation_path,
            branch=packet.branch,
            packet_id=packet.packet_id,
        )
        implementation_head = self.head(implementation.path)
        if implementation_head != contract_head:
            raise GitOperationError(
                "fresh implementation worktree is not rooted at the contract head: "
                f"expected {contract_head}, found {implementation_head}"
            )
        implementation_dirty = self.working_status(implementation.path)
        if implementation_dirty:
            raise GitOperationError(
                "fresh implementation worktree is dirty: " + implementation_dirty
            )
        return implementation

    def open_integration(
        self,
        project: Path,
        run_root: Path,
        run_id: str,
        *,
        expected_source_head: str,
        expected_source_branch: str,
        expected_integration_head: str,
    ) -> IntegrationWorkspace:
        source_root = self.discover_root(project)
        current_source_head = self._git(source_root, "rev-parse", "HEAD")
        current_source_branch = self._git(source_root, "branch", "--show-current") or "detached"
        if current_source_head != expected_source_head:
            raise GitOperationError(
                "source HEAD drifted since run creation: "
                f"expected {expected_source_head}, found {current_source_head}"
            )
        if current_source_branch != expected_source_branch:
            raise GitOperationError(
                "source branch drifted since run creation: "
                f"expected {expected_source_branch}, found {current_source_branch}"
            )
        integration_path = Path(run_root).resolve() / "integration"
        if not integration_path.is_dir():
            raise ValueError(f"integration worktree is missing: {integration_path}")
        safe_run = self._safe_component(run_id)
        branch = self._git(integration_path, "branch", "--show-current")
        expected = f"codex/mochicode-{safe_run}-integration"
        if branch != expected:
            raise GitOperationError(
                f"integration branch mismatch: expected {expected}, found {branch or 'detached'}"
            )
        current_integration_head = self._git(integration_path, "rev-parse", "HEAD")
        if current_integration_head != expected_integration_head:
            raise GitOperationError(
                "integration HEAD drifted outside the controller: "
                f"expected {expected_integration_head}, found {current_integration_head}"
            )
        dirty = self.working_status(integration_path)
        if dirty:
            raise GitOperationError(
                "integration worktree is dirty outside the controller: " + dirty
            )
        return IntegrationWorkspace(
            source_root=source_root,
            path=integration_path,
            branch=branch,
            source_head=expected_source_head,
            source_branch=expected_source_branch,
            run_id=safe_run,
        )

    def commit_packet(self, packet: PacketWorkspace, message: str) -> str:
        commit, changed = self.commit_if_changed(packet, message)
        if not changed:
            raise GitOperationError(f"packet {packet.packet_id!r} produced no file changes")
        return commit

    def stage_all(self, packet: PacketWorkspace) -> None:
        self._git(packet.path, "add", "-A")

    def commit_staged(
        self,
        packet: PacketWorkspace,
        message: str,
    ) -> tuple[str, bool]:
        unstaged = self.unstaged_path_statuses(packet)
        if unstaged:
            paths = ", ".join(path for _, path in unstaged)
            raise GitOperationError(
                "packet worktree has unstaged changes before staged commit: " + paths
            )
        staged_paths = self._git(
            packet.path,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
        )
        if not staged_paths:
            return self._git(packet.path, "rev-parse", "HEAD"), False
        self._commit(packet.path, message)
        return self._git(packet.path, "rev-parse", "HEAD"), True

    def commit_if_changed(
        self,
        packet: PacketWorkspace,
        message: str,
    ) -> tuple[str, bool]:
        self.stage_all(packet)
        return self.commit_staged(packet, message)

    def diff_text(self, packet: PacketWorkspace, base_ref: str) -> str:
        return self._git(packet.path, "diff", "--binary", base_ref)

    def staged_diff_text(self, packet: PacketWorkspace, base_ref: str) -> str:
        return self._git(packet.path, "diff", "--cached", "--binary", base_ref)

    def diff_between(self, path: Path, base_ref: str, head_ref: str = "HEAD") -> str:
        return self._git(Path(path), "diff", "--binary", f"{base_ref}..{head_ref}")

    def changed_paths_between(
        self,
        path: Path,
        base_ref: str,
        head_ref: str = "HEAD",
    ) -> tuple[str, ...]:
        output = self._git(Path(path), "diff", "--name-only", f"{base_ref}..{head_ref}")
        return tuple(line.strip().replace("\\", "/") for line in output.splitlines() if line.strip())

    def path_statuses_between(
        self,
        path: Path,
        base_ref: str,
        head_ref: str = "HEAD",
    ) -> tuple[tuple[str, str], ...]:
        return self._diff_path_statuses(Path(path), f"{base_ref}..{head_ref}")

    def staged_path_statuses_since(
        self,
        packet: PacketWorkspace,
        base_ref: str,
    ) -> tuple[tuple[str, str], ...]:
        return self._diff_path_statuses(packet.path, "--cached", base_ref)

    def staged_file_hashes(
        self,
        packet: PacketWorkspace,
        paths: tuple[str, ...],
    ) -> dict[str, str]:
        return self._object_file_hashes(packet, "", paths)

    def ref_file_hashes(
        self,
        packet: PacketWorkspace,
        ref: str,
        paths: tuple[str, ...],
    ) -> dict[str, str]:
        return self._object_file_hashes(packet, ref, paths)

    def _object_file_hashes(
        self,
        packet: PacketWorkspace,
        ref: str,
        paths: tuple[str, ...],
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for raw_path in sorted(set(paths)):
            normalized = raw_path.replace("\\", "/")
            parts = tuple(part for part in normalized.split("/") if part)
            if (
                not normalized
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:", normalized)
                or ".." in parts
            ):
                raise GitOperationError(
                    f"staged hash path escapes packet worktree: {raw_path}"
                )
            try:
                content = self._git_bytes(
                    packet.path,
                    "show",
                    f"{ref}:{normalized}",
                )
            except GitOperationError:
                continue
            hashes[normalized] = hashlib.sha256(content).hexdigest()
        return hashes

    def unstaged_path_statuses(
        self,
        packet: PacketWorkspace,
    ) -> tuple[tuple[str, str], ...]:
        statuses = list(self._diff_path_statuses(packet.path))
        untracked_output = self._git_bytes(
            packet.path,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        for encoded in untracked_output.split(b"\0"):
            if encoded:
                statuses.append(("A", os.fsdecode(encoded).replace("\\", "/")))
        return tuple(sorted(set(statuses), key=lambda item: item[1]))

    def head(self, path: Path) -> str:
        return self._git(Path(path), "rev-parse", "HEAD")

    def current_branch(self, path: Path) -> str:
        return self._git(Path(path), "branch", "--show-current")

    def branch_head(self, path: Path, branch: str) -> str:
        return self._git(
            Path(path),
            "rev-parse",
            "--verify",
            f"refs/heads/{branch}",
        )

    def status(self, path: Path) -> str:
        return self._git(Path(path), "status", "--short", "--branch")

    def working_status(self, path: Path) -> str:
        return self._git(
            Path(path),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )

    def workspace_fingerprint(self, path: Path, base_ref: str) -> str:
        root = Path(path).resolve()
        digest = hashlib.sha256()
        digest.update(self._git_bytes(root, "diff", "--binary", base_ref))
        untracked_output = self._git_bytes(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        untracked = sorted(item for item in untracked_output.split(b"\0") if item)
        for encoded_relative in untracked:
            relative = Path(os.fsdecode(encoded_relative))
            candidate = (root / relative).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                raise GitOperationError(f"untracked path escapes or is unreadable: {relative}")
            content = candidate.read_bytes()
            digest.update(len(encoded_relative).to_bytes(8, "big"))
            digest.update(encoded_relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    def changed_paths_since(self, packet: PacketWorkspace, base_ref: str) -> tuple[str, ...]:
        committed = self._git(packet.path, "diff", "--name-only", f"{base_ref}..HEAD")
        working = self._git(packet.path, "diff", "--name-only", "HEAD")
        untracked = self._git(packet.path, "ls-files", "--others", "--exclude-standard")
        paths = {
            line.strip().replace("\\", "/")
            for output in (committed, working, untracked)
            for line in output.splitlines()
            if line.strip()
        }
        return tuple(sorted(paths))

    def changed_path_statuses_since(
        self,
        packet: PacketWorkspace,
        base_ref: str,
    ) -> tuple[tuple[str, str], ...]:
        committed = self._git(
            packet.path,
            "diff",
            "--name-status",
            "--no-renames",
            f"{base_ref}..HEAD",
        )
        working = self._git(
            packet.path,
            "diff",
            "--name-status",
            "--no-renames",
            "HEAD",
        )
        untracked = self._git(
            packet.path,
            "ls-files",
            "--others",
            "--exclude-standard",
        )
        statuses: dict[str, str] = {}
        for output in (committed, working):
            for line in output.splitlines():
                if not line.strip():
                    continue
                fields = line.split("\t")
                if len(fields) < 2:
                    raise GitOperationError(f"invalid git name-status row: {line}")
                status = fields[0][:1]
                path = fields[-1].strip().replace("\\", "/")
                prior = statuses.get(path)
                statuses[path] = status if prior in {None, "A"} else prior
        for line in untracked.splitlines():
            path = line.strip().replace("\\", "/")
            if path:
                statuses.setdefault(path, "A")
        return tuple((status, path) for path, status in sorted(statuses.items()))

    def _diff_path_statuses(
        self,
        root: Path,
        *args: str,
    ) -> tuple[tuple[str, str], ...]:
        output = self._git(
            Path(root),
            "diff",
            "--name-status",
            "--no-renames",
            *args,
        )
        statuses: list[tuple[str, str]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) < 2:
                raise GitOperationError(f"invalid git name-status row: {line}")
            statuses.append((fields[0][:1], fields[-1].strip().replace("\\", "/")))
        return tuple(sorted(statuses, key=lambda item: item[1]))

    def integrate_packet(
        self,
        integration: IntegrationWorkspace,
        packet: PacketWorkspace,
        *,
        reviewed_head: str,
        reviewed_fingerprint: str,
        expected_integration_branch: str,
        expected_integration_head: str,
    ) -> str:
        packet_branch = self.current_branch(packet.path)
        if packet_branch != packet.branch:
            raise GitOperationError(
                "packet branch changed after review: "
                f"expected {packet.branch}, found {packet_branch or 'detached'}"
            )
        packet_branch_head = self.branch_head(packet.path, packet.branch)
        if packet_branch_head != reviewed_head:
            raise GitOperationError(
                "packet branch ref changed after review: "
                f"expected {reviewed_head}, found {packet_branch_head}"
            )
        packet_head = self.head(packet.path)
        if packet_head != reviewed_head:
            raise GitOperationError(
                "packet HEAD changed after review: "
                f"expected {reviewed_head}, found {packet_head}"
            )
        packet_status = self.working_status(packet.path)
        if packet_status:
            raise GitOperationError(
                "packet worktree is dirty after review: " + packet_status
            )
        packet_fingerprint = self.workspace_fingerprint(
            packet.path,
            reviewed_head,
        )
        if packet_fingerprint != reviewed_fingerprint:
            raise GitOperationError(
                "packet fingerprint changed after review: "
                f"expected {reviewed_fingerprint}, found {packet_fingerprint}"
            )

        if expected_integration_branch != integration.branch:
            raise GitOperationError(
                "expected integration branch does not match workspace identity: "
                f"expected {integration.branch}, received {expected_integration_branch}"
            )
        integration_branch = self.current_branch(integration.path)
        if integration_branch != expected_integration_branch:
            raise GitOperationError(
                "integration branch changed before merge: "
                f"expected {expected_integration_branch}, "
                f"found {integration_branch or 'detached'}"
            )
        integration_head = self.head(integration.path)
        if integration_head != expected_integration_head:
            raise GitOperationError(
                "integration HEAD changed before merge: "
                f"expected {expected_integration_head}, found {integration_head}"
            )
        integration_status = self.working_status(integration.path)
        if integration_status:
            raise GitOperationError(
                "integration worktree is dirty before merge: "
                + integration_status
            )

        self._git(
            integration.path,
            "-c",
            "user.name=MochiCode",
            "-c",
            "user.email=mochicode@local.invalid",
            "merge",
            "--no-ff",
            "--no-edit",
            reviewed_head,
        )
        merge_head = self.head(integration.path)

        post_integration_branch = self.current_branch(integration.path)
        if post_integration_branch != expected_integration_branch:
            raise GitOperationError(
                "integration branch changed during merge: "
                f"expected {expected_integration_branch}, "
                f"found {post_integration_branch or 'detached'}"
            )
        post_integration_status = self.working_status(integration.path)
        if post_integration_status:
            raise GitOperationError(
                "integration worktree is dirty after merge: "
                + post_integration_status
            )
        first_parent = self._git(integration.path, "rev-parse", f"{merge_head}^1")
        second_parent = self._git(integration.path, "rev-parse", f"{merge_head}^2")
        if first_parent != expected_integration_head:
            raise GitOperationError(
                "merge first parent is not the expected integration HEAD: "
                f"expected {expected_integration_head}, found {first_parent}"
            )
        if second_parent != reviewed_head:
            raise GitOperationError(
                "merge second parent is not the reviewed packet HEAD: "
                f"expected {reviewed_head}, found {second_parent}"
            )

        post_packet_branch = self.current_branch(packet.path)
        post_packet_branch_head = self.branch_head(packet.path, packet.branch)
        post_packet_head = self.head(packet.path)
        post_packet_status = self.working_status(packet.path)
        post_packet_fingerprint = self.workspace_fingerprint(
            packet.path,
            reviewed_head,
        )
        if (
            post_packet_branch != packet.branch
            or post_packet_branch_head != reviewed_head
            or post_packet_head != reviewed_head
            or post_packet_status
            or post_packet_fingerprint != reviewed_fingerprint
        ):
            raise GitOperationError(
                "reviewed packet identity changed during integration"
            )
        return merge_head

    @staticmethod
    def _safe_component(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]
        if not normalized:
            raise ValueError("run and packet identifiers must contain a letter or number")
        return normalized

    def _branch_exists(self, root: Path, branch: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            check=False,
            shell=False,
            timeout=30,
        )
        return result.returncode == 0

    def _commit(self, root: Path, message: str) -> None:
        self._git(
            root,
            "-c",
            "user.name=MochiCode",
            "-c",
            "user.email=mochicode@local.invalid",
            "commit",
            "-m",
            message,
        )

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise GitOperationError(f"git {' '.join(args)} failed: {detail}")
        return result.stdout.strip()

    @staticmethod
    def _git_bytes(root: Path, *args: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            shell=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise GitOperationError(
                f"git {' '.join(args)} failed: {os.fsdecode(result.stderr).strip()}"
            )
        return result.stdout

    @staticmethod
    def _git_with_input(root: Path, content: bytes, *args: str) -> None:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            input=content,
            capture_output=True,
            check=False,
            shell=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise GitOperationError(
                f"git {' '.join(args)} failed: {os.fsdecode(result.stderr).strip()}"
            )
