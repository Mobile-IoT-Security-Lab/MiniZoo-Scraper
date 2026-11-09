import copy
import hashlib
import json
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from minizoo_collection.runtime import EvidenceError, canonical_path, finish_evidence, read_messages, select_binding, package_config_roots


APPID = "wx0123456789abcdef"
REMOTE = "/data/user/0/com.tencent.mm/MicroMsg/appbrand/pkg/general/_123_68.wxapkg"


def package_bytes(config):
    name = b"/app-config.json"
    body = json.dumps(config).encode()
    index_size = 4 + 4 + len(name) + 8
    index = struct.pack(">II", 1, len(name)) + name + struct.pack(">II", 14 + index_size, len(body))
    return b"\xbe" + struct.pack(">III", 0, index_size, len(body)) + b"\xed" + index + body


class RuntimeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.local = self.root / "main.wxapkg"
        self.local.write_bytes(b"verified main package bytes")
        self.md5 = hashlib.md5(self.local.read_bytes()).hexdigest()
        self.package = {"remote_path": canonical_path(REMOTE), "sha256": hashlib.sha256(self.local.read_bytes()).hexdigest(),
                        "local_path": str(self.local), "bytes": self.local.stat().st_size}
        common = {"kind": "runtime-package-metadata", "target_found": True, "target_appid": APPID,
                  "attempt_id": "test-attempt", "process_id": 1234, "observed_at": "2026-11-02T12:00:00Z"}
        self.runtime = dict(common, runtime_class="com.tencent.mm.plugin.appbrand.m6",
                            objects=[{"path": "runtime", "identity_hash": "runtime-id"}],
                            lifecycle=[{"source": "android.view.View", "shown": True, "window_focus": True}],
                            values=[{"path": "runtime.n", "declaring_class": "com.tencent.mm.plugin.appbrand.AppBrandRuntime", "value": APPID},
                                    {"path": "runtime.p.N.g", "declaring_class": "com.tencent.mm.plugin.appbrand.config.WxaAttributes$WxaVersionInfo", "value": self.md5},
                                    {"path": "runtime.p.N.d", "declaring_class": "com.tencent.mm.plugin.appbrand.config.WxaAttributes$WxaVersionInfo", "type": "int", "value": "68"}])
        self.config = dict(common, runtime_class="com.tencent.mm.plugin.appbrand.config.AppBrandSysConfigWC",
                           objects=[{"path": "runtime", "class_name": "com.tencent.mm.plugin.appbrand.config.AppBrandSysConfigWC",
                                     "identity_hash": "config-id"},
                                    {"path": "runtime.r", "class_name": "com.tencent.mm.plugin.appbrand.appcache.WxaPkgWrappingInfo",
                                     "identity_hash": "package-id"}],
                           values=[{"path": "runtime.r.pkgPath", "value": REMOTE},
                                   {"path": "runtime.r.md5", "value": self.md5},
                                   {"path": "runtime.r.pkgVersion", "value": "68"},
                                   {"path": "runtime.e", "value": APPID}], collections=[])
        self.records = [self.runtime, self.config]

    def bind(self):
        return select_binding(self.records, APPID, "test-attempt")

    def configure_inventory(self, roots):
        self.local.write_bytes(package_bytes({"pages": ["pages/index"],
                                              "subPackages": [{"root": root + "/"} for root in roots]}))
        self.md5 = hashlib.md5(self.local.read_bytes()).hexdigest()
        self.package.update(sha256=hashlib.sha256(self.local.read_bytes()).hexdigest(), bytes=self.local.stat().st_size)
        self.runtime["values"][1]["value"] = self.md5
        self.config["values"][1]["value"] = self.md5
        self.config["collections"] = [{"path": "runtime.r.g", "declaring_class": "com.tencent.mm.plugin.appbrand.appcache.WxaPkgWrappingInfo",
                                       "field_type": "java.util.LinkedList", "size": len(roots), "inspected": len(roots),
                                       "metadata_items": len(roots), "truncated": False}]
        self.config["modules"] = []
        result = []
        for i, root in enumerate(roots):
            local = self.root / ("sub-" + str(i) + ".wxapkg")
            local.write_bytes(("package for " + root).encode())
            md5 = hashlib.md5(local.read_bytes()).hexdigest()
            self.config["modules"].append({"path": "runtime.r.g[" + str(i) + "]",
                                           "class_name": "com.tencent.mm.plugin.appbrand.appcache.ModulePkgInfo",
                                           "fields": {"name": "/" + root + "/", "md5": md5,
                                                      "pkgVersion": "68", "pkgPath": None}})
            self.runtime["values"].append({"path": "runtime.p.N.o[" + str(i) + "].f",
                                           "declaring_class": "com.tencent.mm.plugin.appbrand.config.WxaAttributes$WxaVersionModuleInfo",
                                           "value": md5})
            result.append({"remote_path": canonical_path(REMOTE).replace("_123_68", "_sub" + str(i) + "_68"),
                           "sha256": hashlib.sha256(local.read_bytes()).hexdigest(), "bytes": local.stat().st_size,
                           "local_path": str(local)})
        return result

    def test_named_release_bound_to_active_runtime_checksum(self):
        binding = self.bind()
        self.assertEqual(binding["main"]["version"], "68")
        self.assertEqual(binding["method"], "active_runtime_version_info_content_md5")
        evidence, packages = finish_evidence(binding, [self.package], "serial", APPID, "test-attempt", self.root)
        self.assertEqual(evidence["coverage"], "partial")
        self.assertEqual(evidence["packages"][0]["sha256"], self.package["sha256"])
        self.assertEqual(packages, [self.package])

    def test_named_summary_fallback_is_consistent(self):
        for value in self.config["values"]:
            value["path"] = value["path"].replace("runtime.r.", "runtime.r.toString().")
        self.assertEqual(self.bind()["main"]["version"], "68")
        self.config["values"].append({"path": "runtime.r.pkgVersion", "value": "67"})
        with self.assertRaises(EvidenceError):
            self.bind()

    def test_active_release_module_checksum_can_bind_split_main_package(self):
        self.runtime["values"][1]["value"] = "0" * 32  # Aggregate release differs from its main component.
        self.runtime["values"].append({"path": "runtime.p.N.o[0].f",
                                       "declaring_class": "com.tencent.mm.plugin.appbrand.config.WxaAttributes$WxaVersionModuleInfo",
                                       "value": self.md5})
        self.assertEqual(self.bind()["main"]["md5"], self.md5)
        self.runtime["values"][-1]["declaring_class"] = "unrelated.LibraryConfig"
        with self.assertRaises(EvidenceError):
            self.bind()

    def test_inactive_wrong_attempt_and_mismatching_runtime_md5_rejected(self):
        for mutate in (lambda: self.runtime["lifecycle"][0].update(shown=False),
                       lambda: self.runtime.update(attempt_id="old-attempt"),
                       lambda: self.runtime["values"][1].update(value="0" * 32),
                       lambda: self.config.update(process_id=9999)):
            original = copy.deepcopy(self.records)
            mutate()
            with self.assertRaises(EvidenceError):
                self.bind()
            self.records = original
            self.runtime, self.config = self.records

    def test_two_distinct_active_target_instances_are_ambiguous(self):
        other = copy.deepcopy(self.runtime)
        other["objects"][0]["identity_hash"] = "second-runtime"
        self.records.append(other)
        with self.assertRaises(EvidenceError):
            self.bind()

    def test_reused_bytes_do_not_authorize_a_stale_release_version(self):
        self.config["values"][2]["value"] = "67"
        with self.assertRaises(EvidenceError):
            self.bind()

    def attach_config_to_runtime(self):
        """Model a real reference through the existing runtime registry deque."""
        prefix = "runtime.E[0]"
        for key in ("objects", "values", "modules", "collections"):
            for item in self.config.get(key, []):
                copied = copy.deepcopy(item)
                copied["path"] = prefix + item["path"][len("runtime"):]
                self.runtime.setdefault(key, []).append(copied)
        return prefix

    def test_direct_runtime_config_binds_when_download_md5_differs(self):
        self.configure_inventory([])
        prefix = self.attach_config_to_runtime()
        self.runtime["values"][1]["value"] = "0" * 32
        binding = self.bind()
        self.assertEqual(binding["method"], "runtime_object_graph")
        self.assertEqual(binding["binding_proof"]["config_path"], prefix)
        self.assertEqual(binding["binding_proof"]["config_identity_hash"], "config-id")
        evidence, _ = finish_evidence(binding, [self.package], "serial", APPID, "test-attempt", self.root)
        self.assertEqual(evidence["coverage"], "complete")
        self.assertEqual(evidence["binding_proof"]["package_path"], prefix + ".r")
        self.local.write_bytes(b"unrelated package")
        with self.assertRaisesRegex(EvidenceError, "checksum"):
            finish_evidence(binding, [self.package], "serial", APPID, "test-attempt", self.root)

    def test_direct_runtime_config_requires_own_identity_and_release_version(self):
        prefix = self.attach_config_to_runtime()
        self.runtime["values"][1]["value"] = "0" * 32
        for field, invalid in ((".e", "wx0000000000000000"), (".r.pkgVersion", "67")):
            value = next(v for v in self.runtime["values"] if v["path"] == prefix + field)
            previous = value["value"]
            value["value"] = invalid
            with self.assertRaises(EvidenceError):
                self.bind()
            value["value"] = previous

    def test_bare_package_reference_and_detached_other_owner_are_rejected(self):
        self.runtime["values"][1]["value"] = "0" * 32
        self.runtime["objects"].append(copy.deepcopy(self.config["objects"][1]))
        self.runtime["values"].extend(copy.deepcopy(self.config["values"][:3]))
        with self.assertRaises(EvidenceError):
            self.bind()
        self.runtime["values"][1]["value"] = self.md5
        self.config["values"][3]["value"] = "wx0000000000000000"
        with self.assertRaises(EvidenceError):
            self.bind()

    def test_direct_reference_is_preserved_when_duplicate_heap_config_matches(self):
        self.configure_inventory(["feature"])
        self.attach_config_to_runtime()
        self.assertEqual(self.bind()["method"], "runtime_object_graph")

    def test_cached_main_is_copied_only_by_verified_runtime_path(self):
        with patch("minizoo_collection.runtime.artifacts.capture_selected", return_value=[self.package]) as capture:
            evidence, packages = finish_evidence(self.bind(), [], "serial", APPID, "test-attempt", self.root)
        capture.assert_called_once_with("serial", [canonical_path(REMOTE)], self.root, adb="adb")
        self.assertEqual(packages, [self.package])
        self.assertTrue(evidence["active_loaded_release"])

    def test_captured_bytes_must_match_runtime_md5(self):
        binding = self.bind()
        self.local.write_bytes(b"different bytes")
        with self.assertRaisesRegex(EvidenceError, "checksum"):
            finish_evidence(binding, [self.package], "serial", APPID, "test-attempt", self.root)

    def test_path_alias_is_specific_and_traversal_rejected(self):
        self.assertTrue(canonical_path(REMOTE).startswith("/data/data/com.tencent.mm/"))
        for invalid in (REMOTE.replace("/general/", "/../"), REMOTE.replace("/user/0/", "/user/10/"), "/sdcard/x.wxapkg"):
            with self.assertRaises(EvidenceError):
                canonical_path(invalid)

    def test_cli_literal_and_json_messages_parse_without_executing_text(self):
        envelope = {"type": "send", "payload": self.runtime}
        raw = "message: " + repr(envelope) + " data: None\n" + json.dumps(envelope) + "\ninvalid"
        self.assertEqual(read_messages(raw), [self.runtime, self.runtime])

    def test_main_only_complete_requires_empty_inventory_and_original_config(self):
        self.configure_inventory([])
        with patch("minizoo_collection.runtime.cache_md5_inventory") as inventory:
            evidence, _ = finish_evidence(self.bind(), [self.package], "serial", APPID, "test-attempt", self.root)
        inventory.assert_not_called()
        self.assertEqual(evidence["coverage"], "complete")
        self.assertEqual(evidence["coverage_limits"], [])
        self.assertEqual(evidence["expected_remote_paths"], [self.package["remote_path"]])
        self.config["collections"] = []
        evidence, _ = finish_evidence(self.bind(), [self.package], "serial", APPID, "test-attempt", self.root)
        self.assertEqual(evidence["coverage"], "partial")

    def test_all_declared_subpackages_copied_by_hash_and_loading_not_invented(self):
        subpackages = self.configure_inventory(["feature", "nested/second"])
        cache = {p["remote_path"]: hashlib.md5(Path(p["local_path"]).read_bytes()).hexdigest() for p in subpackages}
        # The first recorded path is stale; a filename is never proof of content.
        self.config["modules"][0]["fields"]["pkgPath"] = canonical_path(REMOTE).replace("_123_68", "stale")
        cache[self.config["modules"][0]["fields"]["pkgPath"]] = "0" * 32
        with patch("minizoo_collection.runtime.cache_md5_inventory", return_value=cache), \
                patch("minizoo_collection.runtime.artifacts.capture_selected", return_value=subpackages) as capture:
            evidence, copied = finish_evidence(self.bind(), [self.package], "serial", APPID, "test-attempt", self.root)
        self.assertEqual(capture.call_count, len(subpackages))
        self.assertEqual({c.args[1][0] for c in capture.call_args_list},
                         {p["remote_path"] for p in subpackages})
        self.assertEqual(evidence["coverage"], "complete")
        self.assertEqual(len(evidence["declared_modules"]), 2)
        self.assertEqual(len(copied), 3)
        for package in evidence["packages"][1:]:
            self.assertFalse(package["loaded"])
            self.assertTrue(package["active_release_member"])
            self.assertEqual(package["availability"], "cache_verified")

    def test_missing_module_and_truncated_inventory_remain_partial(self):
        subpackages = self.configure_inventory(["feature", "missing"])
        with patch("minizoo_collection.runtime.cache_md5_inventory", return_value={}):
            evidence, _ = finish_evidence(self.bind(), [self.package, subpackages[0]], "serial", APPID, "test-attempt", self.root)
        self.assertEqual(evidence["coverage"], "partial")
        self.assertEqual([m["status"] for m in evidence["declared_modules"]], ["verified", "missing"])
        self.config["collections"][0]["truncated"] = True
        evidence, _ = finish_evidence(self.bind(), [self.package] + subpackages, "serial", APPID, "test-attempt", self.root)
        self.assertEqual(evidence["coverage"], "partial")

    def test_parent_runtime_hash_provenance_can_differ_from_release_download(self):
        subpackages = self.configure_inventory(["feature"])
        self.runtime["values"][-1]["value"] = "0" * 32
        evidence, _ = finish_evidence(self.bind(), [self.package] + subpackages, "serial", APPID, "test-attempt", self.root)
        self.assertEqual(evidence["coverage"], "complete")
        self.assertFalse(evidence["packages"][1]["release_download_md5_match"])
        self.assertEqual(evidence["packages"][1]["binding_method"], "parent_runtime_inventory")

    def test_config_root_disagreement_prevents_unmatched_module_capture(self):
        subpackages = self.configure_inventory(["feature"])
        self.config["modules"][0]["fields"]["name"] = "/different/"
        with patch("minizoo_collection.runtime.cache_md5_inventory") as inventory:
            evidence, _ = finish_evidence(self.bind(), [self.package] + subpackages, "serial", APPID, "test-attempt", self.root)
        inventory.assert_not_called()
        self.assertEqual(evidence["coverage"], "partial")
        self.assertEqual(len(evidence["packages"]), 1)

    def test_original_config_traversal_and_conflicting_inventories_are_rejected(self):
        self.local.write_bytes(package_bytes({"pages": [], "subPackages": [{"root": "../escape"}]}))
        with self.assertRaises(EvidenceError):
            package_config_roots(self.local)
        self.configure_inventory(["feature"])
        duplicate = copy.deepcopy(self.config)
        duplicate["modules"][0]["fields"]["md5"] = "f" * 32
        self.records.append(duplicate)
        with self.assertRaisesRegex(EvidenceError, "Conflicting runtime inventories"):
            self.bind()


if __name__ == "__main__":
    unittest.main()
