from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .state import exclusive_file_lock


def _canonical(record: dict[str, Any]) -> bytes:
    content = {key: value for key, value in record.items() if key != "record_hash"}
    return json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _record_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record)).hexdigest()


class EvidenceLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            ok, reason = self.verify()
            if not ok:
                raise ValueError(f"ledger verification failed before append: {reason}")
            rows = self._read_rows()
            stored = dict(record)
            stored["seq"] = len(rows) + 1
            stored["previous_hash"] = rows[-1]["record_hash"] if rows else None
            stored["record_hash"] = _record_hash(stored)
            line = json.dumps(stored, sort_keys=True, separators=(",", ":")) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            return stored

    def verify(self) -> tuple[bool, str]:
        try:
            rows = self._read_rows()
        except (json.JSONDecodeError, ValueError) as error:
            return False, f"invalid ledger data: {error}"
        previous_hash: str | None = None
        for index, row in enumerate(rows, start=1):
            if row.get("seq") != index:
                return False, f"record {index} has an invalid sequence"
            if row.get("previous_hash") != previous_hash:
                return False, f"record {index} has an invalid previous hash"
            if row.get("record_hash") != _record_hash(row):
                return False, f"record {index} hash mismatch"
            previous_hash = str(row["record_hash"])
        return True, f"{len(rows)} records verified"

    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._read_rows())

    def _read_rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} is not an object")
            rows.append(value)
        return rows
