"""Runtime public metadata survives verified capture and never borrows query data."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minizoo_collection import artifacts, public_metadata as metadata, runtime
import test_runtime_evidence as fixtures
APPID = fixtures.APPID


class LivePublicMetadataTests(unittest.TestCase):
    def setUp(self):
        # Reuse real package/runtime shape; mutations below exercise the public
        # API rather than copying the decoder's implementation.
        self.fixture = fixtures.RuntimeEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.configure_inventory([])
        self.records = self.fixture.records
        for record in self.records:
            record['metadata_schema'] = metadata.SCHEMA
        self.public = {
            'kind': 'runtime-public-metadata', 'metadata_schema': metadata.SCHEMA,
            'appid': APPID, 'attempt_id': 'test-attempt', 'process_id': 1234,
            'metadata_class': metadata.CLASS,
            'identity_field': {'field': 'field_appId', 'value': APPID},
            'fields': {}, 'provenance': {}}
        self.set_field('name', 'Observed app', 'nickname')
        self.set_field('description', 'Observed description', 'signature')
        self.set_field('icon_url', 'https://wx.qlogo.cn/mmhead/example/0', 'brandIconURL')
        # Shape and taxonomy transcribed from preserved 2026-11-09 WeChat
        # metadata_categories_probe-b7db3a9…; registration body anonymized.
        self.set_field('raw_dynamic_info', json.dumps({
            'NewSetting': {'MaxLocalstorageSize': 10},
            'NewCategories': [{'first': '生活服务', 'second': '共享服务'}],
            'auto_preconnect_request_domain_list': []}), 'dynamicInfo')
        self.set_field('raw_register_source', json.dumps({'RegisterBody': 'Fixture Company'}), 'registerSource')
        self.records.append(self.public)

    def set_field(self, key, value, column):
        self.public['fields'][key] = value
        self.public['provenance'][key] = {'field': 'field_' + column, 'value': value}

    def bind(self):
        return runtime.select_opened_binding(self.records, 'test-attempt')

    def test_service_taxonomy_and_registration_body_are_recovered(self):
        binding = self.bind()
        self.assertEqual(binding['metadata']['category'], '生活服务 / 共享服务')
        self.assertEqual(binding['metadata']['developer'], 'Fixture Company')
        self.assertEqual(binding['public_metadata_status']['status'], 'complete')

    def test_each_taxonomy_branch_and_optional_third_level_survive(self):
        self.set_field('raw_dynamic_info', json.dumps({'NewCategories': [
            {'first': 'A', 'second': 'B', 'third': 'C'}, {'first': 'D', 'second': 'E'},
            {'first': 'A', 'second': 'B', 'third': 'C'}]}), 'dynamicInfo')
        self.assertEqual(self.bind()['metadata']['category'], 'A / B / C; D / E')

    def test_keyword_and_platform_numeric_code_are_never_category(self):
        self.set_field('raw_dynamic_info', '{"AppServiceType":4,"keyword":"shopping"}', 'dynamicInfo')
        self.public['fields']['category'] = 'shopping'
        self.assertNotIn('category', self.bind()['metadata'])

    def test_malformed_json_wrong_types_or_oversized_data_are_explicit(self):
        for raw in ('{', '[]', '{"NewCategories":"shopping"}',
                    '{"NewCategories":[{"first":17,"second":"B"}]}', ' ' * 65537):
            with self.subTest(raw=raw[:60]):
                self.set_field('raw_dynamic_info', raw, 'dynamicInfo')
                binding = self.bind()
                self.assertNotIn('category', binding['metadata'])
                self.assertIn('category', binding['public_metadata_status']['invalid_fields'])

    def test_wrong_attempt_appid_process_and_raw_provenance_are_rejected(self):
        original = copy.deepcopy(self.public)
        for key, value in (('appid', 'wx0000000000000000'), ('attempt_id', 'other'), ('process_id', 999)):
            with self.subTest(key=key):
                self.public[key] = value
                self.assertNotIn('category', self.bind()['metadata'])
                self.public.clear(); self.public.update(copy.deepcopy(original))
        self.public['identity_field']['value'] = 'wx0000000000000000'
        self.assertNotIn('developer', self.bind()['metadata'])
        self.public.clear(); self.public.update(copy.deepcopy(original))
        self.public['provenance']['raw_dynamic_info']['value'] = '{}'
        binding = self.bind()
        self.assertNotIn('category', binding['metadata'])
        self.assertIn('category', binding['public_metadata_status']['invalid_fields'])

    def test_conflicting_sources_do_not_choose_arbitrarily(self):
        other = copy.deepcopy(self.public)
        raw = '{"RegisterBody":"Different Company"}'
        other['fields']['raw_register_source'] = raw
        other['provenance']['raw_register_source']['value'] = raw
        self.records.append(other)
        binding = self.bind()
        self.assertNotIn('developer', binding['metadata'])
        self.assertIn('developer', binding['public_metadata_status']['conflicting_fields'])

    def database_records(self):
        common = {'appid': APPID, 'attempt_id': 'test-attempt', 'process_id': 1234,
                  'metadata_schema': metadata.SCHEMA,
                  'database_path': '/data/user/0/com.tencent.mm/MicroMsg/' + 'a' * 32 + '/AppBrandComm.db',
                  'table': 'WxaAttributesTable'}
        row = {metadata.COLUMNS[key]: value for key, value in self.public['fields'].items() if key in metadata.COLUMNS}
        return [dict(common, kind='runtime-cached-public-metadata', row=dict(row, appId=APPID)),
                dict(common, kind='metadata-public-table-complete', count=1, truncated=False)]

    def test_completed_exact_appid_cache_query_recovers_missing_heap_objects(self):
        self.records.remove(self.public)
        self.records.extend(self.database_records())
        self.assertEqual(self.bind()['metadata']['category'], '生活服务 / 共享服务')
        self.assertEqual(self.bind()['public_metadata_status']['status'], 'complete')

    def test_cache_query_wrong_identity_path_table_and_incomplete_read_are_rejected(self):
        self.records.remove(self.public)
        for change in ('appid', 'path', 'table', 'truncated', 'count', 'missing_done'):
            with self.subTest(change=change):
                row, done = self.database_records()
                if change == 'appid': row['row']['appId'] = 'wx0000000000000000'
                if change == 'path': row['database_path'] = done['database_path'] = '/sdcard/other.db'
                if change == 'table': row['table'] = done['table'] = 'message'
                if change == 'truncated': done['truncated'] = True
                if change == 'count': done['count'] = 2
                messages = [row] if change == 'missing_done' else [row, done]
                binding = runtime.select_opened_binding(self.records + messages, 'test-attempt')
                self.assertNotIn('category', binding['metadata'])

    def test_main_process_cache_is_bound_to_observed_runtime_context(self):
        self.records.remove(self.public)
        row, done = self.database_records()
        for record in (row, done):
            record.update(metadata_source='wechat_main_cache', runtime_process_id=1234,
                          process_id=5678, metadata_process_name='com.tencent.mm')
        self.records.extend([row, done])
        self.assertEqual(self.bind()['metadata']['developer'], 'Fixture Company')
        for key, wrong in (('runtime_process_id', 7777), ('metadata_process_name', 'other.app'),
                           ('appid', 'wx0000000000000000'), ('attempt_id', 'other')):
            original = row[key]
            row[key] = wrong
            self.assertNotIn('developer', self.bind()['metadata'])
            row[key] = original

    def test_nested_appinfo_identity_is_checked(self):
        self.set_field('raw_app_info', '{"Appid":"wx0000000000000000"}', 'appInfo')
        self.assertNotIn('developer', self.bind()['metadata'])
        self.records.remove(self.public)
        row, done = self.database_records()
        row['row']['appInfo'] = '{"Appid":"wx0000000000000000"}'
        self.records.extend([row, done])
        self.assertNotIn('category', self.bind()['metadata'])

    def test_actual_main_cache_script_queries_only_bound_app_and_public_table(self):
        source = Path(runtime.__file__).with_name('cached_public_metadata_probe.js').read_text()
        row = {metadata.COLUMNS[key]: value for key, value in self.public['fields'].items()}
        row.update(appId=APPID, appInfo=json.dumps({'Appid': APPID}))
        # Execute the actual JavaScript against a tiny read-only Java bridge
        # fixture. No device, Frida session, network request or mutation.
        harness = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let messages=[], queries=[];
const fields=['appId','nickname','signature','brandIconURL','registerSource','dynamicInfo','appInfo'];
const db={getPath:()=>'/data/user/0/com.tencent.mm/MicroMsg/'+'a'.repeat(32)+'/AppBrandComm.db',
 rawQuery:{overload:()=>({call:(self, sql)=>{
  queries.push(sql); let read=false;
  return {moveToNext:()=>{if(read)return false;read=true;return true;},
   isNull:i=>input.row[fields[i]]==null,getString:i=>input.row[fields[i]],close:()=>{}};
 }})}};
const context={globalThis:{CACHED_METADATA_CONTEXT:{appid:input.appid,attempt_id:'test-attempt',runtime_process_id:1234}},
 Process:{id:5678},send:m=>messages.push(m),setImmediate:f=>f(),
 Java:{perform:f=>f(),use:n=>({currentProcessName:()=> 'com.tencent.mm'}),
 choose:(n,h)=>{h.onMatch(db);h.onComplete();}}};
vm.runInNewContext(input.source,context);
process.stdout.write(JSON.stringify({messages,queries}));
"""
        process = subprocess.run(['node', '-e', harness], input=json.dumps({
            'source': source, 'row': row, 'appid': APPID}), text=True, capture_output=True, check=True)
        result = json.loads(process.stdout)
        self.assertEqual(len(result['queries']), 2)
        self.assertTrue(all("WHERE appId='" + APPID + "' LIMIT 3" in q for q in result['queries']))
        self.records.remove(self.public)
        self.records.extend(result['messages'])
        self.assertEqual(self.bind()['metadata']['category'], '生活服务 / 共享服务')
        self.assertEqual(self.bind()['public_metadata_status']['status'], 'complete')

    def test_observe_automatically_queries_existing_main_process_for_missing_fields(self):
        cache = self.database_records()
        for item in cache:
            item.update(metadata_source='wechat_main_cache', runtime_process_id=1234,
                        process_id=5678, metadata_process_name='com.tencent.mm')
        self.public['fields'].pop('raw_dynamic_info')
        self.public['fields'].pop('raw_register_source')
        base = self.records + [{'kind': 'metadata-probe-complete'}]
        cached = cache + [{'kind': 'metadata-probe-complete'}]
        calls = []
        def completed(args, **kwargs):
            calls.append(args)
            if 'pidof' in args:
                self.assertEqual(args[-1], 'com.tencent.mm')
                return subprocess.CompletedProcess(args, 0, '5678\n', '')
            pid = args[args.index('-p') + 1]
            if pid == '5678':
                script = Path(args[-1]).read_text()
                self.assertIn(APPID, script)
                self.assertIn('"runtime_process_id": 1234', script)
            output = base if pid == '1234' else cached
            return subprocess.CompletedProcess(args, 0,
                '\n'.join(json.dumps({'payload': item}) for item in output), '')
        probe = runtime.RuntimeProbe('fixture-device', 'frida')
        directory = self.fixture.root / 'automatic-probe'
        with patch.object(runtime.subprocess, 'run', side_effect=completed):
            observed = probe.observe('test-attempt', directory, pid=1234)
        binding = runtime.select_opened_binding(observed, 'test-attempt')
        self.assertEqual(binding['metadata']['category'], '生活服务 / 共享服务')
        self.assertEqual(binding['public_metadata_status']['status'], 'complete')
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads((directory / 'messages.json').read_text()), observed)
        self.assertTrue((directory / 'main-metadata-5678.log').is_file())

    def capture(self, after_change=None):
        records = self.records
        class Probe:
            serial = 'fixture-device'
            calls = 0
            def observe(self, attempt, directory, pid=None):
                result = copy.deepcopy(records)
                if self.calls and after_change:
                    after_change(result)
                self.calls += 1
                runtime.write_json(Path(directory) / 'messages.json', result)
                return result
        evidence, packages = runtime.collect_runtime(
            Probe(), 'test-attempt', self.fixture.root,
            capture_fn=lambda *args, **kwargs: [self.fixture.package], cache_fn=lambda *args: {})
        path = self.fixture.root / 'evidence.json'
        runtime.write_json(path, evidence)
        return evidence, packages, path

    def test_capture_replay_and_validator_preserve_verified_fields_and_status(self):
        evidence, packages, path = self.capture()
        verified = artifacts.validate_evidence(path, APPID, packages, 'test-attempt')
        self.assertEqual(verified['observed_metadata']['developer'], 'Fixture Company')
        self.assertEqual(verified['public_metadata_status']['status'], 'complete')
        evidence['observed_metadata']['category'] = 'invented'
        runtime.write_json(path, evidence)
        with self.assertRaisesRegex(artifacts.ArtifactError, 'observations failed replay'):
            artifacts.validate_evidence(path, APPID, packages, 'test-attempt')

    def test_after_observation_can_fill_missing_before_field(self):
        saved = self.public['fields'].pop('raw_dynamic_info')
        def later(records):
            records[-1]['fields']['raw_dynamic_info'] = saved
        evidence, _, _ = self.capture(later)
        self.assertEqual(evidence['observed_metadata']['category'], '生活服务 / 共享服务')

    def test_before_after_disagreement_stays_missing(self):
        def later(records):
            record = records[-1]
            record['fields']['raw_register_source'] = '{"RegisterBody":"Other Company"}'
            record['provenance']['raw_register_source']['value'] = record['fields']['raw_register_source']
        evidence, _, _ = self.capture(later)
        self.assertNotIn('developer', evidence['observed_metadata'])
        self.assertIn('developer', evidence['public_metadata_status']['conflicting_fields'])

    def test_legacy_observations_keep_original_projection(self):
        for record in self.records:
            record.pop('metadata_schema', None)
        binding = self.bind()
        self.assertEqual(binding['metadata']['name'], 'Observed app')
        self.assertNotIn('category', binding['metadata'])
        evidence, packages, path = self.capture()
        self.assertNotIn('public_metadata_status', evidence)
        artifacts.validate_evidence(path, APPID, packages, 'test-attempt')

    def test_status_tampering_is_rejected_even_when_values_unchanged(self):
        evidence, packages, path = self.capture()
        evidence['public_metadata_status']['missing_fields'] = ['category']
        runtime.write_json(path, evidence)
        with self.assertRaisesRegex(artifacts.ArtifactError, 'metadata status'):
            artifacts.validate_evidence(path, APPID, packages, 'test-attempt')

    def test_observation_file_is_synced_before_atomic_replace(self):
        path = self.fixture.root / 'durable.json'
        with patch.object(runtime.os, 'fsync', wraps=runtime.os.fsync) as sync:
            runtime.write_json(path, {'observed': True})
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(json.loads(path.read_text()), {'observed': True})


if __name__ == '__main__':
    unittest.main()
