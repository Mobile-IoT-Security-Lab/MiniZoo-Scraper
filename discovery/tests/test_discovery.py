"""Offline response/checkpoint tests; MiniCrawler integration uses an explicit mount."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main as discovery
from fakes import Crawler, Batch


def response(items=(), **extra):
    body = {"ret": 0, "data": [{"subBoxes": [{"items": list(items)}]}] if items else [],
            "continueFlag": 0, **extra}
    return {"phase": "completed", "result": {"ret": 0, "json": json.dumps(body)}}


def item(appid="wx0000000000000001", title="中文测试应用"):
    return {"title": title, "jumpInfo": {"appID": appid, "appVersion": 7}, "desc": "Test description"}


class ResponseTests(unittest.TestCase):
    def test_explicit_empty_null_is_valid_but_unknown_null_pauses(self):
        value = response(data=None, noMoreData={"noMoreWording": "暂无相关结果"})
        self.assertEqual(discovery.inspect_response(value)["data"], [])
        with self.assertRaises(discovery.Pause):
            discovery.inspect_response(response(data=None))

    def test_transport_and_wechat_errors_are_not_empty_searches(self):
        for value in ({}, response(ret=500), response(data="unavailable")):
            with self.assertRaises(RuntimeError):
                discovery.inspect_response(value)

    def test_explicit_rate_limits_and_verification_pause(self):
        for value in (dict(response(), visible_text="操作过于频繁"),
                      response(errMsg="security verification")):
            with self.assertRaises(discovery.Pause):
                discovery.inspect_response(value)

    def test_source_missing_fails_before_import(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "pinned submodule"):
            discovery.load_source(Path(tmp))


class QueueTests(unittest.TestCase):
    integration = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(os.environ["MINICRAWLER_SOURCE"]) if self.integration else Path(self.tmp.name) / 'adapter'
        if not self.integration:
            adapter = patch.object(discovery, 'load_source', return_value=(Crawler, Batch))
            adapter.start()
            self.addCleanup(adapter.stop)
        self.path = Path(self.tmp.name) / "discovery/data.db"
        discovery.initialize(self.path, self.source, ["seed", "seed"])
        self.crawler, self.batch = discovery.load_source(self.source)
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)

    def apply(self, query, value):
        return discovery.apply_page(self.db, self.crawler, self.batch, query,
                                    discovery.cursor_for(self.db, query), value, 5)

    def args(self):
        return argparse.Namespace(source=self.source, database=self.path, serial="TEST-SERIAL",
                                  frida="frida", max_pages=5, max_queries=1, query_delay=0, page_delay=0)

    def test_seed_deduplicates_and_grows_queue_from_valid_identity(self):
        self.assertEqual(discovery.status(self.db)["queue"], {"pending": 1})
        self.apply("seed", response([item(), item("not-an-appid", "Wrong result")]))
        self.assertEqual(discovery.status(self.db)["apps"], 1)
        queries = {row[0] for row in self.db.execute("SELECT query FROM query_queue")}
        self.assertTrue({"中文测试应用", "中文", "中文测", "中文测试"}.issubset(queries))
        self.assertNotIn("Wrong result", queries)
        discovery.export(self.db, self.path.parent)
        with (self.path.parent / "catalog.csv").open() as stream:
            record = next(csv.DictReader(stream))
        self.assertEqual(record["appid"], "wx0000000000000001")
        self.assertEqual(record["app_version"], "7")
        self.assertEqual(record["category"], "")

    def test_page_cursor_survives_database_reopen(self):
        outcome = self.apply("seed", response([item()], continueFlag=1, offset=20, cookies="test-cookie", searchID="search-1"))
        self.assertFalse(outcome["complete"])
        with sqlite3.connect(self.path) as reopened:
            cursor = discovery.cursor_for(reopened, "seed")
        self.assertEqual((cursor["page"], cursor["offset"], cursor["found"]), (2, 20, 1))
        self.assertEqual(cursor["cookies"], "test-cookie")
        self.apply("seed", response())
        self.assertEqual(discovery.state(self.db, "empty_queries"), [])
        self.assertEqual(self.db.execute("SELECT status FROM query_queue WHERE query='seed'").fetchone()[0], "done")

    def test_sparse_response_preserves_prior_metadata_and_actual_raw_items(self):
        full = dict(item(), source={"title": "Example developer"}, iconUrl="https://example.org/icon.png",
                    categoryName="Tools")
        self.apply("seed", response([full]))
        sparse = {"jumpInfo": full["jumpInfo"], "title": "Updated observed name"}
        self.apply("seed", response([sparse]))
        with sqlite3.connect(self.path) as reopened:
            row = reopened.execute("SELECT name,description,developer,icon_url,category,raw_json FROM miniapps").fetchone()
            observations = [json.loads(r[0]) for r in reopened.execute("SELECT response FROM discovery_observations ORDER BY id")]
        self.assertEqual(row[:5], ("Updated observed name", "Test description", "Example developer",
                                   "https://example.org/icon.png", "Tools"))
        self.assertEqual(json.loads(row[5]), sparse)
        self.assertEqual(list(self.crawler.iter_items(discovery.inspect_response(observations[0]))), [full])
        self.assertEqual(list(self.crawler.iter_items(discovery.inspect_response(observations[1]))), [sparse])

    def test_nameless_appid_is_preserved_and_is_not_an_empty_search(self):
        self.apply("seed", response([{"jumpInfo": {"appID": "wx0000000000000001"}}]))
        self.assertEqual(discovery.status(self.db)["apps"], 1)
        self.assertEqual(discovery.state(self.db, "empty_queries", []), [])
        self.assertEqual(self.db.execute("SELECT name FROM miniapps").fetchone()[0], "")

    def test_empty_nested_result_is_still_an_empty_query(self):
        self.apply("seed", response(data=[{"subBoxes": [{"items": []}]}]))
        self.assertEqual(discovery.state(self.db, "empty_queries", []), ["seed"])

    def test_category_requires_native_value_and_conflicting_identity_is_rejected(self):
        self.apply("seed", response([dict(item(), category={"unexpected": "value"})]))
        self.assertEqual(self.db.execute("SELECT category FROM miniapps").fetchone()[0], "")
        self.apply("seed", response([dict(item(), isMinigame=1)]))
        self.assertEqual(self.db.execute("SELECT category FROM miniapps").fetchone()[0], "Mini game")
        with self.assertRaisesRegex(RuntimeError, "no valid Mini Program"):
            self.apply("seed", response([dict(item(), appID="wx0000000000000002")]))
        self.assertEqual(discovery.status(self.db)["apps"], 1)

    def test_failed_projection_preserves_raw_response_for_retry(self):
        with patch.object(discovery, "save_records", side_effect=RuntimeError("disk write failed")):
            with self.assertRaisesRegex(RuntimeError, "disk write failed"):
                self.apply("seed", response([item()]))
        with sqlite3.connect(self.path) as reopened:
            saved = json.loads(reopened.execute("SELECT response FROM discovery_observations").fetchone()[0])
        self.apply("seed", saved)
        self.assertEqual(discovery.status(self.db)["apps"], 1)

    def test_page_cap_retains_cursor_and_needs_explicit_requeue(self):
        outcome = discovery.apply_page(self.db, self.crawler, self.batch, "seed",
                                        discovery.cursor_for(self.db, "seed"),
                                        response([item()], continueFlag=1, offset=20), 1)
        self.assertTrue(outcome["capped"])
        self.assertEqual(self.db.execute("SELECT status FROM query_queue WHERE query='seed'").fetchone()[0], "capped")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(discovery.main(["--database", str(self.path), "resume", "--include-capped"]), 0)
        self.assertEqual(discovery.cursor_for(self.db, "seed")["page"], 2)
        self.assertEqual(self.db.execute("SELECT status FROM query_queue WHERE query='seed'").fetchone()[0], "pending")

    def test_nonadvancing_continuation_retains_current_cursor(self):
        self.apply("seed", response([item()], continueFlag=1, offset=20))
        for value in (response([item()], continueFlag=1, offset=20), response(continueFlag=1, offset=40)):
            with self.assertRaisesRegex(RuntimeError, "did not advance"):
                self.apply("seed", value)
        self.assertEqual(discovery.cursor_for(self.db, "seed")["offset"], 20)
        self.assertEqual(self.db.execute("SELECT status FROM query_queue WHERE query='seed'").fetchone()[0], "pending")

    def test_ten_empty_queries_pause_and_explicit_resume_retains_work(self):
        queries = [f"empty-{n}" for n in range(10)]
        self.batch.enqueue(self.db, queries, "test")
        for query in queries[:9]:
            self.apply(query, response())
        with self.assertRaisesRegex(discovery.Pause, "ten_consecutive"):
            self.apply(queries[9], response())
        self.assertEqual(discovery.state(self.db, "paused"), "ten_consecutive_empty_queries")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM discovery_observations").fetchone()[0], 10)
        with patch.object(discovery, "query_phone", side_effect=AssertionError("Must not contact a phone")):
            with self.assertRaises(discovery.Pause):
                discovery.run(self.db, self.args())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(discovery.main(["--database", str(self.path), "resume"]), 0)
        self.assertIsNone(discovery.state(self.db, "paused"))
        self.assertEqual(discovery.status(self.db)["queue"], {"pending": 11})

    def test_transport_failure_pauses_without_incrementing_empty_counter(self):
        with patch.object(discovery, "query_phone", side_effect=RuntimeError("Transport unavailable")), \
                patch.object(discovery.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "Transport unavailable"):
                discovery.run(self.db, self.args())
        self.assertEqual(discovery.state(self.db, "empty_queries", []), [])
        self.assertEqual(discovery.state(self.db, "paused"), "Transport unavailable")
        self.assertEqual(discovery.status(self.db)["queue"], {"pending": 1})

    def test_unrecognized_nonempty_data_does_not_count_as_empty(self):
        for value in (response([item("invalid")]), response(data=[{"unexpected": "schema"}])):
            with self.assertRaisesRegex(RuntimeError, "no valid Mini Program records"):
                self.apply("seed", value)
        self.assertEqual(discovery.state(self.db, "empty_queries", []), [])
        self.assertEqual(discovery.status(self.db)["queue"], {"pending": 1})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM discovery_observations").fetchone()[0], 2)

    def test_checkpoint_cannot_switch_device(self):
        discovery.put_state(self.db, "serial", "OTHER-SERIAL")
        self.db.commit()
        with patch.object(discovery, "query_phone", side_effect=AssertionError("Must not contact a phone")):
            with self.assertRaisesRegex(RuntimeError, "another device"):
                discovery.run(self.db, self.args())

    def test_frida_and_adb_both_select_the_explicit_serial(self):
        value = response([item()])
        value["request"] = {"query": "seed", "offset": 0, "requestId": "request-1"}
        value["result"]["requestId"] = "request-1"
        message = {"type": "send", "payload": {"kind": "direct-query-result", "value": json.dumps(value)}}
        output = subprocess.CompletedProcess([], 0, stdout="message: " + repr(message) + " data: None\n", stderr="")
        with patch.object(discovery.subprocess, "check_output", return_value="123\n") as adb, \
                patch.object(discovery.subprocess, "run", return_value=output) as frida:
            self.assertEqual(discovery.query_phone(self.args(), self.crawler, "seed", discovery.cursor_for(self.db, "seed")), value)
        self.assertEqual(adb.call_args.args[0][:3], ["adb", "-s", "TEST-SERIAL"])
        self.assertEqual(frida.call_args.args[0][:3], ["frida", "-D", "TEST-SERIAL"])

    def test_stale_or_uncorrelated_callback_is_rejected(self):
        for request, request_id in [
            ({"query": "old-query", "offset": 0, "requestId": "new"}, "new"),
            ({"query": "seed", "offset": 20, "requestId": "new"}, "new"),
            ({"query": "seed", "offset": 0, "requestId": "new"}, "stale"),
            ({"query": "seed", "offset": 0}, None),
        ]:
            value = response([item()])
            value["request"] = request
            value["result"]["requestId"] = request_id
            message = {"type": "send", "payload": {"kind": "direct-query-result", "value": json.dumps(value)}}
            output = subprocess.CompletedProcess([], 0, stdout="message: " + repr(message) + " data: None\n", stderr="")
            with self.subTest(request=request, request_id=request_id), \
                    patch.object(discovery.subprocess, "check_output", return_value="123\n"), \
                    patch.object(discovery.subprocess, "run", return_value=output), \
                    self.assertRaisesRegex(RuntimeError, "does not match"):
                discovery.query_phone(self.args(), self.crawler, "seed", discovery.cursor_for(self.db, "seed"))

    def test_lock_prevents_concurrent_database_writers(self):
        with discovery.exclusive(self.path), self.assertRaisesRegex(RuntimeError, "Another discovery"):
            with discovery.exclusive(self.path):
                self.fail("Second writer acquired the lock")


@unittest.skipUnless(os.environ.get("MINICRAWLER_SOURCE"), "Optional pinned adapter: use test discovery --integration")
class MountedSourceTests(QueueTests):
    integration = True


if __name__ == "__main__":
    unittest.main()
