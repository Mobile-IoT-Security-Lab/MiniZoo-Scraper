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
            command = [sys.executable, str(ROOT / "docker/configure.py"), "--serial", "TEST-SERIAL", "--csv", str(source), "--output", str(output)]
            subprocess.run(command, check=True, capture_output=True, text=True)
            config = json.loads(output.read_text())
            self.assertEqual(config["source_csv"], "/input/baseline.csv")
            self.assertEqual(config["expected_records"], 1)
            self.assertFalse(any(key.startswith(("drive_", "upload_", "rclone_")) for key in config))
            self.assertEqual(config["devices"][0]["udid"], "TEST-SERIAL")
            before = output.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
