from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CLI = PLUGIN_ROOT / "scripts" / "mochicode.py"


class CliSmokeTests(unittest.TestCase):
    def test_cli_help_is_available(self) -> None:
        self.assertTrue(CLI.is_file(), "the portable controller entrypoint must exist")

        result = subprocess.run(
            [sys.executable, str(CLI), "--help"],
            cwd=PLUGIN_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MochiCode", result.stdout)
        self.assertIn("doctor", result.stdout)
        self.assertIn("demo", result.stdout)


if __name__ == "__main__":
    unittest.main()
