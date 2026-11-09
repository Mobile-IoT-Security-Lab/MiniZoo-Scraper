import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required to exercise the write serialization')
class SerialWriteTest(unittest.TestCase):
    def run_node(self, script):
        preload = Path(__file__).resolve().parents[1] / 'minizoo_collection' / 'tools' / 'serial_writes.cjs'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['node', '-e', script, str(preload)], cwd=directory,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_path_writes_are_fifo_while_other_paths_can_progress(self):
        self.run_node(r'''
const fs = require('fs'), path = require('path'), assert = require('assert');
const started = [], completed = [];
fs.writeFile = (...args) => started.push(args);
require(process.argv[1]);
fs.writeFile('same.json', 'long content', () => completed.push('first'));
fs.writeFile(path.resolve('same.json'), 'short', () => completed.push('second'));
fs.writeFile('other.json', 'independent', () => completed.push('other'));
assert.equal(started.length, 2);
assert.equal(started[1][0], 'other.json');
started[0][2](null);
assert.equal(started.length, 3);
assert.equal(started[2][1], 'short');
started[2][2](null);
started[1][2](null);
assert.deepEqual(completed, ['first', 'second', 'other']);
''')

    def test_real_overlapping_long_and_short_json_writes_leave_no_tail(self):
        self.run_node(r'''
const fs = require('fs'), assert = require('assert');
require(process.argv[1]);
let completed = 0;
for (let i = 0; i < 40; i++) {
  const content = JSON.stringify(i % 2 ? {v: i} : {data: 'x'.repeat(300000), v: i});
  fs.writeFile('app.json', content, error => {
    assert.ifError(error);
    if (++completed === 40) {
      const actual = fs.readFileSync('app.json', 'utf8');
      assert.equal(actual, '{"v":39}');
      assert.deepEqual(JSON.parse(actual), {v: 39});
    }
  });
}
''')


if __name__ == '__main__':
    unittest.main()
