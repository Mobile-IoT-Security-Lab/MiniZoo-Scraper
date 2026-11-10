import csv
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'version_scraper'), str(ROOT / 'scraper'),
                str(ROOT / 'MiniZooCollectionCore')]
from minizoo_collection.native_privacy import (
    NativePrivacyError, NativePrivacyRateLimit, NativePrivacySecurity,
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phone = load('public_phone', 'version_scraper/phone.py')
initial = load('public_initial', 'scraper/wechat_csv_scraper.py')


class CollectorPrivacyHooks(unittest.TestCase):
    def phone(self):
        value = phone.Phone.__new__(phone.Phone)
        value.config = {'output_dir': 'collection_results/test'}
        value.device = {'udid': 'test-phone'}
        value.pacer = Mock()
        value.call = Mock(return_value='closed')
        return value

    def test_version_closes_after_known_consent_fallback(self):
        value = self.phone()
        value.config['adb'] = 'custom-adb'
        with patch.object(phone, 'privacy_before_close', return_value={'outcome': 'close_only'}) as guard:
            self.assertEqual(value.mobile('terminateApp', {'appId': 'com.tencent.mm'}), 'closed')
        self.assertEqual(guard.call_args.args, ('test-phone', 'collection_results/test'))
        self.assertEqual(guard.call_args.kwargs['hierarchy_source'], value.source)
        self.assertEqual(guard.call_args.kwargs['adb'], 'custom-adb')
        guard.call_args.kwargs['check_stop']()
        value.pacer.wait.assert_called_once_with(0)
        value.call.assert_called_once()

    def test_version_preserves_existing_safety_error_types_without_close(self):
        for error, expected in ((NativePrivacyError('unknown'), phone.ScreenUnavailable),
                                (NativePrivacySecurity('verification'), phone.Suspended),
                                (NativePrivacyRateLimit('limited', 60), phone.RateLimit)):
            value = self.phone()
            with self.subTest(error=error), patch.object(phone, 'privacy_before_close', side_effect=error):
                with self.assertRaises(expected) as caught:
                    value.mobile('terminateApp', {'appId': 'com.tencent.mm'})
                value.call.assert_not_called()
                if isinstance(caught.exception, phone.RateLimit):
                    self.assertEqual(caught.exception.retry_after, 60)

    def test_activation_and_other_apps_do_not_accept_wechat_consent(self):
        value = self.phone()
        with patch.object(phone, 'privacy_before_close') as guard:
            value.mobile('activateApp', {'appId': 'com.tencent.mm'})
            value.mobile('terminateApp', {'appId': 'example.app'})
            guard.assert_not_called()
        self.assertEqual(value.call.call_count, 2)

    def test_initial_closes_after_known_failed_consent(self):
        with patch.object(initial, 'ADB_SERIAL', 'test-phone'), \
             patch.object(initial, 'COLLECTION_OUTPUT', Path('collection_results/test')), \
             patch.object(initial, 'privacy_before_close', return_value={'outcome': 'close_only'}) as guard, \
             patch.object(initial, 'shell') as shell, patch.object(initial.time, 'sleep'):
            initial.force_stop_wechat()
        guard.assert_called_once_with('test-phone', Path('collection_results/test'))
        shell.assert_called_once_with('am force-stop com.tencent.mm', check=False)

    def test_initial_refusal_does_not_close_phone(self):
        with patch.object(initial, 'COLLECTION_OUTPUT', Path('collection_results/test')), \
             patch.object(initial, 'privacy_before_close', side_effect=NativePrivacyError('bad hash')), \
             patch.object(initial, 'shell') as shell:
            with self.assertRaises(NativePrivacyError):
                initial.force_stop_wechat()
        shell.assert_not_called()

    def test_initial_records_safety_failure_then_stops_before_next_row_or_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input.csv'
            source.write_text('name\nFirst app\nSecond app\n')
            output = root / 'result'
            arguments = ['collector', '--csv', str(source), '--output', str(output),
                         '--serial', 'test-phone', '--open-method', 'search', '--limit', '2']
            with patch.object(sys, 'argv', arguments), \
                 patch.object(initial, 'resolve_frida', return_value='frida'), \
                 patch.object(initial, 'check_env', return_value=True), \
                 patch.object(initial, 'RuntimeProbe'), \
                 patch.object(initial, 'search_and_open_name', side_effect=NativePrivacySecurity('verification')) as search, \
                 patch.object(initial, 'back') as back:
                with self.assertRaises(NativePrivacySecurity):
                    initial.main()
            search.assert_called_once()
            back.assert_not_called()
            with (output / 'manifest.csv').open() as stream:
                records = list(csv.DictReader(stream))
            self.assertEqual(len(records), 1)
            self.assertIn('verification', records[0]['error'])
            self.assertNotEqual(records[0]['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
