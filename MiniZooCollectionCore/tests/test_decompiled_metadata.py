"""App-declared fallback values require unchanged acquisition provenance."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from minizoo_collection import artifacts, decompiled_metadata as reader, processing

APPID = "wx0123456789abcdef"
OTHER_APPID = "wxfedcba9876543210"
FIELDS = {"name": "Declared app", "description": "Declared description",
          "developer": "Declared company", "category": "Tools",
          "icon_url": "https://example.org/icon.png"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class DecompiledMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        original = self.root / "main.wxapkg"
        original.write_bytes(b"Original package contents, preserved byte-for-byte")
        self.package = {"local_path": str(original), "sha256": artifacts.sha256_file(original),
                        "bytes": original.stat().st_size, "role": "main", "remote_path": "/cache/main.wxapkg"}
        self.output = self.root / "decompiled" / "restored-main"
        self.output.mkdir(parents=True)
        (self.output / "app.js").write_text("App({onLaunch: function() { return true; }});")
        self.manifest = {"appid": APPID, "appName": FIELDS["name"],
                         "appDescription": FIELDS["description"], "developerName": FIELDS["developer"],
                         "category": FIELDS["category"], "iconUrl": FIELDS["icon_url"],
                         "pages": ["index"]}
        write_json(self.output / "app.json", self.manifest)
        self.processing_path = self.root / "processing.json"
        self.build_receipt()

    def build_receipt(self):
        """Create the same pinned files emitted by the existing decompiler."""
        self.receipt = {"appid": APPID, "command_sha256": "c" * 64,
            "package_sha256": self.package["sha256"], "role": "main",
            "output": str(self.output), "log": str(self.root / "logs" / "decompile.log"),
            "files": artifacts._verify_decompiled(self.output, "main"),
            "status": "verified", "method": "configured_external_decompiler"}
        self.receipt_path = self.root / "checkpoints" / (
            "decompile-" + self.package["sha256"] + "-main-" + "c" * 12 + ".json")
        write_json(self.receipt_path, self.receipt)
        metadata_path = self.root / "package_metadata.json"
        write_json(metadata_path, [{**self.package, "schema": processing.SCHEMA,
                                    "observed_appid": APPID, "attempt_id": "fixture-attempt"}])
        self.source = {"schema": processing.SCHEMA, "observed_appid": APPID,
            "attempt_id": "fixture-attempt", "package_acquisition_status": "acquired",
            "decompilation_status": "verified", "metadata_path": str(metadata_path),
            "metadata_sha256": artifacts.sha256_file(metadata_path), "decompiled": [self.receipt]}
        write_json(self.processing_path, self.source)

    def extract(self):
        return reader.extract_report(self.processing_path, expected_appid=APPID,
                                     expected_attempt_id="fixture-attempt")

    def replay(self, report):
        return reader.replay_report(report, expected_appid=APPID,
                                    expected_package_sha256=self.package["sha256"],
                                    expected_attempt_id="fixture-attempt")

    def test_declarations_replay_with_original_identity_and_file_provenance(self):
        result = self.extract()
        self.assertEqual(result["fields"], FIELDS)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["provenance"]["name"][0]["pointer"], "/appName")
        self.assertEqual(result["provenance"]["category"][0]["kind"], "app_declared")
        self.assertEqual(self.replay(result), result)
        self.assertFalse({"version", "version_code", "collection_time", "size"} & result["fields"].keys())

    def test_genuine_template_extension_fields_are_host_bound(self):
        write_json(self.output / "app.json", {"pages": ["index"], "extAppid": OTHER_APPID,
            "ext": {"appid": APPID, "appName": "尊享健身", "appLogo": "http://wx.qlogo.cn/icon/0",
                    "name": "Template vendor", "description": "Vendor description", "version": "9.9"}})
        self.build_receipt()
        result = self.extract()
        self.assertEqual(result["fields"], {"name": "尊享健身", "icon_url": "http://wx.qlogo.cn/icon/0"})
        self.assertEqual(result["provenance"]["name"][0]["pointer"], "/ext/appName")
        self.replay(result)

    def test_page_titles_vendor_package_and_sdk_values_are_not_app_metadata(self):
        write_json(self.output / "app.json", {"appid": APPID, "pages": ["index"],
            "name": "npm-like project", "author": "Build tool", "version": "8.0.0",
            "window": {"navigationBarTitleText": "Home"}, "SDKVersion": "3.0.0",
            "plugins": {"vendor": {"appid": OTHER_APPID, "appName": "Plugin"}},
            "ext": {"name": "Vendor", "appName": "No identity"}})
        write_json(self.output / "package.json", {"appid": APPID, "appName": "Dependency",
                                                   "author": "NPM author", "version": "999"})
        write_json(self.output / "pages" / "app.json", self.manifest)
        write_json(self.output / "vendor" / "manifest.json", self.manifest)
        self.build_receipt()
        self.assertEqual(self.extract()["fields"], {})

    def test_missing_identity_is_not_filled_from_processing_or_ext_appid(self):
        write_json(self.output / "app.json", {"extAppid": APPID, "appName": "Unbound name",
                                               "description": "Unbound description"})
        self.build_receipt()
        result = self.extract()
        self.assertEqual(result["fields"], {})
        self.assertEqual(result["status"], "no_app_metadata")

    def test_conflicting_values_are_withheld_without_discarding_independent_fields(self):
        write_json(self.output / "game.json", {"appid": APPID, "appName": "Different name"})
        self.build_receipt()
        result = self.extract()
        self.assertNotIn("name", result["fields"])
        self.assertEqual(result["fields"]["description"], FIELDS["description"])
        self.assertEqual(result["conflicts"]["name"], ["Declared app", "Different name"])

    def test_wrong_app_declaration_withholds_all_fallback_fields(self):
        write_json(self.output / "game.json", {"appid": OTHER_APPID, "appName": "Other app"})
        self.build_receipt()
        result = self.extract()
        self.assertEqual(result["fields"], {})
        self.assertEqual(result["status"], "identity_conflict")
        self.assertEqual(result["identity_conflicts"][0]["declared_appids"], [OTHER_APPID])

    def test_manually_filled_fields_cannot_pass_replay(self):
        result = self.extract()
        result["fields"]["name"] = "Invented name"
        with self.assertRaises(ValueError):
            self.replay(result)

    def test_runtime_identity_and_attempt_are_required_to_match(self):
        result = self.extract()
        for kwargs in ({"expected_appid": OTHER_APPID}, {"expected_attempt_id": "other-attempt"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                reader.extract_report(self.processing_path, **kwargs)
        with self.assertRaises(ValueError):
            reader.replay_report(result, expected_appid=APPID, expected_package_sha256="f" * 64)

    def test_changes_to_each_pinned_source_are_detected(self):
        for key in ("original", "manifest", "script", "receipt", "metadata", "processing"):
            paths = {"original": Path(self.package["local_path"]), "manifest": self.output / "app.json",
                     "script": self.output / "app.js", "receipt": self.receipt_path,
                     "metadata": Path(self.source["metadata_path"]), "processing": self.processing_path}
            result = self.extract()
            path = paths[key]
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            try:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    self.replay(result)
            finally:
                path.write_bytes(original)

    def test_legacy_receipt_order_is_irrelevant_but_duplicate_paths_are_rejected(self):
        self.receipt["files"].reverse()
        write_json(self.receipt_path, self.receipt)
        write_json(self.processing_path, self.source)
        self.replay(self.extract())
        self.receipt["files"].append(dict(self.receipt["files"][0]))
        write_json(self.receipt_path, self.receipt)
        write_json(self.processing_path, self.source)
        with self.assertRaises(ValueError):
            self.extract()

    def test_malformed_icon_url_does_not_drop_independent_metadata(self):
        self.manifest["iconUrl"] = "https://[invalid-ipv6/icon.png"
        write_json(self.output / "app.json", self.manifest)
        self.build_receipt()
        result = self.extract()
        self.assertNotIn("icon_url", result["fields"])
        self.assertEqual(result["fields"]["name"], FIELDS["name"])

    def test_added_output_and_symbolic_links_are_rejected(self):
        result = self.extract()
        extra = self.output / "unlisted.json"
        extra.write_text("{}")
        with self.assertRaises(ValueError):
            self.replay(result)
        extra.unlink()
        link = self.output / "linked.json"
        link.symlink_to(self.output / "app.json")
        with self.assertRaises(ValueError):
            self.extract()

    def test_package_and_receipt_output_path_escape_are_rejected(self):
        outside = self.root.parent / (self.root.name + "-outside.wxapkg")
        outside.write_bytes(Path(self.package["local_path"]).read_bytes())
        self.addCleanup(outside.unlink)
        metadata = [{**self.package, "local_path": str(outside), "observed_appid": APPID,
                     "attempt_id": "fixture-attempt"}]
        write_json(Path(self.source["metadata_path"]), metadata)
        self.source["metadata_sha256"] = artifacts.sha256_file(self.source["metadata_path"])
        write_json(self.processing_path, self.source)
        with self.assertRaises(ValueError):
            self.extract()

    def test_duplicate_keys_oversized_and_malformed_manifests_are_not_parsed(self):
        # Keep a separate valid main configuration so receipt creation succeeds.
        write_json(self.output / "game.json", {"pages": ["index"]})
        samples = ['{"appid":"' + APPID + '","appName":"A","appName":"B"}',
                   '{"appid":', " " * (reader.MAX_MANIFEST_BYTES + 1)]
        for sample in samples:
            (self.output / "app.json").write_text(sample)
            self.build_receipt()
            with self.subTest(sample=sample[:20]):
                self.assertEqual(self.extract()["fields"], {})

    def test_javascript_is_never_executed_and_nonpublic_icon_is_rejected(self):
        sentinel = self.root / "should-not-exist"
        (self.output / "app.js").write_text("require('fs').writeFileSync(" + json.dumps(str(sentinel)) + ", 'executed');")
        self.manifest["iconUrl"] = "file:///private/secret"
        write_json(self.output / "app.json", self.manifest)
        self.build_receipt()
        with patch("subprocess.run", side_effect=AssertionError("Must not run package code")):
            result = self.extract()
            self.replay(result)
        self.assertNotIn("icon_url", result["fields"])
        self.assertFalse(sentinel.exists())

    def test_offline_report_preserves_original_processing_and_rejects_overwrite(self):
        before = self.processing_path.read_bytes()
        with self.assertRaises(ValueError):
            reader.write_report(self.processing_path, self.output / "new-report.json")
        self.assertFalse((self.output / "new-report.json").exists())
        target = self.root / "new-evidence" / "metadata.json"
        saved = reader.write_report(self.processing_path, target, expected_appid=APPID)
        verified = reader.verify_report(target, saved["sha256"], expected_appid=APPID,
                                        expected_package_sha256=self.package["sha256"])
        self.assertEqual(verified, saved["report"])
        self.assertEqual(self.processing_path.read_bytes(), before)
        with self.assertRaises(ValueError):
            reader.write_report(self.processing_path, target)
        with self.assertRaises(ValueError):
            reader.verify_report(target, "f" * 64, expected_appid=APPID,
                                 expected_package_sha256=self.package["sha256"])

    def run_processing(self, report_error=None):
        extraction = {"status": "parsed"}
        with patch.object(processing, "extract_package", return_value=extraction), \
                patch.object(artifacts, "decompile", return_value=[self.receipt]):
            if report_error:
                with patch.object(reader, "write_report", side_effect=report_error):
                    return processing.process_acquisition(self.root, [self.package], appid=APPID,
                        attempt_id="fixture-attempt", coverage="complete")
            return processing.process_acquisition(self.root, [self.package], appid=APPID,
                attempt_id="fixture-attempt", coverage="complete")

    def test_processing_runs_second_pass_without_a_circular_processing_hash(self):
        result = self.run_processing()
        self.assertEqual(result["decompiled_metadata_status"], "verified")
        saved = reader.verify_report(result["decompiled_metadata_path"], result["decompiled_metadata_sha256"],
            expected_appid=APPID, expected_package_sha256=self.package["sha256"])
        self.assertNotEqual(saved["processing_path"], str(self.processing_path))
        self.assertEqual(saved["fields"], FIELDS)
        self.assertEqual(result["package_acquisition_status"], "acquired")

    def test_optional_metadata_failure_preserves_acquisition_and_decompilation(self):
        result = self.run_processing(OSError("Disk unavailable for optional report"))
        self.assertEqual(result["decompiled_metadata_status"], "failed")
        self.assertEqual(result["decompilation_status"], "verified")
        self.assertEqual(result["package_acquisition_status"], "acquired")
        self.assertEqual(artifacts.sha256_file(self.package["local_path"]), self.package["sha256"])


if __name__ == "__main__":
    unittest.main()
