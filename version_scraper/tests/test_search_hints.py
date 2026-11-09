import json
from pathlib import Path
import tempfile
import unittest

from search_hints import load_search_hints
from store import sha256


class SearchHintsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'metadata.csv'
        self.source.write_text('extracted_names_json\n["Alternative title"]\n')
        self.path = self.root / 'hints.json'
        self.appid = 'wx0123456789abcdef'
        self.entry = {'query': 'Alternative title', 'identity_verified': False,
                      'source': {'path': 'metadata.csv', 'sha256': sha256(self.source),
                                 'package_sha256': 'a' * 64}}

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        self.path.write_text(json.dumps({'schema_version': 1, 'entries': {self.appid: self.entry}}))
        return load_search_hints(self.path)

    def test_hints_are_unverified_and_keep_source_and_mapping_hashes(self):
        hint = self.load()[self.appid]
        self.assertEqual(hint['query'], 'Alternative title')
        source = hint['query_source']
        self.assertFalse(source['identity_verified'])
        self.assertEqual(source['source']['path'], str(self.source))
        self.assertEqual(source['source']['sha256'], sha256(self.source))
        self.assertEqual(source['hints_file']['sha256'], sha256(self.path))
        self.assertEqual(load_search_hints(None), {})

    def test_changed_source_is_rejected(self):
        self.source.write_text('changed metadata')
        with self.assertRaises(ValueError):
            self.load()

    def test_invalid_query_or_claimed_verified_identity_is_rejected(self):
        for query in ('', '   ', 'x' * 101, 'one\ntwo', 'one\x00two', None):
            self.entry['query'] = query
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.load()
        self.entry['query'] = 'Alternative title'
        self.entry['identity_verified'] = True
        with self.assertRaises(ValueError):
            self.load()


if __name__ == '__main__':
    unittest.main()
