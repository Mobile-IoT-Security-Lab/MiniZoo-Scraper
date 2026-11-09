import copy
import json
from pathlib import Path
import subprocess
import unittest

from tools.search_metadata import classify, frida_source, parse_callback


APPID = "wx0123456789abcdef"


class SearchMetadataTests(unittest.TestCase):
    def record(self):
        card = {"appid": APPID, "app_version": 12, "title": "Public app", "developer": "Publisher",
                "rect_css": {"left": 10, "top": 20, "right": 110, "bottom": 60},
                "visible": True, "bound_to_open_weapp": True, "jump_type": 2,
                "identity_conflict": False, "selectable": True}
        return {"dom": {"status": "ready", "target_appid": APPID, "page_supported": True, "cards": [card], "selected": card,
                        "viewport": {"width": 400, "height": 800, "scale": 1, "offset_left": 0, "offset_top": 0}},
                "native_view": {"x": 0, "y": 100, "width": 1200, "height": 2400,
                                "visible": {"left": 0, "top": 0, "right": 1200, "bottom": 2400}}}

    def test_coordinates_use_webview_offset_and_matching_scale(self):
        result = classify(self.record(), APPID)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidate"]["physical_bounds"], {"left": 30, "top": 160, "right": 330, "bottom": 280})
        self.assertEqual(result["candidate"]["tap"], {"x": 180, "y": 220})
        self.assertEqual(result["candidate"]["published_app_version"], 12)

    def test_geometry_and_conflicting_binding_fail_closed(self):
        for change in (lambda r: r["native_view"].update(height=2000),
                       lambda r: r["native_view"]["visible"].update(top=80),
                       lambda r: r["dom"]["viewport"].update(scale=2),
                       lambda r: r["dom"]["selected"].update(identity_conflict=True),
                       lambda r: r["dom"]["selected"].update(app_version=True),
                       lambda r: r["dom"]["selected"].update(visible=False)):
            record = self.record()
            change(record)
            self.assertEqual(classify(record, APPID)["status"], "unavailable")

    def test_absent_ambiguous_blocked_and_unsupported_are_distinct(self):
        record = self.record()
        record["dom"].update(status="blocked", reason="no_exact_selectable_card")
        record["dom"]["cards"][0]["appid"] = "wx0000000000000000"
        self.assertEqual(classify(record, APPID)["status"], "absent")
        for reason, status in (("public_ui_wall", "blocked"), ("ambiguous_target_cards", "ambiguous"),
                               ("target_identity_conflict", "ambiguous"), ("public_search_vue_root_unavailable", "unavailable")):
            record["dom"]["reason"] = reason
            self.assertEqual(classify(record, APPID)["status"], status)
        record["dom"].update(cards=[], reason="no_exact_selectable_card", page_supported=False)
        self.assertEqual(classify(record, APPID)["status"], "unavailable")
        record["dom"]["page_supported"] = True
        self.assertEqual(classify(record, APPID)["status"], "absent")

    def test_callback_is_bound_to_attempt_and_only_identical_delivery_deduplicates(self):
        record = self.record()
        payload = {"kind": "search-snapshot", "appid": APPID, "attempt_id": "attempt",
                   "value": json.dumps(json.dumps(record["dom"])), "native_view": record["native_view"]}
        line = "message: " + repr({"type": "send", "payload": payload}) + " data: None\n"
        self.assertEqual(parse_callback(line + line, APPID, "attempt")["dom"], record["dom"])
        self.assertIn("error", parse_callback(line, APPID, "other-attempt"))
        other = copy.deepcopy(payload)
        other["native_view"]["x"] = 1
        self.assertIn("error", parse_callback(line + json.dumps(other), APPID, "attempt"))
        self.assertIn("error", parse_callback("message: __import__('os').getcwd() data: None", APPID, "attempt"))

    def test_dom_reads_exact_cards_without_click_or_frontend_method_call(self):
        source = Path(__file__).resolve().parents[1] / "tools" / "search_metadata.js"
        harness = r'''
const fs = require('fs'), vm = require('vm');
const read = vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
let clicks=0;
const id='wx0123456789abcdef';
const button={isConnected:true,innerText:'New official title',contains:x=>x===button,
 getBoundingClientRect:()=>({left:2,top:4,right:80,bottom:44,width:78,height:40}),click:()=>clicks++};
const el={matches:()=>true,querySelectorAll:()=>[button]};
const item={source:{title:'Public publisher'},jumpInfo:{appID:id,appVersion:12,jumpType:2}};
const component={$props:{item},$el:el,$children:[],
 $options:{methods:{onTapAccount:function(){this.handleWeApp()},handleWeApp:function(){this.M_go()}}},
 _vnode:{elm:el,data:{on:{click:function(){this.onTapAccount()}}},children:[{elm:button}]}};
const root={$route:{name:'result'},$children:[component]};
global.window={};global.innerWidth=400;global.innerHeight=800;
global.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:'1',pointerEvents:'auto'});
global.document={body:{innerText:'Public search results'},querySelector:()=>({__vue__:root}),elementFromPoint:()=>button};
let r=read(id);if(r.status!=='ready'||r.selected.appid!==id||clicks)throw Error(JSON.stringify(r));
delete item.jumpInfo.appID;item.jumpInfo.appId=id;
if(read(id).status!=='ready')throw Error('valid appId alias refused');
item.jumpInfo.appID='wx0000000000000000';
if(read(id).reason!=='target_identity_conflict')throw Error('conflicting alias accepted');
delete item.jumpInfo.appId;item.jumpInfo.appID=id;
const second=Object.assign({},button,{getBoundingClientRect:()=>({left:100,top:4,right:180,bottom:44,width:80,height:40})});
const secondEl={matches:()=>true,querySelectorAll:()=>[second]};
root.$children.push(Object.assign({},component,{$el:secondEl,_vnode:{elm:second,data:{on:{click:function(){this.onTapAccount()}}}}}));
document.elementFromPoint=x=>x>90?second:button;
if(read(id).reason!=='ambiguous_target_cards')throw Error('duplicate distinct cards accepted');
root.$children.pop();document.elementFromPoint=()=>button;
button.isConnected=false;if(read(id).status==='ready')throw Error('hidden card accepted');button.isConnected=true;
window.visualViewport={scale:2,offsetLeft:0,offsetTop:0};if(read(id).reason!=='zoomed_or_offset_viewport')throw Error('zoom accepted');
if(clicks!==0)throw Error('reader clicked');
console.log('PASS');
'''
        run = subprocess.run(["node", "-e", harness, str(source)], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_generated_probe_is_valid_javascript(self):
        run = subprocess.run(["node", "--check"], input=frida_source(APPID, "attempt"), text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_dom_throttle_markers_stop_before_empty_results_classification(self):
        source = Path(__file__).resolve().parents[1] / "tools" / "search_metadata.js"
        harness = r'''
const fs=require('fs'),vm=require('vm');
const read=vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const id='wx0123456789abcdef';
global.window={};global.innerWidth=400;global.innerHeight=800;
global.document={body:{innerText:''},querySelector:()=>({__vue__:{$route:{name:'result'},$children:[]}})};
const cases=[['操作频繁 2 分钟',120],['访问太频繁 45 秒',45],['请求过于频繁',0],
 ['Too frequent attempts; wait 3 minutes',180],['稍后再试',0],['Too many requests 60 seconds',60]];
for(const [message,delay] of cases){
 document.body.innerText='暂无相关结果\n'+message;
 const r=read(id);
 if(r.reason!=='public_ui_wall'||r.wall_kind!=='rate_limit'||r.retry_after!==delay)
   throw Error(JSON.stringify(r));
}
document.body.innerText='暂无相关结果';
const absent=read(id);
if(absent.reason!=='no_exact_selectable_card'||!absent.page_supported||absent.wall_kind)
 throw Error('ordinary absence became throttling: '+JSON.stringify(absent));
document.body.innerText='安全验证';
const security=read(id);
if(security.reason!=='public_ui_wall'||security.wall_kind!=='security')
 throw Error('security wall became throttling: '+JSON.stringify(security));
document.body.innerText='Network unavailable';
const unknown=read(id);
if(unknown.wall_kind||unknown.page_supported)throw Error('generic error classified as throttling/absence');
console.log('PASS');
'''
        run = subprocess.run(["node", "-e", harness, str(source)], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_public_title_is_separate_from_badges_and_never_coerces_objects(self):
        source = Path(__file__).resolve().parents[1] / "tools" / "search_metadata.js"
        harness = r'''
const fs=require('fs'),vm=require('vm');
const read=vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const id='wx0123456789abcdef';
const button={isConnected:true,innerText:'\n兰方园\n交易保障\n,\n杭州炉上好牛品牌策划有限公司\n账号描述:',contains:x=>x===button,
 getBoundingClientRect:()=>({left:0,top:0,right:100,bottom:100,width:100,height:100})};
const el={matches:()=>true,querySelectorAll:()=>[button]};
const item={title:' <em>公开名称</em> ',source:{title:'Public publisher'},jumpInfo:{appID:id,appVersion:39,jumpType:2}};
const component={$props:{item},$el:el,$children:[],$options:{methods:{onTapAccount:function(){this.handleWeApp()},handleWeApp:function(){this.M_go()}}},
 _vnode:{elm:button,data:{on:{click:function(){this.onTapAccount()}}}}};
global.window={};global.innerWidth=400;global.innerHeight=800;
global.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:'1',pointerEvents:'auto'});
global.document={body:{innerText:'Public search results'},querySelector:()=>({__vue__:{$route:{name:'result'},$children:[component]}}),elementFromPoint:()=>button};
function expect(title,source){const r=read(id);if(r.status!=='ready'||r.selected.appid!==id||r.selected.title!==title||r.selected.title_source!==source)throw Error(JSON.stringify(r));}
expect('公开名称','item.title');
delete item.title;expect('兰方园','header_first_visible_line');
item.title={toString(){throw Error('object title was coerced')}};expect('兰方园','header_first_visible_line');
item.title='  ';expect('兰方园','header_first_visible_line');
button.innerText={toString(){throw Error('object header was coerced')}};expect('','header_first_visible_line');
if(JSON.stringify(read(id)).includes('[object Object]'))throw Error('object coercion leaked');
console.log('PASS');
'''
        run = subprocess.run(["node", "-e", harness, str(source)], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
