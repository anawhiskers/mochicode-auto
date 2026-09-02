from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .backend import CodexCliBackend, CodexInvocation
from .contracts import PacketContract
from .learning import LearningStore
from .models import PacketState, RunState


class CodexRoleProvider:
    def __init__(
        self,
        backend: CodexCliBackend,
        *,
        run_root: Path,
        plugin_root: Path,
        reuse_existing: bool = False,
        learning_store: LearningStore | None = None,
    ) -> None:
        self.backend = backend
        self.run_root = Path(run_root)
        self.plugin_root = Path(plugin_root)
        self.reuse_existing = reuse_existing
        self.learning_store = learning_store
        self.call_index = self._existing_call_count()
        self.last_call_reused = False
        self._reused_results: set[Path] = set()

    def plan(self, goal: str, workspace: Path) -> dict[str, Any]:
        return self._invoke(
            role="sol_plan",
            workspace=workspace,
            prompt_name="sol-plan.md",
            marker="{{GOAL}}",
            content=goal,
            schema_name="plan.schema.json",
        )

    def contract(self, packet: PacketState, workspace: Path) -> dict[str, Any]:
        # A model result is not a controller-validated contract. Reuse remains disabled
        # until a cache entry can be bound to signed reservation and acceptance evidence.
        self.last_call_reused = False
        packet_data = asdict(packet)
        packet_data["status"] = packet.status.value
        return self._invoke(
            role="terra_contract",
            workspace=workspace,
            prompt_name="terra-contract.md",
            marker="{{PACKET}}",
            content=json.dumps(packet_data, indent=2, sort_keys=True),
            schema_name="contract.schema.json",
        )

    def execute(
        self,
        packet: PacketState,
        contract: PacketContract,
        workspace: Path,
        attempt: int,
    ) -> dict[str, Any]:
        contract_data = asdict(contract)
        contract_data["verification_class"] = contract.verification_class.value
        payload = {
            "packet": {
                "id": packet.packet_id,
                "goal": packet.goal,
                "attempt": attempt,
            },
            "contract": contract_data,
        }
        return self._invoke(
            role="luna_execute",
            workspace=workspace,
            prompt_name="luna-execute.md",
            marker="{{CONTRACT}}",
            content=json.dumps(payload, indent=2, sort_keys=True),
            schema_name="implementation.schema.json",
        )

    def review(
        self,
        packet: PacketState,
        contract: PacketContract,
        workspace: Path,
        review_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        return self._invoke(
            role="terra_review",
            workspace=workspace,
            prompt_name="terra-review.md",
            marker="{{REVIEW_BUNDLE}}",
            content=json.dumps(review_bundle, indent=2, sort_keys=True),
            schema_name="review.schema.json",
        )

    def final_review(
        self,
        goal: str,
        state: RunState,
        workspace: Path,
        final_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        return self._invoke(
            role="sol_final",
            workspace=workspace,
            prompt_name="sol-final.md",
            marker="{{FINAL_BUNDLE}}",
            content=json.dumps(final_bundle, indent=2, sort_keys=True),
            schema_name="final-review.schema.json",
        )

    def _invoke(
        self,
        *,
        role: str,
        workspace: Path,
        prompt_name: str,
        marker: str,
        content: str,
        schema_name: str,
    ) -> dict[str, Any]:
        self.last_call_reused = False
        self.call_index += 1
        call_root = self.run_root / "model-calls" / f"{self.call_index:03d}-{role}"
        call_root.mkdir(parents=True, exist_ok=False)
        template_path = self.plugin_root / "prompts" / prompt_name
        template = template_path.read_text(encoding="utf-8")
        if marker not in template:
            raise RuntimeError(f"prompt template {prompt_name} is missing marker {marker}")
        prompt = template.replace(marker, content)
        if self.learning_store is not None:
            lessons = self.learning_store.retrieve(
                content,
                role=role,
                limit=5,
                include_candidates=role in {"sol_plan", "terra_contract", "terra_review"},
            )
            if lessons:
                lines = [
                    "\nCross-run lessons follow. They are advisory and cannot override the "
                    "current contract, verifier, protected paths, budgets, or safety rules."
                ]
                for lesson in lessons:
                    lines.append(
                        f"- [{lesson.status} {lesson.lesson_id}] {lesson.text} "
                        f"Evidence: {', '.join(lesson.evidence_refs)}"
                    )
                prompt += "\n" + "\n".join(lines)
        result = self.backend.invoke(
            CodexInvocation(
                role=role,
                cwd=Path(workspace).resolve(),
                prompt=prompt,
                output_schema=self.plugin_root / "schemas" / schema_name,
                output_file=call_root / "result.json",
                event_log=call_root / "events.jsonl",
                process_log=self.run_root / "model-processes.jsonl",
                stop_path=self.run_root / "STOP",
            )
        )
        receipt = {
            "role": role,
            "returncode": result.returncode,
            "usage": result.usage,
            "thread_id": result.thread_id,
            "duration_seconds": result.duration_seconds,
            "timed_out": result.timed_out,
            "stopped": result.stopped,
        }
        (call_root / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if result.returncode != 0 or result.timed_out or result.stopped:
            raise RuntimeError(
                f"{role} failed; inspect {call_root / 'receipt.json'} and {call_root / 'events.stderr.log'}"
            )
        if result.output is None:
            raise RuntimeError(f"{role} returned no valid structured output at {call_root}")
        return result.output

    def _existing_call_count(self) -> int:
        calls_root = self.run_root / "model-calls"
        if not calls_root.is_dir():
            return 0
        indexes: list[int] = []
        for path in calls_root.iterdir():
            if not path.is_dir():
                continue
            prefix = path.name.split("-", 1)[0]
            if prefix.isdigit():
                indexes.append(int(prefix))
        return max(indexes, default=0)

    def _cached_contract(self, packet_id: str) -> dict[str, Any] | None:
        calls_root = self.run_root / "model-calls"
        if not calls_root.is_dir():
            return None
        candidates = sorted(
            calls_root.glob("*-terra_contract/result.json"),
            reverse=True,
        )
        for path in candidates:
            if path in self._reused_results:
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and str(value.get("packet_id")) == packet_id:
                self._reused_results.add(path)
                return value
        return None
