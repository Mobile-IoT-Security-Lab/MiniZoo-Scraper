"""Small Appium W3C client with no external Python dependencies."""
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


class PhoneError(RuntimeError):
    pass


class Suspended(PhoneError):
    pass


class Ambiguous(PhoneError):
    pass


class ScreenUnavailable(PhoneError):
    """The device UI is unavailable; this is not an account suspension."""


class RateLimit(PhoneError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class Stopped(PhoneError):
    pass


def request(url, method="GET", data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        raise PhoneError(payload[:1500]) from error
    value = result.get("value", result)
    if isinstance(value, dict) and value.get("error"):
        raise PhoneError(value.get("message", str(value)))
    return value


class Pacer:
    """Share pacing by account and persist suspensions across restarts."""
    def __init__(self, path, stop, gap=5):
        self.path, self.stop, self.gap = Path(path), stop, max(5, gap)
        self.lock = threading.RLock()
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        if saved.get("schema_version") == 1:
            self.accounts = saved["accounts"]
            self.next_request = saved.get("next_request", 0)
        else:
            self.accounts = saved
            self.next_request = max((s.get("next", 0) for s in saved.values()), default=0)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump({"schema_version": 1, "accounts": self.accounts,
                       "next_request": self.next_request}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def wait(self, seconds):
        if self.stop.wait(max(0, seconds)):
            raise Stopped("Stop requested")

    def before(self, account):
        while True:
            self.wait(0)
            with self.lock:
                state = self.accounts.setdefault(account, {})
                if state.get("blocked"):
                    raise Suspended(state.get("reason", "Account suspended"))
                delay = max(state.get("until", 0), state.get("next", 0), self.next_request) - time.time()
                if delay <= 0:
                    state["next"] = time.time() + self.gap
                    self.next_request = state["next"]
                    self._save()
                    return
            self.wait(min(delay, 1))

    def suspend(self, account, reason, seconds=None):
        with self.lock:
            state = self.accounts.setdefault(account, {})
            # A late retry from a sibling phone cannot lift a permanent block.
            if state.get("blocked"):
                return
            state.update(reason=reason, blocked=seconds is None,
                         until=max(state.get("until", 0), time.time() + max(0, seconds or 0)))
            self._save()


def xpath_literal(text):
    if "'" not in text:
        return "'" + text + "'"
    if '"' not in text:
        return '"' + text + '"'
    return "concat(" + ",\"'\",".join("'" + p + "'" for p in text.split("'")) + ")"


def text_xpath(*labels):
    # App names may contain both types of quotes.
    return "//*[" + " or ".join("@text=" + xpath_literal(s) + " or @content-desc=" + xpath_literal(s)
                                for s in labels) + "]"


DEFAULT_SELECTORS = {
    "home": text_xpath("Contacts", "Contatti", "通讯录"),
    "discover": text_xpath("Discover", "Scopri", "发现"),
    "mini_programs": text_xpath("Mini Programs", "Mini programs", "小程序"),
    "search": text_xpath("Search", "Cerca", "搜索"),
    "search_input": "//android.widget.EditText",
    "no_results": text_xpath("No results", "No results found", "没有找到相关小程序", "无搜索结果"),
}


class Phone:
    def __init__(self, config, device, pacer):
        self.config, self.device, self.pacer = config, device, pacer
        self.account = device.get("account", "shared")
        self.url = config["appium_url"].rstrip("/")
        self.session = None
        self.selectors = dict(DEFAULT_SELECTORS, **config.get("selectors", {}))
        # A restarted worker cannot know how recently the previous one finished.
        self.last_finished = time.monotonic()
        self.failed_source = None
        self.failed_screenshot = None
        # True: explicit empty results; False: results observed; None: unknown.
        self.search_was_empty = None

    def connect(self):
        caps = {"platformName": "Android", "appium:automationName": "UiAutomator2",
                "appium:udid": self.device["udid"], "appium:systemPort": self.device["system_port"],
                "appium:noReset": True, "appium:fullReset": False,
                "appium:autoLaunch": False, "appium:newCommandTimeout": 600,
                "appium:skipUnlock": True}
        self.session = request(self.url + "/session", "POST", {
            "capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}})["sessionId"]

    def call(self, endpoint, method="POST", data=None):
        return request(self.url + "/session/" + self.session + endpoint, method, data)

    def mobile(self, command, arguments):
        return self.call("/execute/sync", data={"script": "mobile: " + command, "args": [arguments]})

    def source(self):
        return self.call("/source", "GET")

    def check_screen(self):
        source = self.source()
        if re.search(r"too (?:many|frequent) (?:requests|attempts)|rate limit(?:ed| exceeded)?|操作(?:过于|太)?频繁|访问(?:过于|太)?频繁|请求过于频繁", source, re.I):
            delay = re.search(r"(\d+)\s*(seconds?|minutes?|秒|分钟)", source, re.I)
            seconds = int(delay[1]) * (60 if delay and delay[2].lower().startswith(("minute", "分钟")) else 1) if delay else 0
            raise RateLimit("WeChat reports a rate limit", seconds)
        if re.search(r"captcha|security verification|安全验证|帐号被封|账号被封", source, re.I):
            raise Suspended("WeChat requires verification or reports a block")
        return source

    def elements(self, selector):
        return self.call("/elements", data={"using": "xpath", "value": selector})

    def find(self, selector, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            self.check_screen()
            elements = self.elements(selector)
            if elements:
                if len(elements) != 1:
                    raise Ambiguous("Ambiguous selector: " + selector)
                element_id = elements[0].get("element-6066-11e4-a52e-4f735466cecf")
                if not element_id:
                    raise PhoneError("Appium response has no W3C element identifier")
                return element_id
            if time.monotonic() >= deadline:
                raise PhoneError("Element not found: " + selector)
            self.pacer.wait(1)

    def click(self, selector):
        element = self.find(selector)
        self.pacer.before(self.account)
        self.call("/element/" + element + "/click", data={})

    @staticmethod
    def has_wechat_nodes(source):
        """An empty/disabled root is not evidence that Back can navigate safely."""
        try:
            root = ET.fromstring(source)
        except (ET.ParseError, TypeError):
            return False
        for node in root.iter():
            attributes = node.attrib
            if (attributes.get("package") != "com.tencent.mm"
                    or attributes.get("enabled") == "false"
                    or attributes.get("displayed") == "false"):
                continue
            bounds = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", attributes.get("bounds", ""))
            if bounds is None or int(bounds[3]) <= int(bounds[1]) or int(bounds[4]) <= int(bounds[2]):
                continue
            if (attributes.get("text", "").strip() or attributes.get("content-desc", "").strip()
                    or attributes.get("clickable") == "true" or attributes.get("scrollable") == "true"):
                return True
        return False

    def screen_unavailable(self, reason, source):
        self.failed_source = source
        try:
            self.failed_screenshot = self.call("/screenshot", "GET")
        except Exception:
            self.failed_screenshot = None
        raise ScreenUnavailable(reason)

    def refresh(self):
        self.failed_source = self.failed_screenshot = None
        self.pacer.wait(max(0, self.last_finished + max(10, self.config.get("between_apps", 10)) - time.monotonic()))
        self.pacer.before(self.account)
        self.mobile("terminateApp", {"appId": "com.tencent.mm"})
        self.mobile("activateApp", {"appId": "com.tencent.mm"})
        source = ""
        for attempt in range(5):
            self.pacer.wait(2)
            source = self.check_screen()
            if self.elements(self.selectors["home"]):
                self.pacer.wait(5)
                return
            if self.has_wechat_nodes(source) and attempt < 4:
                self.pacer.before(self.account)
                self.call("/back", data={})
        if not self.has_wechat_nodes(source):
            self.screen_unavailable("native hierarchy empty: Appium exposes no accessible WeChat nodes; navigation stopped", source)
        self.screen_unavailable("WeChat home not recognized: check the device screen and login", source)

    def search_and_open(self, name):
        self.search_was_empty = None
        self.click(self.selectors["discover"])
        self.click(self.selectors["mini_programs"])
        self.click(self.selectors["search"])
        element = self.find(self.selectors["search_input"])
        self.pacer.before(self.account)
        self.call("/element/" + element + "/clear", data={})
        self.pacer.before(self.account)
        self.call("/element/" + element + "/value", data={"text": name})
        self.pacer.before(self.account)
        self.mobile("performEditorAction", {"action": "search"})
        self.pacer.wait(5)
        self.check_screen()
        if self.elements(self.selectors["no_results"]):
            self.search_was_empty = True
            return "not_found"
        # The name selects a candidate only. Package evidence verifies identity.
        candidate = self.selectors.get("result", "//*[@text={name} and not(self::android.widget.EditText)]")
        candidate = candidate.replace("{name}", xpath_literal(name))
        # Multiple matching cards are ambiguous, but are not an empty search.
        if self.elements(candidate):
            self.search_was_empty = False
        self.click(candidate)
        self.search_was_empty = False
        self.pacer.wait(max(5, self.config.get("load_wait", 8)))
        self.check_screen()
        return "opened"

    def diagnostics(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        source = self.failed_source if self.failed_source is not None else self.source()
        screenshot = self.failed_screenshot if self.failed_screenshot is not None else self.call("/screenshot", "GET")
        (directory / "screen.xml").write_text(source, encoding="utf-8")
        (directory / "screen.png").write_bytes(base64.b64decode(screenshot))

    def close(self):
        if self.session:
            self.call("", "DELETE")
            self.session = None
