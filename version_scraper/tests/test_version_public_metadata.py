"""Regression tests for live runtime metadata, before optional decompilation."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import main
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'MiniZooCollectionCore' / 'tests'))
import test_runtime_evidence as fixtures
from minizoo_collection import artifacts, runtime
from store import sha256, write_json

FIELDS = {'name': 'Observed app', 'description': 'Observed description',
          'category': 'Tools / Utilities', 'developer': 'Observed company',
          'icon_url': 'https://example.org/icon.png'}
SCHEMA = 'minizoo_public_metadata_v2'


class VersionPublicMetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RuntimeEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.output = fixture.root / 'output'
        self.root = self.output / 'collections' / fixtures.APPID / 'test-attempt'
        self.root.mkdir(parents=True)
        moved = self.root / 'main.wxapkg'
        fixture.local.rename(moved)
        fixture.local = moved
        fixture.package['local_path'] = str(moved)
        for message in fixture.records:
            message['metadata_schema'] = SCHEMA
        fields = {'name': FIELDS['name'], 'description': FIELDS['description'],
                  'icon_url': FIELDS['icon_url'],
                  'raw_dynamic_info': json.dumps({'NewCategories': [{'first': 'Tools', 'second': 'Utilities'}]}),
                  'raw_register_source': json.dumps({'RegisterBody': FIELDS['developer']})}
        names = {'name': 'field_nickname', 'description': 'field_signature',
                 'icon_url': 'field_brandIconURL', 'raw_dynamic_info': 'field_dynamicInfo',
                 'raw_register_source': 'field_registerSource'}
        fixture.records.append({'kind': 'runtime-public-metadata', 'metadata_schema': SCHEMA,
            'attempt_id': 'test-attempt', 'appid': fixtures.APPID, 'process_id': 1234,
            'observed_at': '2026-11-02T12:00:00Z',
            'metadata_class': 'com.tencent.mm.plugin.appbrand.config.WxaAttributes',
            'identity_field': {'field': 'field_appId', 'value': fixtures.APPID},
            'fields': fields,
            'provenance': {key: {'field': names[key], 'value': value} for key, value in fields.items()}})
        self.capture()
        self.path = self.root / 'manifest.json'
        self.manifest = {'row_index': 0, 'appid': fixtures.APPID, 'attempt_id': 'test-attempt',
            'collection_id': 'test-attempt', 'packages': self.packages, 'stage': 'captured',
            'captured_at': '2026-11-02T12:00:01+00:00', 'created_at': '2026-11-02T11:50:00Z'}
        write_json(self.path, self.manifest)

    def capture(self):
        fixture = self.fixture
        records = fixture.records
        class Probe:
            serial = 'fixture-device'
            def observe(self, attempt, directory, pid=None):
                runtime.write_json(directory / 'messages.json', records)
                return copy.deepcopy(records)
        self.evidence, self.packages = runtime.collect_runtime(Probe(), 'test-attempt', self.root,
            expected_appid=fixtures.APPID, capture_fn=lambda *a, **k: [fixture.package],
            cache_fn=lambda *a: {})
        write_json(self.root / 'evidence.json', self.evidence)

    def verified(self):
        return artifacts.validate_evidence(self.root / 'evidence.json', fixtures.APPID,
                                            self.packages, 'test-attempt')

    def test_verified_live_fields_survive_failed_decompilation(self):
        with patch.object(main, 'process_acquisition', side_effect=RuntimeError('decompiler failed')):
            with self.assertRaisesRegex(RuntimeError, 'decompiler failed'):
                main.finish_local({'decompiler_command': ['unused']}, Mock(), self.path)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['stage'], 'verified')
        self.assertEqual({key: saved['observed_metadata'][key] for key in FIELDS}, FIELDS)
        self.assertEqual(saved['public_metadata_collection']['missing_fields'], [])
        self.assertFalse(saved['public_metadata_collection']['retry_required'])
        self.assertEqual(saved['captured_at'], self.manifest['captured_at'])
        self.assertEqual(saved['public_metadata_collection']['evidence_sha256'], sha256(self.root / 'evidence.json'))

    def test_absent_fields_are_checkpointed_for_retry_without_losing_package(self):
        record = self.fixture.records[-1]
        for key in ('raw_dynamic_info', 'raw_register_source'):
            record['fields'].pop(key)
            record['provenance'].pop(key)
        self.capture()
        digest = sha256(self.fixture.local)
        with patch.object(main, 'process_acquisition', side_effect=RuntimeError('decompiler failed')):
            with self.assertRaises(RuntimeError):
                main.finish_local({'decompiler_command': ['unused']}, Mock(), self.path)
        saved = json.loads(self.path.read_text())
        self.assertEqual(set(saved['public_metadata_collection']['missing_fields']), {'category', 'developer'})
        self.assertTrue(saved['public_metadata_collection']['retry_required'])
        self.assertEqual(saved['observed_metadata']['name'], FIELDS['name'])
        self.assertEqual(sha256(self.fixture.local), digest)
        self.assertTrue(saved['identity_verified'])

    def test_arbitrary_generic_metadata_claim_is_not_promoted(self):
        generic = dict(self.evidence)
        generic.pop('collection_schema')
        generic['observed_metadata'] = dict(FIELDS, appid=fixtures.APPID)
        write_json(self.root / 'evidence.json', generic)
        self.assertNotIn('observed_metadata', self.verified())

    def test_changed_saved_runtime_cannot_supply_public_fields(self):
        self.evidence['observed_metadata']['developer'] = 'Unproven publisher'
        write_json(self.root / 'evidence.json', self.evidence)
        with self.assertRaises(artifacts.ArtifactError):
            self.verified()
