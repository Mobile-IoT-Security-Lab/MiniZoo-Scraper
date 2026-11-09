"""Full orchestration with real files and simulated devices and local storage."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import artifacts
import main
from phone import Ambiguous, PhoneError, RateLimit, Stopped, ScreenUnavailable
from store import Store, sha256, write_json


class FakePacer:
    def __init__(self):
        self.waits, self.suspensions = [], []
        self.accounts = {}
        self.lock = threading.RLock()

    def wait(self, seconds):
        self.waits.append(seconds)

    def suspend(self, account, reason, seconds=None):
        self.suspensions.append((account, seconds))


class FakePhone:
    calls = []
    error = None

    def __init__(self, config, device, pacer):
        self.device, self.pacer = device, pacer
        self.account = "shared"
        self.last_finished = 0

    def connect(self):
        self.calls.append((self.device["name"], "connect"))

    def refresh(self):
        self.calls.append((self.device["name"], "refresh"))

    def search_and_open(self, name):
        self.calls.append((self.device["name"], "search"))
        if self.error:
            raise self.error
        return "not_found"

    def diagnostics(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "screen.xml").write_text("<hierarchy />")
        (directory / "screen.png").write_bytes(b"diagnostic-fixture")

    def close(self):
        self.calls.append((self.device["name"], "close"))


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        source = self.root / "source.csv"
        source.write_text("appid,name,previous_app_version\n" + "".join(
            "wx%016x,app%d,1\n" % (i + 1, i) for i in range(4)), encoding="utf-8")
        self.config = {"source_csv": str(source), "work_csv": str(self.root / "work" / "apps.csv"),
                       "output_dir": str(self.root / "work" / "output"), "expected_records": 4,
                       "decompiler_command": ["fixture"],
                       "appium_url": "http://unused", "devices": [
                           {"name": "phone" + str(i), "udid": "SERIAL" + str(i), "system_port": 8200 + i}
                           for i in range(3)]}
        self.config_path = self.root / "config.json"
        write_json(self.config_path, self.config)
        self.store = Store(self.config)
        self.store.initialize()
        FakePhone.calls = []
        FakePhone.error = None

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def captured(self, index=0):
        appid = "wx%016x" % (index + 1)
        collection = "fixture" + str(index)
        root = self.store.output / "collections" / appid / collection
        (root / "packages").mkdir(parents=True)
        (root / "logs").mkdir()
        package = root / "packages" / "package.wxapkg"
        package.write_bytes(b"verified package fixture" + str(index).encode())
        remote = artifacts.CACHE_ROOT + "/fixture.wxapkg"
        record = {"local_path": str(package), "remote_path": remote,
                  "sha256": sha256(package), "bytes": package.stat().st_size}
        manifest = {"appid": appid, "row_index": index, "name": "app" + str(index),
                    "collection_id": collection, "attempt_id": collection, "stage": "captured",
                    "packages": [record], "search_attempts": 1, "errors": [],
                    "identity_verified": False, "artifacts_verified": False, "version": None}
        write_json(root / "manifest.json", manifest)
        write_json(root / "evidence.json", {
            "appid": appid, "attempt_id": collection, "source": "fixture runtime",
            "active_loaded_release": True, "package_binding": "runtime_metadata",
            "coverage": "complete", "expected_remote_paths": [remote],
            "version": "2", "version_kind": "integer", "version_source": "loaded fixture metadata",
            "packages": [{"remote_path": remote, "sha256": record["sha256"], "role": "main", "loaded": True}]})
        self.store.save_task(index, "pending", root / "manifest.json")
        return root / "manifest.json"

    def decompile(self, packages, root, appid, command):
        output = root / "decompiled" / "main"
        output.mkdir(parents=True, exist_ok=True)
        (output / "app.js").write_text("console.log('verified fixture');")
        (output / "app.json").write_text('{"pages":["index"]}')
        files = [{"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}
                 for path in sorted(output.iterdir())]
        return [{"package_sha256": packages[0]["sha256"], "output": str(output), "files": files}]

    def runner(self, max_items=None):
        runner = main.Runner(self.config, self.store, threading.Event(), max_items)
        runner.pacer = FakePacer()
        return runner

    def hinted_runner_phone(self, outcomes, supported=True):
        source = self.root / "package_metadata.csv"
        source.write_text('extracted_names_json\n["Alternative title"]\n')
        hint_file = self.root / "search-hints.json"
        write_json(hint_file, {"schema_version": 1, "entries": {"wx0000000000000001": {
            "query": "Alternative title", "identity_verified": False,
            "source": {"path": str(source), "sha256": sha256(source), "package_sha256": "a" * 64}}}})
        self.config["search_hints_file"] = str(hint_file)
        runner = self.runner()

        class HintPhone(main.VisualPhone):
            def __init__(self, *args):
                super().__init__(*args)
                self.queries, self.requirements, self.refreshes = [], [], 0

            def refresh(self):
                self.refreshes += 1

            def search_and_open(self, query):
                self.queries.append(query)
                self.requirements.append(self.require_public_identity)
                outcome = outcomes.pop(0)
                path = Path(self.visual_dir).parent / ("hint-search-%d.json" % len(self.queries))
                write_json(path, {"schema_version": 1, "expected_appid": self.expected_appid,
                                  "attempt_id": self.search_attempt_id, "serial": self.device["udid"],
                                  "status": "absent" if outcome == "absent" else "blocked",
                                  "reason": "no_exact_selectable_card", "page_supported": supported,
                                  "candidates": [], "candidate": None})
                self.search_observation = {"path": str(path), "sha256": sha256(path)}
                if outcome == "rate_limit":
                    raise RateLimit("Search limit", 137)
                if outcome == "stop":
                    raise ScreenUnavailable("Phone not ready")
                return "not_found"

            def diagnostics(self, directory):
                pass

        return runner, HintPhone(self.config, self.config["devices"][0], runner.pacer)

    def test_one_unverified_hint_follows_public_absence_without_changing_csv_name(self):
        before = {path: path.read_bytes() for path in (self.store.source, self.store.work)}
        runner, phone = self.hinted_runner_phone(["absent", "absent"])
        with patch("main.artifacts.inventory", return_value={}), \
                patch("main.artifacts.capture", side_effect=AssertionError("no capture")):
            runner.process(phone, runner.claim())
        task = self.store.report()["tasks"][0]
        manifest = json.loads(Path(task["manifest"]).read_text())
        self.assertEqual(task["status"], "not_found")
        self.assertEqual(phone.queries, ["app0", "Alternative title"])
        self.assertEqual(phone.requirements, [False, True])
        self.assertEqual(phone.refreshes, 2)
        self.assertEqual(manifest["name"], "app0")
        history = manifest["search_attempt_devices"]
        self.assertEqual([entry["query"] for entry in history], phone.queries)
        self.assertEqual(history[0]["query_source"]["kind"], "csv")
        self.assertEqual(history[1]["query_source"]["kind"], "package_name_hint")
        self.assertFalse(history[1]["query_source"]["identity_verified"])
        self.assertNotEqual(history[0]["search_observation"], history[1]["search_observation"])
        self.assertTrue(all(Path(entry["search_observation"]["path"]).exists() for entry in history))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_hint_is_not_used_without_supported_public_absence(self):
        runner, phone = self.hinted_runner_phone(["absent"], supported=False)
        with patch("main.artifacts.inventory", return_value={}):
            runner.process(phone, runner.claim())
        self.assertEqual(phone.queries, ["app0"])
        self.assertEqual(self.store.report()["tasks"][0]["status"], "not_found")

    def test_rate_limit_stops_and_resumes_same_hint_without_spending_retry(self):
        runner, phone = self.hinted_runner_phone(["absent", "rate_limit", "absent"])
        with patch("main.artifacts.inventory", return_value={}):
            self.assertFalse(runner.process(phone, runner.claim()))
        self.assertEqual(phone.queries, ["app0", "Alternative title"])
        self.assertEqual(self.store.report()["tasks"][0]["status"], "pending")
        saved = main.scraping_pause(self.store)
        self.assertEqual(saved["events"][0]["query"], "Alternative title")
        with patch("main.time.time", return_value=saved["resume_not_before"]):
            self.assertIsNone(main.resume_scraping(self.store))
        resumed = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            self.assertTrue(resumed.process(phone, resumed.claim()))
        self.assertEqual(phone.queries, ["app0", "Alternative title", "Alternative title"])
        self.assertEqual(phone.requirements, [False, True, True])
        self.assertEqual(runner.pacer.suspensions, [("shared", 900)])
        task = self.store.report()["tasks"][0]
        self.assertEqual(task["status"], "not_found")
        manifest = json.loads(Path(task["manifest"]).read_text())
        self.assertEqual(manifest["search_attempts"], 3)
        self.assertEqual(main.search_attempts_used(manifest), 2)

    def test_resume_preserves_active_hint_instead_of_replaying_csv_query(self):
        outcomes = ["absent", "stop"]
        runner, phone = self.hinted_runner_phone(outcomes)
        with patch("main.artifacts.inventory", return_value={}):
            self.assertFalse(runner.process(phone, runner.claim()))
        task = self.store.report()["tasks"][0]
        self.assertEqual(task["status"], "pending")
        # The saved query/provenance remains usable even when the optional map
        # is omitted on the resumed run. Structured identity stays mandatory.
        self.config.pop("search_hints_file")
        resumed = self.runner()
        outcomes.append("absent")
        with patch("main.artifacts.inventory", return_value={}):
            self.assertTrue(resumed.process(phone, task))
        self.assertEqual(phone.queries, ["app0", "Alternative title", "Alternative title"])
        self.assertEqual(phone.requirements, [False, True, True])
        final = self.store.report()["tasks"][0]
        self.assertEqual(final["status"], "not_found")
        self.assertEqual(json.loads(Path(final["manifest"]).read_text())["search_attempts"], 3)

    def test_crash_resume_finishes_locally_without_phone_adapter_or_preflight(self):
        path = self.captured()
        self.store.save_task(0, "running", path)
        self.store.close()
        with patch("main.artifacts.decompile", side_effect=self.decompile), \
                patch("main.Phone", side_effect=AssertionError("no phone")), \
                patch("main.run_adapter", side_effect=AssertionError("no live adapter")), \
                patch("main.doctor", side_effect=AssertionError("no preflight needed")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main.main(["--config", str(self.config_path), "resume", "--max-items", "1"])
        self.assertEqual(code, 0)
        self.store = Store(self.config).open_readonly()
        report = self.store.report()
        self.assertEqual(report["tasks"][0]["status"], "updated")
        self.assertEqual(report["collection_count"], 1)
        self.assertEqual(report["csv_decisions"][0]["result"]["previous_version"], "1")
        self.assertTrue(report["source_unchanged"])

    def test_adapter_can_add_verified_prefetched_package_after_empty_cache_delta(self):
        self.config["evidence_command"] = ["fixture-adapter"]
        runner = self.runner(max_items=1)
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)

        def adapter(command, replacements, log):
            root = Path(replacements["collection_dir"])
            self.assertEqual(read_json(replacements["packages_json"]), [])
            remote = artifacts.CACHE_ROOT + "/prefetched.wxapkg"
            with patch("artifacts._read_device", return_value=b"prefetched fixture bytes"):
                packages = artifacts.capture_selected(phone.device["udid"], [remote], root)
            write_json(replacements["packages_json"], packages)
            write_json(replacements["output"], {
                "appid": replacements["appid"], "attempt_id": replacements["attempt_id"],
                "source": "fixture runtime", "active_loaded_release": True,
                "package_binding": "runtime_metadata", "coverage": "complete",
                "expected_remote_paths": [remote], "version": "2", "version_kind": "integer",
                "version_source": "fixture runtime release", "packages": [
                    {"remote_path": remote, "sha256": packages[0]["sha256"], "role": "main", "loaded": True}]})

        def read_json(path):
            return json.loads(Path(path).read_text())

        with patch.object(phone, "search_and_open", return_value="opened"), \
                patch("main.artifacts.inventory", return_value={}), \
                patch("main.artifacts.capture", return_value=[]), \
                patch("main.run_adapter", side_effect=adapter), \
                patch("main.artifacts.decompile", side_effect=self.decompile):
            runner.process(phone, runner.claim())
        task = self.store.report()["tasks"][0]
        manifest = read_json(task["manifest"])
        self.assertEqual(task["status"], "updated")
        self.assertEqual(len(manifest["packages"]), 1)
        self.assertEqual(manifest["stage"], "local_done")
        self.assertEqual(self.store.report()["collection_count"], 1)

    def test_ambiguous_search_preserves_public_observation_and_exports_without_csv_update(self):
        class ObservedAmbiguousPhone(main.VisualPhone):
            def refresh(self):
                pass

            def search_and_open(self, name):
                candidate = {"observed_appid": self.expected_appid, "title": "Public observed title",
                             "developer": "Public developer", "published_app_version": "999",
                             "bounds": [100, 400, 500, 500]}
                path = Path(self.visual_dir).parent / "search-observation-fixture.json"
                write_json(path, {"schema_version": 1, "expected_appid": self.expected_appid,
                                  "attempt_id": self.search_attempt_id, "serial": self.device["udid"],
                                  "observed_at": "2026-11-02T12:00:00Z", "status": "matched",
                                  "candidate": candidate, "candidates": [candidate],
                                  "source": "wechat_search_dom", "metadata_sources": ["search card"]})
                self.search_observation = {"path": str(path), "sha256": sha256(path)}
                raise Ambiguous("Candidate observed, opening unverified")

            def diagnostics(self, directory):
                pass

        before = {path: path.read_bytes() for path in (self.store.source, self.store.work)}
        runner = self.runner()
        phone = ObservedAmbiguousPhone(self.config, self.config["devices"][0], runner.pacer)
        with patch("main.artifacts.inventory", return_value={}), \
                patch("main.artifacts.capture", side_effect=AssertionError("no package capture")), \
                patch("main.run_adapter", side_effect=AssertionError("no runtime adapter")), \
                patch.object(self.store, "apply_collection", side_effect=AssertionError("no CSV update")):
            self.assertTrue(runner.process(phone, runner.claim()))
        task = self.store.report()["tasks"][0]
        manifest = json.loads(Path(task["manifest"]).read_text())
        self.assertEqual(task["status"], "ambiguous")
        self.assertEqual(manifest["stage"], "created")
        self.assertFalse(manifest["identity_verified"])
        self.assertFalse(manifest["artifacts_verified"])
        self.assertIsNone(manifest["version"])
        self.assertEqual(manifest["search_observation"], phone.search_observation)
        observation = json.loads(Path(phone.search_observation["path"]).read_text())
        self.assertEqual(observation["attempt_id"], manifest["attempt_id"])
        self.assertEqual(observation["expected_appid"], task["appid"])
        self.assertEqual(observation["serial"], manifest["device"])
        self.assertEqual(manifest["errors"][-1]["type"], "Ambiguous")
        self.assertEqual(self.store.report()["collections"], [])
        self.assertEqual(self.store.report()["csv_decisions"], [])

        main.export_report(self.store)
        report = json.loads((self.store.output / "reports/latest.json").read_text())
        self.assertEqual(len(report["search_observations"]), 1)
        public = report["search_observations"][0]
        self.assertEqual(public["search_status"], "matched")
        self.assertEqual(public["search_observed_appid"], task["appid"])
        self.assertEqual(public["search_title"], "Public observed title")
        self.assertEqual(public["search_developer"], "Public developer")
        self.assertEqual(public["search_published_version"], "999")
        with (self.store.output / "reports/results.csv").open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual((row["status"], row["search_status"]), ("ambiguous", "matched"))
        self.assertEqual(row["search_published_version"], "999")
        self.assertEqual(row["version"], "")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_resume_loads_adapter_package_list_saved_before_manifest_update(self):
        path = self.captured()
        manifest = json.loads(path.read_text())
        write_json(path.parent / "packages.json", manifest["packages"])
        manifest["packages"] = []
        write_json(path, manifest)
        with patch("main.artifacts.decompile", side_effect=self.decompile), \
                patch("main.run_adapter", side_effect=AssertionError("resume must remain local")):
            self.runner(max_items=1).resume_local()
        self.assertEqual(self.store.report()["tasks"][0]["status"], "updated")
        self.assertEqual(len(json.loads(path.read_text())["packages"]), 1)

    def test_local_done_is_verified_again_before_registration(self):
        path = self.captured()
        with patch("main.artifacts.decompile", side_effect=self.decompile):
            main.finish_local(self.config, self.store, path)
        manifest = json.loads(path.read_text())
        with self.store.conn:
            self.store.conn.execute("DELETE FROM collections")
        self.store.save_task(0, "pending", path)
        Path(manifest["decompiled_zip"]).write_bytes(b"tampered ZIP")
        self.runner().resume_local()
        self.assertEqual(self.store.report()["collections"], [])
        self.assertEqual(self.store.report()["tasks"][0]["status"], "decompilation_failed")

    def test_zip_content_is_stable_across_collections_and_file_timestamps(self):
        first, second = self.captured(0), self.captured(1)
        a, b = json.loads(first.read_text()), json.loads(second.read_text())
        Path(b["packages"][0]["local_path"]).write_bytes(Path(a["packages"][0]["local_path"]).read_bytes())
        b["packages"][0].update(sha256=a["packages"][0]["sha256"], bytes=a["packages"][0]["bytes"])
        write_json(second, b)
        evidence = json.loads((second.parent / "evidence.json").read_text())
        evidence["packages"][0]["sha256"] = a["packages"][0]["sha256"]
        write_json(second.parent / "evidence.json", evidence)

        def restore(packages, root, appid, command):
            outputs = self.decompile(packages, root, appid, command)
            stamp = 1600000000 if root == first.parent else 1700000000
            for item in outputs[0]["files"]:
                os.utime(item["path"], (stamp, stamp))
            return outputs

        with patch("main.artifacts.decompile", side_effect=restore):
            main.finish_local(self.config, self.store, first)
            main.finish_local(self.config, self.store, second)
        self.assertEqual(sha256(first.parent / "decompiled.zip"), sha256(second.parent / "decompiled.zip"))

    def test_retry_cli_preserves_old_collection_and_requires_a_new_attempt(self):
        path = self.captured()
        old = path.read_bytes()
        self.store.save_task(0, "partial", path)
        self.store.close()
        with patch("main.doctor", side_effect=AssertionError("retry must not use devices")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main.main(["--config", str(self.config_path), "retry", "--appids", "wx0000000000000001"])
        self.assertEqual(code, 0)
        self.store = Store(self.config).open_readonly()
        row = self.store.report()["tasks"][0]
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["manifest"])
        self.assertEqual(path.read_bytes(), old)

    def test_decompiler_failure_preserves_verified_stage_and_can_resume_offline(self):
        path = self.captured()
        runner = self.runner()
        with patch("main.artifacts.decompile", side_effect=artifacts.ArtifactError("bad decompiler")):
            runner.resume_local()
        manifest = json.loads(path.read_text())
        self.assertTrue(manifest["identity_verified"])
        self.assertEqual(manifest["stage"], "verified")
        self.assertEqual(self.store.report()["tasks"][0]["status"], "decompilation_failed")
        with patch("main.artifacts.decompile", side_effect=self.decompile), \
                patch("main.Phone", side_effect=AssertionError("no phone")):
            runner.resume_local(retry_failed=True)
        self.assertEqual(self.store.report()["tasks"][0]["status"], "updated")

    def test_missing_identity_evidence_stays_ambiguous(self):
        path = self.captured()
        evidence = json.loads((path.parent / "evidence.json").read_text())
        evidence["appid"] = "wx0000000000000099"
        write_json(path.parent / "evidence.json", evidence)
        with patch("main.artifacts.decompile") as decompiler:
            self.runner().resume_local()
        decompiler.assert_not_called()
        self.assertEqual(self.store.report()["tasks"][0]["status"], "ambiguous")
        self.assertEqual(self.store.report()["collections"], [])

    def test_global_limit_counts_failures_across_three_workers(self):
        runner = self.runner(max_items=2)
        with patch("main.Phone", FakePhone), patch("main.artifacts.inventory", return_value={}), \
                patch("main.time.sleep", return_value=None):
            runner.run(self.config["devices"])
        self.assertEqual(runner.claimed, 2)
        self.assertEqual(sum(action == "search" for _, action in FakePhone.calls), 2)
        self.assertEqual(self.store.report()["task_counts"], {"not_found": 2, "pending": 2})

    def test_saved_local_stage_counts_towards_same_global_limit(self):
        self.captured()
        runner = self.runner(max_items=2)
        with patch("main.artifacts.decompile", side_effect=self.decompile):
            runner.resume_local()
        with patch("main.Phone", FakePhone), patch("main.artifacts.inventory", return_value={}), \
                patch("main.time.sleep", return_value=None):
            runner.run(self.config["devices"])
        self.assertEqual(runner.claimed, 2)
        self.assertEqual(sum(action == "search" for _, action in FakePhone.calls), 1)
        self.assertEqual(self.store.report()["task_counts"], {"updated": 1, "not_found": 1, "pending": 2})

    def test_search_has_at_most_three_attempts_across_resume(self):
        FakePhone.error = PhoneError("temporary")
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        task = runner.claim()
        with patch("main.artifacts.inventory", return_value={}):
            runner.process(phone, task)
        self.assertEqual(sum(action == "search" for _, action in FakePhone.calls), 3)
        saved = self.store.report()["tasks"][0]
        self.assertEqual(saved["status"], "search_failed")
        self.store.save_task(0, "pending", saved["manifest"])
        with patch("main.artifacts.inventory", side_effect=AssertionError("no fourth attempt")):
            runner.process(phone, runner.claim())
        self.assertEqual(sum(action == "refresh" for _, action in FakePhone.calls), 3)

    def test_first_rate_limit_stops_all_scraping_before_diagnostics_and_checkpoints(self):
        FakePhone.error = RateLimit("429")
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        def diagnostics(directory):
            self.assertTrue(runner.stop.is_set())
            self.assertTrue(main.scraping_pause(self.store)["active"])
            self.assertEqual(self.store.report()["tasks"][0]["status"], "pending")
        phone.diagnostics = diagnostics
        with patch("main.artifacts.inventory", return_value={}):
            keep_running = runner.process(phone, runner.claim())
        self.assertFalse(keep_running)
        self.assertEqual(runner.pacer.suspensions, [("shared", 900)])
        self.assertEqual(self.store.report()["task_counts"], {"pending": 4})
        self.assertEqual(sum(action == "search" for _, action in FakePhone.calls), 1)
        self.assertIsNone(runner.claim())
        self.assertFalse(runner.shutdown_stop.is_set())
        saved = main.scraping_pause(self.store)
        self.assertEqual(saved["next_row_index"], 0)
        self.assertEqual(saved["events"][0]["query"], "app0")
        self.assertEqual(saved["checkpoint"], str(self.store.output / "state.sqlite3"))
        self.assertTrue(self.runner().stop.is_set())

    def test_third_search_rate_limit_is_pending_and_resumable(self):
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        phone.search_and_open = Mock(side_effect=[PhoneError("temporary"), PhoneError("temporary"),
                                                 RateLimit("limited", 1800), "not_found"])
        before = self.store.source.read_bytes()
        with patch("main.artifacts.inventory", return_value={}):
            self.assertFalse(runner.process(phone, runner.claim()))
        task = self.store.report()["tasks"][0]
        manifest = json.loads(Path(task["manifest"]).read_text())
        self.assertEqual((manifest["search_attempts"], main.search_attempts_used(manifest)), (3, 2))
        saved = main.scraping_pause(self.store)
        self.assertEqual(runner.pacer.suspensions, [("shared", 1800)])
        with patch("main.time.time", return_value=saved["resume_not_before"]):
            main.resume_scraping(self.store)
        resumed = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            self.assertTrue(resumed.process(phone, resumed.claim()))
        self.assertEqual(self.store.report()["tasks"][0]["status"], "not_found")
        self.assertEqual(phone.search_and_open.call_count, 4)
        self.assertEqual(self.store.source.read_bytes(), before)

    def test_rate_limit_interrupts_sibling_without_marking_missing_or_ambiguous(self):
        for sibling_result in ("not_found", Ambiguous("incomplete result"), PhoneError("failed response"),
                               ScreenUnavailable("no UI")):
            with self.subTest(sibling_result=sibling_result):
                # A different isolated checkpoint for each concurrent outcome.
                self.store.close()
                self.temp.cleanup()
                self.setUp()
                runner = self.runner()
                barrier, paused = threading.Barrier(2), threading.Event()
                original = runner.stop_for_rate_limit
                def stop_for_rate_limit(*args):
                    original(*args)
                    paused.set()
                runner.stop_for_rate_limit = stop_for_rate_limit
                class ConcurrentPhone(FakePhone):
                    def search_and_open(self, name):
                        barrier.wait(timeout=5)
                        if self.device["name"] == "phone0":
                            raise RateLimit("limited")
                        if not paused.wait(5):
                            raise AssertionError("rate limit was not checkpointed")
                        if isinstance(sibling_result, Exception):
                            raise sibling_result
                        return sibling_result
                with patch("main.Phone", ConcurrentPhone), patch("main.artifacts.inventory", return_value={}), \
                        patch("main.time.sleep", return_value=None):
                    runner.run(self.config["devices"][:2])
                self.assertEqual(runner.claimed, 2)
                self.assertEqual(self.store.report()["task_counts"], {"pending": 4})
                for task in self.store.report()["tasks"][:2]:
                    manifest = json.loads(Path(task["manifest"]).read_text())
                    self.assertEqual(main.search_attempts_used(manifest), 0)

    def test_paused_run_and_early_resume_report_without_touching_phones(self):
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        phone.search_and_open = Mock(side_effect=RateLimit("limited"))
        with patch("main.artifacts.inventory", return_value={}):
            runner.process(phone, runner.claim())
        saved = main.scraping_pause(self.store)
        self.store.close()
        for command in ("run", "resume"):
            with self.subTest(command=command), patch("main.time.time", return_value=saved["resume_not_before"] - 1), \
                    patch("main.doctor", side_effect=AssertionError("no preflight during pause")), \
                    patch("main.make_phone", side_effect=AssertionError("no phone during pause")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["--config", str(self.config_path), command]), 2)
        self.store.open_readonly()
        self.assertEqual(main.scraping_pause(self.store)["stop_id"], saved["stop_id"])
        self.assertEqual(self.store.report()["task_counts"], {"pending": 4})

    def test_explicit_resume_archives_pause_without_retrying_historical_failures(self):
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        phone.search_and_open = Mock(side_effect=RateLimit("limited"))
        with patch("main.artifacts.inventory", return_value={}):
            runner.process(phone, runner.claim())
        saved = main.scraping_pause(self.store)
        self.store.close()
        with patch("main.time.time", return_value=saved["resume_not_before"]), \
                patch("main.Runner.resume_local") as resume_local, \
                patch("main.doctor", return_value={"blockers": ["fixture offline"]}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["--config", str(self.config_path), "resume"]), 2)
        resume_local.assert_called_once_with(retry_failed=False)
        self.store.open_readonly()
        self.assertIsNone(main.scraping_pause(self.store))
        archived = self.store.output / "checkpoints" / "rate_limit_history" / (saved["stop_id"] + ".json")
        self.assertFalse(json.loads(archived.read_text())["active"])


    def test_checkpoint_write_failure_still_stops_and_keeps_task_pending(self):
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        phone.search_and_open = Mock(side_effect=RateLimit("limited"))
        original = main.write_json
        def write_json(path, value):
            if Path(path).name == "rate_limit_stop.json":
                raise OSError("fixture checkpoint failure")
            original(path, value)
        with patch("main.artifacts.inventory", return_value={}), patch("main.write_json", side_effect=write_json):
            with self.assertRaisesRegex(OSError, "checkpoint failure"):
                runner.process(phone, runner.claim())
        self.assertTrue(runner.stop.is_set())
        self.assertEqual(self.store.report()["task_counts"], {"pending": 4})
        self.assertIsNone(runner.claim())

    def test_probe_rate_limit_persists_global_stop_and_does_not_visit_next_phone(self):
        class RateLimitedProbe(FakePhone):
            def refresh(self):
                self.calls.append((self.device["name"], "refresh"))
                raise RateLimit("probe found a limit")
        self.store.close()
        with patch("main.Phone", RateLimitedProbe), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["--config", str(self.config_path), "probe"]), 2)
        self.store.open_readonly()
        saved = main.scraping_pause(self.store)
        self.assertTrue(saved["active"])
        self.assertIsNone(saved["events"][0]["row_index"])
        self.assertEqual(sum(action == "connect" for _, action in FakePhone.calls), 1)
        self.assertEqual(self.store.report()["task_counts"], {"pending": 4})

    def empty_search_fixture(self, records=12):
        self.store.close()
        source = self.root / "empty-source.csv"
        source.write_text("appid,name,previous_app_version\n" + "".join(
            "wx%016x,app%d,1\n" % (i + 1, i) for i in range(records)), encoding="utf-8")
        self.config.update(source_csv=str(source), work_csv=str(self.root / "empty-work.csv"),
                           output_dir=str(self.root / "empty-output"), expected_records=records, empty_search_limit=10)
        write_json(self.config_path, self.config)
        self.store = Store(self.config)
        self.store.initialize()

    def result_phone(self, runner, flag=True, result="not_found", index=0):
        phone = FakePhone(self.config, self.config["devices"][index], runner.pacer)
        def search(name):
            phone.search_was_empty = flag
            phone.calls.append((phone.device["name"], "search"))
            if isinstance(result, Exception):
                raise result
            return result
        phone.search_and_open = search
        return phone

    def test_ten_distinct_empty_apps_stop_and_all_ten_are_pending(self):
        self.empty_search_fixture()
        before = self.store.source.read_bytes()
        runner = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            for index in range(10):
                phone = self.result_phone(runner, index=index % 3)
                self.assertEqual(runner.process(phone, runner.claim()), index < 9)
        saved = main.scraping_pause(self.store)
        self.assertEqual(saved["trigger"], "consecutive_empty_searches")
        self.assertEqual(saved["classification"], "suspected_rate_limit")
        self.assertEqual(saved["next_row_index"], 0)
        self.assertEqual(len(saved["retry_tasks"]), 10)
        self.assertEqual(main.empty_search_streak(self.store)["count"], 10)
        self.assertEqual(self.store.report()["task_counts"], {"pending": 12})
        self.assertIsNone(runner.claim())
        for task in self.store.report()["tasks"][:10]:
            manifest = json.loads(Path(task["manifest"]).read_text())
            self.assertEqual(manifest["search_attempt_devices"][-1]["result"], "not_found")
            self.assertEqual(manifest["search_attempt_devices"][-1]["recheck_after_pause"], saved["stop_id"])
            self.assertEqual(main.search_attempts_used(manifest), 0)
        self.assertEqual(self.store.source.read_bytes(), before)

    def test_empty_streak_persists_across_restart_and_deduplicates_app(self):
        self.empty_search_fixture()
        runner = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            for _ in range(6):
                runner.process(self.result_phone(runner), runner.claim())
        task = self.store.report()["tasks"][0]
        manifest = json.loads(Path(task["manifest"]).read_text())
        phone = self.result_phone(runner)
        phone.search_was_empty = True
        self.assertTrue(runner.finish_not_found(phone, task, Path(task["manifest"]), manifest))
        self.assertEqual(main.empty_search_streak(self.store)["count"], 6)
        self.store.close()
        self.store = Store(self.config)
        self.store.initialize()
        resumed = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            for _ in range(4):
                resumed.process(self.result_phone(resumed), resumed.claim())
        self.assertTrue(resumed.stop.is_set())
        self.assertEqual(main.empty_search_streak(self.store)["count"], 10)

    def test_nonempty_wrong_app_resets_streak_unknown_results_do_not_count(self):
        self.empty_search_fixture()
        runner = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            for _ in range(3):
                runner.process(self.result_phone(runner), runner.claim())
            runner.process(self.result_phone(runner, flag=None), runner.claim())
            self.assertEqual(main.empty_search_streak(self.store)["count"], 3)
            runner.process(self.result_phone(runner, flag=False, result=Ambiguous("different AppID")), runner.claim())
            self.assertEqual(main.empty_search_streak(self.store)["count"], 0)
            runner.process(self.result_phone(runner), runner.claim())
            self.assertEqual(main.empty_search_streak(self.store)["count"], 1)
        self.assertIsNone(main.scraping_pause(self.store))

    def test_resume_rechecks_first_empty_app_and_does_not_repeat_retry_credits(self):
        self.empty_search_fixture()
        runner = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            for _ in range(10):
                runner.process(self.result_phone(runner), runner.claim())
        saved = main.scraping_pause(self.store)
        main.recover_empty_search_stop(self.store, self.config)
        main.recover_empty_search_stop(self.store, self.config)
        first = self.store.report()["tasks"][0]
        self.assertEqual(json.loads(Path(first["manifest"]).read_text())["search_retry_exemptions"], 1)
        with patch("main.time.time", return_value=saved["resume_not_before"] - 1):
            self.assertTrue(main.resume_scraping(self.store)["active"])
        self.assertEqual(main.empty_search_streak(self.store)["count"], 10)
        with patch("main.time.time", return_value=saved["resume_not_before"]):
            self.assertIsNone(main.resume_scraping(self.store))
        self.assertEqual(main.empty_search_streak(self.store)["count"], 0)
        resumed = self.runner()
        task = resumed.claim()
        self.assertEqual(task["row_index"], 0)
        phone = self.result_phone(resumed)
        previous_calls = len(phone.calls)
        with patch("main.artifacts.inventory", return_value={}):
            self.assertTrue(resumed.process(phone, task))
        self.assertGreater(len(phone.calls), previous_calls)
        self.assertEqual(main.empty_search_streak(self.store)["count"], 1)
        current = json.loads(Path(task["manifest"]).read_text())
        self.assertEqual((current["search_attempts"], current["search_retry_exemptions"]), (2, 1))

    def test_explicit_rate_limit_requeues_preceding_empty_apps_too(self):
        self.empty_search_fixture()
        runner = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            for _ in range(7):
                runner.process(self.result_phone(runner), runner.claim())
            runner.process(self.result_phone(runner, flag=None, result=RateLimit("explicit wall")), runner.claim())
        saved = main.scraping_pause(self.store)
        self.assertEqual(saved["trigger"], "explicit_rate_limit")
        self.assertEqual(len(saved["retry_tasks"]), 7)
        self.assertEqual(self.store.report()["task_counts"], {"pending": 12})

    def test_threshold_recovery_after_crash_between_counter_marker_and_requeues(self):
        self.empty_search_fixture()
        runner = self.runner()
        with patch("main.artifacts.inventory", return_value={}):
            for _ in range(9):
                runner.process(self.result_phone(runner), runner.claim())
            # The counter reaches ten durably, before the pause file is created.
            with patch("main.recover_empty_search_stop", side_effect=RuntimeError("fixture crash")):
                self.assertFalse(runner.process(self.result_phone(runner), runner.claim()))
        self.assertEqual(main.empty_search_streak(self.store)["count"], 10)
        self.assertIsNone(main.scraping_pause(self.store))
        self.assertEqual(self.store.report()["task_counts"], {"not_found": 9, "pending": 3})
        # A later startup creates the missing pause before starting a phone.
        with patch("main.requeue_empty_searches", side_effect=RuntimeError("fixture second crash")):
            with self.assertRaisesRegex(RuntimeError, "second crash"):
                self.runner()
        self.assertTrue(main.scraping_pause(self.store)["active"])
        recovered = self.runner()
        self.assertTrue(recovered.stop.is_set())
        self.assertEqual(self.store.report()["task_counts"], {"pending": 12})

    def test_empty_hint_queries_count_one_app_and_any_result_disqualifies_it(self):
        for flags, expected in (([True, True], 1), ([False, True], 0)):
            with self.subTest(flags=flags):
                runner, phone = self.hinted_runner_phone(["absent", "absent"])
                original = phone.search_and_open
                remaining = list(flags)
                def search(query):
                    result = original(query)
                    phone.search_was_empty = remaining.pop(0)
                    return result
                phone.search_and_open = search
                with patch("main.artifacts.inventory", return_value={}):
                    self.assertTrue(runner.process(phone, runner.claim()))
                self.assertEqual(len(phone.queries), 2)
                self.assertEqual(main.empty_search_streak(self.store)["count"], expected)
                self.store.close()
                self.temp.cleanup()
                self.setUp()

    def test_crash_after_empty_manifest_before_counter_recovers_cached_outcome(self):
        self.empty_search_fixture()
        runner = self.runner()
        task = runner.claim()
        with patch("main.artifacts.inventory", return_value={}), \
                patch.object(runner, "finish_not_found", side_effect=KeyboardInterrupt("fixture crash")):
            with self.assertRaises(KeyboardInterrupt):
                runner.process(self.result_phone(runner), task)
        self.assertEqual(main.empty_search_streak(self.store)["count"], 0)
        self.store.close()
        self.store = Store(self.config)
        self.store.initialize()
        resumed = self.runner()
        with patch("main.artifacts.inventory", side_effect=AssertionError("cached outcome requires no phone")):
            self.assertTrue(resumed.process(self.result_phone(resumed), resumed.claim()))
        self.assertEqual(main.empty_search_streak(self.store)["count"], 1)
        self.assertEqual(self.store.report()["task_counts"], {"not_found": 1, "pending": 11})

    def test_three_workers_share_one_empty_streak_without_overwriting_requeues(self):
        self.empty_search_fixture(records=20)
        runner = self.runner()
        class EmptyPhone(FakePhone):
            def search_and_open(self, name):
                self.search_was_empty = True
                return "not_found"
        with patch("main.Phone", EmptyPhone), patch("main.artifacts.inventory", return_value={}), \
                patch("main.time.sleep", return_value=None):
            runner.run(self.config["devices"])
        self.assertTrue(runner.stop.is_set())
        self.assertEqual(main.empty_search_streak(self.store)["count"], 10)
        self.assertEqual(self.store.report()["task_counts"], {"pending": 20})
        self.assertLessEqual(runner.claimed, 12)

    def test_inaccessible_ui_stops_only_its_device_and_preserves_task(self):
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        phone.refresh = Mock(side_effect=ScreenUnavailable("native hierarchy empty"))
        with patch("main.artifacts.inventory", side_effect=AssertionError("no collection")):
            self.assertFalse(runner.process(phone, runner.claim()))
        self.assertEqual(runner.pacer.suspensions, [])
        self.assertEqual(self.store.report()["task_counts"], {"pending": 4})

    def test_interrupted_created_collection_reclaimed_by_other_phone_records_capture_device(self):
        self.config["evidence_command"] = ["fixture-adapter"]
        runner = self.runner()
        first = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        first.refresh = Mock(side_effect=Stopped("interrupted before search"))
        self.assertFalse(runner.process(first, runner.claim()))
        task = self.store.report()["tasks"][0]
        path = Path(task["manifest"])
        original = json.loads(path.read_text())
        self.assertEqual(original["stage"], "created")
        self.assertEqual(original["search_attempt_devices"][0]["device"], "SERIAL0")
        second = FakePhone(self.config, self.config["devices"][1], runner.pacer)

        def capture(command, values, log):
            saved = json.loads(path.read_text())
            serial = values["serial"]
            self.assertEqual(serial, "SERIAL1")
            self.assertEqual(saved["device"], serial)
            self.assertEqual(saved["metadata"]["device"], "phone1")
            return []

        with patch.object(second, "search_and_open", return_value="opened"), \
                patch("main.artifacts.inventory", return_value={}), \
                patch("main.artifacts.capture", side_effect=AssertionError("Runtime adapter owns capture")), \
                patch("main.run_adapter", side_effect=capture) as adapter, patch.object(runner, "local", return_value=True):
            self.assertTrue(runner.process(second, runner.claim()))
        self.assertEqual(adapter.call_args.args[1]["serial"], "SERIAL1")
        saved = json.loads(path.read_text())
        self.assertEqual(saved["stage"], "captured")
        self.assertEqual(saved["search_attempts"], 2)
        self.assertEqual(saved["search_attempt_devices"][0], original["search_attempt_devices"][0])
        self.assertEqual([(h["attempt"], h["device"]) for h in saved["search_attempt_devices"]],
                         [(1, "SERIAL0"), (2, "SERIAL1")])

    def test_legacy_created_checkpoint_retains_previous_recorded_assignment(self):
        path = self.captured()
        manifest = json.loads(path.read_text())
        manifest.update(stage="created", device="SERIAL0", metadata={"device": "phone0"})
        write_json(path, manifest)
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][1], runner.pacer)
        with patch("main.artifacts.inventory", return_value={}):
            runner.process(phone, runner.claim())
        saved = json.loads(path.read_text())
        self.assertEqual(saved["device"], "SERIAL1")
        self.assertEqual(saved["search_attempt_devices"][0],
                         {"recorded_attempts": 1, "device": "SERIAL0", "name": "phone0",
                          "source": "previous_checkpoint"})

    def test_local_collection_stages_keep_device_provenance_on_other_phone(self):
        path = self.captured()
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][1], runner.pacer)
        phone.refresh = Mock(side_effect=AssertionError("no new device attempt"))
        for stage in ("captured", "verified", "decompiled", "local_done"):
            with self.subTest(stage=stage):
                manifest = json.loads(path.read_text())
                manifest.update(stage=stage, device="SERIAL0", metadata={"device": "phone0"})
                write_json(path, manifest)
                before = path.read_bytes()
                task = self.store.report()["tasks"][0]
                with patch.object(runner, "local", return_value=True):
                    self.assertTrue(runner.process(phone, task))
                self.assertEqual(path.read_bytes(), before)
        phone.refresh.assert_not_called()

    def test_stop_during_backoff_leaves_task_pending(self):
        FakePhone.error = PhoneError("temporary")
        runner = self.runner()
        phone = FakePhone(self.config, self.config["devices"][0], runner.pacer)
        runner.pacer.wait = Mock(side_effect=Stopped("stop"))
        with patch("main.artifacts.inventory", return_value={}):
            self.assertFalse(runner.process(phone, runner.claim()))
        self.assertEqual(self.store.report()["tasks"][0]["status"], "pending")





    def test_missing_global_prerequisite_does_not_consume_queue(self):
        self.store.close()
        checks = {"blockers": ["adapter missing"], "devices": []}
        with patch("main.doctor", return_value=checks), patch("main.Phone") as phone, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["--config", str(self.config_path), "run"]), 2)
        phone.assert_not_called()
        self.store = Store(self.config).open_readonly()
        self.assertEqual(self.store.report()["task_counts"], {"pending": 4})
        self.assertEqual(sum(t["attempts"] for t in self.store.report()["tasks"]), 0)

    def test_probe_failure_keeps_diagnostics_and_continues_other_devices(self):
        class ProbePhone(FakePhone):
            def refresh(self):
                super().refresh()
                if self.device["name"] == "phone0":
                    raise PhoneError("home unavailable")
        self.store.close()
        with patch("main.Phone", ProbePhone), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["--config", str(self.config_path), "probe"]), 2)
        self.store = Store(self.config).open_readonly()
        for index in range(3):
            directory = self.store.output / "reports" / ("probe-phone" + str(index))
            status = json.loads((directory / "status.json").read_text())
            self.assertEqual(status["ready"], index != 0)
            self.assertTrue((directory / "screen.xml").is_file())
            self.assertTrue((directory / "screen.png").is_file())
            self.assertTrue((directory / "initial" / "screen.xml").is_file())
        self.assertEqual(sum(action == "close" for _, action in FakePhone.calls), 3)

    def test_persisted_account_block_does_not_consume_a_task(self):
        runner = self.runner()
        runner.pacer.accounts["shared"] = {"blocked": True, "reason": "account blocked"}
        with patch("main.Phone", FakePhone):
            runner.worker(self.config["devices"][0], 0)
        self.assertEqual(self.store.report()["task_counts"], {"pending": 4})
        self.assertEqual(runner.claimed, 0)
        self.assertFalse(any(action in ("connect", "refresh", "search") for _, action in FakePhone.calls))

    def test_manifest_for_another_record_cannot_change_that_record(self):
        path = self.captured(index=1)
        self.store.save_task(0, "pending", path)
        runner = self.runner(max_items=1)
        with patch("main.artifacts.decompile", side_effect=self.decompile):
            runner.resume_local()
        self.assertEqual(self.store.report()["tasks"][0]["status"], "checkpoint_failed")
        self.assertEqual(self.store.report()["tasks"][1]["status"], "pending")
        self.assertEqual(len(self.store.report()["csv_decisions"]), 0)
        self.assertEqual(runner.claimed, 1)


if __name__ == "__main__":
    unittest.main()
