import copy
from pathlib import Path
import sys
import os
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import context_trial

BASE = b'model="gpt-6-astra"\nmodel_reasoning_effort="high"\nmodel_context_window=1000000\nmodel_auto_compact_token_limit=850000\n[features]\nmulti_agent=true\n'


class ContextTrialTests(unittest.TestCase):
    def test_only_requested_setting_changes(self):
        for raw in (BASE, BASE.replace(b'\n', b'\r\n'), b'\xef\xbb\xbf'+BASE,
                    BASE + b'[mcp_servers.fixture]\nurl="https://example.invalid/private"\n',
                    b'', b'[features]\nmulti_agent=true'):
            with self.subTest(raw=raw):
                expected = copy.deepcopy(tomllib.loads(raw.decode('utf-8-sig')))
                expected.setdefault('features', {})['context_management'] = {'experimental_mode': True}
                result = context_trial.candidate(raw)
                self.assertTrue(result.startswith(raw))
                self.assertEqual(tomllib.loads(result.decode('utf-8-sig')), expected)
                self.assertEqual(context_trial.candidate(result), result)

    def test_conflicting_forms_and_broken_toml_refuse(self):
        for raw in (b'[features]\ncontext_management=false\n',
                    b'[features.context_management]\nexperimental_mode=false\n',
                    b'features=true', b'features={multi_agent=true}',
                    b'[features]\n[features]\n', b'key="unterminated'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                context_trial.candidate(raw)

    @unittest.skipUnless(os.name == 'nt', 'Windows file-lock implementation')
    def test_apply_backup_and_idempotence(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.toml'
            config.write_bytes(BASE)
            with patch.object(context_trial, 'assert_closed'):
                result = context_trial.apply(config)
                self.assertEqual(Path(result['backup']).read_bytes(), BASE)
                self.assertEqual(config.read_bytes(), context_trial.candidate(BASE))
                self.assertEqual(context_trial.apply(config)['status'], 'already_enabled')

    def test_open_app_refuses_without_modification(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.toml'
            config.write_bytes(BASE)
            with patch.object(context_trial, 'assert_closed', side_effect=ValueError('open')):
                with self.assertRaises(ValueError):
                    context_trial.apply(config)
            self.assertEqual(config.read_bytes(), BASE)
            self.assertFalse((config.parent/'backups').exists())

    def test_stale_preview_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.toml'
            config.write_bytes(BASE)
            with self.assertRaises(ValueError):
                context_trial.apply(config, '0'*64)
            self.assertEqual(config.read_bytes(), BASE)

    @unittest.skipUnless(os.name == 'nt', 'Windows file-lock implementation')
    def test_concurrent_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.toml'
            config.write_bytes(BASE)
            calls = []
            def concurrent():
                calls.append(True)
                if len(calls) == 1:
                    config.write_bytes(BASE + b'# concurrent change\n')
            with patch.object(context_trial, 'assert_closed', side_effect=concurrent):
                with self.assertRaises(ValueError):
                    context_trial.apply(config)
            self.assertEqual(config.read_bytes(), BASE + b'# concurrent change\n')

    @unittest.skipUnless(os.name == 'nt', 'Windows file-lock implementation')
    def test_lock_rejects_an_actual_competing_process(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.toml'
            config.write_bytes(BASE)
            with context_trial.exclusive_config(config):
                result = subprocess.run([sys.executable, '-c',
                    'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"overwritten")', str(config)],
                    capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), BASE)


if __name__ == '__main__':
    unittest.main()
