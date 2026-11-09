"""Contract tests for the real-decompiler wrapper; upstream is simulated here."""
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import decompile_wxapkg as wrapper


def wxapkg(entries):
    encoded = [(name.encode("utf-8"), body) for name, body in entries]
    index_size = 4 + sum(12 + len(name) for name, _ in encoded)
    position = 14 + index_size
    index, contents = [struct.pack(">I", len(encoded))], []
    for name, body in encoded:
        index.append(struct.pack(">I", len(name)) + name + struct.pack(">II", position, len(body)))
        contents.append(body)
        position += len(body)
    body = b"".join(contents)
    return struct.pack(">BIIIB", 0xBE, 0, index_size, len(body), 0xED) + b"".join(index) + body


class PackageValidationTest(unittest.TestCase):
    def test_malformed_and_traversal_packages_are_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "input.wxapkg"
            samples = [b"V1MMWX encrypted fixture", b"\xbe" * 18,
                       wxapkg([("/../../outside.js", b"bad")]),
                       wxapkg([("/pages\\..\\outside.js", b"bad")])]
            for sample in samples:
                source.write_bytes(sample)
                with self.subTest(sample=sample[:20]), self.assertRaises((ValueError, struct.error)):
                    wrapper.package_index(source)
                self.assertEqual(source.read_bytes(), sample)
                self.assertEqual(list(Path(temporary).iterdir()), [source])


class DecompilerContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.tool = self.root / "tool"
        self.tool.mkdir()
        (self.tool / "wuWxapkg.js").write_text("// fixture: external process is mocked")
        self.source = self.root / "original.wxapkg"
        self.original = wxapkg([("/app-service.js", b"compiled service fixture"),
                                ("/app-config.json", b'{"pages":["pages/index"]}'),
                                ("/app-wxss.js", b"compiled templates fixture")])
        self.source.write_bytes(self.original)
        self.output = self.root / "result"

    def tearDown(self):
        self.temp.cleanup()

    def audit(self, kwargs, paths):
        rows = [{"kind": path.suffix.lstrip("."), "path": str(path)} for path in paths]
        Path(kwargs["env"]["WXAPP_COMPAT_AUDIT"]).write_text("".join(json.dumps(row) + "\n" for row in rows))

    def run_wrapper(self, external, extra=None):
        def version(command, **kwargs):
            return wrapper.PINNED_COMMIT + "\n" if command[0] == "git" else "v26.8.1\n"
        with patch.object(wrapper.subprocess, "check_output", side_effect=version), \
                patch.object(wrapper.subprocess, "run", side_effect=external), \
                patch.object(wrapper.shutil, "which", return_value="/fixture/node"), \
                contextlib.redirect_stdout(io.StringIO()):
            code = wrapper.main(["--package", str(self.source), "--output", str(self.output),
                                 "--tool-root", str(self.tool)] + (extra or []))
        report = json.loads(self.output.with_name("result-work").joinpath("report.json").read_text())
        self.assertEqual(self.source.read_bytes(), self.original)
        return code, report

    def test_zero_exit_with_only_unpacked_files_is_not_decompilation(self):
        def unpack_only(command, **kwargs):
            staged = Path(command[-1])
            self.assertEqual(staged.read_bytes(), self.original)
            restored = staged.with_suffix("")
            restored.mkdir()
            (restored / "app-service.js").write_bytes(b"compiled service fixture")
            (restored / "app-config.json").write_bytes(b'{"pages":["pages/index"]}')
            kwargs["stdout"].write(b"Unpack done.\n")
            return SimpleNamespace(returncode=0)
        code, report = self.run_wrapper(unpack_only)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["reconstructed_files"], [])
        self.assertTrue(report["errors"])
        self.assertTrue((self.output / "app-service.js").is_file())

    def test_logged_restore_failure_is_partial_even_when_upstream_exits_zero(self):
        def failed_template(command, **kwargs):
            restored = Path(command[-1]).with_suffix("")
            restored.mkdir()
            (restored / "app.js").write_text("App({onLaunch: function() { return true; }});")
            (restored / "app.json").write_text('{"pages":["index"]}')
            (restored / "index.wxml").write_text("<view>partially restored</view>")
            kwargs["stdout"].write(b"error on pages/other.wxml(Main)\nerr: TypeError: fixture\n")
            return SimpleNamespace(returncode=0)
        code, report = self.run_wrapper(failed_template)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "partial")
        self.assertIn("Reconstruction errors in the log", report["errors"])
        self.assertIn("app.js", report["reconstructed_files"])

    def test_restoration_uses_copy_and_limits_node_access(self):
        def restored_fixture(command, **kwargs):
            self.assertIn("--permission", command)
            self.assertIn("--require", command)
            self.assertFalse(any(arg.startswith("--allow-net") or arg == "--allow-child-process" for arg in command))
            self.assertNotIn(str(self.source), command)
            self.assertNotIn("HOME", kwargs["env"])
            restored = Path(command[-1]).with_suffix("")
            restored.mkdir()
            (restored / "app.js").write_text("App({onLaunch: function() { return true; }});")
            (restored / "app.json").write_text('{"pages":["index"]}')
            (restored / "index.wxml").write_text("<view>restored fixture</view>")
            self.audit(kwargs, [restored / "app.js", restored / "index.wxml"])
            kwargs["stdout"].write(b"Split and make up done.\n")
            return SimpleNamespace(returncode=0)
        code, report = self.run_wrapper(restored_fixture)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "restored")
        self.assertTrue(report["input_unchanged"])
        self.assertEqual(report["commit"], wrapper.PINNED_COMMIT)
        self.assertEqual(len(report["reconstructed_files"]), 3)

    def test_corrupt_json_tail_cannot_be_reported_restored(self):
        def corrupt_configuration(command, **kwargs):
            restored = Path(command[-1]).with_suffix("")
            restored.mkdir()
            (restored / "app.js").write_text("App({onLaunch: function() { return true; }});")
            # Real failure: overlapping asynchronous writes left an old suffix.
            (restored / "app.json").write_text('{"pages":["index"]}ndColor": "#F8F8F8"}')
            (restored / "index.wxml").write_text("<view>restored fixture</view>")
            return SimpleNamespace(returncode=0)
        code, report = self.run_wrapper(corrupt_configuration)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "partial")
        self.assertIn("Reconstructed app/game configuration is invalid JSON", report["errors"])
        self.assertTrue((self.output / "app.json").read_text().endswith('ndColor": "#F8F8F8"}'))

    def test_registered_module_missing_on_disk_is_partial_despite_zero_exit(self):
        def missing_module(command, **kwargs):
            restored = Path(command[-1]).with_suffix("")
            restored.mkdir()
            (restored / "app.js").write_text("App({});")
            (restored / "app.json").write_text('{"pages":["index"]}')
            (restored / "index.wxml").write_text("<view>restored</view>")
            self.audit(kwargs, [restored / "app.js", restored / "index.wxml",
                                restored / "pages/missing.wxss"])
            return SimpleNamespace(returncode=0)

        code, report = self.run_wrapper(missing_module)
        self.assertEqual(code, 2)
        self.assertIn("Artifact declared in the bundle but not reconstructed: pages/missing.wxss", report["errors"])
        self.assertEqual([row["path"] for row in report["expected_outputs"] if not row["present"]],
                         ["pages/missing.wxss"])

    def test_subpackage_preserves_assets_and_does_not_modify_verified_main(self):
        self.original = wxapkg([("/pages/sub/app-service.js", b"compiled subpackage"),
                                ("/pages/sub/page-frame.js", b"compiled sub template"),
                                ("/pages/sub/icon.png", b"asset fixture")])
        self.source.write_bytes(self.original)
        main_dir = self.root / "verified-main"
        main_dir.mkdir()
        (main_dir / "app.js").write_text("App({onLaunch: function() {}});")
        (main_dir / "app.json").write_text('{"pages":["index"]}')
        original_main = {p.name: p.read_bytes() for p in main_dir.iterdir()}

        def restore_sub(command, **kwargs):
            context = Path(next(arg[3:] for arg in command if arg.startswith("-s=")))
            self.assertNotEqual(context, main_dir)
            self.assertEqual((context / "app.js").read_bytes(), original_main["app.js"])
            pages = context / "pages" / "sub"
            pages.mkdir(parents=True)
            (pages / "index.js").write_text("Page({onLoad: function() {}});")
            (pages / "index.wxml").write_text("<view>subpackage fixture</view>")
            raw = Path(command[-1]).with_suffix("") / "pages" / "sub"
            raw.mkdir(parents=True)
            (raw / "icon.png").write_bytes(b"asset fixture")
            self.audit(kwargs, [pages / "index.js", pages / "index.wxml"])
            return SimpleNamespace(returncode=0)

        code, report = self.run_wrapper(restore_sub, ["--main-dir", str(main_dir)])
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "restored")
        self.assertEqual((self.output / "pages/sub/icon.png").read_bytes(), b"asset fixture")
        self.assertTrue((self.output / "pages/sub/index.js").is_file())
        self.assertFalse((self.output / "app.js").exists())
        self.assertEqual({p.name: p.read_bytes() for p in main_dir.iterdir()}, original_main)


if __name__ == "__main__":
    unittest.main()
