"""Check bootstrap paths and existing-config protection without contacting a phone."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConfigureTests(unittest.TestCase):
    def test_private_csv_basename_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "baseline.csv"
            source.write_text("appid,name,previous_app_version\nwx0000000000000001,Test app,7\n", encoding="utf-8")
            output = directory / "config/version.json"
            command = [sys.executable, str(ROOT / "minizoo.py"), "configure-version", "--container", "--serial", "TEST-SERIAL", "--csv", str(source), "--output", str(output)]
            subprocess.run(command, check=True, capture_output=True, text=True)
            config = json.loads(output.read_text())
            self.assertEqual(config["source_csv"], "/input/baseline.csv")
            self.assertEqual(config["expected_records"], 1)
            self.assertFalse(any(key.startswith(("drive_", "upload_", "rclone_")) for key in config))
            self.assertEqual(config["devices"][0]["udid"], "TEST-SERIAL")
            before = output.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_native_config_can_initialize_independently_from_any_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            source = directory / "baseline.csv"
            source.write_text("appid,name,previous_app_version\nwx0000000000000001,Test app,7\n")
            original = source.read_bytes()
            output = directory / "private/version.json"
            data = directory / "results"
            command = [sys.executable, str(ROOT / "minizoo.py")]
            result = subprocess.run([*command, "configure-version", "--serial", "TEST-SERIAL", "--csv", str(source),
                                     "--output", str(output), "--data-dir", str(data)], cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(output.read_text())
            self.assertEqual(config['source_csv'], str(source))
            self.assertEqual(config['work_csv'], str(data / 'apps.csv'))
            self.assertEqual(config['evidence_command'][0], sys.executable)
            for action in ('init', 'status', 'report'):
                result = subprocess.run([*command, "version", "--config", str(output), action], cwd=directory,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual((data / 'apps.csv').read_bytes(), original)
            self.assertTrue((data / 'output/reports/latest.json').is_file())


if __name__ == "__main__":
    unittest.main()
