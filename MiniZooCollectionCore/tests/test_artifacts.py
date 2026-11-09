import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from minizoo_collection import artifacts


class ArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.package = self.root / "source.wxapkg"
        self.package.write_bytes(b"test package bytes")
        self.remote = artifacts.CACHE_ROOT + "/general/_123_4.wxapkg"
        self.digest = hashlib.sha256(self.package.read_bytes()).hexdigest()
        self.packages = [{"remote_path": self.remote, "sha256": self.digest,
                          "bytes": self.package.stat().st_size, "local_path": str(self.package)}]
        self.evidence = {
            "attempt_id": "attempt-1", "appid": "wx0123456789abcdef",
            "source": "test fixture runtime loaded-package metadata",
            "package_binding": "runtime_metadata", "active_loaded_release": True,
            "coverage": "complete", "expected_remote_paths": [self.remote],
            "version": "4", "version_kind": "integer",
            "version_source": "test fixture runtime release record",
            "packages": [{"remote_path": self.remote, "sha256": self.digest,
                          "role": "main", "loaded": True}],
        }

    def validate(self):
        evidence_path = self.root / "evidence.json"
        evidence_path.write_text(json.dumps(self.evidence), encoding="utf-8")
        return artifacts.validate_evidence(evidence_path, "wx0123456789abcdef", self.packages, "attempt-1")

    def test_valid_evidence_joins_verified_bytes_and_roles(self):
        result = self.validate()
        self.assertEqual(result["packages"][0]["local_path"], str(self.package))
        self.assertEqual(result["packages"][0]["role"], "main")
        self.assertEqual(result["version"], "4")

    def test_wrong_identity_old_attempt_and_cache_only_binding_rejected(self):
        for key, value in (("appid", "wx1111111111111111"), ("attempt_id", "previous"),
                           ("package_binding", "cache_delta"), ("active_loaded_release", False)):
            with self.subTest(key=key):
                original = self.evidence[key]
                self.evidence[key] = value
                with self.assertRaises(artifacts.ArtifactError):
                    self.validate()
                self.evidence[key] = original

    def test_changed_local_bytes_and_runtime_hash_rejected(self):
        self.evidence["packages"][0]["sha256"] = "0" * 64
        with self.assertRaises(artifacts.ArtifactError):
            self.validate()
        self.evidence["packages"][0]["sha256"] = self.digest
        self.package.write_bytes(b"substituted bytes")
        with self.assertRaises(artifacts.ArtifactError):
            self.validate()

    def test_complete_coverage_requires_full_inventory(self):
        self.evidence["expected_remote_paths"].append(artifacts.CACHE_ROOT + "/missing.wxapkg")
        with self.assertRaises(artifacts.ArtifactError):
            self.validate()
        self.evidence["coverage"] = "partial"
        self.evidence["coverage_limits"] = ["Unvisited section has an uncaptured subpackage"]
        self.assertEqual(self.validate()["coverage"], "partial")

    def test_cached_subpackage_requires_runtime_membership_and_matching_md5(self):
        subfile = self.root / "sub.wxapkg"
        subfile.write_bytes(b"cached active-release subpackage")
        remote = artifacts.CACHE_ROOT + "/general/sub.wxapkg"
        checksum = artifacts.sha256_file(subfile)
        self.packages.append({"remote_path": remote, "sha256": checksum,
                              "local_path": str(subfile), "bytes": subfile.stat().st_size})
        entry = {"remote_path": remote, "sha256": checksum, "role": "subpackage", "loaded": False,
                 "active_release_member": True, "availability": "cache_verified", "module_root": "extra",
                 "runtime_md5": hashlib.md5(subfile.read_bytes()).hexdigest()}
        self.evidence["packages"].append(entry)
        self.evidence["expected_remote_paths"].append(remote)
        result = self.validate()
        self.assertEqual(result["coverage"], "complete")
        self.assertFalse(result["packages"][1]["loaded"])
        for field, bad in (("runtime_md5", "0" * 32), ("active_release_member", False),
                           ("availability", "filename_guess")):
            original = entry[field]
            entry[field] = bad
            with self.assertRaises(artifacts.ArtifactError):
                self.validate()
            entry[field] = original
        self.evidence["packages"][0]["loaded"] = False
        with self.assertRaises(artifacts.ArtifactError):
            self.validate()

    def test_version_unknown_and_semver_not_decimal(self):
        self.evidence["version"] = None
        self.assertIsNone(self.validate()["version"])
        self.evidence.update(version="1.10.0", version_kind="semver")
        self.assertEqual(self.validate()["version"], "1.10.0")
        for invalid in ("1.2", "01.2.3", "1.2.3-01", 1.2, True):
            self.evidence["version"] = invalid
            with self.assertRaises(artifacts.ArtifactError):
                self.validate()

    def test_capture_preserves_bytes_but_never_assigns_appid(self):
        with patch("minizoo_collection.artifacts.inventory", return_value={self.remote: self.digest}), \
                patch("minizoo_collection.artifacts._read_device", return_value=self.package.read_bytes()):
            copied = artifacts.capture("serial", {}, self.root / "collection")
            self.assertEqual(copied[0]["sha256"], self.digest)
            self.assertNotIn("appid", copied[0])
            self.assertEqual(Path(copied[0]["local_path"]).read_bytes(), self.package.read_bytes())
            self.assertEqual(artifacts.capture("serial", {self.remote: self.digest}, self.root / "collection"), [])

    def test_hash_mismatch_preserves_diagnostic_and_fails(self):
        with patch("minizoo_collection.artifacts.inventory", return_value={self.remote: self.digest}), \
                patch("minizoo_collection.artifacts._read_device", return_value=b"other"):
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.capture("serial", {}, self.root / "collection")
        self.assertEqual(len(list((self.root / "collection/logs").glob("unverified-*.wxapkg"))), 1)
        self.assertEqual(list((self.root / "collection/packages").iterdir()), [])

    def test_inventory_rejects_traversal(self):
        raw = (self.digest + "  " + artifacts.CACHE_ROOT + "/../../outside.wxapkg\n").encode()
        with patch("minizoo_collection.artifacts._read_device", return_value=raw):
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.inventory("serial")

    def test_selected_capture_keeps_prefetched_bytes_without_claiming_identity(self):
        with patch("minizoo_collection.artifacts.inventory", side_effect=AssertionError("no delta needed")), \
                patch("minizoo_collection.artifacts._read_device", return_value=self.package.read_bytes()) as read:
            copied = artifacts.capture_selected("serial", [self.remote, self.remote], self.root / "selected")
        read.assert_called_once()
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0]["sha256"], self.digest)
        self.assertEqual(Path(copied[0]["local_path"]).read_bytes(), self.package.read_bytes())
        self.assertNotIn("appid", copied[0])
        receipt = next((self.root / "selected/logs").glob("capture-selected-*.json"))
        self.assertEqual(json.loads(receipt.read_text())["attribution"], "requires_independent_runtime_evidence")

    def test_selected_capture_rejects_all_unsafe_paths_before_reading(self):
        with patch("minizoo_collection.artifacts._read_device") as read:
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.capture_selected("serial", [self.remote, artifacts.CACHE_ROOT + "/../../private.wxapkg"],
                                           self.root / "selected")
        read.assert_not_called()
        self.assertFalse((self.root / "selected").exists())

    def test_selected_capture_failure_preserves_successful_bytes_and_error_receipt(self):
        another = artifacts.CACHE_ROOT + "/another.wxapkg"
        with patch("minizoo_collection.artifacts._read_device", side_effect=[self.package.read_bytes(), b""]):
            with self.assertRaisesRegex(artifacts.ArtifactError, "empty"):
                artifacts.capture_selected("serial", [self.remote, another], self.root / "selected")
        self.assertEqual((self.root / "selected/packages" / (self.digest + ".wxapkg")).read_bytes(),
                         self.package.read_bytes())
        receipt = json.loads(next((self.root / "selected/logs").glob("capture-selected-*.json")).read_text())
        self.assertEqual(len(receipt["packages"]), 1)
        self.assertEqual(len(receipt["errors"]), 1)

    def test_decompiler_requires_real_outputs_not_empty_directory(self):
        packages = self.validate()["packages"]
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.decompile(packages, self.root / "collection", self.evidence["appid"], [])
        command = [sys.executable, "-c", "pass", "{package}", "{output}"]
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.decompile(packages, self.root / "collection", self.evidence["appid"], command)

    def test_decompiler_verifies_app_javascript_and_json(self):
        packages = self.validate()["packages"]
        fixture = self.root / "fixture_decompiler.py"
        fixture.write_text(
            "import pathlib,sys\n"
            "p=pathlib.Path(sys.argv[2])\n"
            "(p/'app.js').write_text('App({onLaunch: function() {}});')\n"
            "(p/'app.json').write_text('{\"pages\":[\"pages/index\"]}')\n"
        )
        command = [sys.executable, str(fixture), "{package}", "{output}"]
        outputs = artifacts.decompile(packages, self.root / "collection", self.evidence["appid"], command)
        self.assertEqual(outputs[0]["status"], "verified")
        self.assertEqual(len(outputs[0]["files"]), 2)
        with patch("minizoo_collection.artifacts.subprocess.run") as launch:
            resumed = artifacts.decompile(packages, self.root / "collection", self.evidence["appid"], command)
        launch.assert_not_called()
        self.assertEqual(resumed, outputs)

    def test_resume_does_not_repeat_main_after_subpackage_interruption(self):
        packages = self.validate()["packages"]
        subpackage = self.root / "sub.wxapkg"
        subpackage.write_bytes(b"subpackage fixture")
        packages.append({"local_path": str(subpackage), "sha256": artifacts.sha256_file(subpackage),
                         "bytes": subpackage.stat().st_size, "role": "subpackage"})
        command = ["fixture-decompiler", "{package}", "{output}"]

        def complete_main_then_interrupt(arguments, **kwargs):
            if arguments[1] == str(subpackage):
                raise KeyboardInterrupt()
            output = Path(arguments[2])
            (output / "app.js").write_text("App({onLaunch: function() {}});")
            (output / "app.json").write_text('{"pages":["pages/index"]}')
            from subprocess import CompletedProcess
            return CompletedProcess(arguments, 0)

        with patch("minizoo_collection.artifacts.subprocess.run", side_effect=complete_main_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                artifacts.decompile(packages, self.root / "collection", self.evidence["appid"], command)
        self.assertEqual(len(list((self.root / "collection/checkpoints").glob("*.json"))), 1)

        def complete_subpackage(arguments, **kwargs):
            self.assertEqual(arguments[1], str(subpackage))
            (Path(arguments[2]) / "page.js").write_text("Page({onLoad: function() {}});")
            from subprocess import CompletedProcess
            return CompletedProcess(arguments, 0)

        with patch("minizoo_collection.artifacts.subprocess.run", side_effect=complete_subpackage) as launch:
            resumed = artifacts.decompile(packages, self.root / "collection", self.evidence["appid"], command)
        launch.assert_called_once()
        self.assertEqual(len(resumed), 2)
        self.assertEqual(len(list((self.root / "collection/checkpoints").glob("*.json"))), 2)

        Path(resumed[0]["files"][0]["path"]).write_text("corrupted saved output")
        with patch("minizoo_collection.artifacts.subprocess.run") as launch:
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.decompile(packages, self.root / "collection", self.evidence["appid"], command)
        launch.assert_not_called()

    def test_receipt_cannot_redirect_output_outside_collection(self):
        outside = self.root / "outside"
        outside.mkdir()
        receipt = self.root / "receipt.json"
        receipt.write_text(json.dumps({"appid": self.evidence["appid"], "command_sha256": "command",
                                       "package_sha256": self.digest, "role": "main",
                                       "status": "verified", "method": "configured_external_decompiler",
                                       "output": str(outside), "files": []}))
        with self.assertRaises(artifacts.ArtifactError):
            artifacts._resume_decompiled(receipt, {"sha256": self.digest, "role": "main"},
                                         self.evidence["appid"], "command", self.root / "collection/decompiled")

    def test_decompiler_symlinks_are_rejected(self):
        output = self.root / "output"
        output.mkdir()
        (output / "app.js").symlink_to(self.package)
        with self.assertRaises(artifacts.ArtifactError):
            artifacts._verify_decompiled(output, "main")

    def test_subpackage_receives_verified_main_first_and_resumes_with_it(self):
        main = dict(self.validate()["packages"][0])
        subfile = self.root / "sub.wxapkg"
        subfile.write_bytes(b"subpackage fixture")
        sub = {"local_path": str(subfile), "sha256": artifacts.sha256_file(subfile),
               "bytes": subfile.stat().st_size, "role": "subpackage"}
        command = ["fixture", "{package}", "{output}", "{main_output}"]
        calls = []

        def restore(arguments, **kwargs):
            from subprocess import CompletedProcess
            source, output, main_output = arguments[1:]
            calls.append(source)
            output = Path(output)
            if source == str(self.package):
                self.assertEqual(main_output, "")
                (output / "app.js").write_text("App({onLaunch: function() {}});")
                (output / "app.json").write_text('{"pages":["index"]}')
            else:
                self.assertTrue((Path(main_output) / "app.json").is_file())
                (output / "page.js").write_text("Page({onLoad: function() {}});")
            return CompletedProcess(arguments, 0)

        with patch("minizoo_collection.artifacts.subprocess.run", side_effect=restore):
            first = artifacts.decompile([sub, main], self.root / "collection", self.evidence["appid"], command)
        self.assertEqual(calls, [str(self.package), str(subfile)])
        self.assertEqual([item["role"] for item in first], ["main", "subpackage"])
        with patch("minizoo_collection.artifacts.subprocess.run") as run:
            self.assertEqual(artifacts.decompile([sub, main], self.root / "collection", self.evidence["appid"], command), first)
        run.assert_not_called()
        # The same subpackage with a different main cannot reuse an old decompilation.
        self.package.write_bytes(b"different main fixture")
        main.update(sha256=artifacts.sha256_file(self.package), bytes=self.package.stat().st_size)
        calls.clear()
        with patch("minizoo_collection.artifacts.subprocess.run", side_effect=restore):
            second = artifacts.decompile([sub, main], self.root / "collection", self.evidence["appid"], command)
        self.assertEqual(calls, [str(self.package), str(subfile)])
        self.assertNotEqual(second[1]["output"], first[1]["output"])


if __name__ == "__main__":
    unittest.main()
