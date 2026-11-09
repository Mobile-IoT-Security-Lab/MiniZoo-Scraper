"""Exercise the distributable config and operation without historical datasets."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from main import load_config
from store import Store


ROOT = Path(__file__).resolve().parents[1]


class PortableSetupTests(unittest.TestCase):
    def test_bundled_core_and_decompiler_resolve_outside_repository(self):
        environment = dict(os.environ, PYTHONPATH=str(ROOT))
        environment.pop('MINIZOO_CORE_PATH', None)
        environment.pop('WXAPP_TOOL_ROOT', None)
        script = '''import json, artifacts
from minizoo_collection.tools.decompile_wxapkg import DEFAULT_TOOL
print(json.dumps([artifacts.__file__, DEFAULT_TOOL]))'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', script], cwd=directory,
                                    env=environment, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        core, decompiler = map(Path, json.loads(result.stdout))
        self.assertEqual(core.parent, ROOT.parent / 'MiniZooCollectionCore' / 'minizoo_collection')
        self.assertEqual(decompiler, ROOT / 'vendor' / 'wxappUnpacker')

    def test_template_adapter_is_usable_from_collection_directory(self):
        config = load_config(ROOT / 'config.example.json')
        command = config['decompiler_command']
        self.assertTrue(Path(command[1]).is_absolute())
        self.assertTrue(Path(command[command.index('--tool-root') + 1]).is_absolute())
        self.assertIn('{package}', command)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(command[:2] + ['--help'], cwd=directory,
                                    text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--package', result.stdout)




if __name__ == '__main__':
    unittest.main()
