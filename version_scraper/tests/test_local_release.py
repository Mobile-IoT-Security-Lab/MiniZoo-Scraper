"""Public defaults collect inside the project and offline commands stay offline."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

from main import load_config, main
from store import Store

ROOT = Path(__file__).resolve().parents[2]


class LocalReleaseTests(unittest.TestCase):
    def test_distributed_template_uses_project_collection_folder(self):
        config = load_config(ROOT / 'version_scraper/config.example.json')
        folder = (ROOT / 'collection_results').resolve()
        for key in ('work_csv', 'output_dir'):
            self.assertTrue(Path(config[key]).resolve().is_relative_to(folder))
        self.assertTrue(Path(config['source_csv']).is_file())

    def test_init_and_report_from_another_cwd_need_no_network_and_keep_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / 'project'
            module = project / 'version_scraper'
            (module / 'examples').mkdir(parents=True)
            source = module / 'examples/apps.csv'
            shutil.copyfile(ROOT / 'version_scraper/examples/apps.csv', source)
            config_path = module / 'config.json'
            shutil.copyfile(ROOT / 'version_scraper/config.example.json', config_path)
            original = source.read_bytes()
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(socket.socket, 'connect', side_effect=AssertionError('unexpected network connection')), \
                        contextlib.redirect_stdout(io.StringIO()):
                    for command in ('init', 'status', 'report'):
                        self.assertEqual(main(['--config', str(config_path), command]), 0)
            finally:
                os.chdir(previous)
            output = project / 'collection_results/version/output'
            self.assertTrue((output / 'state.sqlite3').is_file())
            self.assertTrue((output / 'reports/latest.json').is_file())
            self.assertTrue((output / 'reports/results.csv').is_file())
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual((project / 'collection_results/version/apps.csv').read_bytes(), original)
            store = Store(load_config(config_path)).open_readonly()
            try:
                self.assertEqual(store.report()['collection_count'], 0)
                tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn('uploads', tables)
            finally:
                store.close()

    def test_remote_command_is_not_exposed(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['upload'])
        self.assertEqual(raised.exception.code, 2)

    def test_private_configuration_is_rejected_before_any_work(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            config = json.loads((ROOT / 'version_scraper/config.example.json').read_text())
            config['upload_enabled'] = True
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, 'collects locally'):
                load_config(path)


if __name__ == '__main__':
    unittest.main()
