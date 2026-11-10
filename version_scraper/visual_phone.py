"""Appium navigation using exact visible labels when accessibility is empty.

OCR chooses candidates only. It never establishes the package AppID or version.
Screenshots and OCR stay in the local diagnostics directory, never on stdout.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import unicodedata
import uuid

from phone import Ambiguous, Phone, PhoneError, RateLimit, ScreenUnavailable, Suspended


DISCOVER = ('Discover', 'Scopri', '发现')
CONTACTS = ('Contacts', 'Contatti', '通讯录')
ME = ('Me', 'Io', '我')
MINI_PROGRAMS = ('Mini Programs', 'Mini programs', '小程序')
SEARCH_LANDING = ('Search Mini Programs', 'Search Mini Program', '搜索小程序',
                  'Q Search Mini Programs', 'Q Search Mini Program')
SEARCH = ('Search', 'Cerca', '搜索')
NO_RESULTS = ('No results', 'No results found', '没有找到相关小程序', '无搜索结果', '未找到相关小程序')
# Only exact known result-card category suffixes are accepted, never arbitrary
# prefix matches. Even these are search candidates awaiting package evidence.
CATEGORIES = ('事业单位', '政府', '企业', '媒体', '其他组织', '个体工商户',
              '持牌金融机构', '交易保障')


def normalized(text):
    return unicodedata.normalize('NFC', text).strip()


def distinct_boxes(nodes):
    """Merge repeated OCR readings of one location, preserving separate results."""
    result = []
    for node in sorted(nodes, key=lambda item: item['confidence'], reverse=True):
        left, top, right, bottom = node['bounds']
        area = (right - left) * (bottom - top)
        repeated = False
        for previous in result:
            a, b, c, d = previous['bounds']
            intersection = max(0, min(right, c) - max(left, a)) * max(0, min(bottom, d) - max(top, b))
            previous_area = (c - a) * (d - b)
            union = area + previous_area - intersection
            # A second OCR pass can read the name alone while another includes
            # its category badge. Containment at the same line start is one title.
            height = max(bottom - top, d - b)
            same_start = (abs(left - a) <= height * 0.5
                          and abs((top + bottom - b - d) / 2) <= height * 0.4)
            contained = (min(area, previous_area) > 0
                         and intersection / min(area, previous_area) >= 0.8 and same_start)
            if (union > 0 and intersection / union >= 0.5) or contained:
                repeated = True
                break
        if not repeated:
            result.append(node)
    return result


class VisualPhone(Phone):
    def __init__(self, config, device, pacer):
        super().__init__(config, device, pacer)
        base = Path(config.get('output_dir', '.'))
        self.visual_dir = base / 'reports' / ('visual-' + device.get('name', device['udid'])) / uuid.uuid4().hex
        self.visual_step = 0
        self.last_visual = None
        self.expected_appid = None
        self.search_attempt_id = None
        self.search_observation = None
        self.require_public_identity = False

    def _search_snapshot(self):
        """Read public card metadata; no navigation or package certification."""
        command = self.config.get('search_metadata_command')
        if not command:
            return None
        if (not isinstance(command, list) or not all(isinstance(arg, str) and arg for arg in command)
                or not re.fullmatch(r'wx[0-9a-f]{16}', self.expected_appid or '') or not self.search_attempt_id):
            raise PhoneError('Search metadata configuration or attempt identity missing')
        output = Path(self.visual_dir).parent / ('search-observation-' + uuid.uuid4().hex + '.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        values = {'serial': self.device['udid'], 'appid': self.expected_appid,
                  'attempt_id': self.search_attempt_id, 'output': str(output)}
        arguments = []
        for arg in command:
            for key, value in values.items():
                arg = arg.replace('{' + key + '}', value)
            arguments.append(arg)
        try:
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=35)
            if result.returncode:
                raise ValueError('search_reader_failed')
            snapshot = json.loads(output.read_text(encoding='utf-8'))
            if (snapshot.get('schema_version') != 1 or snapshot.get('expected_appid') != self.expected_appid
                    or snapshot.get('attempt_id') != self.search_attempt_id
                    or snapshot.get('serial') != self.device['udid']
                    or snapshot.get('status') not in ('matched', 'absent', 'ambiguous', 'unavailable', 'blocked')):
                raise ValueError('search_snapshot_identity_mismatch')
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            snapshot = dict(schema_version=1, expected_appid=self.expected_appid,
                            attempt_id=self.search_attempt_id, serial=self.device['udid'],
                            observed_at=datetime.now(timezone.utc).isoformat(), status='unavailable',
                            candidate=None, candidates=[], source='visible_public_search_card',
                            reason='search_metadata_unavailable')
            output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        self.search_observation = {'path': str(output.absolute()),
                                   'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
        return snapshot

    @staticmethod
    def _snapshot_fresh(snapshot):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot['observed_at'])).total_seconds()
            return 0 <= age <= 15
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _screen_bounds(bounds, screen):
        return (isinstance(bounds, list) and len(bounds) == 4
                and all(type(v) is int for v in bounds)
                and 0 <= bounds[0] < bounds[2] <= screen['width']
                and 0 <= bounds[1] < bounds[3] <= screen['height'])

    def _remember_search_results(self, snapshot):
        """A wrong AppID or a populated first viewport is still a result."""
        if not isinstance(snapshot, dict) or not self._snapshot_fresh(snapshot):
            return
        candidates = snapshot.get('candidates')
        if ((isinstance(candidates, list) and candidates)
                or (snapshot.get('status') == 'matched' and snapshot.get('candidate'))):
            self.search_was_empty = False

    @staticmethod
    def _raise_public_wall(snapshot):
        if snapshot and snapshot.get('status') == 'blocked':
            if snapshot.get('wall_kind') == 'rate_limit':
                raise RateLimit('WeChat reports a public search rate limit', snapshot.get('retry_after', 0))
            if snapshot.get('wall_kind') == 'login':
                raise ScreenUnavailable('Public search requires manual login')
            raise Suspended('Public search requires login or verification, or reports a rate limit')

    def _open_public_candidate(self, screen, query_confirmed):
        """Return opened/not_found, or None when structured reading is unavailable."""
        snapshot = self._search_snapshot()
        self._remember_search_results(snapshot)
        if snapshot is None or snapshot['status'] == 'unavailable':
            if self.require_public_identity:
                raise Ambiguous('Alternative name: public AppID identity unavailable')
            return None
        # One additional viewport, only after submitting a verified query.
        scrolled = False
        if snapshot['status'] == 'absent' and query_confirmed:
            self.pacer.before(self.account)
            if not self._snapshot_fresh(snapshot):
                snapshot = self._search_snapshot()
                self._remember_search_results(snapshot)
                self.pacer.before(self.account)
            bounds = snapshot.get('webview_bounds')
            if snapshot['status'] == 'absent' and self._screen_bounds(bounds, screen):
                if not self._snapshot_fresh(snapshot):
                    raise Ambiguous('Search metadata expired before scrolling')
                left, top, right, bottom = bounds
                self._foreground()
                self.mobile('scrollGesture', {'left': left + (right - left) // 10,
                            'top': top + (bottom - top) // 10, 'width': (right - left) * 8 // 10,
                            'height': (bottom - top) * 8 // 10, 'direction': 'down', 'percent': 0.7})
                self.pacer.wait(5)
                screen = self.check_screen()
                snapshot = self._search_snapshot()
                self._remember_search_results(snapshot)
                scrolled = True
        if (scrolled and snapshot['status'] == 'absent' and snapshot.get('page_supported') is True
                and self._snapshot_fresh(snapshot)
                and not any(c.get('observed_appid') == self.expected_appid for c in snapshot.get('candidates', []))):
            if self.search_was_empty is not False and snapshot.get('candidates') == []:
                self.search_was_empty = True
            return 'not_found'  # Absent in the public results read, not a claim that the app does not exist.
        for attempt in range(2):
            self._raise_public_wall(snapshot)
            if snapshot['status'] != 'matched':
                raise Ambiguous('Search metadata: ' + snapshot.get('reason', snapshot['status']))
            candidate = snapshot.get('candidate') or {}
            if (candidate.get('observed_appid') != self.expected_appid
                    or not self._screen_bounds(candidate.get('bounds'), screen)):
                raise Ambiguous('Public card AppID or coordinates unverified')
            self.pacer.before(self.account)
            if self._snapshot_fresh(snapshot):
                self._foreground()
                x, y = self._center(candidate)
                self.mobile('clickGesture', {'x': x, 'y': y})
                self.search_was_empty = False
                return 'opened'
            if attempt == 0:
                snapshot = self._search_snapshot()
                self._remember_search_results(snapshot)
        raise Ambiguous('Search metadata expired before the tap')

    def screenshot_ocr(self):
        """One current Appium screenshot and its corresponding local OCR result."""
        directory = Path(self.visual_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.visual_step += 1
        stem = '%04d-%s' % (self.visual_step, uuid.uuid4().hex[:8])
        image = directory / (stem + '.png')
        encoded = self.call('/screenshot', 'GET')
        image.write_bytes(base64.b64decode(encoded, validate=True))
        command = self.config.get('ocr_command') or [str(Path(__file__).parent / 'tools' / 'ocr')]
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
            raise ScreenUnavailable('ocr_command must be an argument list')
        arguments = [arg.replace('{image}', str(image)) for arg in command]
        if not any('{image}' in arg for arg in command):
            arguments.append(str(image))
        try:
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.SubprocessError) as error:
            raise ScreenUnavailable('Local OCR unavailable or exceeded the 45-second timeout') from error
        if result.returncode:
            raise ScreenUnavailable('Local OCR failed; screenshot retained at ' + str(image))
        try:
            screen = json.loads(result.stdout)
            width, height = screen['width'], screen['height']
            if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
                raise ValueError('invalid dimensions')
            if not isinstance(screen['nodes'], list):
                raise ValueError('invalid nodes')
            for node in screen['nodes']:
                left, top, right, bottom = node['bounds']
                if (not isinstance(node['text'], str) or not 0 <= node['confidence'] <= 1
                        or any(not isinstance(v, int) for v in node['bounds'])
                        or not 0 <= left < right <= width or not 0 <= top < bottom <= height):
                    raise ValueError('invalid node')
                if not isinstance(node.get('words', []), list):
                    raise ValueError('invalid words')
                for word in node.get('words', []):
                    if (not isinstance(word['text'], str) or not word['text']
                            or not 0 <= word['confidence'] <= 1
                            or not self._screen_bounds(word['bounds'], screen)
                            or not left <= word['bounds'][0] < word['bounds'][2] <= right
                            or not top <= word['bounds'][1] < word['bounds'][3] <= bottom):
                        raise ValueError('invalid word')
        except (KeyError, TypeError, ValueError) as error:
            raise ScreenUnavailable('Invalid OCR response') from error
        # Keep the complete evidence locally, even when a later guard rejects it.
        (directory / (stem + '.json')).write_text(json.dumps(screen, ensure_ascii=False, indent=2), encoding='utf-8')
        screen['screenshot_path'] = str(image)
        self.last_visual = screen
        self.failed_screenshot = encoded
        return screen

    @staticmethod
    def confident_nodes(screen):
        return [node for node in screen['nodes'] if node['confidence'] >= 0.8]

    def check_screen(self):
        screen = self.screenshot_ocr()
        visible = '\n'.join(node['text'] for node in self.confident_nodes(screen))
        if re.search(r'too (?:many|frequent) (?:requests|attempts)|rate limit(?:ed| exceeded)?|操作(?:过于|太)?频繁|访问(?:过于|太)?频繁|请求过于频繁', visible, re.I):
            delay = re.search(r'(\d+)\s*(seconds?|minutes?|秒|分钟)', visible, re.I)
            seconds = int(delay[1]) * (60 if delay and delay[2].lower().startswith(('minute', '分钟')) else 1) if delay else 0
            raise RateLimit('WeChat reports a rate limit', seconds)
        if re.search(r'captcha|security verification|安全验证|帐号被封|账号被封', visible, re.I):
            raise Suspended('WeChat requires verification or reports a block')
        # A mini-app login/consent is not automatically an account suspension.
        # This class never presses Agree, Allow, Confirm, or login controls.
        return screen

    @staticmethod
    def _center(node):
        left, top, right, bottom = node['bounds']
        return (left + right) // 2, (top + bottom) // 2

    @classmethod
    def _home(cls, screen):
        bottom = [normalized(n['text']) for n in cls.confident_nodes(screen)
                  if n['bounds'][1] >= screen['height'] * 0.8]
        return all(any(label in bottom for label in group) for group in (CONTACTS, DISCOVER, ME))

    @classmethod
    def _matching(cls, screen, labels, predicate=None, navigation=False):
        candidates = cls.confident_nodes(screen)
        if navigation:
            for line in screen['nodes']:
                words = line.get('words', [])
                text_words = [word for word in words if any(c.isalnum() for c in word['text'])]
                # A label plus only symbols (e.g. back arrow/caret + Search) is
                # safe to locate by its own word box. Never split a phrase or
                # reuse this rule for typed queries or app-result identities.
                if len(words) > 1 and len(text_words) == 1 and text_words[0]['confidence'] >= 0.8:
                    candidates.append(text_words[0])
        nodes = [node for node in candidates
                 if normalized(node['text']) in labels and (predicate is None or predicate(node, screen))]
        return distinct_boxes(nodes)

    def _foreground(self):
        if self.call('/appium/device/current_package', 'GET') != 'com.tencent.mm':
            raise ScreenUnavailable('WeChat is not in the foreground: no tap performed')

    def _tap_node(self, node):
        # OCR can take time while another device acts on the shared account.
        # Gate the actual action again after recognition, immediately before tap.
        self.pacer.before(self.account)
        self._foreground()
        x, y = self._center(node)
        self.mobile('clickGesture', {'x': x, 'y': y})

    def tap_label(self, labels, predicate=None):
        self.pacer.before(self.account)
        for attempt in range(5):
            screen = self.check_screen()
            matches = self._matching(screen, labels, predicate, navigation=True)
            if len(matches) > 1:
                raise Ambiguous('Visual label appears in multiple locations: ' + '/'.join(labels))
            if matches:
                self._tap_node(matches[0])
                return
            if attempt < 4:
                self.pacer.wait(1)
        raise ScreenUnavailable('Visual label not found: ' + '/'.join(labels))

    def refresh(self):
        self.failed_source = self.failed_screenshot = None
        self.pacer.wait(max(0, self.last_finished + max(10, self.config.get('between_apps', 10)) - time.monotonic()))
        self.pacer.before(self.account)
        self.mobile('terminateApp', {'appId': 'com.tencent.mm'})
        self.mobile('activateApp', {'appId': 'com.tencent.mm'})
        for _ in range(5):
            self.pacer.wait(2)
            screen = self.check_screen()
            if self._home(screen):
                self._foreground()
                self.pacer.wait(5)
                return
        raise ScreenUnavailable('WeChat home not recognized by OCR; no Back or tap performed')

    @classmethod
    def _candidate_titles(cls, screen, name):
        expected = normalized(name)
        matches = []
        for node in cls.confident_nodes(screen):
            if cls._center(node)[1] < screen['height'] * 0.14:
                continue  # The search input is not a search result.
            text = normalized(node['text'])
            remainder = text[len(expected):] if text.startswith(expected) else ''
            suffix = remainder.strip(' \u00a0·|') if remainder[:1] in (' ', '\u00a0', '·', '|') else None
            if text == expected or suffix in CATEGORIES:
                matches.append(node)
        matches = distinct_boxes(matches)
        # The app icon sometimes contains the same name in tiny print. It is
        # part of the adjacent card, not a second result. Keep separate cards.
        titles = []
        for node in matches:
            left, top, right, bottom = node['bounds']
            is_logo = any(
                title is not node and right <= title['bounds'][0]
                and title['bounds'][0] >= screen['width'] * 0.14
                and bottom - top < (title['bounds'][3] - title['bounds'][1]) * 0.7
                and title['bounds'][1] <= cls._center(node)[1]
                <= title['bounds'][3] + 2 * (title['bounds'][3] - title['bounds'][1])
                for title in matches)
            if not is_logo:
                titles.append(node)
        return titles

    @classmethod
    def candidate(cls, screen, name):
        matches = cls._candidate_titles(screen, name)
        if len(matches) != 1:
            reason = 'No visual candidate with the exact name' if not matches else 'Multiple visual candidates with the exact name'
            raise Ambiguous(reason)
        return matches[0]

    def search_and_open(self, name):
        self.search_was_empty = None
        self.search_observation = None
        self.tap_label(DISCOVER, lambda n, s: n['bounds'][1] >= s['height'] * 0.8)
        self.tap_label(MINI_PROGRAMS)
        self.tap_label(SEARCH_LANDING)
        self.tap_label(SEARCH, lambda n, s: self._center(n)[0] < s['width'] * 0.5
                       and n['bounds'][1] < s['height'] * 0.2)
        self.pacer.before(self.account)
        self._foreground()
        self.mobile('type', {'text': name})
        # Submit Search only after exact OCR read-back. WeChat may already show
        # live results while typing; an exact unique visible card is also usable.
        self.pacer.before(self.account)
        screen = self.check_screen()
        typed = self._matching(screen, (normalized(name),),
                               lambda n, s: self._center(n)[1] < s['height'] * 0.14)
        buttons = self._matching(screen, SEARCH,
                                 lambda n, s: self._center(n)[0] > s['width'] * 0.5
                                 and n['bounds'][1] < s['height'] * 0.2)
        query_confirmed = len(typed) == 1 and len(buttons) == 1
        if query_confirmed:
            self._tap_node(buttons[0])
            self.pacer.wait(5)
            # Pace before taking the screenshot used for the next action.
            self.pacer.before(self.account)
            screen = self.check_screen()
            if self._matching(screen, NO_RESULTS):
                # A "no results" section can coexist with other cards. Prefer
                # the structured card evidence when available, without tapping.
                snapshot = self._search_snapshot()
                self._remember_search_results(snapshot)
                self._raise_public_wall(snapshot)
                if self._candidate_titles(screen, name):
                    self.search_was_empty = False
                elif self.search_was_empty is not False:
                    self.search_was_empty = True
                return 'not_found'
        # No fuzzy matching or arbitrary first result: this also guards the
        # direct live-result path when OCR could not read the search input.
        public_result = self._open_public_candidate(screen, query_confirmed=query_confirmed)
        if public_result == 'not_found':
            return 'not_found'
        if public_result is None:
            if len(buttons) != 1 or len(typed) > 1:
                raise Ambiguous('Search text or Search button not verified by OCR')
            if self._candidate_titles(screen, name):
                self.search_was_empty = False
            target = self.candidate(screen, name)
            self._tap_node(target)
        self.search_was_empty = False
        self.pacer.wait(max(5, self.config.get('load_wait', 8)))
        self.check_screen()
        return 'opened'

    def diagnostics(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if self.last_visual is not None:
            (directory / 'ocr.json').write_text(json.dumps(self.last_visual, ensure_ascii=False, indent=2), encoding='utf-8')
        super().diagnostics(directory)
