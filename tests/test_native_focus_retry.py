"""Synthetic focus transitions; no private devices, screenshots or snapshots."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_native_privacy as fixtures

m = fixtures.m


def missing_focus(activity=m.PRIVACY_ACTIVITY):
    return ('mCurrentFocus=null\n'
            'mFocusedApp=ActivityRecord{b u0 com.tencent.mm/' + activity + ' t1}')


class NativeFocusObservationTests(unittest.TestCase):
    def test_known_missing_window_waits_for_fresh_matching_identities(self):
        for activity in (m.PRIVACY_ACTIVITY, '.model.gdpr.ui.MPGdprPolicyUI',
                         'com.tencent.mm.plugin.appbrand.ui.AppBrandUI01'):
            transient = missing_focus(activity), fixtures.screen()
            observations = iter([transient, fixtures.GDPR])
            labels, waits = [], []
            with self.subTest(activity=activity):
                with self.assertRaises(m.TransientNativeFocus):
                    m.classify_focus(*transient)
                result = m.reviewed_observation(
                    lambda label: (labels.append(label) or next(observations)),
                    'before', wait=waits.append)
            self.assertEqual(result, (fixtures.GDPR, (m.PRIVACY_ACTIVITY,) * 2))
            self.assertEqual(labels, ['before', 'before-retry-1'])
            self.assertEqual(waits, [2])

    def test_unsettled_window_never_authorizes_close_and_has_bounded_retries(self):
        labels, waits = [], []
        with self.assertRaises(m.TransientNativeFocus):
            m.reviewed_observation(
                lambda label: (labels.append(label) or (missing_focus(), fixtures.screen())),
                'before', wait=waits.append)
        self.assertEqual(labels, ['before', 'before-retry-1', 'before-retry-2'])
        self.assertEqual(waits, [2, 2])

    def test_unknown_or_ambiguous_native_state_never_retries(self):
        for focus in (missing_focus('com.tencent.mm.ui.LoginUI'),
                      missing_focus('com.tencent.mm.ui.LauncherUI'),
                      missing_focus().replace('com.tencent.mm/', 'com.android.permissioncontroller/'),
                      missing_focus() + '\nmCurrentFocus=null',
                      missing_focus() + '\nmFocusedApp=null',
                      missing_focus().replace('mCurrentFocus=null', 'mCurrentFocus=unknown')):
            wait = Mock()
            with self.subTest(focus=focus), self.assertRaises(m.NativePrivacyError) as caught:
                m.reviewed_observation(lambda _: (focus, fixtures.screen()), 'before', wait=wait)
            self.assertNotIsInstance(caught.exception, m.TransientNativeFocus)
            wait.assert_not_called()

    def test_missing_focus_cannot_hide_security_or_rate_screen(self):
        for text, error in (('CAPTCHA', m.NativePrivacySecurity),
                            ('Too many requests. Wait 2 minutes', m.NativePrivacyRateLimit)):
            wait = Mock()
            with self.subTest(text=text), self.assertRaises(error):
                m.reviewed_observation(lambda _: (missing_focus(), fixtures.screen(text)),
                                       'before', wait=wait)
            wait.assert_not_called()

    def test_catalogue_license_description_is_content_but_security_still_blocks(self):
        focus = fixtures.focus('com.tencent.mm.plugin.appbrand.ui.AppBrandPluginUI')
        for description in ('Example 官方授权游戏', 'Example 正版IP授权游戏'):
            for width, height in ((720, 1600), (1080, 2400), (1440, 3200)):
                for section_top in (.46, .588, .69):
                    # Synthetic catalogue rows move with their section header;
                    # licensing wording is content at every position and scale.
                    rows = [('Mini Programs', .1), ('Search Mini Programs', .2),
                            ('Recently Used', .3), ('Mini Game', section_top),
                            ('Example game', section_top + .05),
                            (description, section_top + .1)]
                    screen = {'width': width, 'height': height, 'nodes': [
                        {'text': text, 'confidence': 1, 'package': 'com.tencent.mm',
                         'bounds': [int(width * .1), int(height * top),
                                    int(width * .9), int(height * (top + .025))]}
                        for text, top in rows]}
                    with self.subTest(description=description, size=(width, height),
                                      section_top=section_top):
                        self.assertEqual(m.classify_focus(focus, screen),
                            ('com.tencent.mm.plugin.appbrand.ui.AppBrandPluginUI',) * 2)
                        for text, error in (
                                ('Security verification', m.NativePrivacySecurity),
                                ('安全验证', m.NativePrivacySecurity),
                                ('Too many requests', m.NativePrivacyRateLimit),
                                ('操作频繁', m.NativePrivacyRateLimit)):
                            warning = dict(screen['nodes'][-1], text=text)
                            changed = dict(screen, nodes=screen['nodes'] + [warning])
                            with self.subTest(warning=text), self.assertRaises(error):
                                m.classify_focus(focus, changed)


class PortableFocusRetryTests(unittest.TestCase):
    def run_boundary(self, transient_reads=(), *, persistent=False, security=False,
                     unknown_after_wait=False, unknown_reads=(), stop_on_wait=False,
                     initially_checked=True, check_when_transient=False):
        state, xml, original_output, run = fixtures.PortableApiTests().make_io(checked=initially_checked)
        focus_reads, hierarchy_reads, waits, events, stopped = [], [], [], [], []

        def output(command, **kwargs):
            ordinary = original_output(command, **kwargs)
            if command[-1] == 'window':
                focus_reads.append(len(focus_reads) + 1)
                if (unknown_after_wait and waits) or focus_reads[-1] in unknown_reads:
                    return fixtures.focus('com.tencent.mm.ui.LoginUI')
                if persistent or focus_reads[-1] in transient_reads:
                    if check_when_transient:
                        state['checked'] = True
                    return missing_focus(state['activity'])
            return ordinary

        def hierarchy():
            hierarchy_reads.append(len(hierarchy_reads) + 1)
            document = xml()
            if security:
                document = document.replace('</hierarchy>',
                    '<node package="com.tencent.mm" text="CAPTCHA" '
                    'bounds="[100,1000][900,1100]"/></hierarchy>')
            return document

        def wait(seconds):
            waits.append((seconds, len(state['taps'])))
            if stop_on_wait and seconds == 2:
                stopped.append(True)

        def check_stop():
            if stopped:
                raise RuntimeError('operator stopped')

        def inventory(*_):
            events.append(('inventory', len(state['taps'])))
            return {}

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            with patch('subprocess.check_output', side_effect=output), \
                 patch('subprocess.run', side_effect=run), \
                 patch.object(m.artifacts, 'inventory', side_effect=inventory), \
                 patch.object(m.time, 'sleep', side_effect=wait):
                receipt, error = None, None
                try:
                    receipt = m.before_close('test-device', str(directory),
                                             check_stop=check_stop, hierarchy_source=hierarchy)
                    events.append(('caller-close', len(state['taps'])))
                except Exception as caught:
                    error = caught
            authorized = list(directory.rglob('CLOSE_AUTHORIZED.json'))
            saved_focus = [p.read_text() for p in directory.rglob('*-focus.txt')]
        return dict(state=state, focus_reads=focus_reads, hierarchy_reads=hierarchy_reads,
                    waits=waits, events=events, receipt=receipt, error=error,
                    authorized=authorized, saved_focus=saved_focus)

    def test_real_api_recovers_initial_probe_and_each_observation_side(self):
        # 1: initial privacy-only probe; subsequent pairs bracket fresh XML.
        # 10 is the final guard immediately before the consent tap: it must
        # remain strict, so a missing window there is deliberately not retried.
        for read in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14):
            with self.subTest(read=read):
                result = self.run_boundary({read})
                self.assertIsNone(result['error'])
                self.assertEqual(result['receipt']['outcome'], 'confirmed')
                self.assertEqual(result['state']['taps'], [(540, 2232)])
                self.assertEqual(len(result['authorized']), 1)
                self.assertEqual(result['events'], [('inventory', 0), ('inventory', 0),
                                                    ('inventory', 1), ('inventory', 1),
                                                    ('caller-close', 1)])
                if read != 1:
                    self.assertEqual(sum(seconds == 2 for seconds, _ in result['waits']), 1)
                    self.assertTrue(any('mCurrentFocus=null' in f for f in result['saved_focus']))
                self.assertFalse(any('uiautomator' in c for c in result['state']['commands']))

    def test_persistent_missing_window_never_taps_preserves_or_authorizes_close(self):
        result = self.run_boundary(persistent=True)
        self.assertIsInstance(result['error'], m.TransientNativeFocus)
        self.assertEqual(len(result['focus_reads']), 7)  # probe + three focus/XML/focus reads
        self.assertEqual(len(result['hierarchy_reads']), 3)
        self.assertEqual(result['waits'], [(2, 0), (2, 0)])
        self.assertEqual(result['state']['taps'], [])
        self.assertEqual(result['events'], [])
        self.assertEqual(result['authorized'], [])

    def test_controls_are_reread_after_null_focus_without_toggling_newly_checked_box(self):
        # The checkbox changes after fresh_controls first reads its XML but
        # before observe confirms focus. The retry must replace those controls.
        result = self.run_boundary({8}, initially_checked=False, check_when_transient=True)
        self.assertIsNone(result['error'])
        self.assertEqual(result['state']['taps'], [(540, 2232)])
        self.assertFalse(result['receipt']['checkbox_tapped'])
        self.assertEqual(result['receipt']['outcome'], 'confirmed')
        self.assertIn((2, 0), result['waits'])

    def test_null_focus_before_tap_still_refuses_without_stale_coordinates(self):
        result = self.run_boundary({10})
        self.assertIsInstance(result['error'], m.TransientNativeFocus)
        self.assertEqual(result['state']['taps'], [])
        self.assertEqual(result['authorized'], [])
        self.assertFalse(result['waits'])

    def test_security_in_xml_during_missing_focus_refuses_without_wait(self):
        result = self.run_boundary({2}, security=True)
        self.assertIsInstance(result['error'], m.NativePrivacySecurity)
        self.assertEqual(result['waits'], [])
        self.assertEqual(result['state']['taps'], [])
        self.assertEqual(result['events'], [])
        self.assertEqual(result['authorized'], [])

    def test_unknown_screen_after_transient_focus_does_not_reach_cache_or_tap(self):
        result = self.run_boundary({2}, unknown_after_wait=True)
        self.assertIsInstance(result['error'], m.NativePrivacyError)
        self.assertNotIsInstance(result['error'], m.TransientNativeFocus)
        self.assertEqual(result['waits'], [(2, 0)])
        self.assertEqual(result['state']['taps'], [])
        self.assertEqual(result['events'], [])
        self.assertEqual(result['authorized'], [])

    def test_unknown_activity_on_either_side_of_xml_refuses_without_wait(self):
        for transient, unknown in ((2, 3), (3, 2)):
            with self.subTest(transient=transient, unknown=unknown):
                result = self.run_boundary({transient}, unknown_reads={unknown})
                self.assertIsInstance(result['error'], m.NativePrivacyError)
                self.assertNotIsInstance(result['error'], m.TransientNativeFocus)
                self.assertEqual(result['waits'], [])
                self.assertEqual(result['state']['taps'], [])
                self.assertEqual(result['events'], [])
                self.assertEqual(result['authorized'], [])

    def test_operator_stop_during_transient_wait_prevents_another_observation(self):
        result = self.run_boundary({2}, stop_on_wait=True)
        self.assertIsInstance(result['error'], RuntimeError)
        self.assertEqual(str(result['error']), 'operator stopped')
        self.assertEqual(len(result['focus_reads']), 3)
        self.assertEqual(len(result['hierarchy_reads']), 1)
        self.assertEqual(result['state']['taps'], [])
        self.assertEqual(result['events'], [])
        self.assertEqual(result['authorized'], [])

    def test_non_gdpr_initial_missing_focus_stays_a_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'uncreated'
            with patch('subprocess.check_output', return_value=missing_focus(
                    'com.tencent.mm.plugin.appbrand.ui.AppBrandUI01')), \
                 patch.object(m.artifacts, 'inventory') as inventory, \
                 patch('subprocess.run') as action:
                self.assertIsNone(m.before_close('test-device', directory))
            self.assertFalse(directory.exists())
            inventory.assert_not_called()
            action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
