import copy
import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import types
import unittest
from unittest import mock
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'discovery'))
spec = importlib.util.spec_from_file_location('discovery_runner', ROOT / 'discovery/main.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
from metadata import COLUMNS


def terminal_empty():
    return {'result': {'ret': 0, 'json': {
        'ret': 0, 'data': None, 'continueFlag': 0,
        'offset': 20, 'nomoreText': 'No more results',
    }}}


def nonempty_page():
    return {'result': {'ret': 0, 'json': {
        'ret': 0, 'data': [{'subBoxes': [{'items': [{
            'title': 'Example mini app', 'jumpInfo': {'appID': 'wx0123456789abcdef'},
        }]}]}], 'continueFlag': 1, 'offset': 20,
    }}}


def iter_items(body):
    for box in body['data']:
        for sub in box['subBoxes']:
            yield from sub['items']


CRAWLER = types.SimpleNamespace(
    iter_items=iter_items,
    item_record=lambda item, query: {
        'appid': item['jumpInfo']['appID'], 'name': item['title'], 'source_query': query,
    },
)
BATCH = types.SimpleNamespace(discovered_queries=lambda records: [])


class TerminalResponseTests(unittest.TestCase):
    def test_successful_terminal_null_accepts_each_known_wording(self):
        for location, wording in (
            ('nomoreText', 'No more results'),
            ('nomoreText', '  NO MORE RESULTS  '),
            ('noMoreWording', '暂无相关结果'),
            ('noMoreWording', 'no results found'),
        ):
            with self.subTest(location=location, wording=wording):
                value = terminal_empty()
                body = value['result']['json']
                body.pop('nomoreText')
                if location == 'noMoreWording':
                    body['noMoreData'] = {'noMoreWording': wording}
                else:
                    body[location] = wording
                original = copy.deepcopy(value)
                self.assertEqual(d.inspect_response(value)['data'], [])
                self.assertEqual(value, original)

    def test_null_still_requires_explicit_success_terminal_data_and_wording(self):
        for missing in ('callback_ret', 'body_ret', 'data', 'continueFlag', 'nomoreText'):
            with self.subTest(missing=missing):
                value = terminal_empty()
                if missing == 'callback_ret':
                    del value['result']['ret']
                else:
                    del value['result']['json']['ret' if missing == 'body_ret' else missing]
                with self.assertRaisesRegex(d.Pause, 'unexplained_null_response'):
                    d.inspect_response(value)
        for field, replacement in (('continueFlag', 1), ('nomoreText', 'Please try again later')):
            with self.subTest(field=field):
                value = terminal_empty()
                value['result']['json'][field] = replacement
                with self.assertRaisesRegex(d.Pause, 'unexplained_null_response'):
                    d.inspect_response(value)

    def test_failed_callback_or_response_never_becomes_empty(self):
        for location in ('callback', 'body'):
            value = terminal_empty()
            (value['result'] if location == 'callback' else value['result']['json'])['ret'] = -1
            with self.subTest(location=location), self.assertRaisesRegex(RuntimeError, 'unsuccessful'):
                d.inspect_response(value)

    def test_rate_and_security_override_terminal_wording_in_all_message_fields(self):
        for warning in ('Too many requests', 'Security verification', 'CAPTCHA', '操作过于频繁'):
            for location in ('visible', 'callback', 'body', 'noMoreWording', 'nomoreText'):
                value = terminal_empty()
                body = value['result']['json']
                body['noMoreData'] = {'noMoreWording': '暂无相关结果'}
                if location == 'visible':
                    value['visible_text'] = warning
                elif location == 'callback':
                    value['result']['errMsg'] = warning
                elif location == 'body':
                    body['errMsg'] = warning
                elif location == 'noMoreWording':
                    body['noMoreData']['noMoreWording'] = warning
                else:
                    body['nomoreText'] = warning
                with self.subTest(warning=warning, location=location), self.assertRaises(d.Pause):
                    d.inspect_response(value)

    def test_regular_nonempty_response_is_unchanged(self):
        value = nonempty_page()
        self.assertIs(d.inspect_response(value), value['result']['json'])


class QueryEmptyCounterTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE miniapps (' + ','.join(
            column + (' TEXT PRIMARY KEY' if column == 'appid' else ' TEXT')
            for column in COLUMNS) + ')')
        self.db.executescript('''
            CREATE TABLE searches(query TEXT, offset INTEGER, item_count INTEGER,
                continue_flag INTEGER, searched_at TEXT, PRIMARY KEY(query,offset));
            CREATE TABLE query_queue(query TEXT PRIMARY KEY, status TEXT DEFAULT 'pending',
                discovered_from TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, updated_at TEXT);
            CREATE TABLE discovery_cursor(query TEXT PRIMARY KEY, cursor TEXT NOT NULL);
            CREATE TABLE discovery_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE discovery_observations(id INTEGER PRIMARY KEY, observed_at TEXT,
                query TEXT, page INTEGER, offset INTEGER, response TEXT);
        ''')

    def tearDown(self):
        self.db.close()

    def apply(self, query, response):
        self.db.execute('INSERT OR IGNORE INTO query_queue(query) VALUES (?)', (query,))
        self.db.commit()
        return d.apply_page(self.db, CRAWLER, BATCH, query, d.cursor_for(self.db, query), response, 5)

    def test_empty_first_page_counts_one_query_and_preserves_raw_null(self):
        value = terminal_empty()
        result = self.apply('empty', value)
        self.assertTrue(result['complete'])
        self.assertEqual(result['query_records'], 0)
        self.assertEqual(d.state(self.db, 'empty_queries'), ['empty'])
        self.assertEqual(self.db.execute('SELECT status FROM query_queue').fetchone()[0], 'done')
        saved = json.loads(self.db.execute('SELECT response FROM discovery_observations').fetchone()[0])
        self.assertIsNone(saved['result']['json']['data'])
        self.assertIsNone(value['result']['json']['data'])

    def test_terminal_second_page_after_results_resets_streak_without_empty_query(self):
        for index in range(9):
            self.apply('empty-' + str(index), terminal_empty())
        first = self.apply('nonempty', nonempty_page())
        self.assertFalse(first['complete'])
        cursor = d.cursor_for(self.db, 'nonempty')
        self.assertEqual((cursor['page'], cursor['offset'], cursor['found']), (2, 20, 1))
        terminal = self.apply('nonempty', terminal_empty())
        self.assertTrue(terminal['complete'])
        self.assertEqual(terminal['records'], 0)
        self.assertEqual(terminal['query_records'], 1)
        self.assertEqual(d.state(self.db, 'empty_queries'), [])
        self.assertIsNone(d.state(self.db, 'paused'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM discovery_cursor').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM miniapps').fetchone()[0], 1)

    def test_tenth_genuine_empty_query_still_pauses_and_requeues(self):
        for index in range(9):
            self.apply('empty-' + str(index), terminal_empty())
        with self.assertRaisesRegex(d.Pause, 'ten_consecutive_empty_queries'):
            self.apply('empty-9', terminal_empty())
        self.assertEqual(d.state(self.db, 'paused'), 'ten_consecutive_empty_queries')
        self.assertEqual(len(d.state(self.db, 'empty_queries')), 10)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM query_queue WHERE status='pending'").fetchone()[0], 10)

    def test_unexplained_null_keeps_cursor_without_counting_empty(self):
        self.apply('retained', nonempty_page())
        cursor = d.cursor_for(self.db, 'retained')
        value = terminal_empty()
        del value['result']['json']['nomoreText']
        with self.assertRaisesRegex(d.Pause, 'unexplained_null_response'):
            self.apply('retained', value)
        self.assertEqual(d.cursor_for(self.db, 'retained'), cursor)
        self.assertEqual(d.state(self.db, 'empty_queries', []), [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM discovery_observations').fetchone()[0], 2)

    def test_missing_callback_keeps_existing_cursor_and_raw_observation(self):
        self.apply('retained', nonempty_page())
        cursor = d.cursor_for(self.db, 'retained')
        response = {'result': None, 'visible_text': 'Normal search page', 'callback_wait_ms': 20000}
        with self.assertRaisesRegex(RuntimeError, 'No completed WeChat search result'):
            self.apply('retained', response)
        self.assertEqual(d.cursor_for(self.db, 'retained'), cursor)
        self.assertEqual(d.state(self.db, 'empty_queries', []), [])
        self.assertEqual(self.db.execute('SELECT status FROM query_queue').fetchone()[0], 'pending')
        saved = json.loads(self.db.execute('SELECT response FROM discovery_observations ORDER BY id DESC').fetchone()[0])
        self.assertEqual(saved, response)

    def test_missing_callback_with_security_warning_still_pauses(self):
        with self.assertRaisesRegex(d.Pause, 'rate_limit_or_verification_required'):
            self.apply('blocked', {'result': None, 'visible_text': 'Security verification'})


class PhoneObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / 'MetadataCrawler').mkdir()
        # Synthetic template integration points only, never upstream source.
        self.template = ('instances.forEach(function (instance) {\n'
                         'instance.evaluateJavascript(expression, Callback.$new());\n'
                         'discardOldFixedWait();\n                });')
        (root / 'MetadataCrawler/frida_direct_query.js').write_text(self.template)
        self.args = types.SimpleNamespace(source=root, database=root / 'test.sqlite', serial='test-device', frida='frida')
        self.cursor = {'offset': 20, 'page': 2, 'cookies': 'private-cookie', 'search_id': 'private-search'}
        self.value = {'phase': 'completed', 'request': {'query': 'query', 'offset': 20, 'requestId': 'expected'},
                      'result': {'requestId': 'expected', 'ret': 0, 'json': {'ret': 0, 'data': []}}}
        self.parser = mock.Mock(return_value=[self.value])
        self.crawler = types.SimpleNamespace(parse_frida_messages=self.parser)
        self.pid = mock.patch.object(d.subprocess, 'check_output', return_value='1234').start()
        self.run = mock.patch.object(d.subprocess, 'run', return_value=types.SimpleNamespace(stdout='output', returncode=0)).start()
        mock.patch.dict(d.SOURCE_HASHES, {'frida_direct_query.js': hashlib.sha256(self.template.encode()).hexdigest()}).start()
        d.STOPPING = False

    def tearDown(self):
        mock.patch.stopall()
        self.temporary.cleanup()
        d.STOPPING = False

    def call(self):
        return d.query_phone(self.args, self.crawler, 'query', self.cursor)

    def test_actual_composer_and_runtime_keep_private_options_out_of_arguments(self):
        def capture(command, **kwargs):
            script = Path(command[command.index('-l') + 1])
            content = script.read_text()
            self.assertEqual(script.stat().st_mode & 0o777, 0o600)
            self.assertIn('globalThis.__MINIZOO_DISCOVERY.start(instance', content)
            self.assertIn('globalThis.__MINIZOO_DISCOVERY.focused(instances)', content)
            self.assertNotIn('discardOldFixedWait', content)
            self.assertIn('private-cookie', content)
            self.assertNotIn('private-cookie', str(command))
            self.assertEqual(command[command.index('-t') + 1], '25')
            self.assertEqual(kwargs['timeout'], 35)
            return types.SimpleNamespace(stdout='output', returncode=0)
        self.run.side_effect = capture
        self.assertIs(self.call(), self.value)
        self.assertEqual(list(self.args.database.parent.glob('.minizoo-query-*')), [])

    def test_missing_callback_is_returned_only_as_an_observation(self):
        self.value['result'] = None
        self.assertIsNone(self.call()['result'])
        with self.assertRaisesRegex(RuntimeError, 'No completed WeChat search result'):
            d.inspect_response(self.value)

    def test_query_offset_and_callback_identity_must_match(self):
        for mutate in (lambda v: v['request'].update(query='other'),
                       lambda v: v['request'].update(offset=0),
                       lambda v: v['request'].update(requestId=''),
                       lambda v: v['result'].update(requestId='other'),
                       lambda v: v.update(result=[])):
            changed = copy.deepcopy(self.value)
            mutate(changed)
            self.parser.return_value = [changed]
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                self.call()

    def test_operator_stop_prevents_device_contact(self):
        d.STOPPING = True
        with self.assertRaisesRegex(d.Pause, 'operator_stop_requested'):
            self.call()
        self.pid.assert_not_called()
        self.run.assert_not_called()

    def test_operator_stop_during_wait_is_checked_before_accepting_result(self):
        def stop(*args, **kwargs):
            d.STOPPING = True
            return types.SimpleNamespace(stdout='output', returncode=0)
        self.run.side_effect = stop
        with self.assertRaisesRegex(d.Pause, 'operator_stop_requested'):
            self.call()
        self.parser.assert_not_called()

    def test_security_gate_failure_keeps_its_pause_reason(self):
        self.parser.return_value = [{'phase': 'error', 'error': 'rate_limit_or_verification_required'}]
        with self.assertRaisesRegex(d.Pause, 'rate_limit_or_verification_required'):
            self.call()

    def test_changed_external_bridge_fails_closed_before_frida(self):
        (self.args.source / 'MetadataCrawler/frida_direct_query.js').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'Modified MiniCrawler bridge'):
            self.call()
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
