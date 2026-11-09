import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from phone import Ambiguous, RateLimit, ScreenUnavailable, Suspended
from visual_phone import SEARCH_LANDING, VisualPhone


def node(text, x=100, y=500, confidence=1):
    return {'text': text, 'bounds': [x, y, x + 160, y + 35], 'confidence': confidence}


def screen(*nodes):
    return {'width': 1080, 'height': 2400, 'nodes': list(nodes)}


class VisualPhoneTests(unittest.TestCase):
    def setUp(self):
        self.pacer = Mock()
        self.phone = VisualPhone({'appium_url': 'http://localhost'}, {'udid': 'test', 'system_port': 8201}, self.pacer)
        self.phone.mobile = Mock()
        self.phone.call = Mock(return_value='com.tencent.mm')
        self.phone.check_screen = Mock()

    def test_main_factory_uses_visual_only_when_explicitly_configured(self):
        from main import make_phone
        from phone import Phone
        device = {'udid': 'test', 'system_port': 8201}
        self.assertIs(type(make_phone({'appium_url': 'http://localhost'}, device, self.pacer)), Phone)
        configured = {'appium_url': 'http://localhost', 'ui_mode': 'visual'}
        self.assertIs(type(make_phone(configured, device, self.pacer)), VisualPhone)

    def test_refresh_requires_three_bottom_home_labels_and_never_blind_back(self):
        self.phone.check_screen.return_value = screen(node('Contacts', y=2300), node('Discover', x=500, y=2300), node('Me', x=850, y=2300))
        self.phone.refresh()
        self.assertEqual([c.args[0] for c in self.phone.mobile.call_args_list], ['terminateApp', 'activateApp'])
        self.assertEqual(self.pacer.wait.call_args_list[-1].args, (5,))
        self.phone.check_screen.return_value = screen(node('Contacts'), node('Discover'), node('Me'))
        self.phone.call.reset_mock()
        with self.assertRaises(ScreenUnavailable):
            self.phone.refresh()
        self.phone.call.assert_not_called()

    def test_rate_limit_ocr_preserves_service_wait(self):
        self.phone.check_screen = VisualPhone.check_screen.__get__(self.phone)
        self.phone.screenshot_ocr = Mock(return_value=screen(node('操作过于频繁 2 分钟')))
        with self.assertRaises(RateLimit) as error:
            self.phone.check_screen()
        self.assertEqual(error.exception.retry_after, 120)

    def test_security_blocks_but_ordinary_miniapp_login_does_not(self):
        self.phone.check_screen = VisualPhone.check_screen.__get__(self.phone)
        self.phone.screenshot_ocr = Mock(return_value=screen(node('Security verification')))
        with self.assertRaises(Suspended):
            self.phone.check_screen()
        self.phone.screenshot_ocr.return_value = screen(node('Login'), node('Please agree to the privacy policy'))
        self.assertEqual(self.phone.check_screen(), self.phone.screenshot_ocr.return_value)
        self.phone.mobile.assert_not_called()

    def test_matching_candidate_excludes_search_input_and_accepts_exact_category(self):
        result = node('云南教育平台 事业单位', y=600)
        view = screen(node('云南教育平台', y=140), result)
        self.assertEqual(VisualPhone.candidate(view, '云南教育平台'), result)
        view = screen(node('云南教育平台旗舰店', y=600))
        with self.assertRaises(Ambiguous):
            VisualPhone.candidate(view, '云南教育平台')

    def test_dark_mode_title_must_be_recognized_before_opening_first_card(self):
        # Real Pixel9 screen: the original OCR omitted the green first title.
        # A query plus other results is insufficient, even if the first card
        # would normally be expected to match the query.
        name = '云南教育平台'
        query = {'text': name, 'confidence': 1, 'bounds': [140, 217, 416, 269]}
        search = {'text': 'Search', 'confidence': 1, 'bounds': [886, 221, 1017, 261]}
        other = {'text': '昆明市数字校园综合管理平台 事业单位', 'confidence': 1,
                 'bounds': [186, 1218, 922, 1269]}
        original = {'width': 1080, 'height': 2424, 'nodes': [query, search, other]}
        self.phone.tap_label = Mock()
        self.phone.check_screen.side_effect = [original, original]
        with self.assertRaises(Ambiguous):
            self.phone.search_and_open(name)
        self.assertEqual(self.phone.mobile.call_args_list[-1].args,
                         ('clickGesture', {'x': 951, 'y': 241}))
        self.assertEqual(sum(c.args[0] == 'clickGesture'
                             for c in self.phone.mobile.call_args_list), 1)

        # Same-sized contrast passes may supply overlapping readings. Only the
        # exact visible title/category match authorizes this candidate tap.
        result = {'text': name + ' 事业单位', 'confidence': 1,
                  'bounds': [186, 351, 602, 402]}
        repeat = {'text': name, 'confidence': 1, 'bounds': [183, 356, 455, 397]}
        enhanced = dict(original, nodes=original['nodes'] + [result, repeat])
        self.phone.mobile.reset_mock()
        self.phone.check_screen.side_effect = [original, enhanced, screen(node('Opened'))]
        self.assertEqual(self.phone.search_and_open(name), 'opened')
        self.assertIs(self.phone.search_was_empty, False)
        self.assertEqual(self.phone.mobile.call_args_list[-1].args,
                         ('clickGesture', {'x': 394, 'y': 376}))

    def test_wrong_multiple_or_low_confidence_results_are_never_selected(self):
        for view in (screen(node('wrong')), screen(node('target'), node('target', y=900)),
                     screen(node('target', confidence=0.7))):
            with self.assertRaises(Ambiguous):
                VisualPhone.candidate(view, 'target')

    def test_real_first_result_below_search_bar_is_not_a_second_query(self):
        name = '一键无损去水印'
        query = {'text': name, 'confidence': 1, 'bounds': [133, 196, 465, 247]}
        result = {'text': name, 'confidence': 1, 'bounds': [172, 331, 504, 382]}
        search = {'text': 'Search', 'confidence': 1, 'bounds': [886, 199, 1014, 241]}
        view = {'width': 1080, 'height': 2424, 'nodes': [query, result, search]}
        self.assertEqual(VisualPhone.candidate(view, name), result)
        self.phone.tap_label = Mock()
        self.phone.check_screen.side_effect = [view, view, screen(node('Opened'))]
        self.assertEqual(self.phone.search_and_open(name), 'opened')
        self.assertEqual(self.phone.mobile.call_args_list[-1].args,
                         ('clickGesture', {'x': 338, 'y': 356}))

    def test_overlapping_ocr_boxes_merge_but_separate_locations_remain_ambiguous(self):
        first = node('target', x=100, y=600, confidence=0.9)
        second = node('target', x=103, y=602, confidence=1)
        self.assertEqual(VisualPhone.candidate(screen(first, second), 'target'), second)
        self.assertEqual(VisualPhone._matching(screen(first, second), ('target',)), [second])
        separate = node('target', x=100, y=700)
        with self.assertRaises(Ambiguous):
            VisualPhone.candidate(screen(first, second, separate), 'target')

    def test_same_card_exact_name_and_category_readings_merge(self):
        exact = node('target', y=600)
        labelled = node('target 事业单位', x=102, y=601)
        self.assertEqual(VisualPhone.candidate(screen(exact, labelled), 'target'), exact)

    def test_observed_financial_and_trade_badges_do_not_change_exact_name(self):
        # Redacted names, real result-card geometry from rows 17/23/24.
        for badge in ('持牌金融机构', '交易保障'):
            title = {'text': '示例银行 ' + badge, 'confidence': 1, 'bounds': [182, 351, 711, 402]}
            self.assertEqual(VisualPhone.candidate(screen(title), '示例银行'), title)
        for text in ('示例银行旗舰店', '示例银行 未知标签', '示例银行持牌金融机构'):
            with self.assertRaises(Ambiguous):
                VisualPhone.candidate(screen(node(text)), '示例银行')

    def test_logo_text_belongs_to_its_title_but_two_cards_are_still_ambiguous(self):
        # Row 11: the same name appears in tiny print inside the card's icon.
        name = '示例柠檬茶'
        logo = {'text': name, 'confidence': 1, 'bounds': [56, 401, 131, 420]}
        title = {'text': name, 'confidence': 1, 'bounds': [182, 331, 416, 382]}
        view = {'width': 1080, 'height': 2424, 'nodes': [logo, title]}
        self.assertEqual(VisualPhone.candidate(view, name), title)
        other = {'text': name, 'confidence': 1, 'bounds': [182, 827, 416, 878]}
        with self.assertRaises(Ambiguous):
            VisualPhone.candidate(dict(view, nodes=[logo, title, other]), name)

    def test_short_title_contained_in_badge_reading_is_one_location(self):
        title = {'text': '示例', 'confidence': 1, 'bounds': [182, 351, 275, 402]}
        badge = {'text': '示例 持牌金融机构', 'confidence': 1, 'bounds': [182, 351, 620, 402]}
        self.assertEqual(VisualPhone.candidate(screen(title, badge), '示例'), title)
        other = {'text': '示例', 'confidence': 1, 'bounds': [182, 781, 275, 832]}
        with self.assertRaises(Ambiguous):
            VisualPhone.candidate(screen(title, badge, other), '示例')

    def test_exact_live_card_can_open_when_query_ocr_is_missing_or_corrupt(self):
        # Rows 7 and 12: typed input is visibly correct but OCR loses it; live
        # results already contain the complete exact name. Never submit Search.
        for name, query in [('BRAND 示例茶饮', 'BRAND ЖТЛ'), ('示例菜谱-今天吃什么', None)]:
            self.phone.mobile.reset_mock()
            self.phone.tap_label = Mock()
            nodes = [node('Search', x=880, y=200), node(name, x=182, y=351)]
            if query:
                nodes.append(node(query, x=137, y=200))
            self.phone.check_screen.side_effect = [screen(*nodes), screen(node('Opened'))]
            self.assertEqual(self.phone.search_and_open(name), 'opened')
            self.assertEqual([call.args for call in self.phone.mobile.call_args_list],
                             [('type', {'text': name}), ('clickGesture', {'x': 262, 'y': 368})])

    def test_unreadable_query_never_opens_wrong_truncated_or_multiple_live_cards(self):
        self.phone.tap_label = Mock()
        for cards in ([node('target…')], [node('target other')],
                      [node('target'), node('target', y=900)]):
            self.phone.mobile.reset_mock()
            self.phone.check_screen.side_effect = None
            self.phone.check_screen.return_value = screen(node('Search', x=880, y=200), *cards)
            with self.assertRaises(Ambiguous):
                self.phone.search_and_open('target')
            self.phone.mobile.assert_called_once_with('type', {'text': 'target'})

    def test_landing_search_accepts_only_observed_icon_prefix(self):
        self.phone.check_screen.return_value = screen(node('Q Search Mini Programs'))
        self.phone.tap_label(SEARCH_LANDING)
        self.phone.mobile.assert_called_once_with('clickGesture', {'x': 180, 'y': 517})
        self.assertEqual(VisualPhone._matching(screen(node('Other Search Mini Programs')), SEARCH_LANDING), [])

    def test_tap_coordinates_come_from_current_exact_label(self):
        self.phone.check_screen.return_value = screen(node('Discover', x=608, y=2311))
        self.phone.tap_label(('Discover',))
        self.phone.mobile.assert_called_once_with('clickGesture', {'x': 688, 'y': 2328})
        self.assertEqual([c.args for c in self.pacer.before.call_args_list],
                         [('shared',), ('shared',)])
        self.phone.call.assert_called_once_with('/appium/device/current_package', 'GET')

    def test_navigation_label_uses_word_box_without_splitting_query_or_result(self):
        label = {'text': 'Search', 'confidence': 0.9, 'bounds': [142, 200, 279, 236]}
        line = {'text': '《 | Search', 'confidence': 0.92, 'bounds': [40, 164, 279, 270],
                'words': [{'text': '《', 'confidence': 0.92, 'bounds': [40, 194, 66, 240]},
                          {'text': '|', 'confidence': 0.91, 'bounds': [109, 164, 116, 270]}, label]}
        self.phone.check_screen.return_value = screen(line)
        self.phone.tap_label(('Search',), lambda n, s: n['bounds'][0] > 100)
        self.phone.mobile.assert_called_once_with('clickGesture', {'x': 210, 'y': 218})
        self.assertEqual(VisualPhone._matching(screen(line), ('Search',)), [])
        result = dict(line, bounds=[40, 600, 279, 706], words=[
            dict(word, bounds=[word['bounds'][0], word['bounds'][1] + 436,
                               word['bounds'][2], word['bounds'][3] + 436]) for word in line['words']])
        with self.assertRaises(Ambiguous):
            VisualPhone.candidate(screen(result), 'Search')

    def test_navigation_word_recovery_rejects_phrases_and_low_confidence(self):
        for prefix, confidence in [('Other', 0.99), ('|', 0.79)]:
            line = {'text': prefix + ' Search', 'confidence': 0.99, 'bounds': [40, 200, 280, 240],
                    'words': [{'text': prefix, 'confidence': 0.99, 'bounds': [40, 200, 100, 240]},
                              {'text': 'Search', 'confidence': confidence, 'bounds': [140, 200, 280, 240]}]}
            self.phone.check_screen.return_value = screen(line)
            with self.assertRaises(ScreenUnavailable):
                self.phone.tap_label(('Search',))
            self.phone.mobile.assert_not_called()

    def test_shared_account_gate_is_rechecked_after_ocr_immediately_before_tap(self):
        events = Mock()
        events.attach_mock(self.pacer.before, 'pace')
        events.attach_mock(self.phone.check_screen, 'ocr')
        events.attach_mock(self.phone.call, 'foreground')
        events.attach_mock(self.phone.mobile, 'mobile')
        self.phone.check_screen.return_value = screen(node('Discover', x=608, y=2311))
        self.phone.tap_label(('Discover',))
        self.assertEqual([entry[0] for entry in events.mock_calls],
                         ['pace', 'ocr', 'pace', 'foreground', 'mobile'])

    def test_ambiguous_navigation_label_and_other_foreground_never_tap(self):
        self.phone.check_screen.return_value = screen(node('Search'), node('Search', x=800))
        with self.assertRaises(Ambiguous):
            self.phone.tap_label(('Search',))
        self.phone.mobile.assert_not_called()
        self.phone.check_screen.return_value = screen(node('Search'))
        self.phone.call.return_value = 'com.android.permissioncontroller'
        with self.assertRaises(ScreenUnavailable):
            self.phone.tap_label(('Search',))
        self.phone.mobile.assert_not_called()

    def test_search_verifies_typed_query_then_opens_one_candidate(self):
        name = '云南教育平台'
        self.phone.tap_label = Mock()
        self.phone.check_screen.side_effect = [
            screen(node(name, y=150), node('Search', x=880, y=150)),
            screen(node(name, y=150), node(name + ' 事业单位', y=600)),
            screen(node('Login')),
        ]
        self.assertEqual(self.phone.search_and_open(name), 'opened')
        self.assertEqual(self.phone.tap_label.call_count, 4)
        self.assertEqual(self.phone.mobile.call_args_list[0].args, ('type', {'text': name}))
        self.assertEqual(sum(c.args[0] == 'clickGesture' for c in self.phone.mobile.call_args_list), 2)

    def test_wrong_typed_query_prevents_search_submission(self):
        self.phone.tap_label = Mock()
        self.phone.check_screen.return_value = screen(node('wrong', y=150), node('Search', x=880, y=150))
        with self.assertRaises(Ambiguous):
            self.phone.search_and_open('expected')
        self.phone.mobile.assert_called_once_with('type', {'text': 'expected'})

    def test_explicit_no_results_does_not_open_a_candidate(self):
        self.phone.search_was_empty = False
        self.phone.tap_label = Mock()
        self.phone.check_screen.side_effect = [
            screen(node('target', y=150), node('Search', x=880, y=150)),
            screen(node('No results found', y=700)),
        ]
        self.assertEqual(self.phone.search_and_open('target'), 'not_found')
        self.assertIs(self.phone.search_was_empty, True)
        self.assertEqual(sum(c.args[0] == 'clickGesture' for c in self.phone.mobile.call_args_list), 1)

    def test_new_query_resets_empty_flag_even_if_navigation_fails(self):
        self.phone.search_was_empty = True
        self.phone.tap_label = Mock(side_effect=ScreenUnavailable('Unreadable search screen'))
        with self.assertRaises(ScreenUnavailable):
            self.phone.search_and_open('alternate query')
        self.assertIsNone(self.phone.search_was_empty)

    def test_duplicate_ocr_cards_are_results_even_when_not_selectable(self):
        self.phone.tap_label = Mock()
        self.phone.check_screen.side_effect = [
            screen(node('target', y=150), node('Search', x=880, y=150)),
            screen(node('target', y=600), node('target', y=1200)),
        ]
        with self.assertRaises(Ambiguous):
            self.phone.search_and_open('target')
        self.assertIs(self.phone.search_was_empty, False)

    def test_screenshot_ocr_uses_local_files_and_validates_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            self.phone.visual_dir = Path(directory)
            self.phone.config['ocr_command'] = ['ocr-tool', '{image}']
            self.phone.call.return_value = base64.b64encode(b'png').decode()
            result = subprocess.CompletedProcess([], 0, json.dumps(screen(node('Discover'))), '')
            with patch('visual_phone.subprocess.run', return_value=result) as run:
                actual = self.phone.screenshot_ocr()
            self.assertEqual(actual['nodes'][0]['text'], 'Discover')
            self.assertEqual(len(list(Path(directory).glob('*.png'))), 1)
            self.assertEqual(len(list(Path(directory).glob('*.json'))), 1)
            self.assertEqual(run.call_args.kwargs['timeout'], 45)
            self.assertEqual(run.call_args.args[0][0], 'ocr-tool')
            result.stdout = json.dumps(screen(node('Search', x=2000)))
            with patch('visual_phone.subprocess.run', return_value=result):
                with self.assertRaises(ScreenUnavailable):
                    self.phone.screenshot_ocr()

    def test_screenshot_ocr_rejects_word_boxes_outside_their_line(self):
        with tempfile.TemporaryDirectory() as directory:
            self.phone.visual_dir = Path(directory)
            self.phone.config['ocr_command'] = ['ocr-tool']
            self.phone.call.return_value = base64.b64encode(b'png').decode()
            for word in (node('Search', x=80), node('Search', confidence=1.1)):
                line = dict(node('| Search'), words=[word])
                result = subprocess.CompletedProcess([], 0, json.dumps(screen(line)), '')
                with patch('visual_phone.subprocess.run', return_value=result):
                    with self.assertRaises(ScreenUnavailable):
                        self.phone.screenshot_ocr()


class PublicSearchMetadataTests(unittest.TestCase):
    def setUp(self):
        self.pacer = Mock()
        self.phone = VisualPhone({'appium_url': 'http://localhost'},
                                 {'udid': 'test', 'system_port': 8201}, self.pacer)
        self.phone.expected_appid = 'wx0123456789abcdef'
        self.phone.search_attempt_id = 'attempt-1'
        self.phone.mobile = Mock()
        self.phone.call = Mock(return_value='com.tencent.mm')
        self.phone.check_screen = Mock(return_value=screen())

    def snapshot(self, status='matched', **changes):
        candidate = {'observed_appid': self.phone.expected_appid,
                     'title': 'Nome pubblico aggiornato', 'bounds': [180, 350, 620, 410]}
        result = {'schema_version': 1, 'serial': 'test', 'attempt_id': 'attempt-1',
                  'expected_appid': self.phone.expected_appid,
                  'observed_at': datetime.now(timezone.utc).isoformat(),
                  'source': 'visible_public_search_card', 'status': status,
                  'candidate': candidate if status == 'matched' else None,
                  'candidates': [candidate] if status == 'matched' else [],
                  'webview_bounds': [0, 300, 1080, 2300]}
        result.update(changes)
        return result

    def test_exact_public_appid_opens_renamed_title_without_ocr_candidate(self):
        self.phone.tap_label = Mock()
        self.phone._search_snapshot = Mock(return_value=self.snapshot())
        # The search card title is not in the OCR at all and differs from the
        # source row's old name. Public AppID is the selection evidence.
        self.phone.check_screen.side_effect = [screen(), screen(node('Opened'))]
        self.assertEqual(self.phone.search_and_open('Previous name'), 'opened')
        self.assertEqual([call.args for call in self.phone.mobile.call_args_list],
                         [('type', {'text': 'Previous name'}),
                          ('clickGesture', {'x': 400, 'y': 380})])

    def test_wrong_appid_or_invalid_card_bounds_never_tap(self):
        wrong_id = dict(self.snapshot()['candidate'], observed_appid='wxfedcba9876543210')
        candidates = [wrong_id] + [dict(self.snapshot()['candidate'], bounds=bounds) for bounds in (
            [-1, 350, 620, 410], [180, 350, 1081, 410], [180, 410, 620, 350],
            [True, 350, 620, 410], [180.0, 350, 620, 410], None)]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.phone.mobile.reset_mock()
                self.phone._search_snapshot = Mock(return_value=self.snapshot(candidate=candidate))
                with self.assertRaises(Ambiguous):
                    self.phone._open_public_candidate(screen(node('Exact OCR name')), True)
                self.assertIs(self.phone.search_was_empty, False)
                self.phone.mobile.assert_not_called()

    def test_absent_appid_scrolls_once_and_uses_second_snapshot(self):
        self.phone._search_snapshot = Mock(side_effect=[self.snapshot('absent'), self.snapshot()])
        self.assertTrue(self.phone._open_public_candidate(screen(), True))
        self.assertEqual(self.phone._search_snapshot.call_count, 2)
        self.assertEqual([call.args[0] for call in self.phone.mobile.call_args_list],
                         ['scrollGesture', 'clickGesture'])
        scroll = self.phone.mobile.call_args_list[0].args[1]
        self.assertEqual(scroll['direction'], 'down')
        self.assertLessEqual(scroll['percent'], 1)
        self.phone.check_screen.assert_called_once()

    def test_absent_on_both_viewports_stops_after_one_scroll(self):
        self.phone._search_snapshot = Mock(side_effect=[self.snapshot('absent'), self.snapshot('absent')])
        with self.assertRaises(Ambiguous):
            self.phone._open_public_candidate(screen(), True)
        self.assertEqual(self.phone._search_snapshot.call_count, 2)
        self.assertEqual([call.args[0] for call in self.phone.mobile.call_args_list], ['scrollGesture'])

    def test_absent_unconfirmed_query_or_invalid_viewport_does_not_scroll(self):
        for confirmed, bounds in ((False, [0, 300, 1080, 2300]), (True, [0, 300, 1081, 2300])):
            with self.subTest(confirmed=confirmed, bounds=bounds):
                self.phone.mobile.reset_mock()
                self.phone._search_snapshot = Mock(return_value=self.snapshot('absent', webview_bounds=bounds))
                with self.assertRaises(Ambiguous):
                    self.phone._open_public_candidate(screen(), confirmed)
                self.phone.mobile.assert_not_called()

    def test_supported_results_without_expected_appid_are_not_found_after_one_scroll(self):
        absent = self.snapshot('absent', page_supported=True)
        self.phone._search_snapshot = Mock(side_effect=[absent, absent])
        self.assertEqual(self.phone._open_public_candidate(screen(), True), 'not_found')
        self.assertIs(self.phone.search_was_empty, True)
        self.assertEqual([call.args[0] for call in self.phone.mobile.call_args_list], ['scrollGesture'])

    def test_other_appid_cards_are_not_zero_results(self):
        other = {'observed_appid': 'wxfedcba9876543210', 'visible': True}
        absent = self.snapshot('absent', page_supported=True, candidates=[other])
        self.phone._search_snapshot = Mock(side_effect=[absent, absent])
        self.assertEqual(self.phone._open_public_candidate(screen(), True), 'not_found')
        self.assertIs(self.phone.search_was_empty, False)

    def test_first_viewport_results_are_not_lost_when_second_viewport_is_empty(self):
        first = self.snapshot('absent', page_supported=True,
                              candidates=[{'observed_appid': 'wxfedcba9876543210', 'visible': True}])
        second = self.snapshot('absent', page_supported=True, candidates=[])
        self.phone._search_snapshot = Mock(side_effect=[first, second])
        self.assertEqual(self.phone._open_public_candidate(screen(), True), 'not_found')
        self.assertIs(self.phone.search_was_empty, False)

    def test_ambiguous_public_cards_reset_empty_result_streak(self):
        ambiguous = self.snapshot('ambiguous', candidates=[{'observed_appid': self.phone.expected_appid}])
        self.phone._search_snapshot = Mock(return_value=ambiguous)
        with self.assertRaises(Ambiguous):
            self.phone._open_public_candidate(screen(), True)
        self.assertIs(self.phone.search_was_empty, False)

    def test_missing_candidate_inventory_does_not_claim_zero_results(self):
        absent = self.snapshot('absent', page_supported=True)
        absent.pop('candidates')
        self.phone._search_snapshot = Mock(side_effect=[absent, absent])
        self.assertEqual(self.phone._open_public_candidate(screen(), True), 'not_found')
        self.assertIsNone(self.phone.search_was_empty)

    def test_no_results_label_with_other_public_cards_is_not_empty(self):
        self.phone.tap_label = Mock()
        self.phone.check_screen.side_effect = [
            screen(node('target', y=150), node('Search', x=880, y=150)),
            screen(node('No results found', y=700)),
        ]
        self.phone._search_snapshot = Mock(return_value=self.snapshot('absent', page_supported=True,
            candidates=[{'observed_appid': 'wxfedcba9876543210', 'visible': True}]))
        self.assertEqual(self.phone.search_and_open('target'), 'not_found')
        self.assertIs(self.phone.search_was_empty, False)

    def test_expected_appid_present_but_offscreen_is_not_reported_missing(self):
        absent = self.snapshot('absent', page_supported=True,
                               candidates=[{'observed_appid': self.phone.expected_appid, 'visible': False}])
        self.phone._search_snapshot = Mock(side_effect=[absent, absent])
        with self.assertRaises(Ambiguous):
            self.phone._open_public_candidate(screen(), True)

    def test_second_stale_snapshot_cannot_authorize_scroll(self):
        stale = self.snapshot('absent', observed_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat())
        self.phone._search_snapshot = Mock(side_effect=[stale, stale])
        with self.assertRaises(Ambiguous):
            self.phone._open_public_candidate(screen(), True)
        self.phone.mobile.assert_not_called()

    def test_unavailable_metadata_falls_back_to_exact_ocr(self):
        self.phone.tap_label = Mock()
        self.phone._search_snapshot = Mock(return_value=self.snapshot('unavailable'))
        self.phone.check_screen.side_effect = [
            screen(node('target', y=150), node('Search', x=880, y=150)),
            screen(node('target', y=600)), screen(node('Opened'))]
        self.assertEqual(self.phone.search_and_open('target'), 'opened')
        self.assertEqual(self.phone.mobile.call_args_list[-1].args,
                         ('clickGesture', {'x': 180, 'y': 617}))
        self.phone._search_snapshot.assert_called_once()

    def test_stale_snapshot_is_read_again_and_only_new_bounds_are_tapped(self):
        old = self.snapshot(observed_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat())
        fresh = self.snapshot(candidate=dict(self.snapshot()['candidate'], bounds=[600, 900, 900, 1000]))
        self.phone._search_snapshot = Mock(side_effect=[old, fresh])
        self.assertTrue(self.phone._open_public_candidate(screen(), True))
        self.assertEqual(self.phone._search_snapshot.call_count, 2)
        self.phone.mobile.assert_called_once_with('clickGesture', {'x': 750, 'y': 950})

    def test_two_stale_or_future_snapshots_never_tap(self):
        for seconds in (-30, 300):
            self.phone.mobile.reset_mock()
            stale = self.snapshot(observed_at=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())
            self.phone._search_snapshot = Mock(side_effect=[stale, stale])
            with self.assertRaises(Ambiguous):
                self.phone._open_public_candidate(screen(), True)
            self.assertEqual(self.phone._search_snapshot.call_count, 2)
            self.phone.mobile.assert_not_called()

    def test_public_rate_limit_keeps_retry_after_without_tapping(self):
        self.phone._search_snapshot = Mock(return_value=self.snapshot('blocked', wall_kind='rate_limit', retry_after=137))
        with self.assertRaises(RateLimit) as error:
            self.phone._open_public_candidate(screen(), True)
        self.assertEqual(error.exception.retry_after, 137)
        self.phone.mobile.assert_not_called()

    def test_alternative_query_requires_structured_identity_even_with_exact_ocr_name(self):
        self.phone.require_public_identity = True
        for snapshot in (None, self.snapshot('unavailable')):
            with self.subTest(snapshot=snapshot):
                self.phone._search_snapshot = Mock(return_value=snapshot)
                with self.assertRaises(Ambiguous):
                    self.phone._open_public_candidate(screen(node('Exact hint')), True)
        self.phone.mobile.assert_not_called()

    def test_reader_rejects_another_attempt_and_keeps_unavailable_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            self.phone.visual_dir = Path(directory) / 'ui'
            self.phone.config['search_metadata_command'] = ['reader', '{serial}', '{appid}', '{attempt_id}', '{output}']
            mismatched = self.snapshot(attempt_id='another-attempt')
            def read_fixture(command, **kwargs):
                self.assertEqual(command[1:4], ['test', self.phone.expected_appid, 'attempt-1'])
                Path(command[-1]).write_text(json.dumps(mismatched))
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch('visual_phone.subprocess.run', side_effect=read_fixture):
                result = self.phone._search_snapshot()
            self.assertEqual(result['status'], 'unavailable')
            receipt = Path(self.phone.search_observation['path'])
            self.assertEqual(json.loads(receipt.read_text())['attempt_id'], 'attempt-1')
            self.assertEqual(len(self.phone.search_observation['sha256']), 64)
            self.phone.mobile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
