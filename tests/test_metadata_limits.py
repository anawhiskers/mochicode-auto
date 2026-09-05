import json
from pathlib import Path
import unittest


class MetadataLimitsTests(unittest.TestCase):
    def test_plugin_starter_prompts_fit_host_limit(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root/'.codex-plugin/plugin.json').read_text(encoding='utf-8'))
        for prompt in manifest['interface']['defaultPrompt']:
            with self.subTest(prompt=prompt):
                self.assertGreater(len(prompt), 0)
                self.assertLessEqual(len(prompt), 128)


if __name__ == '__main__':
    unittest.main()
