"""Offline fixtures only: no original main, devices, networks or package JS."""
import csv
import hashlib
import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('scraper', HERE/'wechat_csv_scraper.py')
scraper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scraper)


def package(members):
    index_size = 4 + sum(4 + len(n.encode()) + 8 for n, _ in members)
    offset = 14 + index_size
    index = struct.pack('>I', len(members))
    body = b''
    for name, value in members:
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        name = name.encode()
        index += struct.pack('>I', len(name)) + name + struct.pack('>II', offset, len(raw))
        offset += len(raw)
        body += raw
    return b'\xbe' + struct.pack('>III', 0, index_size, len(body)) + b'\xed' + index + body


class PackageMetadataTests(unittest.TestCase):
    def test_extracted_csv_uses_package_not_query_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root/'actual.wxapkg'
            source.write_bytes(package([('/app.json', {'appId':'wx0123456789abcdef', 'appName':'Actual', 'appVersion':7, 'developer':'Real developer', 'sdkVersion':'3.0.0'})]))
            self.assertTrue(hasattr(scraper, 'append_collected_package_metadata'), 'post-capture CSV integration missing')
            count = scraper.append_collected_package_metadata(root/'package_metadata.csv', [source], {'appid':'wxfedcba9876543210','name':'Query','app_version':'999','developer':'Query developer'}, source_run='fixture', remote_sources=['/cache/actual.wxapkg'])
            self.assertEqual(count, 1)
            with (root/'package_metadata.csv').open() as stream:
                row = list(csv.DictReader(stream))[0]
            self.assertEqual(json.loads(row['extracted_host_appids_json']), ['wx0123456789abcdef'])
            self.assertEqual(json.loads(row['extracted_names_json']), ['Actual'])
            self.assertEqual(json.loads(row['extracted_app_versions_json']), [7])
            self.assertEqual(json.loads(row['extracted_sdk_versions_json']), ['3.0.0'])
            self.assertEqual(json.loads(row['extracted_developers_json']), ['Real developer'])
            self.assertEqual(row['queried_appid'], 'wxfedcba9876543210')
            self.assertEqual(row['appid'], '')
            self.assertEqual(row['app_version'], '')
            self.assertEqual(row['package_attribution'], 'UNASSIGNED')
            self.assertEqual(row['certified_release'], 'false')
            self.assertTrue(Path(row['extraction_evidence_path']).is_file())

    def test_append_deduplicates_same_sha_provenance_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root/'actual.wxapkg'
            source.write_bytes(package([('/app.json', {'appName':'Actual'})]))
            output = root/'package_metadata.csv'
            query = {'name':'Query'}
            self.assertEqual(scraper.append_collected_package_metadata(output,[source],query,source_run='one'),1)
            evidence = root/'package_metadata_evidence'/hashlib.sha256(source.read_bytes()).hexdigest()/'summary.json'
            before = evidence.read_bytes()
            self.assertEqual(scraper.append_collected_package_metadata(output,[source],query,source_run='one'),0)
            self.assertEqual(evidence.read_bytes(),before)
            self.assertEqual(scraper.append_collected_package_metadata(output,[source],query,source_run='two'),1)
            self.assertEqual(evidence.read_bytes(),before)
            with output.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows),2)
            self.assertEqual([r['source_run'] for r in rows],['one','two'])

    def test_original_history_append_invokes_post_capture_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root/'a.wxapkg'
            source.write_bytes(package([('/plugin.json', {'appName':'Plugin','provider':'wx1111111111111111','sdkVersion':'8.0'})]))
            history = root/'package_history.csv'
            history.write_text(','.join(scraper.package_history_fields())+'\n')
            before = history.read_bytes()
            rec = {'local_files':'a.wxapkg','appid':'wx2222222222222222','name':'Query','app_version':'123','sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'remote_sources':'/cache/a.wxapkg'}
            self.assertEqual(scraper.append_package_history(history,rec,root,source_run='fixture'),1)
            self.assertTrue((root/'package_metadata.csv').is_file(),'original append history path must invoke static exporter')
            self.assertTrue(history.read_bytes().startswith(before))
            with history.open() as stream:
                row = list(csv.DictReader(stream))[0]
            self.assertEqual(row['appid'],'')
            self.assertEqual(row['app_version'],'')
            self.assertEqual(row['selection_mode'],'context_candidate_unassigned')
            with (root/'package_metadata.csv').open() as stream:
                metadata = list(csv.DictReader(stream))[0]
            self.assertEqual(json.loads(metadata['extracted_app_versions_json']),[])
            self.assertEqual(json.loads(metadata['extracted_plugin_ids_json']),['wx1111111111111111'])
            self.assertEqual(metadata['package_role'],'plugin')
            self.assertEqual(json.loads(metadata['extracted_host_appids_json']),[])

    def test_declared_sha_mismatch_is_rejected_before_csv_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root/'actual.wxapkg'
            source.write_bytes(package([('/app.json', {'appName':'Actual'})]))
            with self.assertRaisesRegex(ValueError,'package_sha256_mismatch'):
                scraper.append_collected_package_metadata(root/'out.csv',[source],{'sha256':'0'*64},source_run='fixture')
            if (root/'out.csv').exists():
                with (root/'out.csv').open() as stream:
                    self.assertEqual(list(csv.DictReader(stream)),[])

    def test_manifest_is_exclusive_and_retains_existing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root/'manifest.csv'
            target.write_bytes(b'historical,content\n')
            self.assertTrue(hasattr(scraper,'open_manifest_exclusive'),'exclusive manifest retention guard missing')
            with self.assertRaises(FileExistsError):
                scraper.open_manifest_exclusive(target)
            self.assertEqual(target.read_bytes(),b'historical,content\n')
            with scraper.open_manifest_exclusive(root/'fresh.csv') as stream:
                stream.write('new\n')
            self.assertEqual((root/'fresh.csv').read_text(),'new\n')

    def test_pull_uses_unique_stage_without_deleting_previous_stage(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def shell(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0)
            with patch.object(scraper,'shell',side_effect=shell), patch.object(scraper,'su',return_value=SimpleNamespace(returncode=0)), patch.object(scraper,'adb',return_value=SimpleNamespace(returncode=0)):
                first = scraper.pull_for_app(['/cache/a.wxapkg'],Path(tmp).resolve()/'same')
                second = scraper.pull_for_app(['/cache/a.wxapkg'],Path(tmp).resolve()/'same')
            self.assertTrue(all('rm ' not in command for command in calls),'staging must never delete existing data')
            self.assertNotEqual(first[0][1],second[0][1])

    def test_destructive_cache_flag_and_reused_output_fail_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertTrue(hasattr(scraper,'validate_retention_policy'),'preflight missing')
            with self.assertRaisesRegex(ValueError,'cache_deletion_disabled'):
                scraper.validate_retention_policy(root/'manifest.csv',True)
            (root/'manifest.csv').write_text('history')
            with self.assertRaises(FileExistsError):
                scraper.validate_retention_policy(root/'manifest.csv',False)
            self.assertEqual((root/'manifest.csv').read_text(),'history')
            self.assertIsNone(scraper.validate_retention_policy(root/'fresh.csv',False))

    def test_each_package_row_has_its_own_digest_size_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            a,b = root/'a.wxapkg',root/'b.wxapkg'
            a.write_bytes(package([('/app-service.js',b'var appId="wx1111111111111111"; var appName="Same";')]))
            b.write_bytes(package([('/app.json',{'appId':'wx2222222222222222','appName':'Same'})]))
            rec = {'appid':'wx3333333333333333','name':'Same','app_version':'999','local_files':'a.wxapkg;b.wxapkg','remote_sources':'/a;/b','package_size_bytes':'wrong','sha256':hashlib.sha256(a.read_bytes()).hexdigest()+';'+hashlib.sha256(b.read_bytes()).hexdigest()}
            scraper.append_collected_package_metadata(root/'out.csv',[a,b],rec,source_run='fixture',remote_sources=['/a','/b'],base_fields=scraper.manifest_fields())
            with (root/'out.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows),2)
            for row,path,remote in zip(rows,[a,b],['/a','/b']):
                self.assertEqual(row['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(row['local_files'],str(path))
                self.assertEqual(row['remote_sources'],remote)
                self.assertEqual(row['package_size_bytes'],str(path.stat().st_size))
                self.assertEqual(json.loads(row['extracted_app_versions_json']),[])
                self.assertEqual(row['appid'],'')
            self.assertEqual(json.loads(rows[0]['extracted_host_appids_json']),[])
            self.assertEqual(json.loads(rows[0]['extracted_reference_appids_json']),['wx1111111111111111'])
            self.assertEqual(json.loads(rows[1]['extracted_host_appids_json']),['wx2222222222222222'])

    def test_inherited_parser_handles_unsupported_and_unsafe_without_query_fallback(self):
        cases = [b'V1MMWXencrypted', b'not a package', package([('/../escape',b'x')])]
        for data in cases:
            with self.subTest(header=data[:12]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = root/'x.wxapkg'
                source.write_bytes(data)
                scraper.append_collected_package_metadata(root/'out.csv',[source],{'name':'NeverFallback','app_version':'999'},source_run='fixture')
                with (root/'out.csv').open() as stream:
                    row = list(csv.DictReader(stream))[0]
                self.assertNotEqual(row['extraction_status'],'plaintext_parsed')
                self.assertEqual(json.loads(row['extracted_names_json']),[])
                self.assertEqual(json.loads(row['extracted_app_versions_json']),[])
                self.assertEqual(row['appid'],'')

    def test_inherited_parser_retains_conflicting_host_declarations_and_domain_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root/'x.wxapkg'
            source.write_bytes(package([('/app.json',{'appId':'wx1111111111111111','appName':'Same'}),('/project.config.json',{'appId':'wx2222222222222222'}),('/app-service.js',b'var x="https://api.example.com/path";')]))
            scraper.append_collected_package_metadata(root/'out.csv',[source],{'appid':'wx1111111111111111','name':'Same'},source_run='fixture')
            with (root/'out.csv').open() as stream:
                row = list(csv.DictReader(stream))[0]
            self.assertEqual(json.loads(row['extracted_host_appids_json']),['wx1111111111111111','wx2222222222222222'])
            self.assertEqual(json.loads(row['extracted_domains_json']),['api.example.com'])
            self.assertEqual(row['appid'],'')
            evidence = [json.loads(s) for s in Path(row['extraction_evidence_path']).read_text().splitlines()]
            domain = next(e for e in evidence if e['kind']=='domain')
            self.assertEqual(source.read_bytes()[domain['package_offset']:][:len('api.example.com')],b'api.example.com')
            self.assertEqual(domain['source_sha256'],row['package_sha256'])

    def test_inherited_source_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root/'real.wxapkg').write_bytes(package([('/app.json',{})]))
            (root/'alias.wxapkg').symlink_to(root/'real.wxapkg')
            with self.assertRaisesRegex(ValueError,'symlink'):
                scraper.append_collected_package_metadata(root/'out.csv',[root/'alias.wxapkg'],{},source_run='fixture')


if __name__ == '__main__':
    unittest.main()
