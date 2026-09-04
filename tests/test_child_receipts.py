from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from mochicode_core.child_receipts import ChildReceiptError, validate_child_receipt


def valid_receipt() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "COMPLETED",
        "role": "luna_execute",
        "model": "gpt-5.6-luna",
        "effort": "medium",
        "owned_paths": ["src/feature.py", "tests/test_feature.py"],
        "acceptance_evidence": [
            {
                "criterion_id": "criterion-1",
                "status": "PASS",
                "evidence": "tests/test_feature.py passed",
            }
        ],
        "commands": [
            {"argv": ["python", "-m", "unittest", "tests.test_feature"], "exit_code": 0}
        ],
        "evidence_locations": ["receipts/test-feature.json"],
        "unresolved_risks": [],
        "stop_reason": "completed",
        "telemetry": {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 20,
            "reasoning_output_tokens": 10,
            "tool_calls": 3,
            "retry_count": 0,
            "duration_ms": 1200,
            "termination_reason": "completed",
        },
    }


class ChildReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "child-completion.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(cls.schema)

    def test_valid_completion_receipt_passes(self) -> None:
        payload = valid_receipt()

        result = validate_child_receipt(
            payload,
            allowed_paths=("src/*.py", "tests/*.py"),
            required_criteria=("criterion-1",),
        )

        self.assertIs(result, payload)
        self.assertEqual(list(self.schema_validator.iter_errors(payload)), [])

    def test_completed_receipt_requires_a_real_command(self) -> None:
        payload = valid_receipt()
        payload["commands"] = []

        self.assertEqual(list(self.schema_validator.iter_errors(payload)), [])
        with self.assertRaisesRegex(ChildReceiptError, "at least one command"):
            validate_child_receipt(
                payload,
                allowed_paths=("src/*.py", "tests/*.py"),
                required_criteria=("criterion-1",),
            )

    def test_missing_acceptance_evidence_is_rejected(self) -> None:
        payload = valid_receipt()
        payload["acceptance_evidence"] = []

        with self.assertRaisesRegex(ChildReceiptError, "every required acceptance"):
            validate_child_receipt(
                payload,
                allowed_paths=("src/*.py", "tests/*.py"),
                required_criteria=("criterion-1",),
            )

    def test_fake_success_with_failing_exit_code_is_rejected(self) -> None:
        payload = valid_receipt()
        payload["commands"] = [
            {"argv": ["python", "-m", "unittest", "tests.test_feature"], "exit_code": 1}
        ]

        with self.assertRaisesRegex(ChildReceiptError, "failing exit code"):
            validate_child_receipt(
                payload,
                allowed_paths=("src/*.py", "tests/*.py"),
                required_criteria=("criterion-1",),
            )

    def test_unowned_and_traversal_paths_are_rejected(self) -> None:
        for path in ("../outside.txt", "C:/outside.txt", "other/unowned.py"):
            with self.subTest(path=path):
                payload = valid_receipt()
                payload["owned_paths"] = [path]
                with self.assertRaisesRegex(ChildReceiptError, "unsafe path|unowned path"):
                    validate_child_receipt(
                        payload,
                        allowed_paths=("src/*.py", "tests/*.py"),
                        required_criteria=("criterion-1",),
                    )

    def test_writable_globs_are_segment_aware(self) -> None:
        direct = valid_receipt()
        direct["owned_paths"] = ["src/feature.py"]
        validate_child_receipt(
            direct,
            allowed_paths=("src/*.py",),
            required_criteria=("criterion-1",),
        )

        nested = valid_receipt()
        nested["owned_paths"] = ["src/nested/feature.py"]
        with self.assertRaisesRegex(ChildReceiptError, "unowned path"):
            validate_child_receipt(
                nested,
                allowed_paths=("src/*.py",),
                required_criteria=("criterion-1",),
            )
        validate_child_receipt(
            nested,
            allowed_paths=("src/**/*.py",),
            required_criteria=("criterion-1",),
        )

        windows = valid_receipt()
        windows["owned_paths"] = ["src\\nested\\feature.py"]
        validate_child_receipt(
            windows,
            allowed_paths=("src\\**\\*.py",),
            required_criteria=("criterion-1",),
        )

    def test_truncated_or_unknown_receipt_content_is_rejected(self) -> None:
        truncated = valid_receipt()
        truncated["acceptance_evidence"][0]["evidence"] = "[mochicode output limit exceeded]"
        with self.assertRaisesRegex(ChildReceiptError, "truncation marker"):
            validate_child_receipt(
                truncated,
                allowed_paths=("src/*.py", "tests/*.py"),
                required_criteria=("criterion-1",),
            )

        unknown = valid_receipt()
        unknown["raw_prompt"] = "forbidden"
        with self.assertRaisesRegex(ChildReceiptError, "unsupported fields"):
            validate_child_receipt(
                unknown,
                allowed_paths=("src/*.py", "tests/*.py"),
                required_criteria=("criterion-1",),
            )

    def test_cli_validates_the_same_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            receipt_path = Path(raw) / "receipt.json"
            receipt_path.write_text(json.dumps(valid_receipt()), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(PLUGIN_ROOT / "scripts" / "mochicode.py"),
                    "child-receipt",
                    "validate",
                    "--file",
                    str(receipt_path),
                    "--allowed-path",
                    "src/*.py",
                    "--allowed-path",
                    "tests/*.py",
                    "--criterion",
                    "criterion-1",
                    "--json",
                ],
                cwd=PLUGIN_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
