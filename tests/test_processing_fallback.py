"""A failed reconstruction must retain the acquisition and its diagnostics."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'MiniZooCollectionCore'))
from minizoo_collection import artifacts, processing

APPID = 'wx0123456789abcdef'


class ProcessingFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        name = b'/app.json'
        body = json.dumps({'appid': APPID, 'pages': ['pages/index']}).encode()
        index_size = 4 + 4 + len(name) + 8
        self.original = (b'\xbe' + struct.pack('>III', 0, index_size, len(body))
                         + b'\xed' + struct.pack('>II', 1, len(name)) + name
                         + struct.pack('>II', 14 + index_size, len(body)) + body)
        self.path = self.root / 'package.wxapkg'
        self.path.write_bytes(self.original)
        self.package = {'local_path': str(self.path),
                        'remote_path': artifacts.CACHE_ROOT + '/example.wxapkg',
                        'sha256': hashlib.sha256(self.original).hexdigest(),
                        'bytes': len(self.original), 'role': 'main'}

    def process(self, code):
        return processing.process_acquisition(
            self.root, [self.package], appid=APPID, attempt_id='example-attempt',
            coverage={'status': 'complete'},
            decompiler_command=[sys.executable, '-B', '-c', code,
                                '{package}', '{output}'])

    def test_partial_reconstruction_keeps_package_metadata_and_partial_files(self):
        report = self.process(
            "from pathlib import Path; import sys; "
            "Path(sys.argv[2], 'partial.js').write_text('// incomplete'); "
            "print('ReferenceError: synthetic unsupported module'); sys.exit(2)")
        self.assertEqual(report['package_acquisition_status'], 'acquired')
        self.assertEqual(report['metadata_status'], 'completed')
        self.assertEqual(report['decompilation_status'], 'failed')
        self.assertEqual(report['decompiled_metadata_status'], 'not_available')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(json.loads((self.root / 'processing.json').read_text()), report)
        metadata = json.loads((self.root / 'package_metadata.json').read_text())
        self.assertEqual(metadata[0]['sha256'], self.package['sha256'])
        self.assertEqual(metadata[0]['observed_appid'], APPID)
        self.assertEqual(len(list((self.root / 'decompiled').glob('*/partial.js'))), 1)
        logs = list((self.root / 'logs').glob('*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('ReferenceError', logs[0].read_text())
        self.assertEqual(list((self.root / 'checkpoints').glob('decompile-*.json')), [])

    def test_decompiler_timeout_keeps_verified_input_and_records_failure(self):
        with patch.object(artifacts.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired('example-decompiler', 300)):
            report = self.process('pass')
        self.assertEqual(report['package_acquisition_status'], 'acquired')
        self.assertEqual(report['decompilation_status'], 'failed')
        self.assertIn('timed out', report['decompilation_error'])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_failed_decompiler_cannot_hide_modified_input(self):
        with self.assertRaisesRegex(artifacts.ArtifactError, 'Original package changed'):
            self.process("from pathlib import Path; import sys; "
                         "Path(sys.argv[1]).write_bytes(b'changed'); sys.exit(2)")
        report = json.loads((self.root / 'processing.json').read_text())
        self.assertEqual(report['package_acquisition_status'], 'integrity_failed')
        self.assertEqual(report['decompilation_status'], 'failed')


if __name__ == '__main__':
    unittest.main()
