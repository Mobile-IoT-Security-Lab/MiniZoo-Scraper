"""Offline tests: never scrape, schedule jobs, or access cloud storage."""
import os
import unittest
from unittest.mock import patch


class AutomationConfigurationTests(unittest.TestCase):
    def test_disjoint_failure_first_shards(self):
        from automation.wechat_config import shard_missing
        rows = [
            {'appid': 'wx0000000000000001', 'is_scraped': '0'},
            {'appid': 'wx0000000000000002', 'is_scraped': '0', 'errors_seen': 'failed'},
            {'appid': 'wx0000000000000003', 'is_scraped': '1'},
            {'appid': 'wx0000000000000004', 'is_scraped': '0'},
            {'appid': 'wx0000000000000002', 'is_scraped': '0'},
        ]
        shards = shard_missing(rows, ['TEST_A', 'TEST_B'])
        self.assertEqual([r['appid'] for r in shards['TEST_A']], ['wx0000000000000002', 'wx0000000000000004'])
        self.assertEqual([r['appid'] for r in shards['TEST_B']], ['wx0000000000000001'])

    def test_device_configuration_rejects_duplicates_and_unsafe_names(self):
        from automation.wechat_config import parse_devices
        for value in ('A,A', '../A,B', 'A B', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_devices(value)
        self.assertEqual(parse_devices('TEST_A, TEST_B'), ['TEST_A', 'TEST_B'])

    def test_defaults_are_repository_relative_and_cron_is_opt_in(self):
        from automation.wechat_config import Settings
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        from pathlib import Path
        from automation import wechat_config
        self.assertEqual(settings.root, Path(wechat_config.__file__).resolve().parent.parent)
        self.assertEqual(settings.state, settings.root.parent / 'collection_results' / 'automation')
        self.assertEqual(settings.data, settings.root.parent / 'collection_results' / 'initial')


class AutomationSafetyTests(unittest.TestCase):
    def test_startup_failure_terminates_already_started_workers(self):
        from automation import wechat_dual_scrape_supervisor as supervisor
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        worker = Mock(pid=12345)
        with TemporaryDirectory() as temp:
            run = Path(temp).resolve()
            for device in ['TEST_A', 'TEST_B']:
                (run / f'device_{device}').mkdir()
                (run / f'input_{device}.csv').write_text('appid,name\nwx0000000000000001,Demo\n')
            with patch.object(supervisor, 'DEVICES', ['TEST_A', 'TEST_B']), patch.object(supervisor.subprocess, 'Popen', side_effect=[worker, RuntimeError('spawn failed')]), patch.object(supervisor, 'terminate_workers') as terminate:
                with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
                    supervisor.start_workers(run)
                terminate.assert_called_once_with({'TEST_A': worker})

    def test_merge_rejects_invalid_appid_and_escaped_package(self):
        import csv
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from automation import wechat_active_merge as merge
        for appid, rel in [('wx123', 'packages/test.wxapkg'), ('wx0000000000000001', '../../outside.wxapkg')]:
            with self.subTest(appid=appid, rel=rel), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                run = root / 'active_runs' / 'run'
                device = run / 'device_TEST'
                device.mkdir(parents=True)
                marker = root / 'marker'
                marker.write_text(str(run))
                with (device / 'manifest.csv').open('w') as output:
                    writer = csv.DictWriter(output, fieldnames=['appid', 'local_files'])
                    writer.writeheader()
                    writer.writerow({'appid': appid, 'local_files': rel})
                with patch.object(merge, 'ROOT', root), patch.object(merge, 'SETTINGS', type('Settings', (), {'data': root})()), patch.object(merge, 'OUT', root / 'canonical'), patch.object(merge, 'ACTIVE_MARKER', marker):
                    with self.assertRaises(ValueError):
                        merge.merge_active()


class SupervisorFinalizationTests(unittest.TestCase):
    def test_monitor_error_retains_source_after_successful_final_rebuild(self):
        from contextlib import ExitStack
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from automation import wechat_dual_scrape_supervisor as supervisor
        error = OSError('monitor read failed')
        with TemporaryDirectory() as temp, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'WECHAT_CLEANUP_VERIFIED_RUN': '1'}))
            stack.enter_context(patch.object(supervisor, 'SETTINGS', Mock()))
            stack.enter_context(patch.object(supervisor, 'DEVICES', []))
            stack.enter_context(patch.object(supervisor, 'prepare_run', return_value=Path(temp).resolve()))
            stack.enter_context(patch.object(supervisor, 'wake_devices'))
            stack.enter_context(patch.object(supervisor, 'start_workers', return_value={}))
            stack.enter_context(patch.object(supervisor, 'read_events', side_effect=error))
            stack.enter_context(patch.object(supervisor, 'terminate_workers'))
            merge = stack.enter_context(patch.object(supervisor, 'merge_once'))
            stack.enter_context(patch.object(supervisor, 'dedupe_canonical', return_value={}))
            rebuild = stack.enter_context(patch.object(supervisor, 'final_catalogue_rebuild'))
            clean = stack.enter_context(patch.object(supervisor, 'clean_junk'))
            stack.enter_context(patch.object(supervisor.signal, 'signal'))
            with self.assertRaises(OSError) as raised:
                supervisor.main()
            self.assertIs(raised.exception, error)
            merge.assert_called_once_with('final')
            rebuild.assert_called_once_with('interrupted_or_monitor_error')
            clean.assert_not_called()

    def test_source_retention_on_success_or_failed_rebuild(self):
        from contextlib import ExitStack
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from automation import wechat_dual_scrape_supervisor as supervisor
        for cleanup, error in [('', None), ('1', RuntimeError('rebuild failed'))]:
            with self.subTest(cleanup=cleanup), TemporaryDirectory() as temp, ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'WECHAT_CLEANUP_VERIFIED_RUN': cleanup}))
                settings = Mock()
                stack.enter_context(patch.object(supervisor, 'SETTINGS', settings))
                stack.enter_context(patch.object(supervisor, 'DEVICES', []))
                stack.enter_context(patch.object(supervisor, 'prepare_run', return_value=Path(temp).resolve()))
                stack.enter_context(patch.object(supervisor, 'wake_devices'))
                stack.enter_context(patch.object(supervisor, 'start_workers', return_value={}))
                stack.enter_context(patch.object(supervisor, 'read_events', return_value=[]))
                terminate = stack.enter_context(patch.object(supervisor, 'terminate_workers'))
                stack.enter_context(patch.object(supervisor, 'merge_once'))
                stack.enter_context(patch.object(supervisor, 'dedupe_canonical', return_value={}))
                stack.enter_context(patch.object(supervisor, 'final_catalogue_rebuild', side_effect=error))
                clean = stack.enter_context(patch.object(supervisor, 'clean_junk'))
                stack.enter_context(patch.object(supervisor.signal, 'signal'))
                if error:
                    with self.assertRaisesRegex(RuntimeError, 'rebuild failed'):
                        supervisor.main()
                else:
                    self.assertEqual(supervisor.main(), 0)
                terminate.assert_called_once_with({})
                clean.assert_not_called()


class ProcessGroupSafetyTests(unittest.TestCase):
    def test_exited_leader_still_signals_group_and_reaps_child(self):
        from automation import wechat_dual_scrape_supervisor as supervisor
        from unittest.mock import Mock
        worker = Mock(pid=12345)
        worker.poll.return_value = 0
        with patch.object(supervisor.os, 'killpg') as killpg, patch.object(supervisor.time, 'sleep'):
            supervisor.terminate_workers({'TEST': worker})
        self.assertEqual(killpg.call_count, 2)
        worker.wait.assert_called_once_with(timeout=30)

    def test_second_input_read_failure_stops_first_worker(self):
        from automation import wechat_dual_scrape_supervisor as supervisor
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        worker = Mock(pid=12345)
        with TemporaryDirectory() as temp:
            run = Path(temp).resolve()
            (run / 'device_TEST_A').mkdir()
            (run / 'device_TEST_B').mkdir()  # Both outputs pass fresh-output preflight.
            with patch.object(supervisor, 'DEVICES', ['TEST_A', 'TEST_B']), patch.object(supervisor, 'read_csv', side_effect=[[{'appid': 'wx0000000000000001'}], OSError('read failed')]), patch.object(supervisor.subprocess, 'Popen', return_value=worker), patch.object(supervisor, 'terminate_workers') as terminate:
                with self.assertRaisesRegex(OSError, 'read failed'):
                    supervisor.start_workers(run)
                terminate.assert_called_once_with({'TEST_A': worker})


class CanonicalRebuildTests(unittest.TestCase):
    def test_search_query_is_not_a_category(self):
        import build_final_miniapp_csvs as builder
        apps = {}
        appid = 'wx0000000000000001'
        builder.add_source(apps, {'appid': appid, 'source_query': 'tickets'}, 'search')
        self.assertEqual(apps[appid]['category'], '')
        self.assertEqual(apps[appid]['source_query'], 'tickets')
        builder.add_source(apps, {'appid': appid, 'category': 'Travel'}, 'public_category')
        self.assertEqual(apps[appid]['category'], 'Travel')

    def test_package_modification_time_does_not_become_collection_time(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import build_final_miniapp_csvs as builder
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / 'wx0000000000000001' / 'test.wxapkg'
            package.parent.mkdir()
            package.write_bytes(b'package fixture')
            with patch.object(builder, 'OUT', root), patch.object(builder, 'ROOT', root):
                records = builder.canonical_package_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['collected_at'], '')

    def test_rebuild_preserves_existing_package_provenance(self):
        import csv
        import io
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import build_final_miniapp_csvs as builder
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            out = root / 'collection_results' / 'all_mini_apps'
            out.mkdir(parents=True)
            prior = {'appid': 'wx0000000000000001', 'name': 'Demo',
                     'remote_sources': '/android/package.wxapkg', 'device_serials': 'TEST_DEVICE',
                     'source_runs': 'original_run', 'source_files': 'original_file',
                     'first_collected_at': '2020-01-01T00:00:00+00:00',
                     'last_collected_at': '2020-02-01T00:00:00+00:00',
                     'metadata_sources': 'original_catalog', 'raw_json': '{"demo": true}'}
            builder.write_csv(out / 'mini_app_scraping_status.csv', list(prior), [prior])
            package = out / prior['appid'] / 'test.wxapkg'
            package.parent.mkdir()
            package.write_bytes(b'fixture bytes')
            package.rename(package.with_name(builder.local_sha256(package) + '.wxapkg'))
            with patch.object(builder, 'ROOT', root), patch.object(builder, 'OUT', out), patch.object(builder, 'DB', root / 'absent.db'), patch('sys.stdout', new=io.StringIO()):
                builder.main()
                builder.main()
            with (out / 'mini_app_scraping_status.csv').open() as source:
                row = next(csv.DictReader(source))
            for field in ('remote_sources', 'device_serials', 'source_runs', 'source_files', 'metadata_sources'):
                self.assertIn(prior[field], row[field])
            self.assertEqual(row['first_collected_at'], prior['first_collected_at'])
            self.assertEqual(row['raw_json'], prior['raw_json'])
            self.assertEqual(row['is_scraped'], '1')

    def test_rebuild_preserves_catalog_and_failed_attempts_without_database(self):
        import csv
        import io
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import build_final_miniapp_csvs as builder
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            out = root / 'collection_results' / 'all_mini_apps'
            out.mkdir(parents=True)
            builder.write_csv(out / 'all_mini_apps_total.csv', ['appid', 'name'], [{'appid': 'wx0000000000000001', 'name': 'Demo'}])
            run = out.parent / 'active_runs' / 'test' / 'device_TEST'
            run.mkdir(parents=True)
            builder.write_csv(run / 'manifest.csv', ['appid', 'status', 'error'], [{'appid': 'wx0000000000000001', 'status': 'error', 'error': 'offline'}])
            with patch.object(builder, 'ROOT', root), patch.object(builder, 'OUT', out), patch.object(builder, 'DB', root / 'absent.db'), patch('sys.stdout', new=io.StringIO()):
                builder.main()
                builder.main()  # Metadata and failure state also survive repeated builds.
            with (out / 'mini_app_scraping_status.csv').open() as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['appid'], 'wx0000000000000001')
            self.assertEqual(rows[0]['is_scraped'], '0')
            self.assertEqual(rows[0]['statuses_seen'], 'error')
            self.assertEqual(rows[0]['errors_seen'], 'offline')


if __name__ == '__main__':
    unittest.main()
