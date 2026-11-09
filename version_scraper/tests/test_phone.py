import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from phone import Ambiguous, Pacer, Phone, PhoneError, RateLimit, ScreenUnavailable, Stopped, Suspended, text_xpath, xpath_literal


ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
ACCESSIBLE_SCREEN = '<hierarchy><node package="com.tencent.mm" enabled="true" text="Previous Mini Program" bounds="[0,0][500,500]"/></hierarchy>'
EMPTY_SCREEN = '<hierarchy><android.view.View package="com.tencent.mm" text="" enabled="false" bounds="[0,0][0,0]"/></hierarchy>'


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.stopped = False
        self.waits = []

    def time(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.stopped


class PacerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pacing.json"
        self.clock = Clock()
        patcher = patch("phone.time.time", self.clock.time)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_global_gap_covers_different_accounts_and_restart(self):
        pacer = Pacer(self.path, self.clock, gap=0)
        pacer.before("account-a")
        self.assertEqual(self.clock.now, 1000)
        pacer.before("account-b")
        self.assertEqual(self.clock.now, 1005)
        resumed = Pacer(self.path, self.clock)
        resumed.before("account-c")
        self.assertEqual(self.clock.now, 1010)
        self.assertEqual(json.loads(self.path.read_text())["next_request"], 1015)

    def test_account_pause_survives_restart_and_does_not_pause_other_account(self):
        pacer = Pacer(self.path, self.clock)
        pacer.suspend("shared", "rate limit", seconds=60)
        resumed = Pacer(self.path, self.clock)
        resumed.before("independent")
        self.assertEqual(self.clock.now, 1000)
        resumed.before("shared")
        self.assertEqual(self.clock.now, 1060)

    def test_account_block_survives_restart_and_late_temporary_suspension(self):
        pacer = Pacer(self.path, self.clock)
        pacer.suspend("shared", "persistent limit")
        pacer.suspend("shared", "sibling retry", seconds=60)
        resumed = Pacer(self.path, self.clock)
        with self.assertRaisesRegex(Suspended, "persistent limit"):
            resumed.before("shared")
        self.assertEqual(self.clock.now, 1000)

    def test_shorter_pause_cannot_reduce_service_wait(self):
        pacer = Pacer(self.path, self.clock)
        pacer.suspend("shared", "service says 120", seconds=120)
        pacer.suspend("shared", "sibling default", seconds=60)
        pacer.before("shared")
        self.assertEqual(self.clock.now, 1120)

    def test_stop_rejects_even_immediately_available_request(self):
        pacer = Pacer(self.path, self.clock)
        self.clock.stopped = True
        with self.assertRaises(Stopped):
            pacer.before("shared")
        with self.assertRaises(Stopped):
            pacer.wait(60)
        self.assertFalse(self.path.exists())


class PhoneTests(unittest.TestCase):
    def setUp(self):
        self.pacer = Mock()
        self.phone = Phone({"appium_url": "http://localhost:4723", "between_apps": 1},
                           {"udid": "test", "system_port": 8200}, self.pacer)

    def test_xpath_literals_preserve_quotes_and_injection_looking_names(self):
        self.assertEqual(xpath_literal("微信"), "'微信'")
        self.assertEqual(xpath_literal("L'app"), '"L\'app"')
        self.assertEqual(xpath_literal('a"b'), "'a\"b'")
        self.assertEqual(xpath_literal('a\'b"c'), 'concat(\'a\',"\'",\'b"c\')')
        name = "fake or @content-desc='x' and \"y\""
        expected = 'concat(\'fake or @content-desc=\',"\'",\'x\',"\'",\' and "y"\')'
        self.assertEqual(xpath_literal(name), expected)
        self.assertEqual(text_xpath(name), "//*[@text=" + expected + " or @content-desc=" + expected + "]")

    def test_refresh_waits_ten_seconds_on_restart_then_home_and_extra_five(self):
        events = []
        with patch("phone.time.monotonic", return_value=500):
            phone = Phone({"appium_url": "http://localhost", "between_apps": 0},
                          {"udid": "test", "system_port": 8200}, self.pacer)
            self.pacer.wait.side_effect = lambda seconds: events.append(("wait", seconds))
            self.pacer.before.side_effect = lambda account: events.append(("before", account))
            phone.mobile = Mock(side_effect=lambda name, args: events.append((name, args)))
            phone.check_screen = Mock(side_effect=lambda: events.append(("screen",)))
            phone.elements = Mock(return_value=[{ELEMENT: "home"}])
            phone.refresh()
        self.assertEqual(events, [("wait", 10), ("before", "shared"),
                                  ("terminateApp", {"appId": "com.tencent.mm"}),
                                  ("activateApp", {"appId": "com.tencent.mm"}),
                                  ("wait", 2), ("screen",), ("wait", 5)])

    def test_refresh_returns_from_previous_miniapp_before_ready_wait(self):
        self.phone.mobile = Mock()
        self.phone.check_screen = Mock(return_value=ACCESSIBLE_SCREEN)
        self.phone.elements = Mock(side_effect=[[], [{ELEMENT: "home"}]])
        self.phone.call = Mock()
        self.phone.refresh()
        self.phone.call.assert_called_once_with("/back", data={})
        self.assertEqual(self.pacer.wait.call_args_list[-1].args, (5,))

    def test_refresh_unrecognized_home_is_device_failure_and_bounded(self):
        self.phone.mobile = Mock()
        self.phone.check_screen = Mock(return_value=ACCESSIBLE_SCREEN)
        self.phone.elements = Mock(return_value=[])
        self.phone.call = Mock(return_value="cG5n")
        with self.assertRaises(ScreenUnavailable):
            self.phone.refresh()
        self.assertEqual(sum(call.args[0] == "/back" for call in self.phone.call.call_args_list), 4)
        self.assertEqual(self.phone.call.call_args_list[-1].args, ("/screenshot", "GET"))
        self.pacer.suspend.assert_not_called()

    def test_empty_hierarchy_waits_then_preserves_screen_without_back(self):
        self.phone.mobile = Mock()
        self.phone.check_screen = Mock(return_value=EMPTY_SCREEN)
        self.phone.elements = Mock(return_value=[])
        self.phone.call = Mock(return_value="cG5n")
        with self.assertRaisesRegex(ScreenUnavailable, "native hierarchy empty"):
            self.phone.refresh()
        self.phone.call.assert_called_once_with("/screenshot", "GET")
        self.assertEqual(sum(call.args == (2,) for call in self.pacer.wait.call_args_list), 5)
        self.phone.source = Mock(side_effect=AssertionError("Failing source must be reused"))
        with tempfile.TemporaryDirectory() as directory:
            self.phone.diagnostics(directory)
            self.assertEqual((Path(directory) / "screen.xml").read_text(), EMPTY_SCREEN)
            self.assertEqual((Path(directory) / "screen.png").read_bytes(), b"png")
        self.phone.call.assert_called_once()
        self.pacer.suspend.assert_not_called()

    def test_launcher_nodes_do_not_authorize_back_navigation(self):
        launcher = '<hierarchy><node package="com.google.android.apps.nexuslauncher" text="Search" clickable="true" bounds="[0,0][500,500]"/></hierarchy>'
        self.assertFalse(Phone.has_wechat_nodes(launcher))
        self.assertFalse(Phone.has_wechat_nodes(EMPTY_SCREEN))
        self.assertTrue(Phone.has_wechat_nodes(ACCESSIBLE_SCREEN))

    def test_search_explicit_no_results_is_terminal(self):
        self.phone.click = Mock()
        self.phone.find = Mock(return_value="input")
        self.phone.call = Mock()
        self.phone.mobile = Mock()
        self.phone.check_screen = Mock()
        self.phone.elements = Mock(return_value=[{ELEMENT: "no-results"}])
        self.assertEqual(self.phone.search_and_open("app"), "not_found")
        self.assertIs(self.phone.search_was_empty, True)
        self.assertEqual(self.phone.click.call_count, 3)
        self.assertEqual(self.phone.call.call_args_list[0].args, ("/element/input/clear",))
        self.assertEqual(self.phone.call.call_args_list[1].kwargs["data"], {"text": "app"})

    def test_search_resets_previous_empty_evidence_before_navigation_error(self):
        self.phone.search_was_empty = True
        self.phone.click = Mock(side_effect=ScreenUnavailable("No search screen"))
        with self.assertRaises(ScreenUnavailable):
            self.phone.search_and_open("another app")
        self.assertIsNone(self.phone.search_was_empty)

    def test_multiple_search_cards_are_not_empty_even_when_ambiguous(self):
        candidate = "//*[@text='app' and not(self::android.widget.EditText)]"
        def find(selector):
            if selector == candidate:
                raise Ambiguous("Multiple cards")
            return "input"
        self.phone.find = Mock(side_effect=find)
        self.phone.call = Mock()
        self.phone.mobile = Mock()
        self.phone.check_screen = Mock()
        self.phone.elements = Mock(side_effect=lambda selector: [] if selector == self.phone.selectors["no_results"]
                                   else [{ELEMENT: "one"}, {ELEMENT: "two"}])
        with self.assertRaises(Ambiguous):
            self.phone.search_and_open("app")
        self.assertIs(self.phone.search_was_empty, False)

    def test_multiple_name_matches_are_ambiguous_and_never_clicked(self):
        self.phone.check_screen = Mock()
        self.phone.elements = Mock(return_value=[{ELEMENT: "first"}, {ELEMENT: "second"}])
        self.phone.call = Mock()
        with self.assertRaises(Ambiguous):
            self.phone.click(text_xpath("same name"))
        self.phone.call.assert_not_called()
        self.pacer.before.assert_not_called()

    def test_missing_element_at_deadline_and_malformed_w3c_response_fail(self):
        self.phone.check_screen = Mock()
        self.phone.elements = Mock(return_value=[])
        with self.assertRaisesRegex(PhoneError, "Element not found"):
            self.phone.find("//missing", timeout=0)
        self.phone.elements.return_value = [{"unexpected": "value"}]
        with self.assertRaisesRegex(PhoneError, "W3C"):
            self.phone.find("//malformed")

    def test_rate_limit_chinese_and_service_wait_are_read(self):
        self.phone.source = Mock(return_value='<node text="操作过于频繁 2 分钟"/>')
        with self.assertRaises(RateLimit) as error:
            self.phone.check_screen()
        self.assertEqual(error.exception.retry_after, 120)
        self.phone.source.return_value = '<node text="Too many requests; wait 45 seconds"/>'
        with self.assertRaises(RateLimit) as error:
            self.phone.check_screen()
        self.assertEqual(error.exception.retry_after, 45)


if __name__ == "__main__":
    unittest.main()
