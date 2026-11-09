"""Device isolation regression tests; no ADB commands reach real devices."""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import wechat_csv_scraper as scraper


class DeviceIsolationTests(unittest.TestCase):
    def test_adb_routes_to_explicit_device(self):
        with patch.object(scraper, 'ADB_SERIAL', 'TEST_DEVICE_A'), patch.object(scraper, 'run') as run:
            scraper.adb(['shell', 'id'], check=False, timeout=7)
            run.assert_called_once_with(
                ['adb', '-s', 'TEST_DEVICE_A', 'shell', 'id'], check=False, timeout=7
            )

    def test_default_device_keeps_adb_ambiguity_protection(self):
        with patch.object(scraper, 'ADB_SERIAL', ''), patch.object(scraper, 'run') as run:
            scraper.adb(['get-state'], check=False)
            run.assert_called_once_with(['adb', 'get-state'], check=False, timeout=None)

    def test_offline_or_ambiguous_device_never_reaches_root_commands(self):
        for state in ('', 'offline', 'unauthorized'):
            with self.subTest(state=state), patch.object(scraper, 'adb', return_value=subprocess.CompletedProcess([], 1, state)), patch.object(scraper, 'shell') as shell:
                with self.assertRaisesRegex(RuntimeError, 'not connected/authorized'):
                    scraper.check_env()
                shell.assert_not_called()

    def test_prepare_only_preserves_metadata_without_device_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'input.csv'
            fields = ['appid', 'name', 'category', 'rating', 'description']
            row = dict(appid='wx_test_fixture', name='测试', category='fixture', rating='4.2', description='test only')
            with source.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            args = ['wechat_csv_scraper.py', '--csv', str(source), '--output', str(root / 'out'), '--prepare-only', '--serial', 'TEST_DEVICE_A']
            with patch.object(sys, 'argv', args), patch.object(scraper, 'ADB_SERIAL', ''), patch.object(scraper, 'adb') as adb, patch.object(scraper.urllib.request, 'urlopen') as request:
                scraper.main()
                adb.assert_not_called()
                request.assert_not_called()
            with (root / 'out' / 'manifest.csv').open(encoding='utf-8', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            context = json.loads(rows[0]['query_metadata_json'])
            for key, value in row.items():
                self.assertEqual(context[key], value)
                self.assertEqual(rows[0][key], '')
            self.assertEqual(rows[0]['device_serial'], 'TEST_DEVICE_A')
            self.assertEqual(rows[0]['status'], 'prepared')

    def test_missing_root_fails_preflight(self):
        with patch.object(scraper, 'adb', return_value=subprocess.CompletedProcess([], 0, 'device\n')), patch.object(scraper, 'shell', return_value=subprocess.CompletedProcess([], 0, 'uid=2000(shell)')):
            with self.assertRaisesRegex(RuntimeError, 'Root not available'):
                scraper.check_env()

    def test_missing_keyboard_is_not_silently_accepted(self):
        responses = [subprocess.CompletedProcess([], 0, 'uid=0(root)'), subprocess.CompletedProcess([], 0, '')]
        with patch.object(scraper, 'adb', return_value=subprocess.CompletedProcess([], 0, 'device\n')), patch.object(scraper, 'shell', side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, 'ADBKeyboard is not installed'):
                scraper.check_env()


if __name__ == '__main__':
    unittest.main()
