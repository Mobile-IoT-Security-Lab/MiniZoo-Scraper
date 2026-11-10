import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'MiniZooCollectionCore'))

from minizoo_collection import native_privacy as m


class CachePreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = {m.CACHE + name + '.wxapkg': name.encode() for name in ('first', 'late', 'third', 'fourth')}
        self.hashes = {path: hashlib.sha256(data).hexdigest() for path, data in self.data.items()}
        self.first, self.late, self.third, self.fourth = self.data

    def inventory(self, *paths):
        return {path: self.hashes[path] for path in paths}

    def run_preservation(self, snapshots, **kwargs):
        inventory = Mock(side_effect=snapshots)
        pull = Mock(side_effect=self.data.__getitem__)
        wait = Mock()
        result = m.preserve_stable('device-test', self.root, inventory, pull, wait=wait, **kwargs)
        return result, inventory, pull, wait

    def receipts(self):
        return [json.loads(path.read_text()) for path in self.root.glob('preservation-*.json')]

    def test_late_addition_is_preserved_before_stable_success(self):
        first = self.inventory(self.first)
        complete = self.inventory(self.first, self.late)
        result, inventory, pull, wait = self.run_preservation([first, complete, complete, complete])
        self.assertTrue(result['complete'])
        self.assertEqual(result['inventory_before'], complete)
        self.assertEqual(result['inventory_after'], complete)
        self.assertEqual(len(result['packages']), 2)
        self.assertEqual([call.args[0] for call in pull.call_args_list], [self.first, self.late])
        self.assertEqual(inventory.call_count, 4)
        wait.assert_called_once_with(1)
        self.assertEqual(sorted(r['complete'] for r in self.receipts()), [False, True])
        for path, digest in complete.items():
            self.assertEqual((self.root / 'blobs' / (digest + '.wxapkg')).read_bytes(), self.data[path])

    def test_continuous_additions_stop_after_three_passes(self):
        first = self.inventory(self.first)
        second = self.inventory(self.first, self.late)
        third = self.inventory(self.first, self.late, self.third)
        fourth = self.inventory(self.first, self.late, self.third, self.fourth)
        inventory = Mock(side_effect=[first, second, second, third, third, fourth])
        wait = Mock()
        with self.assertRaises(m.CacheInventoryChanged):
            m.preserve_stable('device-test', self.root, inventory, self.data.__getitem__, wait=wait)
        self.assertEqual(inventory.call_count, 6)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(len(self.receipts()), 3)
        self.assertTrue(all(not receipt['complete'] for receipt in self.receipts()))

    def test_removal_or_replacement_during_pass_is_not_retried(self):
        first = self.inventory(self.first)
        for after in ({}, {self.first: 'a' * 64}):
            with self.subTest(after=after):
                inventory = Mock(side_effect=[first, after])
                wait = Mock()
                with self.assertRaises(m.CacheInventoryChanged):
                    m.preserve_stable('device-test', self.root, inventory, self.data.__getitem__, wait=wait)
                self.assertEqual(inventory.call_count, 2)
                wait.assert_not_called()

    def test_newly_observed_file_cannot_disappear_or_change_between_passes(self):
        first = self.inventory(self.first)
        second = self.inventory(self.first, self.late)
        for next_before in (first, dict(second, **{self.late: 'a' * 64})):
            with self.subTest(next_before=next_before):
                inventory = Mock(side_effect=[first, second, next_before])
                pull = Mock(side_effect=self.data.__getitem__)
                with self.assertRaisesRegex(m.NativePrivacyError, 'between preservation passes'):
                    m.preserve_stable('device-test', self.root, inventory, pull, wait=lambda _: None)
                self.assertEqual(inventory.call_count, 3)
                self.assertNotIn(self.late, [call.args[0] for call in pull.call_args_list])

    def test_pulled_hash_mismatch_is_not_retried(self):
        inventory = Mock(return_value=self.inventory(self.first))
        pull = Mock(return_value=b'corrupted')
        wait = Mock()
        with self.assertRaisesRegex(m.NativePrivacyError, 'Pulled SHA mismatch'):
            m.preserve_stable('device-test', self.root, inventory, pull, wait=wait)
        self.assertEqual(inventory.call_count, 1)
        pull.assert_called_once_with(self.first)
        wait.assert_not_called()
        self.assertFalse(self.receipts())

    def test_stop_during_retry_wait_prevents_next_inventory_or_pull(self):
        first = self.inventory(self.first)
        second = self.inventory(self.first, self.late)
        inventory = Mock(side_effect=[first, second])
        pull = Mock(side_effect=self.data.__getitem__)
        stopped = False
        def wait(_):
            nonlocal stopped
            stopped = True
        def check_stop():
            if stopped:
                raise RuntimeError('operator stop')
        with self.assertRaisesRegex(RuntimeError, 'operator stop'):
            m.preserve_stable('device-test', self.root, inventory, pull, check_stop, wait)
        self.assertEqual(inventory.call_count, 2)
        pull.assert_called_once_with(self.first)
        self.assertEqual([receipt['complete'] for receipt in self.receipts()], [False])


if __name__ == '__main__':
    unittest.main()
