from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


class ProtectedInputChanged(RuntimeError):
    pass


class InvalidProtectedPattern(ValueError):
    pass


def expand_protected_pattern(root: Path, pattern: str) -> tuple[Path, ...]:
    root = Path(root).resolve()
    normalized = str(pattern).replace("\\", "/")
    if "::" in normalized:
        raise InvalidProtectedPattern(
            f"protected pattern is a test node selector: {pattern}"
        )
    parts = tuple(part for part in normalized.split("/") if part)
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in parts
    ):
        raise InvalidProtectedPattern(
            f"protected pattern must stay inside the repository: {pattern}"
        )

    files: set[Path] = set()
    try:
        for path in root.glob(normalized):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise InvalidProtectedPattern(
                    f"protected pattern resolves outside the repository: {pattern}"
                )
            files.add(resolved)
    except InvalidProtectedPattern:
        raise
    except (OSError, ValueError) as error:
        raise InvalidProtectedPattern(
            f"protected pattern cannot be expanded: {pattern}"
        ) from error
    return tuple(sorted(files))


def hash_protected(root: Path, patterns: tuple[str, ...]) -> dict[str, str]:
    root = Path(root).resolve()
    files: set[Path] = set()
    for pattern in patterns:
        files.update(expand_protected_pattern(root, pattern))
    result: dict[str, str] = {}
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def assert_protected_unchanged(
    before: dict[str, str],
    after: dict[str, str],
) -> None:
    changed = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    if changed:
        raise ProtectedInputChanged(
            "protected measurement inputs changed: " + ", ".join(changed)
        )


def attempt_fingerprint(diff_text: str, verifier_exit: int, verifier_output: str) -> str:
    def normalize(value: str) -> str:
        lines = []
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            normalized = re.sub(r"\s+", " ", line.strip())
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)

    payload = {
        "diff": normalize(diff_text),
        "verifier_exit": int(verifier_exit),
        "verifier_output": normalize(verifier_output),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
