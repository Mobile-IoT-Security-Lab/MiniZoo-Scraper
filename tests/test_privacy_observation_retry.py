"""Synthetic native/UI fixtures; no devices, private snapshots or live state."""
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import test_native_privacy as fixtures

m = fixtures.m


def loading():
    return fixtures.focus(), {'width': 1080, 'height': 2424, 'nodes': [
        {'text': '09:30', 'confidence': 1, 'bounds': [87, 66, 236, 113]},
        {'text': 'X', 'confidence': 1, 'bounds': [31, 214, 85, 268]},
        {'text': 'WeChat', 'confidence': 1, 'bounds': [460, 214, 630, 265]},
    ]}


def obscured():
    screen = fixtures.screen('Shell was granted Superuser rights')
    screen['nodes'] = [node for node in screen['nodes'] if node['text'] != 'Confirm']
    return fixtures.focus(), screen


def loading_xml():
    root = ET.Element('hierarchy', rotation='0')
    for node in loading()[1]['nodes']:
        left, top, right, bottom = node['bounds']
        ET.SubElement(root, 'android.widget.TextView', package='com.tencent.mm',
                      text=node['text'], bounds=f'[{left},{top}][{right},{bottom}]')
    return ET.tostring(root, encoding='unicode')


def obscured_xml():
    xml = fixtures.appium_hierarchy(True)
    return xml.replace('text="Confirm"', 'text="Shell was granted Superuser rights"')


class ReviewedObservationTests(unittest.TestCase):
    def test_loading_and_obscured_dialog_need_fresh_complete_evidence(self):
        for transient, error in ((loading(), m.LoadingPrivacyScreen),
                                 (obscured(), m.IncompletePrivacyScreen)):
            with self.subTest(error=error):
                with self.assertRaises(error):
                    m.classify_focus(*transient)
                observations = iter([transient, fixtures.GDPR])
                labels, waits = [], []
                result = m.reviewed_observation(
                    lambda label: (labels.append(label) or next(observations)),
                    'before', wait=waits.append)
                self.assertEqual(result, (fixtures.GDPR, (m.PRIVACY_ACTIVITY,) * 2))
                self.assertEqual(labels, ['before', 'before-retry-1'])
                self.assertEqual(waits, [2])

    def test_persistent_loading_or_obscuration_refuses_after_three_reads(self):
        for transient, error in ((loading(), m.LoadingPrivacyScreen),
                                 (obscured(), m.IncompletePrivacyScreen)):
            reads, waits = [], []
            with self.subTest(error=error), self.assertRaises(error):
                m.reviewed_observation(lambda label: (reads.append(label) or transient),
                                       'before', wait=waits.append)
            self.assertEqual(reads, ['before', 'before-retry-1', 'before-retry-2'])
            self.assertEqual(waits, [2, 2])

    def test_loading_requires_exact_header_geometry_empty_body_and_wechat_package(self):
        for mutation in ('no_title', 'no_close', 'geometry', 'confidence', 'body',
                         'low_confidence_body', 'no_bounds', 'dimensions', 'package'):
            native, screen = loading()
            title = screen['nodes'][-1]
            if mutation == 'no_title': screen['nodes'].pop()
            elif mutation == 'no_close': screen['nodes'].pop(1)
            elif mutation == 'geometry': title['bounds'] = [460, 1200, 630, 1251]
            elif mutation == 'confidence': title['confidence'] = .79
            elif mutation in ('body', 'low_confidence_body'):
                screen['nodes'].append({'text': 'Unknown page', 'bounds': [100, 900, 700, 950],
                                       'confidence': .79 if mutation.startswith('low') else 1})
            elif mutation == 'no_bounds': title.pop('bounds')
            elif mutation == 'dimensions': screen.pop('height')
            else: title['package'] = 'another.app'
            self.assert_immediate_refusal((native, screen), mutation)

    def assert_immediate_refusal(self, observation, case):
        reads, waits = [], []
        with self.subTest(case=case), self.assertRaises(m.NativePrivacyError) as caught:
            m.reviewed_observation(lambda label: (reads.append(label) or observation),
                                   'before', wait=waits.append)
        self.assertNotIsInstance(caught.exception, (m.LoadingPrivacyScreen, m.IncompletePrivacyScreen))
        self.assertEqual(reads, ['before'])
        self.assertEqual(waits, [])

    def test_obscured_confirm_requires_exact_trusted_title_and_agreement(self):
        for mutation in ('title', 'agreement', 'confidence', 'package'):
            native, screen = obscured()
            if mutation in ('title', 'agreement'):
                screen['nodes'].pop(0 if mutation == 'title' else 1)
            elif mutation == 'confidence': screen['nodes'][0]['confidence'] = .79
            else: screen['nodes'][0]['package'] = 'another.app'
            self.assert_immediate_refusal((native, screen), mutation)

    def test_unknown_or_mismatched_native_identity_never_retries(self):
        for native in (fixtures.focus('com.tencent.mm.ui.LoginUI'),
                       fixtures.focus().replace('mFocusedApp=ActivityRecord{b u0 com.tencent.mm/' + m.PRIVACY_ACTIVITY,
                                                'mFocusedApp=ActivityRecord{b u0 com.tencent.mm/com.tencent.mm.ui.LauncherUI')):
            self.assert_immediate_refusal((native, loading()[1]), native)

    def test_security_and_rate_checks_precede_loading_wait_even_in_status_bar(self):
        for text, exception in [('Security verification', m.NativePrivacySecurity),
                                ('Too many requests. Wait 2 minutes', m.NativePrivacyRateLimit)]:
            native, screen = loading()
            screen['nodes'].append({'text': text, 'confidence': 1, 'bounds': [300, 60, 800, 110]})
            wait = Mock()
            with self.subTest(text=text), self.assertRaises(exception) as caught:
                m.reviewed_observation(lambda _: (native, screen), 'before', wait=wait)
            wait.assert_not_called()
            if exception is m.NativePrivacyRateLimit:
                self.assertEqual(caught.exception.retry_after, 120)

    def test_security_or_unknown_identity_after_wait_still_refuses(self):
        for changed in [(fixtures.focus(), fixtures.screen('CAPTCHA')),
                        (fixtures.focus('com.tencent.mm.ui.LoginUI'), fixtures.screen())]:
            observations = iter([loading(), changed])
            waits = []
            with self.subTest(changed=changed), self.assertRaises(m.NativePrivacyError):
                m.reviewed_observation(lambda _: next(observations), 'before', wait=waits.append)
            self.assertEqual(waits, [2])

    def test_stop_during_wait_prevents_next_observation(self):
        stopped, reads = [], []
        def check_stop():
            if stopped: raise RuntimeError('operator stopped')
        with self.assertRaisesRegex(RuntimeError, 'operator stopped'):
            m.reviewed_observation(lambda label: (reads.append(label) or loading()), 'before',
                                   wait=lambda _: stopped.append(True), check_stop=check_stop)
        self.assertEqual(reads, ['before'])

    def test_stop_after_read_prevents_accepting_complete_dialog(self):
        stopped = []
        def check_stop():
            if stopped: raise RuntimeError('operator stopped')
        def observe(_):
            stopped.append(True)
            return fixtures.GDPR
        with self.assertRaisesRegex(RuntimeError, 'operator stopped'):
            m.reviewed_observation(observe, 'before', check_stop=check_stop)

    def test_xml_retains_bounds_for_native_loading_classification(self):
        xml = loading_xml().replace('text="WeChat"', 'text="WeChat" content-desc="WeChat"')
        screen = m.screen_from_xml(xml, (1080, 2424))
        self.assertEqual(len(screen['nodes']), 3)
        with self.assertRaises(m.LoadingPrivacyScreen):
            m.classify_focus(fixtures.focus(), screen)
        with self.assertRaises(m.NativePrivacyError):
            m.classify_focus(fixtures.focus(), m.screen_from_xml('<hierarchy/>', (1080, 2424)))


class ConsentRetryTests(unittest.TestCase):
    def test_controls_are_reread_after_wait_without_toggling_newly_checked_box(self):
        for transient in (loading(), obscured()):
            samples = iter([fixtures.GDPR, transient, fixtures.GDPR, fixtures.APP])
            xmls = iter([fixtures.hierarchy(False, False), fixtures.hierarchy(True)])
            labels, taps = [], []
            def hierarchy(label):
                labels.append(label)
                return next(xmls)
            with self.subTest(transient=transient):
                observed, receipt = m.resolve_privacy_once(
                    lambda _: next(samples), hierarchy, taps.append, lambda _: None, lambda: None)
            self.assertEqual(observed, fixtures.APP)
            self.assertEqual(labels, ['privacy-controls', 'privacy-controls-retry-1'])
            self.assertEqual(taps, [(540, 2232)])
            self.assertFalse(receipt['checkbox_tapped'])

    def test_transition_during_controls_retry_never_uses_stale_controls(self):
        samples = iter([fixtures.GDPR, obscured(), fixtures.APP])
        hierarchy, tap = Mock(return_value=fixtures.hierarchy(True)), Mock()
        observed, receipt = m.resolve_privacy_once(
            lambda _: next(samples), hierarchy, tap, lambda _: None, lambda: None)
        self.assertEqual(observed, fixtures.APP)
        self.assertEqual(receipt['outcome'], 'already_left_dialog')
        self.assertEqual(hierarchy.call_count, 2)
        tap.assert_not_called()

    def test_initial_loading_timeout_never_reads_controls_or_taps(self):
        hierarchy, tap = Mock(), Mock()
        with self.assertRaises(m.LoadingPrivacyScreen):
            m.resolve_privacy_once(lambda _: loading(), hierarchy, tap, lambda _: None, lambda: None)
        hierarchy.assert_not_called()
        tap.assert_not_called()

    def test_late_privacy_return_and_final_preservation_reobserve_without_repeat_consent(self):
        samples = iter([loading(), fixtures.GDPR, obscured(), fixtures.GDPR])
        preserve = Mock(return_value={'complete': True})
        waits = []
        _, receipt = m.preserve_after_privacy(
            fixtures.APP, {'outcome': 'confirmed', 'confirm_tapped': True}, preserve,
            lambda _: next(samples), wait=waits.append)
        self.assertEqual(preserve.call_count, 2)
        self.assertEqual(waits, [2, 2])
        self.assertEqual(receipt['outcome'], 'close_only')
        self.assertTrue(receipt['confirm_tapped'])


class PortableRetryTests(unittest.TestCase):
    def run_boundary(self, transient_xml, first_transient=1, persistent=False, stop_on_wait=False):
        state, xml, output, run = fixtures.PortableApiTests().make_io(
            checked=True, return_activity=m.PRIVACY_ACTIVITY)
        reads, stopped, inventories = [], [], []
        def source():
            reads.append(len(reads) + 1)
            if reads[-1] == first_transient or (persistent and reads[-1] > first_transient):
                return transient_xml
            return xml()
        def check_stop():
            if stopped: raise RuntimeError('operator stopped')
        def wait(delay):
            if stop_on_wait and delay == 2: stopped.append(True)
        def inventory(*args):
            inventories.append(len(state['taps']))
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            with patch('subprocess.check_output', side_effect=output), \
                 patch('subprocess.run', side_effect=run), \
                 patch.object(m.artifacts, 'inventory', side_effect=inventory), \
                 patch.object(m.time, 'sleep', side_effect=wait):
                receipt, error = None, None
                try:
                    receipt = m.before_close('test-device', tmp, check_stop=check_stop, hierarchy_source=source)
                except Exception as caught:
                    error = caught
            authorized = list(Path(tmp).rglob('CLOSE_AUTHORIZED.json'))
            evidence_files = [p.name for p in Path(tmp).rglob('*.xml')]
        return state, reads, inventories, receipt, error, authorized, evidence_files

    def test_portable_boundary_retries_loading_and_obscuration_at_every_stage(self):
        # XML observations: before, after-preservation, controls, controls-fresh,
        # then final preservation after the single unsuccessful confirmation.
        for transient in (loading_xml(), obscured_xml()):
            for read in (1, 2, 3, 5, 9):
                with self.subTest(read=read, transient=transient):
                    state, _, inventories, receipt, error, authorized, files = self.run_boundary(transient, read)
                    self.assertIsNone(error)
                    self.assertEqual(state['taps'], [(540, 2232)])
                    self.assertEqual(inventories, [0, 0, 1, 1])
                    self.assertEqual(receipt['outcome'], 'close_only')
                    self.assertEqual(len(authorized), 1)
                    self.assertTrue(any('retry-1' in name for name in files))
                    self.assertFalse(any('uiautomator' in command for command in state['commands']))

    def test_portable_timeout_never_authorizes_close(self):
        for first in (1, 2, 9):
            with self.subTest(first=first):
                state, reads, inventories, _, error, authorized, _ = self.run_boundary(
                    loading_xml(), first, persistent=True)
                self.assertIsInstance(error, m.LoadingPrivacyScreen)
                self.assertEqual(len(reads), first + 2)
                self.assertEqual(authorized, [])
                self.assertEqual(state['taps'], [(540, 2232)] if first == 9 else [])
                self.assertEqual(len(inventories), {1: 0, 2: 2, 9: 4}[first])

    def test_portable_stop_during_loading_wait_prevents_controls_cache_and_close(self):
        state, reads, inventories, _, error, authorized, _ = self.run_boundary(
            loading_xml(), stop_on_wait=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'operator stopped')
        self.assertEqual(reads, [1])
        self.assertEqual(inventories, [])
        self.assertEqual(state['taps'], [])
        self.assertEqual(authorized, [])


if __name__ == '__main__':
    unittest.main()
