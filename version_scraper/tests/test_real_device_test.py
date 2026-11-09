import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from real_device_test import verify_run
from store import sha256, write_json


class VerifySavedRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.output = self.base / 'output'
        self.run = self.output / 'live_tests' / 'smoke-abcdef123456'
        self.root = self.run / 'test'
        self.root.mkdir(parents=True)
        self.source = self.base / 'source.csv'
        self.source.write_text('appid,name,previous_app_version\nwx0123456789abcdef,Test,1\n', encoding='utf-8')
        self.work = self.base / 'apps.csv'
        self.work.write_bytes(self.source.read_bytes())
        self.device = {'name': 'test', 'udid': '123', 'system_port': 8201, 'enabled': True, 'account': 'shared'}
        self.config = {'source_csv': str(self.source), 'work_csv': str(self.work), 'output_dir': str(self.output),
                       'version_column': 'previous_app_version', 'version_kind': 'integer',
                       'devices': [self.device], 'evidence_command': ['adapter'], 'decompiler_command': ['decompiler']}
        self.result = {'appid': 'wx0123456789abcdef', 'name': 'Test', 'baseline': '1', 'row_index': 0,
                       'device': self.device, 'attempt_id': '1' * 32, 'opened_at': '2026-11-02T10:00:00+00:00',
                       'packages': [], 'identity_verified': False, 'version_verified': False,
                       'decompilation_verified': False, 'status': 'no_new_cache_files'}
        self.before = {key: sha256(self.config[key]) for key in ('source_csv', 'work_csv')}
        write_json(self.root / 'result.json', self.result)
        write_json(self.root / 'packages.json', [])
        write_json(self.run / 'report.json', {'results': [self.result], 'csv_before': self.before})

    def adapter(self, command, values, log):
        self.assertEqual(values['collection_dir'], self.root / 'collection')
        package = values['collection_dir'] / 'active.wxapkg'
        package.write_bytes(b'actual cached bytes')
        remote = '/data/data/com.tencent.mm/MicroMsg/appbrand/pkg/active.wxapkg'
        item = {'local_path': str(package), 'remote_path': remote, 'sha256': sha256(package), 'bytes': package.stat().st_size}
        write_json(values['packages_json'], [item])
        write_json(values['output'], {'appid': self.result['appid'], 'attempt_id': self.result['attempt_id'],
                                     'source': 'mock runtime', 'package_binding': 'runtime_metadata',
                                     'active_loaded_release': True, 'version': '2', 'version_kind': 'integer',
                                     'version_source': 'mock loaded release', 'coverage': 'partial',
                                     'coverage_limits': ['Subpackages not loaded'],
                                     'packages': [{'remote_path': remote, 'sha256': item['sha256'], 'role': 'main', 'loaded': True}]})

    def test_saved_run_reloads_cached_packages_without_search_or_csv_changes(self):
        with patch('real_device_test.run_adapter', side_effect=self.adapter), \
             patch('real_device_test.decompile', return_value=[{'status': 'verified'}]) as decompile, \
             patch('real_device_test.VisualPhone', side_effect=AssertionError('No UI on resume')):
            code = verify_run(self.config, [self.device], self.run)
        result = json.loads((self.root / 'result.json').read_text())
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['version_comparison'], 'newer')
        self.assertEqual(len(decompile.call_args.args[0]), 1)
        self.assertTrue(result['identity_verified'])
        self.assertTrue(result['decompilation_verified'])
        self.assertEqual(result['attempt_id'], '1' * 32)
        self.assertEqual(self.before, {key: sha256(self.config[key]) for key in self.before})
        backup = next(self.root.glob('result.before-verify-*.json'))
        self.assertEqual(json.loads(backup.read_text()), self.result)

    def test_wrong_source_row_is_rejected_before_adapter(self):
        self.result['appid'] = 'wx1111111111111111'
        write_json(self.root / 'result.json', self.result)
        with patch('real_device_test.run_adapter') as adapter:
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                verify_run(self.config, [self.device], self.run)
        adapter.assert_not_called()

    def test_outside_run_and_modified_csv_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'requires an existing'):
            verify_run(self.config, [self.device], self.base)
        self.work.write_text(self.work.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'CSV files differ'):
            verify_run(self.config, [self.device], self.run)

    def test_missing_adapter_remains_unverified(self):
        self.config['evidence_command'] = []
        with patch('real_device_test.VisualPhone', side_effect=AssertionError('No UI')):
            self.assertEqual(verify_run(self.config, [self.device], self.run), 2)
        result = json.loads((self.root / 'result.json').read_text())
        self.assertFalse(result['identity_verified'])
        self.assertEqual(result['status'], 'no_new_cache_files')


if __name__ == '__main__':
    unittest.main()
