"""Exercise independent public commands, input preservation and exit status."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import minizoo

ROOT = Path(__file__).resolve().parents[1]


class CommandTests(unittest.TestCase):
    def command(self, *args, cwd):
        # No external executable is reachable by these help/inspection commands.
        return subprocess.run([sys.executable, str(ROOT / 'minizoo.py'), *args], cwd=cwd,
                              env=dict(os.environ, PATH=''), text=True, capture_output=True, timeout=15)

    def test_every_command_starts_independently_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in [*minizoo.COMMANDS, 'test']:
                with self.subTest(command=name):
                    result = self.command(name, '--help', cwd=directory)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout.lower())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_no_arguments_or_unknown_module_never_start_a_run(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.command(cwd=directory).returncode, 0)
            self.assertEqual(self.command('not-a-module', cwd=directory).returncode, 2)
            for name in minizoo.COMMANDS:
                self.assertEqual(self.command(name, cwd=directory).returncode, 0, name)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_inspect_preserves_original_and_reports_malformed_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory).resolve() / 'fixture.wxapkg'
            name = b'/app.json';body = b'{"appid":"wx0123456789abcdef","appName":"Fixture"}'
            index_size = 4 + 4 + len(name) + 8
            index = struct.pack('>II', 1, len(name)) + name + struct.pack('>II', 14+index_size, len(body))
            original = b'\xbe' + struct.pack('>III', 0, index_size, len(body)) + b'\xed' + index + body
            package.write_bytes(original)
            result = self.command('inspect', str(package), cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['package']['sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(report['package']['host_appids'], ['wx0123456789abcdef'])
            self.assertEqual(package.read_bytes(), original)
            package.write_bytes(b'invalid')
            self.assertEqual(self.command('inspect', str(package), cwd=directory).returncode, 1)
            self.assertEqual(package.read_bytes(), b'invalid')

    def test_test_runner_isolates_modules_and_reports_failures(self):
        with patch.object(minizoo.subprocess, 'run', side_effect=[SimpleNamespace(returncode=1),
                          *[SimpleNamespace(returncode=0) for _ in range(len(minizoo.SUITES)-1)]]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(minizoo.main(['test']), 1)
        self.assertEqual(run.call_count, len(minizoo.SUITES))
        with patch.object(minizoo.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(minizoo.main(['test', 'core']), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['cwd'], ROOT / 'MiniZooCollectionCore')


if __name__ == '__main__':
    unittest.main()
