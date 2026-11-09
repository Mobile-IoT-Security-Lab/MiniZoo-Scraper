"""Exercise compatibility parsing and actual CSS reconstruction on small bundles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("WXAPP_TOOL_ROOT", str(ROOT / "vendor" / "wxappUnpacker")))


@unittest.skipUnless(shutil.which("node") and (TOOL / "node_modules/esprima").is_dir(),
                     "The installed pinned decompiler is required")
class CompatibilityTest(unittest.TestCase):
    def run_node(self, script, directory, success=True):
        env = dict(os.environ, WXAPP_TOOL_ROOT=str(TOOL),
                   WXAPP_COMPAT_AUDIT=str(Path(directory) / "expected.jsonl"))
        result = subprocess.run(["node", "-e", script, str(ROOT / "tools/decompiler_compat.cjs")],
                                cwd=directory, env=env, text=True, capture_output=True, timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_modern_factories_are_independent_and_keep_wxs_table(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_node(r'''
const assert = require('assert'), compat = require(process.argv[1]);
const factory = (name, page, wxs) => `var ${name}=(function(){return function(path){
${wxs ? 'var nv_require=(function(){var nnm={"p:module":function(){return 3;}};return function(){};})();' : ''}
var z=[];var x=[${JSON.stringify(page)}];e_[x[0]]={f:function(){return "view";}};
if(path&&e_[path])return e_[path];};})();`;
const frames = compat.wxmlFrames('<script>' + factory('$gwx', 'root.wxml', true)
  + factory('$gwx_XC_0', 'chunk.wxml', false) + '</script>');
assert.deepEqual(frames.map(frame => frame.name), ['$gwx', '$gwx_XC_0']);
assert(frames[0].registrationCode.includes('root.wxml'));
assert(!frames[0].registrationCode.includes('chunk.wxml'));
assert(frames[1].registrationCode.includes('chunk.wxml'));
assert(frames[1].requireInfo.includes('p:module'));
assert.throws(() => compat.wxmlFrames('var $gwx_XC_0=(function(){return function(){};})();'), /boundaries/);
''', directory)

    def test_one_line_html_extracts_css_calls_without_dom_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_node(r'''
const assert = require('assert'), vm = require('vm'), compat = require(process.argv[1]);
const code = '<html><script>if(false)document.bad();else setCssToHead([".card{width:",[0,12],";}"])();document.dispatchEvent("ready");</script></html>';
let styles=[];
const extracted=compat.cssScript(code);
assert(!extracted.includes('document'));
vm.runInNewContext(extracted, {setCssToHead: data => () => styles.push(JSON.parse(JSON.stringify(data)))});
assert.deepEqual(styles, [['.card{width:', [0,12], ';}']]);
assert.equal(compat.styleOutput('/main', '/sub', './packages/common.wxss'), '/main/packages/common.wxss');
assert.equal(compat.styleOutput('/main', '/sub', '12'), '/sub/__wuBaseWxss__/12.wxss');
assert.throws(() => compat.styleOutput('/main', '/sub', '../outside.wxss'), /Unsafe/);
''', directory)

    def test_shared_named_stylesheet_is_restored_and_imports_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app-wxss.js").write_text('''
var __COMMON_STYLESHEETS__={};
__COMMON_STYLESHEETS__["./styles/shared.wxss"]=[".shared{width:",[0,24],";}"];
var setCssToHead=function(){};
var _C=__COMMON_STYLESHEETS__;
__wxAppCode__["pages/a.wxss"]=setCssToHead([[2,"./styles/shared.wxss"],".a{color:red}"]);
__wxAppCode__["pages/b.wxss"]=setCssToHead([[2,"./styles/shared.wxss"],".b{color:blue}"]);
''')
            self.run_node('''
require(process.argv[1]);
require(require('path').join(process.env.WXAPP_TOOL_ROOT, 'wuWxss.js')).doWxss(process.cwd(), () => {});
''', directory)
            shared = (root / "styles/shared.wxss").read_text()
            self.assertIn("24rpx", shared)
            for page in ("a", "b"):
                restored = (root / ("pages/" + page + ".wxss")).read_text()
                self.assertIn('@import "../styles/shared.wxss";', restored)
                self.assertIn("." + page, restored)
            self.assertFalse((root / "__wuBaseWxss__").exists())

    def test_missing_named_import_fails_instead_of_empty_stylesheet(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "app-wxss.js").write_text('''
var _C={};
__wxAppCode__["pages/a.wxss"]=setCssToHead([[2,"./styles/missing.wxss"]]);
''')
            result = self.run_node('''
require(process.argv[1]);
require(require('path').join(process.env.WXAPP_TOOL_ROOT, 'wuWxss.js')).doWxss(process.cwd(), () => {});
''', directory, success=False)
            self.assertIn("Missing common stylesheet", result.stderr)


if __name__ == "__main__":
    unittest.main()
