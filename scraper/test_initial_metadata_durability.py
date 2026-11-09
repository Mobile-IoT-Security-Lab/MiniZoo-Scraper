"""Metadata and capture receipts survive processing failures and offline recovery."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import opened_app_identity as identity
import wechat_csv_scraper as scraper
from minizoo_collection import public_metadata
from test_opened_app_identity import FakeProbe, records, OPENED, QUERIED
from test_package_metadata import package


class InitialMetadataDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / 'run'
        self.source = self.root / 'input.csv'
        self.source.write_text('appid,name\n' + QUERIED + ',Requested app\n')
        self.data = package([('/app.json', {'pages': ['index'], 'appName': 'Package title'})])
        self.messages = records(self.data)

    def pull(self, paths, destination):
        destination.mkdir(parents=True)
        for index, remote in enumerate(paths, 1):
            (destination / (str(index).zfill(2) + '_' + Path(remote).name)).write_bytes(self.data)
        return [(remote, '/stage/' + Path(remote).name) for remote in paths]

    def read(self, name):
        with (self.run / name).open(newline='', encoding='utf-8') as stream:
            return list(csv.DictReader(stream))

    def run_main(self, error):
        arguments = ['scraper', '--csv', str(self.source), '--output', str(self.run),
                     '--serial', FakeProbe.serial]
        with patch('sys.argv', arguments), patch.object(scraper, 'resolve_frida', return_value='frida'), \
                patch.object(scraper, 'check_env', return_value=True), \
                patch.object(scraper, 'RuntimeProbe', return_value=FakeProbe(self.messages)), \
                patch.object(scraper, 'search_and_open_name', return_value=True), \
                patch.object(scraper, 'pull_for_app', side_effect=self.pull), \
                patch.object(scraper, 'process_acquisition', side_effect=error), \
                patch.object(scraper, 'back'), patch('sys.stdout', new=io.StringIO()):
            scraper.main()

    def receipt(self):
        path = next(self.run.glob('attempts/*/opened_app_evidence.json'))
        return path, json.loads(path.read_text())

    def test_processing_error_preserves_verified_identity_metadata_and_capture_time(self):
        self.run_main(RuntimeError('decompiler unavailable'))
        row = self.read('manifest.csv')[0]
        _, receipt = self.receipt()
        self.assertEqual(row['status'], 'ok')
        self.assertEqual(row['appid'], OPENED)
        self.assertEqual(row['name'], 'Actually opened')
        self.assertEqual(row['identity_status'], 'runtime_verified')
        self.assertEqual(row['decompilation_status'], 'error')
        self.assertIn('decompiler unavailable', row['error'])
        self.assertEqual(row['collected_at'], receipt['collection_time'])
        self.assertEqual(self.read('package_history.csv')[0]['collected_at'], receipt['collection_time'])
        self.assertEqual(self.read('package_metadata.csv')[0]['collected_at'], receipt['collection_time'])
        identity.load_verified_receipt(row, self.run)

    def test_interruption_before_final_csv_is_recovered_offline_without_duplicates(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(KeyboardInterrupt())
        self.assertEqual(self.read('manifest.csv'), [])
        _, receipt = self.receipt()
        record = json.loads(next((self.run / 'attempt_records').glob('*.json')).read_text())
        self.assertEqual(record['collected_at'], receipt['collection_time'])
        with patch.object(scraper, 'adb') as adb, patch.object(scraper, 'process_acquisition') as process:
            first = scraper.recover_output(self.run)
            second = scraper.recover_output(self.run)
            adb.assert_not_called()
            process.assert_not_called()
        self.assertEqual(first['errors'], [])
        self.assertEqual(second['errors'], [])
        self.assertEqual(len(self.read('manifest.csv')), 1)
        self.assertEqual(len(self.read('package_history.csv')), 1)
        self.assertEqual(len(self.read('package_metadata.csv')), 1)
        row = self.read('manifest.csv')[0]
        self.assertEqual(row['collected_at'], receipt['collection_time'])
        self.assertEqual(row['queried_appid'], QUERIED)
        self.assertEqual(row['appid'], OPENED)
        self.assertTrue(list(self.run.glob('manifest.before_recovery.*.csv')))

    def test_capture_to_journal_gap_is_recovered_from_receipt_and_original_context(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(KeyboardInterrupt())
        journal = next((self.run / 'attempt_records').glob('*.json'))
        original = json.loads(journal.read_text())
        context = scraper.make_manifest_record({'appid': QUERIED, 'name': 'Requested app'}, scraper.manifest_fields())
        context.update(attempt_id=original['attempt_id'], device_serial=FakeProbe.serial)
        scraper.save_attempt_record(self.run, context)
        self.assertEqual(scraper.recover_output(self.run)['errors'], [])
        row = self.read('manifest.csv')[0]
        self.assertEqual(row['appid'], OPENED)
        self.assertEqual(row['queried_appid'], QUERIED)
        self.assertTrue(row['collected_at'])

    def test_tampered_package_cannot_be_recovered_as_verified(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(KeyboardInterrupt())
        path, receipt = self.receipt()
        (path.parent / receipt['packages'][0]['local_file']).write_bytes(b'changed')
        report = scraper.recover_output(self.run)
        self.assertEqual(report['recovered_attempts'], [])
        self.assertTrue(report['errors'])
        self.assertEqual(self.read('manifest.csv'), [])

    def test_interrupted_csv_row_is_rebuilt_and_original_bytes_retained(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(KeyboardInterrupt())
        record = json.loads(next((self.run / 'attempt_records').glob('*.json')).read_text())
        fields = scraper.manifest_fields()
        with (self.run / 'manifest.csv').open('a', newline='') as stream:
            csv.writer(stream).writerow([record[k] for k in fields[:fields.index('attempt_id') + 1]])
        before = (self.run / 'manifest.csv').read_bytes()
        self.assertEqual(scraper.recover_output(self.run)['errors'], [])
        self.assertEqual(len(self.read('manifest.csv')), 1)
        self.assertTrue(any(path.read_bytes() == before for path in self.run.glob('manifest.before_recovery.*.csv')))
        identity.load_verified_receipt(self.read('manifest.csv')[0], self.run)

    def test_recovery_restores_acquisition_even_when_static_extraction_fails(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(KeyboardInterrupt())
        with patch.object(scraper, 'append_package_history', side_effect=RuntimeError('parser failure')):
            report = scraper.recover_output(self.run)
        self.assertEqual(len(report['recovered_attempts']), 1)
        self.assertIn('package extraction pending', report['errors'][0]['error'])
        identity.load_verified_receipt(self.read('manifest.csv')[0], self.run)

    def test_missing_legacy_capture_date_is_not_replaced_with_recovery_time(self):
        write_json = identity.write_json
        def legacy_write(path, value):
            if Path(path).name == 'opened_app_evidence.json':
                value.pop('collection_time')
            write_json(path, value)
        with patch.object(identity, 'write_json', side_effect=legacy_write), self.assertRaises(KeyboardInterrupt):
            self.run_main(KeyboardInterrupt())
        self.assertEqual(scraper.recover_output(self.run)['errors'], [])
        for name in ('manifest.csv', 'package_history.csv', 'package_metadata.csv'):
            self.assertEqual(self.read(name)[0]['collected_at'], '')

    def test_recovery_respects_prior_receipt_hash_pin_and_device_identity(self):
        self.run_main(RuntimeError('processing failure'))
        path, receipt = self.receipt()
        before = (self.run / 'manifest.csv').read_bytes()
        receipt['collection_time'] = '2001-01-01T00:00:00Z'
        path.write_text(json.dumps(receipt))
        report = scraper.recover_output(self.run)
        self.assertTrue(report['errors'])
        self.assertEqual(report['recovered_attempts'], [])
        self.assertEqual((self.run / 'manifest.csv').read_bytes(), before)

    def test_offline_recovery_rejects_an_active_writer_without_touching_data(self):
        self.run_main(RuntimeError('processing failure'))
        before = (self.run / 'manifest.csv').read_bytes()
        with scraper.output_lock(self.run):
            with self.assertRaisesRegex(RuntimeError, 'output is active'):
                scraper.recover_output(self.run)
        self.assertEqual((self.run / 'manifest.csv').read_bytes(), before)

    def test_recovery_rebuilds_torn_history_and_metadata_rows_from_verified_receipt(self):
        self.run_main(RuntimeError('processing failure'))
        originals = {}
        for filename, last_field in (('package_history.csv', 'attempt_id'),
                                      ('package_metadata.csv', 'query_record_json')):
            path = self.run / filename
            with path.open(newline='') as stream:
                reader = csv.DictReader(stream)
                fields, row = reader.fieldnames, next(reader)
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(fields)
                writer.writerow([row[key] for key in fields[:fields.index(last_field)]])
            originals[filename] = path.read_bytes()
        # Include attempt_id in the history tail so its original acquisition
        # can be identified even though the following columns were not flushed.
        history = self.run / 'package_history.csv'
        with history.open(newline='') as stream:
            fields = next(csv.reader(stream))
        record = self.read('manifest.csv')[0]
        content = history.read_text().rstrip('\n') + ',' + record['attempt_id'] + '\n'
        history.write_text(content)
        originals['package_history.csv'] = history.read_bytes()
        report = scraper.recover_output(self.run)
        self.assertEqual(report['errors'], [])
        for filename in originals:
            rows = self.read(filename)
            self.assertEqual(len(rows), 1)
            self.assertNotIn(None, rows[0].values())
            self.assertEqual(rows[0]['appid'], OPENED)
            self.assertTrue(any(path.read_bytes() == originals[filename]
                                for path in self.run.glob(Path(filename).stem + '.before_recovery.*.csv')))

    def test_failed_receipt_write_never_publishes_partial_json_or_overwrites_evidence(self):
        path = self.root / 'receipt.json'
        with patch.object(identity.json, 'dump', side_effect=RuntimeError('write interrupted')):
            with self.assertRaises(RuntimeError):
                identity.write_json(path, {'name': 'value'})
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.glob('.*.tmp')), [])
        with patch.object(identity.os, 'fsync', wraps=identity.os.fsync) as sync:
            identity.write_json(path, {'name': 'first'})
            self.assertGreaterEqual(sync.call_count, 2)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            identity.write_json(path, {'name': 'replacement'})
        self.assertEqual(path.read_bytes(), before)

    def test_category_projection_is_receipt_owned_not_query_owned(self):
        self.assertIn('category', identity.METADATA_FIELDS)
        rec = scraper.make_manifest_record({'appid': QUERIED, 'category': 'Wrong query category'}, scraper.manifest_fields())
        evidence = self.root / 'evidence.json'
        evidence.write_text('{}')
        receipt = {'appid': OPENED, 'observed_at': '2026-11-09T10:00:00Z',
                   'collection_time': '2026-11-09T10:00:01Z', 'packages': [],
                   'metadata': {'appid': OPENED, 'name': 'Actual', 'category': 'Tools > Weather',
                                'developer': 'Actual developer'}, 'coverage': 'complete', 'coverage_limits': []}
        scraper.apply_opened_collection(rec, evidence, receipt, self.root)
        self.assertEqual(rec['category'], 'Tools > Weather')
        self.assertEqual(rec['developer'], 'Actual developer')
        self.assertEqual(rec['collected_at'], receipt['collection_time'])

    def test_verified_live_category_and_developer_reach_all_initial_outputs(self):
        for item in self.messages:
            item['metadata_schema'] = public_metadata.SCHEMA
        fields = {'raw_dynamic_info': ('dynamicInfo', '{"NewCategories":[{"first":"生活服务","second":"共享服务"}]}'),
                  'raw_register_source': ('registerSource', '{"RegisterBody":"Actual Developer"}'),
                  'description': ('signature', 'Observed description'),
                  'icon_url': ('brandIconURL', 'https://wx.qlogo.cn/mmhead/example/0')}
        self.messages.append({
            'kind': 'runtime-public-metadata', 'metadata_schema': public_metadata.SCHEMA,
            'appid': OPENED, 'attempt_id': 'attempt', 'process_id': 42,
            'metadata_class': public_metadata.CLASS,
            'identity_field': {'field': 'field_appId', 'value': OPENED},
            'fields': {key: value for key, (_, value) in fields.items()},
            'provenance': {key: {'field': 'field_' + column, 'value': value}
                           for key, (column, value) in fields.items()}})
        self.run_main(RuntimeError('decompiler unavailable'))
        for name in ('manifest.csv', 'package_history.csv', 'package_metadata.csv'):
            row = self.read(name)[0]
            self.assertEqual(row['category'], '生活服务 / 共享服务')
            self.assertEqual(row['developer'], 'Actual Developer')
        _, receipt = self.receipt()
        self.assertEqual(receipt['public_metadata_status']['status'], 'complete')
        identity.load_verified_receipt(self.read('manifest.csv')[0], self.run)


if __name__ == '__main__':
    unittest.main()
