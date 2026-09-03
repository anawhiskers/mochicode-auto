from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from .evidence import EvidenceLedger


@dataclass(frozen=True, slots=True)
class Lesson:
    lesson_id: str
    role: str
    scope: str
    text: str
    tags: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    status: str
    created_at: float
    updated_at: float
    retirement_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LessonTrial:
    lesson_id: str
    role: str
    expected: bool

    def applies_to(self, role: str) -> bool:
        return self.expected and self.role in {"*", role}


class LearningStore:
    TRIAL_FIELDS = frozenset(
        {
            "lesson_id",
            "lesson_expected",
            "lesson_applied",
            "lesson_backend",
            "model_receipt_hash",
        }
    )
    FAILURE_CLASSES = {
        "protected_input_changed",
        "permission_read_only",
        "review_missing_evidence",
        "verifier_failed",
    }
    ROLES = {"*", "sol_plan", "terra_contract", "terra_review", "luna_execute", "sol_final"}
    OUTCOME_FIELDS = frozenset(
        {
            "run_id",
            "packet_id",
            "role",
            "success",
            "failure_class",
            "fingerprint",
            "goal_hash",
            "evidence_ref",
            "lesson_id",
            "lesson_expected",
            "lesson_applied",
            "lesson_backend",
            "model_receipt_hash",
        }
    )
    REQUIRED_OUTCOME_FIELDS = frozenset(
        {"run_id", "packet_id", "role", "success", "fingerprint", "goal_hash"}
    )
    MAX_OUTCOME_STRING_LENGTH = 1000
    SAFE_OUTCOME_SCALAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
    UNSAFE_OUTCOME_MARKER = re.compile(
        r"(?i)"
        r"(?:raw[_ -]?goals?|goal[_ -]?texts?|prompt[_ -]?texts?|instructions?|"
        r"transcripts?|messages?|diffs?|std(?:out|err)|logs?|credentials?|"
        r"secrets?|tokens?|cookies?|passwords?|api[_ -]?keys?)"
    )

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.outcomes = EvidenceLedger(self.root / "outcomes.jsonl")
        self.lessons = EvidenceLedger(self.root / "lessons.jsonl")

    def record_outcome(self, **fields: Any) -> dict[str, Any]:
        if self.TRIAL_FIELDS & set(fields):
            raise ValueError("lesson trial outcomes require record_trial_outcome")
        return self._append_outcome(fields)

    def record_trial_outcome(
        self,
        *,
        receipt_path: Path,
        **fields: Any,
    ) -> dict[str, Any]:
        required = {"lesson_id", "lesson_expected", "lesson_applied"}
        if not required <= set(fields):
            raise ValueError("lesson trial outcome is missing trial fields")
        forbidden = {"lesson_backend", "model_receipt_hash"} & set(fields)
        if forbidden:
            raise ValueError("lesson trial provenance is computed from the receipt")
        receipt_hash = self._attest_trial_receipt(Path(receipt_path), fields)
        return self._append_outcome(
            {
                **fields,
                "lesson_backend": "codex",
                "model_receipt_hash": receipt_hash,
            }
        )

    def _append_outcome(self, fields: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(fields) - self.OUTCOME_FIELDS)
        if unknown:
            raise ValueError(f"unknown outcome fields: {unknown}")
        missing = sorted(self.REQUIRED_OUTCOME_FIELDS - set(fields))
        if missing:
            raise ValueError(f"outcome is missing fields: {missing}")
        self._validate_outcome_fields(fields)
        record = {
            "event": "outcome",
            "schema_version": 1,
            "created_at": time.time(),
        }
        record.update(fields)
        return self.outcomes.append(record)

    def candidate_trial(self, lesson_id: str, *, expected: bool) -> LessonTrial:
        self._require_valid()
        lesson = self.current_lessons().get(lesson_id)
        if lesson is None:
            raise KeyError(lesson_id)
        if lesson.status != "candidate":
            raise ValueError("lesson trials require a candidate lesson")
        return LessonTrial(
            lesson_id=lesson.lesson_id,
            role=lesson.role,
            expected=expected,
        )

    @classmethod
    def _validate_outcome_fields(cls, fields: dict[str, Any]) -> None:
        for field, value in fields.items():
            if field in {"success", "lesson_expected", "lesson_applied"}:
                if type(value) is not bool:
                    raise ValueError(f"outcome field '{field}' must be of type bool")
                continue
            if field == "failure_class":
                if value is None:
                    continue
                if type(value) is not str:
                    raise ValueError(
                        "outcome field 'failure_class' must be a string scalar or null"
                    )
                cls._validate_outcome_string(field, value)
                if value not in cls.FAILURE_CLASSES:
                    raise ValueError("outcome field 'failure_class' has an unsupported enum")
                continue
            if type(value) is not str:
                raise ValueError(f"outcome field '{field}' must be a string scalar")
            cls._validate_outcome_string(field, value)
        if "role" in fields:
            role = fields["role"]
            if role not in cls.ROLES:
                raise ValueError("outcome field 'role' has an unsupported enum")
        lesson_fields = {
            "lesson_id",
            "lesson_expected",
            "lesson_applied",
            "lesson_backend",
            "model_receipt_hash",
        }
        present_lesson_fields = lesson_fields & set(fields)
        if present_lesson_fields and present_lesson_fields != lesson_fields:
            raise ValueError(
                "lesson trial outcomes require lesson_id, lesson_expected, and lesson_applied"
            )

    @classmethod
    def _validate_outcome_string(cls, field: str, value: str) -> None:
        if not value:
            raise ValueError(f"outcome field '{field}' must not be empty")
        if len(value) > cls.MAX_OUTCOME_STRING_LENGTH:
            raise ValueError(
                f"outcome field '{field}' exceeds maximum string length "
                f"of {cls.MAX_OUTCOME_STRING_LENGTH}"
            )
        if cls.UNSAFE_OUTCOME_MARKER.search(value):
            raise ValueError(f"outcome field '{field}' contains an unsafe marker")
        if cls.SAFE_OUTCOME_SCALAR.fullmatch(value) is None:
            raise ValueError(f"outcome field '{field}' contains unsafe scalar content")

    def propose_recovery_lesson(
        self,
        *,
        role: str,
        scope: str,
        failure_class: str,
        tags: tuple[str, ...],
        failure_evidence: str,
        success_evidence: str,
    ) -> Lesson:
        self._require_valid()
        if not failure_evidence or not success_evidence:
            raise ValueError("recovery lessons require failure and success evidence")
        failure_record = self._known_outcome(failure_evidence, expected_success=False)
        success_record = self._known_outcome(success_evidence, expected_success=True)
        if (
            failure_record.get("run_id") != success_record.get("run_id")
            or failure_record.get("packet_id") != success_record.get("packet_id")
        ):
            raise ValueError("recovery evidence must describe one packet's failure and recovery")
        if role not in self.ROLES:
            raise ValueError(f"unsupported lesson role: {role}")
        if failure_class not in self.FAILURE_CLASSES:
            raise ValueError(f"unsupported failure class: {failure_class}")
        normalized_scope = f"{role}:{failure_class}"
        normalized_tags = (role, failure_class)
        lesson_id = "les-" + hashlib.sha256(
            f"{role}|{normalized_scope}|{failure_class}".encode("utf-8")
        ).hexdigest()[:12]
        existing = self.current_lessons().get(lesson_id)
        if existing is not None and existing.status != "retired":
            return existing
        text = self._lesson_text(role, failure_class)
        now = time.time()
        lesson = Lesson(
            lesson_id=lesson_id,
            role=role,
            scope=normalized_scope,
            text=text,
            tags=normalized_tags,
            evidence_refs=(failure_evidence, success_evidence),
            status="candidate",
            created_at=now,
            updated_at=now,
        )
        self.lessons.append(
            {
                "event": "lesson_candidate",
                "schema_version": 1,
                "lesson": asdict(lesson),
            }
        )
        return lesson

    def promote(
        self,
        lesson_id: str,
        *,
        verification_refs: tuple[str, ...],
        negative_control_refs: tuple[str, ...] = (),
        human_approved: bool = False,
    ) -> Lesson:
        self._require_valid()
        lessons = self.current_lessons()
        if lesson_id not in lessons:
            raise KeyError(lesson_id)
        current = lessons[lesson_id]
        if current.status == "retired":
            raise ValueError("retired lessons cannot be promoted without a new candidate")
        unique_refs = tuple(dict.fromkeys(ref for ref in verification_refs if ref))
        verification_records = [
            self._known_outcome(ref, expected_success=True)
            for ref in unique_refs
        ]
        new_refs = tuple(ref for ref in unique_refs if ref not in current.evidence_refs)
        new_records = [
            record
            for ref, record in zip(unique_refs, verification_records)
            if ref in new_refs
        ]
        for record in new_records:
            if (
                record.get("lesson_id") != current.lesson_id
                or record.get("lesson_expected") is not True
                or record.get("lesson_applied") is not True
                or record.get("lesson_backend") != "codex"
                or not self._is_hash(str(record.get("model_receipt_hash", "")))
            ):
                raise ValueError(
                    "positive verification evidence must bind expected lesson activation to a Codex model receipt"
                )
            self._verify_stored_trial_receipt(record)
        if not human_approved and len(new_refs) < 2:
            raise ValueError("automatic promotion requires two independent verification refs")
        if not human_approved and len({str(record.get("run_id")) for record in new_records}) < 2:
            raise ValueError("automatic promotion requires evidence from two independent runs")
        if human_approved and not new_refs:
            raise ValueError("human promotion still requires an evidence reference")
        unique_negative_refs = tuple(
            dict.fromkeys(ref for ref in negative_control_refs if ref)
        )
        if not unique_negative_refs:
            raise ValueError("lesson promotion requires a successful negative-control reference")
        negative_records = [
            self._known_outcome(ref, expected_success=True)
            for ref in unique_negative_refs
        ]
        for record in negative_records:
            if (
                record.get("lesson_id") != current.lesson_id
                or record.get("lesson_expected") is not False
                or record.get("lesson_applied") is not False
                or record.get("lesson_backend") != "codex"
                or not self._is_hash(str(record.get("model_receipt_hash", "")))
            ):
                raise ValueError(
                    "negative-control evidence must bind inactive lesson state to a Codex model receipt"
                )
            self._verify_stored_trial_receipt(record)
        positive_run_ids = {str(record.get("run_id")) for record in new_records}
        negative_run_ids = {str(record.get("run_id")) for record in negative_records}
        if positive_run_ids & negative_run_ids:
            raise ValueError("negative-control evidence must come from an independent run")
        lesson = Lesson(
            lesson_id=current.lesson_id,
            role=current.role,
            scope=current.scope,
            text=current.text,
            tags=current.tags,
            evidence_refs=tuple(
                dict.fromkeys(
                    (*current.evidence_refs, *unique_refs, *unique_negative_refs)
                )
            ),
            status="active",
            created_at=current.created_at,
            updated_at=time.time(),
        )
        self.lessons.append(
            {
                "event": "lesson_promoted",
                "schema_version": 1,
                "human_approved": human_approved,
                "verification_refs": list(unique_refs),
                "negative_control_refs": list(unique_negative_refs),
                "lesson": asdict(lesson),
            }
        )
        return lesson

    def retire(self, lesson_id: str, *, reason: str) -> Lesson:
        self._require_valid()
        current = self.current_lessons().get(lesson_id)
        if current is None:
            raise KeyError(lesson_id)
        if not reason.strip():
            raise ValueError("retirement reason must not be empty")
        lesson = Lesson(
            lesson_id=current.lesson_id,
            role=current.role,
            scope=current.scope,
            text=current.text,
            tags=current.tags,
            evidence_refs=current.evidence_refs,
            status="retired",
            created_at=current.created_at,
            updated_at=time.time(),
            retirement_reason=reason.strip(),
        )
        self.lessons.append(
            {
                "event": "lesson_retired",
                "schema_version": 1,
                "reason": reason.strip(),
                "lesson": asdict(lesson),
            }
        )
        return lesson

    def retrieve(
        self,
        query: str,
        *,
        role: str,
        limit: int = 5,
        include_candidates: bool = False,
    ) -> tuple[Lesson, ...]:
        self._require_valid()
        if limit <= 0:
            return ()
        query_tokens = self._tokens(query)
        ranked: list[tuple[int, float, str, Lesson]] = []
        for lesson in self.current_lessons().values():
            if lesson.status == "retired":
                continue
            if lesson.status == "candidate" and not include_candidates:
                continue
            lesson_tokens = self._tokens(
                " ".join((lesson.scope, lesson.text, *lesson.tags))
            )
            overlap = len(query_tokens & lesson_tokens)
            if overlap == 0 and lesson.role not in {"*", role}:
                continue
            role_score = 3 if lesson.role in {"*", role} else 0
            status_score = 2 if lesson.status == "active" else 0
            score = overlap * 10 + role_score + status_score
            ranked.append((score, lesson.updated_at, lesson.lesson_id, lesson))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return tuple(item[3] for item in ranked[:limit])

    def current_lessons(self) -> dict[str, Lesson]:
        self._require_valid()
        current: dict[str, Lesson] = {}
        for record in self.lessons.records():
            raw = record.get("lesson")
            if not isinstance(raw, dict):
                continue
            lesson = Lesson(
                lesson_id=str(raw["lesson_id"]),
                role=str(raw["role"]),
                scope=str(raw["scope"]),
                text=str(raw["text"]),
                tags=tuple(str(item) for item in raw.get("tags", [])),
                evidence_refs=tuple(str(item) for item in raw.get("evidence_refs", [])),
                status=str(raw["status"]),
                created_at=float(raw["created_at"]),
                updated_at=float(raw["updated_at"]),
                retirement_reason=(
                    None
                    if raw.get("retirement_reason") is None
                    else str(raw["retirement_reason"])
                ),
            )
            current[lesson.lesson_id] = lesson
        return current

    def verify(self) -> tuple[bool, str]:
        outcomes_ok, outcomes_reason = self.outcomes.verify()
        if not outcomes_ok:
            return False, f"outcomes: {outcomes_reason}"
        lessons_ok, lessons_reason = self.lessons.verify()
        if not lessons_ok:
            return False, f"lessons: {lessons_reason}"
        return True, f"outcomes {outcomes_reason}; lessons {lessons_reason}"

    def redacted_export(self) -> dict[str, Any]:
        self._require_valid()
        known = self._known_evidence_records()
        lessons = []
        for lesson in self.current_lessons().values():
            if lesson.status != "active":
                continue
            lessons.append(
                {
                    "lesson_id": lesson.lesson_id,
                    "role": lesson.role,
                    "text": self._redact_paths(lesson.text),
                    "evidence_refs": [
                        ref
                        for ref in lesson.evidence_refs
                        if self._is_hash(ref) and ref in known
                    ],
                    "status": lesson.status,
                    "updated_at": lesson.updated_at,
                }
            )
        lessons.sort(key=lambda item: item["lesson_id"])
        return {"schema_version": 1, "lessons": lessons}

    def latest_failed_outcome(
        self,
        *,
        run_id: str,
        packet_id: str,
    ) -> dict[str, Any] | None:
        self._require_valid()
        for record in reversed(self.outcomes.records()):
            if (
                record.get("run_id") == run_id
                and record.get("packet_id") == packet_id
                and record.get("success") is False
            ):
                return record
        return None

    def _require_valid(self) -> None:
        ok, reason = self.verify()
        if not ok:
            raise ValueError(f"learning store verification failed: {reason}")

    def _known_evidence_records(self) -> dict[str, dict[str, Any]]:
        known: dict[str, dict[str, Any]] = {}
        for ledger in (self.outcomes, self.lessons):
            for record in ledger.records():
                record_hash = str(record.get("record_hash", ""))
                if self._is_hash(record_hash):
                    known[record_hash] = record
        return known

    def _known_outcome(
        self,
        reference: str,
        *,
        expected_success: bool,
    ) -> dict[str, Any]:
        if not self._is_hash(reference):
            raise ValueError("evidence reference is not known hash-chained evidence")
        record = self._known_evidence_records().get(reference)
        if (
            record is None
            or record.get("event") != "outcome"
            or record.get("success") is not expected_success
        ):
            raise ValueError("evidence reference is not known hash-chained evidence")
        return record

    def _attest_trial_receipt(
        self,
        receipt_path: Path,
        fields: dict[str, Any],
    ) -> str:
        if not receipt_path.is_file() or receipt_path.stat().st_size > 1_000_000:
            raise ValueError("lesson trial receipt is missing or oversized")
        receipt_bytes = receipt_path.read_bytes()
        try:
            receipt = json.loads(receipt_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("lesson trial receipt is not valid JSON") from error
        self._validate_trial_receipt(receipt, fields)
        receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
        trusted_root = self.root / "trial-receipts"
        trusted_root.mkdir(parents=True, exist_ok=True)
        trusted_path = trusted_root / f"{receipt_hash}.json"
        if trusted_path.exists():
            if trusted_path.read_bytes() != receipt_bytes:
                raise ValueError("lesson trial receipt hash collision")
        else:
            trusted_path.write_bytes(receipt_bytes)
        return receipt_hash

    def _verify_stored_trial_receipt(self, record: dict[str, Any]) -> None:
        receipt_hash = str(record.get("model_receipt_hash", ""))
        if not self._is_hash(receipt_hash):
            raise ValueError("lesson trial outcome has no valid receipt hash")
        trusted_path = self.root / "trial-receipts" / f"{receipt_hash}.json"
        if not trusted_path.is_file() or trusted_path.stat().st_size > 1_000_000:
            raise ValueError("lesson trial receipt evidence is missing")
        receipt_bytes = trusted_path.read_bytes()
        if hashlib.sha256(receipt_bytes).hexdigest() != receipt_hash:
            raise ValueError("lesson trial receipt evidence was tampered")
        try:
            receipt = json.loads(receipt_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("lesson trial receipt evidence is invalid") from error
        self._validate_trial_receipt(receipt, record)

    @staticmethod
    def _validate_trial_receipt(receipt: Any, fields: dict[str, Any]) -> None:
        if not isinstance(receipt, dict):
            raise ValueError("lesson trial receipt must be an object")
        required = {
            "backend",
            "role",
            "returncode",
            "timed_out",
            "stopped",
            "lesson_trial",
        }
        if not required <= set(receipt):
            raise ValueError("lesson trial receipt is missing required evidence")
        trial = receipt.get("lesson_trial")
        if not isinstance(trial, dict) or set(trial) != {
            "lesson_id",
            "lesson_expected",
            "lesson_applied",
        }:
            raise ValueError("lesson trial receipt has invalid trial state")
        if (
            receipt.get("backend") != "codex"
            or type(receipt.get("returncode")) is not int
            or receipt.get("returncode") != 0
            or receipt.get("timed_out") is not False
            or receipt.get("stopped") is not False
            or receipt.get("role") != fields.get("role")
            or trial.get("lesson_id") != fields.get("lesson_id")
            or trial.get("lesson_expected") is not fields.get("lesson_expected")
            or trial.get("lesson_applied") is not fields.get("lesson_applied")
        ):
            raise ValueError("lesson trial receipt does not match the recorded outcome")

    @staticmethod
    def _is_hash(value: str) -> bool:
        return re.fullmatch(r"[0-9a-f]{64}", value) is not None

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 1}

    @staticmethod
    def _lesson_text(role: str, failure_class: str) -> str:
        messages = {
            "protected_input_changed": (
                "Freeze and hash protected measurement inputs before execution, then refuse the "
                "attempt if any protected byte changes."
            ),
            "permission_read_only": (
                "Verify the effective sandbox can perform the required write before launching the "
                "full role chain."
            ),
            "review_missing_evidence": (
                "Give reviewers the committed source-to-integration diff, changed paths, artifact "
                "hashes, verifier receipts, and prior finding dispositions."
            ),
            "verifier_failed": (
                "Run the exact verifier as a bare command and preserve its exit code and raw output "
                "before changing another artifact."
            ),
        }
        return messages.get(
            failure_class,
            f"Before {role} repeats similar work, inspect the verified failure and recovery receipts for {failure_class}.",
        )

    @staticmethod
    def _redact_paths(value: str) -> str:
        value = re.sub(r"(?i)\b[a-z]:\\[^\s,;]+", "<path>", value)
        value = re.sub(r"(?<!:)\/(?:[^\s/]+\/)+[^\s,;]*", "<path>", value)
        return value
