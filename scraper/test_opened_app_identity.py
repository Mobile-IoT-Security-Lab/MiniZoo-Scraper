"""Offline capture -> manifest -> extraction -> merge -> catalogue regression tests."""
import copy
import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import opened_app_identity as identity
import wechat_csv_scraper as scraper
import build_final_miniapp_csvs as builder
from automation import wechat_active_merge as merge
from test_package_metadata import package

QUERIED = "wx1111111111111111"
OPENED = "wx2222222222222222"
REMOTE = identity.CACHE_ROOT + "/general/actual.wxapkg"


def records(data, attempt="attempt", appid=OPENED):
    checksum = hashlib.md5(data).hexdigest()
    common = {"kind": "runtime-package-metadata", "attempt_id": attempt, "process_id": 42,
              "observed_at": "2026-11-03T11:00:00Z"}
    runtime = dict(common, runtime_class="com.tencent.mm.plugin.appbrand.runtime",
                   objects=[{"path": "runtime", "identity_hash": "runtime-42"}],
                   lifecycle=[{"source": "android.view.View", "shown": True, "window_focus": True}],
                   values=[
                       {"path": "runtime.n", "declaring_class": identity.RUNTIME_CLASS, "value": appid},
                       {"path": "runtime.p.N.g", "declaring_class": "config.WxaAttributes$WxaVersionInfo", "value": checksum},
                       {"path": "runtime.p.N.d", "declaring_class": "config.WxaAttributes$WxaVersionInfo", "type": "int", "value": "7"},
                       {"path": "runtime.p.appName", "declaring_class": "config.AppBrandInitConfig", "value": "Actually opened"},
                       {"path": "runtime.p.username", "declaring_class": "config.AppBrandInitConfig", "value": "gh_actual@app"},
                   ])
    config = dict(common, runtime_class="config.AppBrandSysConfigWC",
                  objects=[{"path": "runtime", "class_name": "config.AppBrandSysConfigWC", "identity_hash": "config-42"},
                           {"path": "runtime.r", "class_name": "appcache.WxaPkgWrappingInfo", "identity_hash": "pkg-42"}],
                  values=[{"path": "runtime.appId", "value": appid},
                          {"path": "runtime.r.pkgPath", "value": REMOTE},
                          {"path": "runtime.r.md5", "value": checksum},
                          {"path": "runtime.r.pkgVersion", "value": "7"}],
                  collections=[])
    return [runtime, config]


class FakeProbe:
    serial = "fixture-device"

    def __init__(self, messages, change=None):
        self.messages, self.change = messages, change
        self.calls = 0

    def observe(self, attempt, directory, pid=None):
        result = copy.deepcopy(self.messages)
        for item in result:
            item["attempt_id"] = attempt
        if self.calls and self.change:
            self.change(result)
        self.calls += 1
        Path(directory).mkdir(parents=True)
        identity.write_json(Path(directory) / "messages.json", result)
        return result


class OpenedAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / "collection_results/active_runs/run/device_fixture-device"
        self.run.mkdir(parents=True)
        self.data = package([("/app.json", {"pages": ["index"], "appName": "Package title"})])
        self.messages = records(self.data)

    def pull(self, remotes, destination):
        destination.mkdir(parents=True)
        for index, remote in enumerate(remotes, 1):
            (destination / (str(index).zfill(2) + "_" + Path(remote).name)).write_bytes(self.data)
        return [(p, "/staging/" + Path(p).name) for p in remotes]

    def capture(self, messages=None, change=None, data=None):
        query = {"appid": QUERIED, "name": "Requested name", "developer": "Requested developer",
                 "app_version": "999", "description": "Requested description"}
        rec = scraper.make_manifest_record(query, scraper.manifest_fields(), id_column="appid")
        rec["device_serial"] = FakeProbe.serial
        evidence, receipt = identity.collect_opened_app(
            FakeProbe(messages or self.messages, change), rec["attempt_id"],
            self.run / "attempts" / rec["attempt_id"], self.pull)
        scraper.apply_opened_collection(rec, evidence, receipt, self.run)
        rec["collected_at"] = "2026-11-03T11:00:30+00:00"
        return rec, evidence

    def manifest(self, rec):
        builder.write_csv(self.run / "manifest.csv", scraper.manifest_fields(), [rec])

    def do_merge(self):
        marker = self.root / "collection_results/active_runs/CURRENT"
        marker.write_text(str(self.run.parent))
        out = self.root / "collection_results/all_mini_apps"
        with patch.multiple(merge, ROOT=self.root, OUT=out, ACTIVE_MARKER=marker,
                            SETTINGS=SimpleNamespace(data=self.root / "collection_results")):
            return merge.merge_active()

    def test_query_is_context_and_owned_fields_start_empty(self):
        rec = scraper.make_manifest_record({"appid": QUERIED, "name": "Requested", "developer": "wrong"},
                                          scraper.manifest_fields())
        self.assertEqual(rec["queried_appid"], QUERIED)
        self.assertEqual(rec["queried_name"], "Requested")
        for key in ("appid", "name", "developer", "description", "app_version"):
            self.assertEqual(rec[key], "")

    def test_observed_other_app_is_owner_without_query_metadata_fallback(self):
        rec, _ = self.capture()
        self.assertEqual(rec["appid"], OPENED)
        self.assertEqual(rec["name"], "Actually opened")
        self.assertEqual(rec["queried_appid"], QUERIED)
        self.assertEqual(rec["query_match"], "different_appid")
        self.assertEqual(rec["app_version"], "7")
        self.assertEqual(rec["developer"], "")
        self.assertEqual(rec["description"], "")
        identity.load_verified_receipt(rec, self.run)

    def test_focused_runtime_is_discovered_even_with_wrong_target_hint(self):
        self.messages[0].update(target_appid=QUERIED, target_found=False)
        self.assertEqual(identity.select_opened_binding(self.messages, "attempt")["appid"], OPENED)

    def test_background_runtime_cannot_supply_identity(self):
        self.messages[0]["lifecycle"][0]["shown"] = False
        with self.assertRaises(identity.EvidenceError):
            identity.select_opened_binding(self.messages, "attempt")

    def test_unrelated_appid_reference_cannot_supply_identity(self):
        self.messages[0]["values"][0]["declaring_class"] = "some.Plugin"
        with self.assertRaises(identity.EvidenceError):
            identity.select_opened_binding(self.messages, "attempt")

    def test_two_focused_apps_are_ambiguous(self):
        other = copy.deepcopy(self.messages[0])
        other["objects"][0]["identity_hash"] = "other"
        other["values"][0]["value"] = QUERIED
        with self.assertRaises(identity.EvidenceError):
            identity.select_opened_binding(self.messages + [other], "attempt")

    def test_incomplete_runtime_enumeration_cannot_certify_uniqueness(self):
        for message in ({"kind": "metadata-class-complete", "instances": 7},
                        {"kind": "metadata-probe-error", "error": "incomplete"}):
            with self.subTest(message=message):
                message["class_name"] = identity.RUNTIME_CLASS
                with self.assertRaises(identity.EvidenceError):
                    identity.select_opened_binding(self.messages + [message], "attempt")

    def public_metadata(self, appid, name):
        return {"kind": "runtime-public-metadata", "attempt_id": "attempt", "process_id": 42,
                "appid": appid, "metadata_class": "com.tencent.mm.plugin.appbrand.config.WxaAttributes",
                "identity_field": {"field": "field_appId", "value": appid},
                "fields": {"name": name},
                "provenance": {"name": {"field": "field_nickname", "value": name}}}

    def test_public_metadata_is_keyed_by_opened_app_not_the_search_request(self):
        self.messages[0]["values"] = [v for v in self.messages[0]["values"]
                                      if not v["path"].endswith("appName")]
        self.messages.extend([self.public_metadata(QUERIED, "Wrong searched app"),
                              self.public_metadata(OPENED, "Actual cached public name")])
        binding = identity.select_opened_binding(self.messages, "attempt")
        self.assertEqual(binding["metadata"]["name"], "Actual cached public name")

    def test_unrelated_or_conflicting_public_names_are_left_unknown(self):
        self.messages[0]["values"] = [v for v in self.messages[0]["values"]
                                      if not v["path"].endswith("appName")]
        for additions in ([self.public_metadata(QUERIED, "Requested only")],
                          [self.public_metadata(OPENED, "Name one"), self.public_metadata(OPENED, "Name two")]):
            with self.subTest(additions=additions):
                binding = identity.select_opened_binding(self.messages + additions, "attempt")
                self.assertNotIn("name", binding["metadata"])

    def test_wrong_attempt_and_config_process_cannot_bind(self):
        for field, value in (("attempt_id", "old"), ("process_id", 91)):
            with self.subTest(field=field):
                mutated = copy.deepcopy(self.messages)
                mutated[1][field] = value
                with self.assertRaises(identity.EvidenceError):
                    identity.select_opened_binding(mutated, "attempt")

    def test_wrong_owner_and_wrong_version_cannot_bind(self):
        for index, value in ((0, QUERIED), (3, "6")):
            with self.subTest(index=index):
                mutated = copy.deepcopy(self.messages)
                mutated[1]["values"][index]["value"] = value
                with self.assertRaises(identity.EvidenceError):
                    identity.select_opened_binding(mutated, "attempt")

    def test_hash_mismatch_keeps_bytes_but_never_assigns(self):
        self.data = package([("/app.json", {"pages": ["wrong"]})])
        with self.assertRaisesRegex(identity.EvidenceError, "checksum"):
            self.capture()
        self.assertEqual(len(list(self.run.glob("attempts/*/packages/*.wxapkg"))), 1)
        self.assertFalse(list(self.run.glob("attempts/*/opened_app_evidence.json")))

    def test_app_switch_during_capture_never_assigns(self):
        def change(items):
            items[0]["objects"][0]["identity_hash"] = "replacement-runtime"
        with self.assertRaisesRegex(identity.EvidenceError, "changed"):
            self.capture(change=change)
        self.assertFalse(list(self.run.glob("attempts/*/opened_app_evidence.json")))

    def test_missing_package_is_not_a_success(self):
        with patch.object(self, "pull", return_value=[]):
            with self.assertRaises(identity.PackageUnavailable):
                self.capture()

    def test_copied_file_order_is_from_runtime_paths_not_directory_sort(self):
        def extra(remotes, destination):
            copied = self.pull(remotes, destination)
            (destination / "00_unrelated.wxapkg").write_bytes(b"unrelated")
            return copied
        evidence, receipt = identity.collect_opened_app(
            FakeProbe(self.messages), "attempt", self.run / "capture", extra)
        self.assertEqual(len(receipt["packages"]), 1)
        self.assertEqual(receipt["packages"][0]["remote_source"], REMOTE)

    def test_metadata_export_and_history_use_actual_app(self):
        rec, _ = self.capture()
        scraper.append_package_history(self.run / "package_history.csv", rec, self.run)
        for filename in ("package_history.csv", "package_metadata.csv"):
            with self.subTest(file=filename):
                row = builder.read_csv(self.run / filename)[0]
                self.assertEqual(row["appid"], OPENED)
                self.assertEqual(row["name"], "Actually opened")
                self.assertEqual(row["queried_appid"], QUERIED)
                self.assertEqual(row["developer"], "")
                self.assertEqual(row["package_attribution"], "runtime_verified")
        row = builder.read_csv(self.run / "package_metadata.csv")[0]
        self.assertIn("Package title", json.loads(row["extracted_names_json"]))
        self.assertNotIn("Requested name", row["extracted_names_json"])

    def test_receipt_wrong_device_and_wrong_manifest_identity_rejected(self):
        rec, _ = self.capture()
        for field, value in (("appid", QUERIED), ("device_serial", "other"),
                             ("attempt_id", "old"), ("runtime_evidence_sha256", "0" * 64)):
            with self.subTest(field=field):
                changed = dict(rec, **{field: value})
                with self.assertRaises(identity.EvidenceError):
                    identity.load_verified_receipt(changed, self.run)

    def test_manifest_cannot_add_unrelated_files_or_omit_hashes(self):
        rec, _ = self.capture()
        for field in ("local_files", "sha256", "remote_sources"):
            with self.subTest(field=field):
                with self.assertRaises(identity.EvidenceError):
                    identity.load_verified_receipt(dict(rec, **{field: rec[field] + ";extra"}), self.run)

    def test_changed_package_after_capture_rejected(self):
        rec, _ = self.capture()
        (self.run / rec["local_files"]).write_bytes(b"changed")
        with self.assertRaisesRegex(identity.EvidenceError, "bytes"):
            identity.load_verified_receipt(rec, self.run)

    def test_changed_runtime_observation_after_capture_rejected(self):
        rec, evidence = self.capture()
        before = evidence.parent / json.loads(evidence.read_text())["before"]
        before.write_text("[]")
        with self.assertRaisesRegex(identity.EvidenceError, "observation checksum"):
            identity.load_verified_receipt(rec, self.run)

    def test_canonical_provenance_symlink_rejected(self):
        rec, _ = self.capture()
        self.manifest(rec)
        out = self.root / "collection_results/all_mini_apps" / OPENED
        out.mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text("[]")
        (out / (rec["sha256"] + ".runtime-provenance.json")).symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.do_merge()
        self.assertEqual(outside.read_text(), "[]")

    def test_name_only_query_never_gains_a_fabricated_requested_appid(self):
        rec, _ = self.capture()
        rec["queried_appid"] = ""
        scraper.append_package_history(self.run / "package_history.csv", rec, self.run)
        row = builder.read_csv(self.run / "package_metadata.csv")[0]
        self.assertEqual(row["appid"], OPENED)
        self.assertEqual(row["queried_appid"], "")

    def test_older_observation_does_not_overwrite_newer_runtime_metadata(self):
        apps = {}
        first = dict(appid=OPENED, name="New name", app_version="8",
                     identity_status="runtime_verified", metadata_verified_at="2026-11-03T11:00:00Z")
        second = dict(first, name="Old name", app_version="7", metadata_verified_at="2026-11-02T11:00:00Z")
        builder.add_source(apps, first, "new")
        builder.add_source(apps, second, "old")
        self.assertEqual(apps[OPENED]["name"], "New name")
        self.assertEqual(apps[OPENED]["app_version"], "8")

    def test_unsafe_runtime_paths_rejected(self):
        for value in ("/etc/private.wxapkg", identity.CACHE_ROOT + "/../secret.wxapkg",
                      REMOTE + "\n", REMOTE.replace(".wxapkg", ".txt")):
            with self.subTest(path=value):
                with self.assertRaises(identity.EvidenceError):
                    identity.canonical_path(value)

    def test_merge_and_catalogue_track_opened_app_and_leave_query_unresolved(self):
        rec, _ = self.capture()
        self.manifest(rec)
        result = self.do_merge()
        self.assertEqual(result["new_packages"], 1)
        out = self.root / "collection_results/all_mini_apps"
        self.assertTrue((out / OPENED / (rec["sha256"] + ".wxapkg")).is_file())
        self.assertFalse((out / QUERIED).exists())
        with patch.multiple(builder, ROOT=self.root, OUT=out, DB=self.root / "absent.db"), patch("sys.stdout", new=io.StringIO()):
            builder.main()
        status = {r["appid"]: r for r in builder.read_csv(out / "mini_app_scraping_status.csv")}
        self.assertEqual(status[OPENED]["is_scraped"], "1")
        self.assertEqual(status[OPENED]["name"], "Actually opened")
        self.assertEqual(status[OPENED]["identity_status"], "runtime_verified")
        self.assertEqual(status[QUERIED]["is_scraped"], "0")
        self.assertIn("opened_different_app", status[QUERIED]["statuses_seen"])

    def test_global_duplicate_content_does_not_erase_another_app_association(self):
        rec, _ = self.capture()
        self.manifest(rec)
        prior = self.root / "collection_results/all_mini_apps" / QUERIED / (rec["sha256"] + ".wxapkg")
        prior.parent.mkdir(parents=True)
        prior.write_bytes(self.data)
        self.assertEqual(self.do_merge()["new_packages"], 1)
        self.assertTrue(prior.is_file())
        self.assertEqual(self.do_merge()["new_packages"], 0)
        self.assertEqual(len(list(prior.parent.parent.glob("wx*/*.wxapkg"))), 2)

    def test_unverified_new_manifest_never_uses_requested_appid(self):
        rec = scraper.make_manifest_record({"appid": QUERIED, "name": "Requested"}, scraper.manifest_fields())
        rec.update(status="opened_identity_unverified", local_files="retained.wxapkg")
        (self.run / "retained.wxapkg").write_bytes(self.data)
        self.manifest(rec)
        self.assertEqual(self.do_merge()["new_packages"], 0)
        self.assertEqual(builder.appid_of(rec), "")

    def test_legacy_unverified_row_is_retained_without_new_canonical_assignment(self):
        rec = dict(appid=QUERIED, csv_id=QUERIED, status="ok", local_files="legacy.wxapkg")
        (self.run / "legacy.wxapkg").write_bytes(self.data)
        self.manifest(rec)
        self.assertEqual(self.do_merge()["new_packages"], 0)
        self.assertTrue((self.run / "legacy.wxapkg").is_file())

    def test_main_search_flow_is_runtime_based_end_to_end(self):
        input_csv = self.root / "input.csv"
        builder.write_csv(input_csv, ["appid", "name"], [{"appid": QUERIED, "name": "Requested"}])
        output = self.root / "main-run"
        args = ["scraper", "--csv", str(input_csv), "--output", str(output), "--serial", FakeProbe.serial,
                "--open-method", "search"]
        with patch("sys.argv", args), patch.object(scraper, "resolve_frida", return_value="frida"), \
                patch.object(scraper, "check_env", return_value=True), \
                patch.object(scraper, "RuntimeProbe", return_value=FakeProbe(self.messages)), \
                patch.object(scraper, "search_and_open_name", return_value=True), \
                patch.object(scraper, "pull_for_app", side_effect=self.pull), \
                patch.object(scraper, "fetch_weweplay_metadata") as fetch, \
                patch.object(scraper, "list_packages") as inventory, \
                patch.object(scraper, "choose_candidates") as choose, \
                patch.object(scraper, "back"), patch("sys.stdout", new=io.StringIO()):
            scraper.main()
        fetch.assert_not_called()
        inventory.assert_not_called()
        choose.assert_not_called()
        row = builder.read_csv(output / "manifest.csv")[0]
        self.assertEqual(row["appid"], OPENED)
        self.assertEqual(row["status"], "ok")
        identity.load_verified_receipt(row, output)

    def test_metadata_and_evidence_are_written_before_searching_the_next_app(self):
        input_csv = self.root / "two-inputs.csv"
        builder.write_csv(input_csv, ["appid", "name"], [
            {"appid": QUERIED, "name": "First search"},
            {"appid": "wx3333333333333333", "name": "Second search"},
        ])
        output = self.root / "inline-run"
        searches = []

        def search(name, *args, **kwargs):
            if searches:
                # Inspect files at the next navigation boundary, while main()
                # is still running: no batch exporter or follow-up job can help.
                metadata = builder.read_csv(output / "package_metadata.csv")
                self.assertEqual(len(metadata), 1)
                self.assertEqual(metadata[0]["appid"], OPENED)
                self.assertEqual(metadata[0]["extraction_status"], "plaintext_parsed")
                self.assertIn("Package title", json.loads(metadata[0]["extracted_names_json"]))
                self.assertTrue(Path(metadata[0]["extraction_evidence_path"]).is_file())
                self.assertTrue(Path(metadata[0]["extraction_summary_path"]).is_file())
                self.assertEqual(len(builder.read_csv(output / "package_history.csv")), 1)
                row = builder.read_csv(output / "manifest.csv")[0]
                identity.load_verified_receipt(row, output)
            searches.append(name)
            return True

        args = ["scraper", "--csv", str(input_csv), "--output", str(output),
                "--serial", FakeProbe.serial, "--open-method", "search"]
        with patch("sys.argv", args), patch.object(scraper, "resolve_frida", return_value="frida"), \
                patch.object(scraper, "check_env", return_value=True), \
                patch.object(scraper, "RuntimeProbe", return_value=FakeProbe(self.messages)), \
                patch.object(scraper, "search_and_open_name", side_effect=search), \
                patch.object(scraper, "pull_for_app", side_effect=self.pull), \
                patch.object(scraper, "back"), patch("sys.stdout", new=io.StringIO()):
            scraper.main()
        self.assertEqual(searches, ["First search", "Second search"])
        self.assertEqual(len(builder.read_csv(output / "package_metadata.csv")), 2)


if __name__ == "__main__":
    unittest.main()
