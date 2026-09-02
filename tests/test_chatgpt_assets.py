from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = REPO_ROOT / "chatgpt"
ASSETS = (
    ASSET_ROOT / "CUSTOM-INSTRUCTIONS-COMPACT.txt",
    ASSET_ROOT / "NEW-PC-HANDOFF-PROMPT.md",
    ASSET_ROOT / "APPLY-AND-VERIFY.md",
)

# The account field is intentionally kept below the commonly supported limit.
CUSTOM_INSTRUCTIONS_MAX_CHARS = 1_500
HANDOFF_MAX_CHARS = 4_000
GUIDE_MAX_CHARS = 2_500

PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:[\\/]"),
    re.compile(r"(?i)(?:^|[\s`])/(?:Users|home|private|var)/"),
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)\b(?:password|secret|token)\s*[=:]\s*\S+"),
)
SENSITIVE_DETAIL_TERMS = (
    "deadname",
    "gender identity",
    "hormone replacement",
    "medical history",
    "transgender",
)


def _asset_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in ASSETS)


class ChatGPTAssetTests(unittest.TestCase):
    def test_expected_assets_exist(self) -> None:
        for path in ASSETS:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())

    def test_assets_contain_no_private_paths_secrets_or_sensitive_details(self) -> None:
        text = _asset_text()
        for pattern in (*PRIVATE_PATH_PATTERNS, *SECRET_PATTERNS):
            self.assertIsNone(pattern.search(text), pattern.pattern)
        lowered = text.casefold()
        for term in SENSITIVE_DETAIL_TERMS:
            self.assertNotIn(term, lowered, term)

    def test_assets_fit_their_size_limits(self) -> None:
        limits = {
            ASSET_ROOT / "CUSTOM-INSTRUCTIONS-COMPACT.txt": CUSTOM_INSTRUCTIONS_MAX_CHARS,
            ASSET_ROOT / "NEW-PC-HANDOFF-PROMPT.md": HANDOFF_MAX_CHARS,
            ASSET_ROOT / "APPLY-AND-VERIFY.md": GUIDE_MAX_CHARS,
        }
        for path, limit in limits.items():
            with self.subTest(path=path.name):
                self.assertLessEqual(len(path.read_text(encoding="utf-8")), limit)

    def test_assets_preserve_account_local_boundary_and_state_limits(self) -> None:
        handoff = (ASSET_ROOT / "NEW-PC-HANDOFF-PROMPT.md").read_text(encoding="utf-8")
        guide = (ASSET_ROOT / "APPLY-AND-VERIFY.md").read_text(encoding="utf-8")
        for text in (handoff, guide):
            lowered = text.casefold()
            self.assertIn("account-level", lowered)
            self.assertIn("local codex", lowered)
            self.assertIn("login state", lowered)
            self.assertIn("plugin installation state", lowered)
            self.assertIn("connector state", lowered)
            self.assertIn("browser permissions", lowered)
        self.assertIn("one writer", handoff.casefold())
        self.assertIn("evidence", handoff.casefold())
        self.assertIn("Never claim", (ASSET_ROOT / "CUSTOM-INSTRUCTIONS-COMPACT.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
