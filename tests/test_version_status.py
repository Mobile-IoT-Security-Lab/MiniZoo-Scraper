import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "version_scraper"))

from status import read_status
from store import Store


class VersionStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source.csv"
        self.source.write_text("name,appid,previous_app_version\nExample,wx0000000000000001,1\n")
        self.output = self.root / "output"
        self.config = {"source_csv": str(self.source),
                       "work_csv": str(self.root / "working.csv"),
                       "output_dir": str(self.output),
                       "devices": [{"name": "example", "udid": "EXAMPLE_DEVICE",
                                    "system_port": 8200}]}
        store = Store(self.config)
        try:
            store.initialize()
            with store.conn:
                store.conn.execute("UPDATE tasks SET status='running'")
        finally:
            store.close()
        self.database = self.output / "state.sqlite3"
        self.pause_path = self.output / "checkpoints" / "rate_limit_stop.json"
        self.pause = {"schema_version": 1, "active": True, "stop_id": "example-stop",
                      "resume_not_before": 1234567890, "reason": "explicit_rate_limit"}
        self.pause_path.write_text(json.dumps(self.pause))
        self.streak_path = self.output / "checkpoints" / "empty_search_streak.json"
        self.streak_path.write_text(json.dumps({"schema_version": 1, "limit": 10,
                                               "entries": [{"appid": "wx0000000000000001"}],
                                               "count": 0}))
        self.config_path = self.root / "version.json"
        self.config_path.write_text(json.dumps(self.config))

    def snapshot(self):
        paths = [self.source, Path(self.config["work_csv"]), self.database,
                 self.output / "initial_manifest.json", self.pause_path, self.streak_path]
        return {path: path.read_bytes() for path in paths}

    def cli(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "minizoo.py"),
                                 "version", "--config", str(self.config_path), "status"],
                                capture_output=True, text=True, timeout=30, cwd=self.root)
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    def test_public_cli_keeps_existing_summary_and_never_resumes_or_requeues(self):
        before = self.snapshot()
        code, result = self.cli()
        self.assertEqual(code, 0)
        self.assertTrue(result["available"])
        self.assertTrue(result["progress_available"])
        self.assertEqual(result["pause_state"], "paused")
        self.assertEqual(result["scraping_pause"], self.pause)
        self.assertEqual(result["task_counts"], {"running": 1})
        self.assertEqual(result["empty_search_streak"]["count"], 1)
        self.assertEqual(result["records"], 1)
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(result["collection_count"], 0)
        self.assertNotIn("tasks", result)
        self.assertNotIn("errors", result)
        self.assertEqual(self.snapshot(), before)

    def test_missing_database_is_unavailable_not_empty_and_next_cli_poll_recovers(self):
        before = self.snapshot()
        saved = self.database.with_suffix(".saved")
        self.database.replace(saved)
        code, result = self.cli()
        self.assertEqual(code, 2)
        self.assertFalse(result["available"])
        self.assertFalse(result["progress_available"])
        self.assertEqual(result["errors"]["progress"]["error_type"], "OperationalError")
        self.assertEqual(result["scraping_pause"], self.pause)
        self.assertEqual(result["pause_state"], "paused")
        self.assertNotIn("task_counts", result)
        self.assertNotIn("state", result)  # No evidence about a worker process.
        self.assertFalse(self.database.exists())  # No writable SQLite fallback.
        saved.replace(self.database)
        code, recovered = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(recovered["task_counts"], {"running": 1})
        self.assertEqual(recovered["scraping_pause"], self.pause)
        self.assertEqual(self.snapshot(), before)

    def test_query_failure_closes_connection_and_retains_independent_pause(self):
        store = Store(self.config)
        with patch.object(store, "report", side_effect=sqlite3.OperationalError("unable to open database file")):
            failed = read_status(store)
        self.assertIsNone(store.conn)
        self.assertFalse(failed["available"])
        self.assertEqual(failed["scraping_pause"], self.pause)
        self.assertNotIn("task_counts", failed)
        recovered = read_status(store)
        self.assertTrue(recovered["available"])
        self.assertEqual(recovered["task_counts"], {"running": 1})
        self.assertIsNone(store.conn)

    def test_failure_after_connect_also_closes_readonly_connection(self):
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("checkpoint temporarily unavailable")
        with patch("store.sqlite3.connect", return_value=connection) as connect:
            result = read_status(Store(self.config))
        self.assertFalse(result["available"])
        self.assertEqual(result["scraping_pause"], self.pause)
        self.assertTrue(connect.call_args.args[0].endswith("?mode=ro"))
        self.assertTrue(connect.call_args.kwargs["uri"])
        connection.close.assert_called_once()

    def test_unreadable_pause_does_not_claim_clear_even_when_progress_is_available(self):
        self.pause_path.write_text("{invalid")
        before = self.snapshot()
        code, result = self.cli()
        self.assertEqual(code, 2)
        self.assertFalse(result["available"])
        self.assertTrue(result["progress_available"])
        self.assertEqual(result["pause_state"], "unavailable")
        self.assertIn("scraping_pause", result["errors"])
        self.assertNotIn("scraping_pause", result)
        self.assertEqual(result["task_counts"], {"running": 1})
        self.assertEqual(self.snapshot(), before)

    def test_manifest_failure_keeps_pause_and_does_not_open_database(self):
        (self.output / "initial_manifest.json").write_text("{invalid")
        with patch("store.sqlite3.connect") as connect:
            result = read_status(Store(self.config))
        connect.assert_not_called()
        self.assertFalse(result["available"])
        self.assertEqual(result["pause_state"], "paused")
        self.assertEqual(result["scraping_pause"], self.pause)

    def test_bad_streak_does_not_hide_readable_pause_or_progress(self):
        self.streak_path.write_text(json.dumps({"schema_version": 1, "entries": None}))
        result = read_status(Store(self.config))
        self.assertFalse(result["available"])
        self.assertTrue(result["progress_available"])
        self.assertEqual(result["pause_state"], "paused")
        self.assertEqual(result["task_counts"], {"running": 1})
        self.assertIn("empty_search_streak", result["errors"])
        self.assertNotIn("empty_search_streak", result)

    def test_inactive_or_absent_pause_is_reported_clear_without_changing_marker(self):
        self.pause["active"] = False
        self.pause_path.write_text(json.dumps(self.pause))
        before = self.snapshot()
        result = read_status(Store(self.config))
        self.assertTrue(result["available"])
        self.assertIsNone(result["scraping_pause"])
        self.assertEqual(result["pause_state"], "clear")
        self.assertEqual(self.snapshot(), before)
        self.pause_path.unlink()
        result = read_status(Store(self.config))
        self.assertTrue(result["available"])
        self.assertEqual(result["pause_state"], "clear")
        self.assertFalse(self.pause_path.exists())


if __name__ == "__main__":
    unittest.main()
