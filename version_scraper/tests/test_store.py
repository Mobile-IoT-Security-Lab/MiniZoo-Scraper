import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from store import Store, compare_versions, sha256, write_json


class VersionsTest(unittest.TestCase):
    def test_numeric_and_semver_order(self):
        self.assertEqual(compare_versions("10", "9"), 1)
        self.assertEqual(compare_versions("1.10.0", "1.9.0", "semver"), 1)
        versions = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
                    "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                    "1.0.0-rc.1", "1.0.0"]
        for previous, current in zip(versions, versions[1:]):
            self.assertEqual(compare_versions(current, previous, "semver"), 1)
        self.assertEqual(compare_versions("1.0.0+new", "1.0.0+old", "semver"), 0)

    def test_uncomparable_versions_never_become_zero(self):
        for value in (None, "", "1.1", "1e2", "NaN", "-1"):
            self.assertIsNone(compare_versions(value, "0"))
        self.assertIsNone(compare_versions("1.0.0-01", "1.0.0", "semver"))
        self.assertIsNone(compare_versions("01.0.0", "1.0.0", "semver"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"source_csv": str(self.root / "source.csv"),
                       "work_csv": str(self.root / "new area" / "apps.csv"),
                       "output_dir": str(self.root / "new area" / "output"),
                       "expected_records": 2}
        self.source = Path(self.config["source_csv"])
        self.source.write_bytes('appid;name;previous_app_version;device\r\nwx0000000000000001;"教育\n平台";9;old\r\nwx0000000000000002;second;;old\r\n'.encode("utf-8-sig"))
        self.original = self.source.read_bytes()
        self.store = Store(self.config)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def manifest_file(self):
        path = self.store.output / "manifest.json"
        if not path.exists():
            path.write_text('{"fixture":true}')
        return path

    def collection(self, index=0, version="10"):
        package = self.store.output / "collections" / "one.wxapkg"
        package.write_bytes(b"real-package-fixture")
        script = self.store.output / "collections" / "app.js"
        script.write_bytes(b"console.log('fixture')")
        return {"row_index": index, "appid": "wx0000000000000001" if index == 0 else "wx0000000000000002",
                "version": version, "version_kind": "integer", "identity_verified": True,
                "artifacts_verified": True, "coverage": "complete",
                "packages": [{"local_path": str(package), "sha256": sha256(package), "bytes": package.stat().st_size}],
                "decompiled": [{"local_path": str(script), "sha256": sha256(script)}],
                "metadata": {"device": "new", "invented_column": "not added"}}

    def test_exact_initial_copy_csv_count_unicode_and_upgrade(self):
        initial = self.store.initialize()
        self.assertEqual(initial["record_count"], 2)
        self.assertEqual(initial["schema"]["delimiter"], ";")
        self.assertEqual(self.store.work.read_bytes(), self.original)
        self.assertEqual(self.store.backup.read_bytes(), self.original)
        result = self.store.apply_collection(self.collection())
        self.assertEqual(result, {"status": "updated", "previous_version": "9", "version": "10"})
        rows = list(csv.DictReader(io.StringIO(self.store.work.read_text(encoding="utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0], {"appid": "wx0000000000000001", "name": "教育\n平台", "previous_app_version": "10", "device": "new"})
        self.assertEqual(rows[1]["previous_app_version"], "")
        self.assertTrue(self.store.work.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.store.apply_collection(self.collection())["status"], "unchanged")
        self.assertEqual(self.store.apply_collection(self.collection(version="8"))["status"], "older")
        self.assertTrue(self.store.report()["source_unchanged"])

    def test_missing_baseline_and_wrong_type_do_not_update(self):
        self.store.initialize()
        result = self.store.apply_collection(self.collection(index=1))
        self.assertEqual(result["status"], "baseline_missing")
        collection = self.collection()
        collection["version_kind"] = "semver"
        self.assertEqual(self.store.apply_collection(collection)["status"], "version_unknown")
        self.assertEqual(self.store.work.read_bytes(), self.original)

    def test_tampered_partial_and_wrong_identity_are_rejected(self):
        self.store.initialize()
        collection = self.collection()
        collection["coverage"] = "partial"
        self.assertEqual(self.store.apply_collection(collection)["status"], "artifacts_unverified")
        collection["coverage"] = "complete"
        collection["appid"] = "someone-else"
        self.assertEqual(self.store.apply_collection(collection)["status"], "ambiguous")
        collection["appid"] = "wx0000000000000001"
        Path(collection["packages"][0]["local_path"]).write_bytes(b"different")
        self.assertEqual(self.store.apply_collection(collection)["status"], "artifacts_unverified")
        self.assertEqual(self.store.work.read_bytes(), self.original)

    def test_existing_copy_without_provenance_is_not_replaced(self):
        self.store.work.parent.mkdir(parents=True)
        self.store.work.write_text("do not replace")
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.store.initialize()
        self.assertEqual(self.store.work.read_text(), "do not replace")

    def test_resume_preserves_upgrades_and_retains_stage(self):
        self.store.initialize()
        task = self.store.claim()
        self.assertEqual(task["row_index"], 0)
        self.store.apply_collection(self.collection())
        self.store.save_task(0, "running", manifest=self.store.output / "manifest.json")
        collection_id = self.store.record_collection(self.manifest_file())
        self.store.close()
        self.store = Store(self.config)
        self.store.initialize()
        claimed = self.store.claim()
        self.assertTrue(claimed["manifest"].endswith("manifest.json"))
        self.assertEqual(claimed["attempts"], 2)
        self.assertEqual(self.store.collections()[0]["id"], collection_id)
        self.assertIn(b";10;new", self.store.work.read_bytes())

    def test_duplicate_appids_stay_in_order_and_are_not_claimed(self):
        self.source.write_bytes(b"appid,name,previous_app_version\nwx0000000000000001,one,1\nwx0000000000000001,two,2\n")
        self.store.initialize()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.report()["task_counts"], {"invalid_record": 2})
        self.assertEqual(self.store.apply_collection(self.collection())["status"], "ambiguous")
        self.assertEqual(self.store.work.read_bytes(), self.source.read_bytes())

    def test_external_csv_edit_is_refused(self):
        self.store.initialize()
        self.store.work.write_bytes(self.original.replace(b";9;", b";99;"))
        with self.assertRaisesRegex(ValueError, "outside the pipeline"):
            self.store.apply_collection(self.collection())

    def test_journal_recovers_replace_before_manifest_commit(self):
        self.store.initialize()
        old_hash = sha256(self.store.work)
        changed = self.original.replace(b";9;", b";10;")
        self.store.initial["pending_write"] = {"before_sha256": old_hash,
                                                "after_sha256": hashlib.sha256(changed).hexdigest()}
        write_json(self.store.manifest_path, self.store.initial)
        self.store.work.write_bytes(changed)
        self.store.close()
        self.store = Store(self.config)
        self.store.initialize()
        self.assertEqual(self.store.work.read_bytes(), changed)
        self.assertNotIn("pending_write", self.store.initial)
        self.assertEqual(self.source.read_bytes(), self.original)


    def test_collection_decision_survives_a_restart(self):
        self.store.initialize()
        collection = dict(self.collection(), collection_id="collection-one")
        result = self.store.apply_collection(collection)
        self.store.close()
        self.store = Store(self.config)
        self.store.initialize()
        self.assertEqual(self.store.apply_collection(collection), result)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["previous_version"], "9")
        self.assertEqual(len(self.store.report()["csv_decisions"]), 1)

    def test_recovered_csv_journal_preserves_old_version_in_report(self):
        self.store.initialize()
        collection = dict(self.collection(), collection_id="crashed-collection")
        result = {"status": "updated", "previous_version": "9", "version": "10"}
        changed = self.original.replace(b";9;", b";10;")
        self.store.initial["pending_write"] = {
            "before_sha256": sha256(self.store.work),
            "after_sha256": hashlib.sha256(changed).hexdigest(),
            "decision": {"collection_id": "crashed-collection", "row_index": 0,
                         "appid": "wx0000000000000001", "result": result}}
        write_json(self.store.manifest_path, self.store.initial)
        self.store.work.write_bytes(changed)
        self.store.close()
        self.store = Store(self.config)
        self.store.initialize()
        self.assertEqual(self.store.apply_collection(collection), result)
        self.assertNotIn("pending_write", json.loads(self.store.manifest_path.read_text()))
        self.assertEqual(self.store.report()["csv_decisions"][0]["result"], result)

    def test_readonly_status_does_not_requeue_active_work(self):
        self.store.initialize()
        self.store.claim()
        collection_id = self.store.record_collection(self.manifest_file())
        manifest_before = self.store.manifest_path.read_bytes()
        reader = Store(self.config).open_readonly()
        try:
            report = reader.report()
            self.assertEqual(report["task_counts"], {"running": 1, "pending": 1})
            self.assertEqual(report["collection_count"], 1)
            with self.assertRaises(Exception):
                reader.save_task(0, "pending")
        finally:
            reader.close()
        self.assertEqual(self.store.report()["task_counts"], {"running": 1, "pending": 1})
        self.assertEqual(self.store.report()["collection_count"], 1)
        self.assertEqual(self.store.manifest_path.read_bytes(), manifest_before)



    def test_source_hardlink_as_work_is_rejected(self):
        self.store.work.parent.mkdir(parents=True)
        os.link(self.source, self.store.work)
        with self.assertRaisesRegex(ValueError, "must differ"):
            self.store.initialize()
        with self.assertRaisesRegex(ValueError, "must differ"):
            Store(self.config)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_output_symlink_is_rejected(self):
        self.store.output.parent.mkdir(parents=True)
        self.store.output.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.store.initialize()
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            Store(self.config)


    def test_invalid_appid_is_reported_without_rewriting_the_row(self):
        self.source.write_text("appid,name,previous_app_version\n../escape,invalid,1\n")
        original = self.source.read_bytes()
        self.store.initialize()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.report()["task_counts"], {"invalid_record": 1})
        self.assertEqual(self.store.report()["anomalies"]["invalid_appid_rows"], [0])
        self.assertEqual(self.store.work.read_bytes(), original)

    def test_registered_manifest_hash_cannot_silently_change(self):
        self.store.initialize()
        path = self.manifest_file()
        collection_id = self.store.record_collection(path)
        record = self.store.collections()[0]
        self.assertEqual(record["manifest_sha256"], sha256(path))
        self.assertEqual(record["integrity_error"], "")
        path.write_text('{"fixture":true,"version":"999"}')
        self.assertIn("changed", self.store.collections()[0]["integrity_error"])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.record_collection(path)
        self.assertEqual(self.store.report()["collections"][0]["id"], collection_id)
        self.assertEqual(self.store.report()["collections"][0]["manifest_sha256"], record["manifest_sha256"])


    def test_requeue_preserves_old_local_collection_and_csv(self):
        self.store.initialize()
        task = self.store.claim()
        path = self.manifest_file()
        self.store.save_task(task["row_index"], "partial", path, "missing component")
        self.store.save_task(1, "not_found", error="no result")
        self.store.record_collection(path)
        before_collections = self.store.report()["collections"]
        before_manifest = path.read_bytes()
        result = self.store.requeue(["wx0000000000000001", "wx0000000000000002"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["history_ids"]), 2)
        self.assertEqual(result["rows"][0]["previous_manifest"], str(path))
        self.assertEqual(self.store.report()["task_counts"], {"pending": 2})
        self.assertTrue(all(row["manifest"] is None for row in self.store.report()["tasks"]))
        self.assertEqual(self.store.report()["collections"], before_collections)
        self.assertEqual(path.read_bytes(), before_manifest)
        self.assertEqual(self.store.work.read_bytes(), self.original)
        self.assertEqual(self.source.read_bytes(), self.original)
        history = self.store.conn.execute("SELECT * FROM task_history ORDER BY id").fetchall()
        self.assertEqual([row["status"] for row in history], ["partial", "not_found"])
        self.assertEqual(history[0]["error"], "missing component")
        self.assertEqual(self.store.claim()["attempts"], 2)

    def test_requeue_validates_whole_selection_before_mutating(self):
        self.store.initialize()
        self.store.save_task(0, "partial", self.manifest_file())
        for forbidden in ("updated", "unchanged", "older", "pending", "running", "baseline_missing"):
            self.store.save_task(1, forbidden)
            with self.subTest(status=forbidden), self.assertRaisesRegex(ValueError, "cannot be requeued"):
                self.store.requeue(["wx0000000000000001", "wx0000000000000002"])
            self.assertEqual(self.store.report()["tasks"][0]["status"], "partial")
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0], 0)
        for selection in ([], ["wx0000000000000001", "wx0000000000000001"], ["../bad"], ["wx0000000000000099"]):
            with self.assertRaises(ValueError):
                self.store.requeue(selection)

    def test_requeue_rejects_duplicate_source_appids(self):
        self.source.write_text("appid,name,previous_app_version\nwx0000000000000001,one,1\nwx0000000000000001,two,2\n")
        self.store.initialize()
        with self.assertRaisesRegex(ValueError, "duplicated"):
            self.store.requeue(["wx0000000000000001"])


if __name__ == "__main__":
    unittest.main()
