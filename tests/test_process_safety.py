from __future__ import annotations

import threading
import unittest

from scripts.mochicode_core.process_safety import (
    BoundedTextCapture,
    TRUNCATION_MARKER,
    build_child_environment,
)


class ProcessSafetyTests(unittest.TestCase):
    def test_child_environment_is_allowlisted_by_default(self) -> None:
        environment = build_child_environment(
            {
                "PATH": "tools",
                "SYSTEMROOT": "windows",
                "DATABASE_URL": "must-not-pass",
                "CI_JOB_JWT": "must-not-pass",
                "KUBECONFIG": "must-not-pass",
                "ARBITRARY_SENTINEL": "must-not-pass",
            }
        )

        self.assertEqual(environment, {"PATH": "tools", "SYSTEMROOT": "windows"})

    def test_operator_can_explicitly_allow_nonsecret_toolchain_variable(self) -> None:
        environment = build_child_environment(
            {
                "PATH": "tools",
                "MOCHICODE_CHILD_ENV_ALLOWLIST": "SDK_ROOT",
                "SDK_ROOT": "C:/toolchain",
                "UNRELATED": "must-not-pass",
            }
        )

        self.assertEqual(environment, {"PATH": "tools", "SDK_ROOT": "C:/toolchain"})

    def test_overrides_replace_isolated_runtime_paths(self) -> None:
        environment = build_child_environment(
            {"PATH": "tools", "HOME": "real-home", "DATABASE_URL": "secret"},
            overrides={"HOME": "isolated-home", "TEMP": "isolated-temp"},
        )

        self.assertEqual(
            environment,
            {"PATH": "tools", "HOME": "isolated-home", "TEMP": "isolated-temp"},
        )

    def test_bounded_capture_signals_and_retains_only_the_limit(self) -> None:
        event = threading.Event()
        capture = BoundedTextCapture(8, limit_event=event)

        self.assertEqual(capture.append("abcd"), "abcd")
        self.assertEqual(capture.append("efghijkl"), "efgh")
        self.assertEqual(capture.append("ignored"), "")

        self.assertTrue(event.is_set())
        self.assertTrue(capture.truncated)
        self.assertEqual(capture.stored_bytes, 8)
        self.assertEqual(capture.text(), "abcdefgh" + TRUNCATION_MARKER)

    def test_bounded_capture_does_not_split_utf8(self) -> None:
        capture = BoundedTextCapture(5)
        capture.append("abc🙂")

        self.assertTrue(capture.truncated)
        self.assertLessEqual(capture.stored_bytes, 5)
        self.assertTrue(capture.text().startswith("abc"))


if __name__ == "__main__":
    unittest.main()
