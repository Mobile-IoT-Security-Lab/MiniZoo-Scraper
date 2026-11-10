from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'MiniZooCollectionCore'))

from minizoo_collection import native_privacy as m


def focus(activity=m.PRIVACY_ACTIVITY):
    return ('mCurrentFocus=Window{a u0 com.tencent.mm/' + activity + '}\n'
            'mFocusedApp=ActivityRecord{b u0 com.tencent.mm/' + activity + ' t1}')


def screen(*extra):
    return {'width':1080,'height':2424,'nodes':[
        {'text':t,'confidence':1} for t in (m.PRIVACY_TITLE,m.PRIVACY_AGREEMENT,'Confirm') + extra]}


def hierarchy(checked=False, enabled=True):
    return '''UI hierchary dumped to: /dev/tty
<hierarchy rotation="0"><node package="com.tencent.mm" bounds="[0,0][1080,2424]">
<node package="com.tencent.mm" text="Authorize Data: WeChat Login" bounds="[230,190][870,250]"/>
<node package="com.tencent.mm" text="I have read and agree to the above conditions" bounds="[96,2045][950,2100]"/>
<node package="com.tencent.mm" checkable="true" checked="%s" enabled="true" clickable="true" bounds="[54,2057][90,2093]"/>
<node package="com.tencent.mm" text="Confirm" clickable="true" enabled="%s" bounds="[54,2170][1027,2294]"/>
</node></hierarchy>''' % (str(checked).lower(),str(enabled).lower())


def appium_hierarchy(checked=False, enabled=True):
    xml=hierarchy(checked,enabled)
    tree=ET.fromstring(xml[xml.index('<hierarchy'):])
    for element in tree.iter():
        if element.tag=='node':
            element.tag=('android.widget.CheckBox' if element.get('checkable')=='true'
                         else 'android.widget.TextView' if element.get('text')
                         else 'android.widget.FrameLayout')
    return ET.tostring(tree,encoding='unicode')


GDPR = (focus(),screen())
APP = (focus('com.tencent.mm.plugin.appbrand.ui.AppBrandUI01'),
       {'width':1080,'height':2424,'nodes':[{'text':'Loading','confidence':1}]})


class PrivacyFallbackTests(unittest.TestCase):
    def test_only_exact_native_reviewed_dialog_admitted(self):
        self.assertEqual(m.classify_focus(*GDPR),(m.PRIVACY_ACTIVITY,)*2)
        for observation in [
            (focus('com.tencent.mm.ui.LoginUI'),screen()),
            (focus().replace('mFocusedApp=ActivityRecord{b u0 com.tencent.mm/'+m.PRIVACY_ACTIVITY,
                              'mFocusedApp=ActivityRecord{b u0 com.tencent.mm/com.tencent.mm.ui.LauncherUI'),screen()),
            (focus(),dict(screen(),nodes=screen()['nodes'][:-1])),
            (focus(),screen('CAPTCHA')),
            (focus(),screen('Too many requests')),
        ]:
            with self.subTest(observation=observation), self.assertRaises(m.NativePrivacyError):
                m.classify_focus(*observation)

    def run_attempt(self, observations, xmls):
        observed = iter(observations)
        data = iter(xmls)
        actions = []
        value = m.resolve_privacy_once(lambda label: next(observed),lambda label:next(data),
                                      lambda point:actions.append(point),lambda delay:None,lambda:None)
        return value,actions

    def test_unchecked_is_verified_then_confirmed_once(self):
        (observation,receipt),actions = self.run_attempt(
            [GDPR,GDPR,GDPR,GDPR,APP],[hierarchy(False,False),hierarchy(True)])
        self.assertEqual(observation,APP)
        self.assertEqual(receipt['outcome'],'confirmed')
        self.assertEqual(actions,[(72,2075),(540,2232)])

    def test_already_checked_is_never_toggled(self):
        (_,receipt),actions = self.run_attempt([GDPR,GDPR,APP],[hierarchy(True)])
        self.assertEqual(actions,[(540,2232)])
        self.assertFalse(receipt['checkbox_tapped'])
        self.assertTrue(receipt['confirm_tapped'])

    def test_unchecked_after_tick_falls_back_without_confirmation(self):
        (_,receipt),actions = self.run_attempt(
            [GDPR,GDPR,GDPR,GDPR,GDPR],[hierarchy(False),hierarchy(False)])
        self.assertEqual(actions,[(72,2075)])
        self.assertEqual(receipt['outcome'],'close_only')
        self.assertIn('did not become checked',receipt['reason'])

    def test_missing_or_ambiguous_controls_fall_back_without_taps(self):
        for xml in ('not XML',hierarchy().replace('checkable="true"','checkable="false"'),
                    hierarchy().replace('checked="false"','checked="unknown"'),
                    hierarchy().replace('bounds="[54,2057][90,2093]"','bounds="[54,2057][9000,2093]"')):
            with self.subTest(xml=xml):
                (_,receipt),actions = self.run_attempt([GDPR,GDPR,GDPR],[xml])
                self.assertEqual(actions,[])
                self.assertEqual(receipt['outcome'],'close_only')

    def test_disabled_confirm_falls_back(self):
        (_,receipt),actions = self.run_attempt([GDPR,GDPR,GDPR],[hierarchy(True,False)])
        self.assertEqual(actions,[])
        self.assertEqual(receipt['outcome'],'close_only')

    def test_persistent_dialog_closes_without_repeat_consent(self):
        (_,receipt),actions = self.run_attempt([GDPR]*5,[hierarchy(True)])
        self.assertEqual(actions,[(540,2232)])
        self.assertEqual(receipt['outcome'],'close_only')
        self.assertIn('remained or returned',receipt['reason'])

    def test_valid_app_replacing_dialog_during_hierarchy_does_not_get_stale_tap(self):
        (observation,receipt),actions=self.run_attempt([GDPR,APP],[hierarchy(True)])
        self.assertEqual(observation,APP)
        self.assertEqual(actions,[])
        self.assertEqual(receipt['outcome'],'already_left_dialog')

    def test_security_after_confirmation_still_stops(self):
        actions=[]
        items=iter([GDPR,GDPR,(focus(),screen('CAPTCHA'))])
        with self.assertRaises(m.NativePrivacyError):
            m.resolve_privacy_once(lambda label:next(items),lambda label:hierarchy(True),
                                   lambda point:actions.append(point),lambda _:None,lambda:None)
        self.assertEqual(actions,[(540,2232)])

    def test_control_failure_reobserves_and_refuses_unknown_native(self):
        items=iter([GDPR,GDPR,(focus('com.tencent.mm.ui.LoginUI'),screen())])
        with self.assertRaises(m.NativePrivacyError):
            m.resolve_privacy_once(lambda _:next(items),lambda _: 'invalid',
                                   lambda _:self.fail('No tap expected'),lambda _:None,lambda:None)

    def test_appium_class_named_elements_have_recognized_text_and_checked_state(self):
        xml=appium_hierarchy(True)
        observed=m.screen_from_xml(xml,(1080,2424))
        self.assertEqual(m.classify_focus(focus(),observed),(m.PRIVACY_ACTIVITY,)*2)
        controls=m.privacy_controls(xml,observed)
        self.assertTrue(controls['checked'])
        self.assertTrue(controls['confirm_enabled'])
        self.assertEqual(controls['confirm'],(540,2232))

    def test_empty_appbrand_hierarchy_after_confirm_is_safe_without_more_taps(self):
        empty=(APP[0],m.screen_from_xml('<hierarchy rotation="0"/>',(1080,2424)))
        (observed,receipt),actions=self.run_attempt([GDPR,GDPR,empty],[hierarchy(True)])
        self.assertEqual(observed,empty)
        self.assertEqual(receipt['outcome'],'confirmed')
        self.assertEqual(actions,[(540,2232)])
        with self.assertRaises(m.NativePrivacyError):m.classify_focus(focus(),empty[1])

    def test_xml_label_inside_button_uses_native_enabled_state(self):
        xml=hierarchy(True).replace(
            '<node package="com.tencent.mm" text="Confirm" clickable="true" enabled="true" bounds="[54,2170][1027,2294]"/>',
            '<node package="com.tencent.mm" clickable="true" enabled="true" bounds="[54,2170][1027,2294]">'
            '<node package="com.tencent.mm" text="Confirm" clickable="false" bounds="[460,2200][630,2260]"/>'
            '</node>')
        self.assertEqual(m.privacy_controls(xml,screen())['confirm'],(545,2230))
        self.assertTrue(m.privacy_controls(xml,screen())['checked'])

    def test_dialog_returning_during_preservation_closes_without_second_consent(self):
        actions=[]
        observations=iter([GDPR,GDPR])
        def preserve():
            actions.append('preserve')
            return {'complete':True,'sequence':len(actions)}
        result,receipt=m.preserve_after_privacy(APP,{'outcome':'confirmed','confirm_tapped':True},
                                                preserve,lambda _:next(observations))
        self.assertEqual(actions,['preserve','preserve'])
        self.assertEqual(result['sequence'],2)
        self.assertEqual(receipt['outcome'],'close_only')
        self.assertIn('returned during preservation',receipt['reason'])
        self.assertTrue(receipt['confirm_tapped'])

    def test_unknown_or_unstable_return_during_preservation_still_stops(self):
        for observations in ([GDPR,APP],[(focus('com.tencent.mm.ui.LoginUI'),screen())],
                             [(focus(),screen('CAPTCHA'))]):
            sequence=iter(observations)
            with self.subTest(observations=observations),self.assertRaises(m.NativePrivacyError):
                m.preserve_after_privacy(APP,{'outcome':'confirmed'},lambda:{'complete':True},
                                         lambda _:next(sequence))


class PortableApiTests(unittest.TestCase):
    def make_io(self, checked=True, invalid_xml=False, return_activity=None):
        state={'checked':checked,'activity':m.PRIVACY_ACTIVITY,'taps':[],'commands':[]}
        def xml():
            if invalid_xml: return 'invalid hierarchy'
            if state['activity']==m.PRIVACY_ACTIVITY: return hierarchy(state['checked'])
            return '<hierarchy rotation="0"><node package="com.tencent.mm" text="Loading" bounds="[0,0][1080,2424]"/></hierarchy>'
        def output(command,**kwargs):
            state['commands'].append(command)
            if command[-1]=='window': return focus(state['activity'])
            if command[-1]=='policy': return 'showing=false'
            if command[-2:]==['wm','size']: return 'Physical size: 1080x2424'
            if command[-2:]==['dump','/dev/tty']: return xml()
            self.fail('Unexpected IO: '+repr(command))
        def run(command,**kwargs):
            point=tuple(map(int,command[-2:]));state['taps'].append(point)
            if point==(72,2075): state['checked']=True
            if point==(540,2232):
                state['activity']=return_activity or 'com.tencent.mm.plugin.appbrand.ui.AppBrandUI01'
            return subprocess.CompletedProcess(command,0)
        return state,xml,output,run

    def test_unrelated_foreground_is_noop_without_output_or_cache_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'does-not-exist'
            with patch('subprocess.check_output',return_value=focus('com.tencent.mm.ui.LauncherUI')), \
                 patch.object(m.artifacts,'inventory') as inventory,patch('subprocess.run') as run:
                self.assertIsNone(m.before_close('test-device',output))
            self.assertFalse(output.exists());inventory.assert_not_called();run.assert_not_called()

    def test_portable_api_preserves_before_and_after_using_active_appium_source(self):
        state,xml,output,run=self.make_io(checked=False)
        events=[]
        def inventory(*args): events.append(('inventory',len(state['taps'])));return {}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            with patch('subprocess.check_output',side_effect=output),patch('subprocess.run',side_effect=run), \
                 patch.object(m.artifacts,'inventory',side_effect=inventory),patch('time.sleep'):
                receipt=m.before_close('test-device',tmp,hierarchy_source=lambda: (
                    appium_hierarchy(state['checked']) if state['activity']==m.PRIVACY_ACTIVITY
                    else '<hierarchy rotation="0"/>'))
            self.assertEqual(receipt['outcome'],'confirmed')
            self.assertTrue(Path(receipt['evidence'],'CLOSE_AUTHORIZED.json').is_file())
            self.assertTrue(Path(receipt['evidence'],'privacy-fallback.json').is_file())
        self.assertEqual(events,[('inventory',0),('inventory',0),('inventory',2),('inventory',2)])
        self.assertEqual(state['taps'],[(72,2075),(540,2232)])
        self.assertFalse(any('uiautomator' in command for command in state['commands']))

    def test_late_package_is_preserved_before_consent_without_extra_taps(self):
        import hashlib
        state,xml,output,run=self.make_io(checked=True)
        remote=m.CACHE+'late.wxapkg';data=b'late completed download'
        digest=hashlib.sha256(data).hexdigest();complete={remote:digest}
        snapshots=iter([{},complete,complete,complete,complete,complete])
        def inventory(*args):
            return next(snapshots)
        def pull(*args):
            self.assertEqual(state['taps'],[])
            return data
        with tempfile.TemporaryDirectory() as tmp:
            tmp=str(Path(tmp).resolve())
            with patch('subprocess.check_output',side_effect=output),patch('subprocess.run',side_effect=run), \
                 patch.object(m.artifacts,'inventory',side_effect=inventory), \
                 patch.object(m.artifacts,'_read_device',side_effect=pull) as read_device,patch('time.sleep'):
                receipt=m.before_close('test-device',tmp,hierarchy_source=xml)
            self.assertEqual(receipt['outcome'],'confirmed')
            self.assertEqual(receipt['packages'],1)
            self.assertTrue(Path(receipt['evidence'],'CLOSE_AUTHORIZED.json').is_file())
            read_device.assert_called_once()
        self.assertEqual(state['taps'],[(540,2232)])

    def test_malformed_native_xml_is_not_autoaccepted_or_closed(self):
        state,xml,output,run=self.make_io(invalid_xml=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            with patch('subprocess.check_output',side_effect=output),patch('subprocess.run',side_effect=run), \
                 patch.object(m.artifacts,'inventory') as inventory:
                with self.assertRaises(m.NativePrivacyError):
                    m.before_close('test-device',tmp)
            inventory.assert_not_called()
            self.assertFalse(list(Path(tmp).rglob('CLOSE_AUTHORIZED.json')))
        self.assertEqual(state['taps'],[])

    def test_cache_preservation_failure_prevents_consent(self):
        state,xml,output,run=self.make_io()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            with patch('subprocess.check_output',side_effect=output),patch('subprocess.run',side_effect=run), \
                 patch.object(m.artifacts,'inventory',side_effect=m.artifacts.ArtifactError('cache unavailable')):
                with self.assertRaises(m.NativePrivacyError):
                    m.before_close('test-device',tmp)
            self.assertFalse(list(Path(tmp).rglob('CLOSE_AUTHORIZED.json')))
        self.assertEqual(state['taps'],[])

    def test_account_login_after_confirm_stops_without_close_receipt(self):
        state,xml,output,run=self.make_io(return_activity='com.tencent.mm.ui.LoginUI')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            with patch('subprocess.check_output',side_effect=output),patch('subprocess.run',side_effect=run), \
                 patch.object(m.artifacts,'inventory',return_value={}),patch('time.sleep'):
                with self.assertRaises(m.NativePrivacyError):
                    m.before_close('test-device',tmp)
            self.assertFalse(list(Path(tmp).rglob('CLOSE_AUTHORIZED.json')))
        self.assertEqual(state['taps'],[(540,2232)])

    def test_io_failure_is_reported_as_privacy_error(self):
        with patch('subprocess.check_output',side_effect=subprocess.TimeoutExpired('adb',30)):
            with self.assertRaises(m.NativePrivacyError):m.before_close('test-device','unused')

    def test_real_rate_and_security_exceptions_remain_distinct(self):
        with self.assertRaises(m.NativePrivacyRateLimit) as caught:
            m.classify_focus(focus(),screen('Too many requests. Wait 2 minutes'))
        self.assertEqual(caught.exception.retry_after,120)
        with self.assertRaises(m.NativePrivacySecurity):m.classify_focus(focus(),screen('CAPTCHA'))


if __name__=='__main__': unittest.main()
