from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODEL_OUTPUT_SCHEMA_NAMES = {
    "contract.schema.json",
    "final-review.schema.json",
    "implementation.schema.json",
    "plan.schema.json",
    "review.schema.json",
}


UNSUPPORTED_CODEX_SCHEMA_KEYS = {
    "uniqueItems",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
}


def find_unsupported(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in UNSUPPORTED_CODEX_SCHEMA_KEYS:
                findings.append(f"{path}.{key}")
            findings.extend(find_unsupported(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(find_unsupported(child, f"{path}[{index}]") )
    return findings


class OutputSchemaTests(unittest.TestCase):
    def test_every_model_output_schema_uses_the_codex_supported_subset(self) -> None:
        schema_root = PLUGIN_ROOT / "schemas"
        schemas = sorted(schema_root / name for name in MODEL_OUTPUT_SCHEMA_NAMES)
        self.assertTrue(all(schema.is_file() for schema in schemas))
        findings: list[str] = []
        for schema in schemas:
            value = json.loads(schema.read_text(encoding="utf-8"))
            findings.extend(f"{schema.name}: {item}" for item in find_unsupported(value))

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
