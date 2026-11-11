'use strict';
// Offline native/WebView simulation. An optional composed-script path also
// checks the separately supplied, hash-verified MiniCrawler template itself.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const overlay = fs.readFileSync(path.join(__dirname, '../discovery/callback_wait.js'), 'utf8');
const composed = process.argv[2] ? fs.readFileSync(process.argv[2], 'utf8') : null;

function scenario(events = [], initial = {}) {
    let clock = 0;
    let sequence = 0;
    let focused = initial.focused !== false;
    let nativeUrl = 'https://example.test/websearch';
    const timers = [], messages = [], requests = [];
    const previous = function () {};
    function schedule(callback, delay) { timers.push({at: clock + delay, sequence: sequence++, callback}); }
    class FakeDate extends Date { static now() { return 1800000000000 + clock; } }
    const page = {
        Date: FakeDate, Math, JSON, URLSearchParams,
        crypto: {randomUUID: () => 'request-123'},
        location: {search: '?lang=en', href: nativeUrl},
        document: {body: {innerText: initial.visible || 'Mini Program search'},
            querySelector: () => ({__vue__: {$store: {getters: {getExtReqParams: []}}}})},
        screen: {availWidth: 1080}, matchMedia: () => ({matches: false}),
        onSearchDataReady: previous,
    };
    page.window = page;
    page.webSearchJSApi = {getSearchData(raw) {
        const request = JSON.parse(raw);
        requests.push(request);
        function deliver(event) {
            if (event.visible) page.document.body.innerText = event.visible;
            if (event.focusLost) focused = false;
            if (event.nativeNavigate) nativeUrl += '?other';
            if (event.pageNavigate) page.location.href += '?other';
            if (event.replaceHandler) page.onSearchDataReady = function () {};
            if (event.replaceObservation) page.__MINIZOO_OBSERVATION = {};
            if (event.replaceRequest) page.__MC_DIRECT_REQUEST = {...page.__MC_DIRECT_REQUEST};
            if (event.mutateRequest) page.__MC_DIRECT_REQUEST.requestId = 'changed-id';
            if (event.callback) page.onSearchDataReady({
                requestId: event.wrongId ? 'other-id' : request.requestId,
                ret: 0, json: {ret: 0, data: [{valid: true}]},
            });
        }
        for (const event of events) {
            if (event.immediate) deliver(event);
            else schedule(() => deliver(event), event.after);
        }
        return 'accepted';
    }};
    const browser = vm.createContext(page);
    const view = {
        isShown: () => true, hasWindowFocus: () => focused,
        getUrl: () => nativeUrl,
        evaluateJavascript(expression, callback) {
            callback.onReceiveValue(JSON.stringify(vm.runInContext(expression, browser)));
        },
    };
    const frida = vm.createContext({
        Date: FakeDate, Math, JSON, setTimeout: schedule,
        __MC_OPTIONS: {query: 'test-query', offset: 20, current_page: 2, wait_ms: 6500,
            cookies: 'private-cookie-value'},
        send(envelope) {
            let payload;
            if (envelope.value) {
                payload = JSON.parse(envelope.value);
                if (typeof payload === 'string') payload = JSON.parse(payload);
            }
            messages.push({...envelope, payload});
        },
        Java: {
            perform: fn => fn(), scheduleOnMainThread: fn => fn(),
            use: () => ({}), retain: value => value,
            registerClass: spec => ({$new: () => spec.methods}),
            choose(name, callbacks) {
                callbacks.onMatch(view);
                if (initial.multiple) callbacks.onMatch(view);
                callbacks.onComplete();
            },
        },
        testView: view,
    });
    if (composed) vm.runInContext(composed, frida);
    else {
        // A synthetic adapter contract, not third-party source. It intentionally
        // leaves request construction and the native submission outside overlay.
        const expression = `(function () {
            var request = {query: 'test-query', offset: 20, requestId: 'request-123', cookies: 'private-cookie-value'};
            window.__MC_DIRECT_RESULT = null;
            window.onSearchDataReady = function (result) { window.__MC_DIRECT_RESULT = result; };
            window.__MC_DIRECT_REQUEST = request;
            window.webSearchJSApi.getSearchData(JSON.stringify(request));
            return JSON.stringify({phase: 'submitted', request: request});
        })()`;
        vm.runInContext(overlay, frida);
        frida.testExpression = expression;
        vm.runInContext(`__MINIZOO_DISCOVERY.focused(${initial.multiple ? '[testView,testView]' : '[testView]'}).forEach(function (view) {
            __MINIZOO_DISCOVERY.start(view, testExpression, 'test-query', 20, 6500);
        });`, frida);
    }
    for (let steps = 0; timers.length; steps++) {
        assert.ok(steps < 200, 'bounded polling must terminate');
        timers.sort((a, b) => a.at - b.at || a.sequence - b.sequence);
        const timer = timers.shift();
        clock = timer.at;
        timer.callback();
    }
    const completed = messages.filter(m => m.payload && m.payload.phase === 'completed');
    assert.ok(completed.length <= 1);
    assert.ok(requests.length <= 1, 'must not retry the submitted native request');
    assert.ok(!JSON.stringify(messages).includes('private-cookie-value'), 'observation envelopes omit cookies');
    return {completed, messages, requests, page, previous};
}

let count = 0;
function test(name, body) { body(); count++; process.stdout.write('ok ' + name + '\n'); }
function completedAt(events, expected) {
    const value = scenario(events);
    assert.equal(value.requests.length, 1);
    assert.equal(value.completed[0].payload.callback_wait_ms, expected);
    assert.deepEqual(JSON.parse(JSON.stringify(value.completed[0].payload.request)),
        {query: 'test-query', offset: 20, requestId: 'request-123'});
    assert.equal(value.page.onSearchDataReady, value.previous);
    return value.completed[0].payload;
}
test('normal callback accepted once and original handler restored', () => {
    assert.equal(completedAt([{after: 1000, callback: true}], 6500).result.requestId, 'request-123');
});
test('synchronous native callback is preserved', () => {
    assert.equal(completedAt([{immediate: true, callback: true}], 6500).result.requestId, 'request-123');
});
test('late callback observed without a second submission', () => {
    assert.equal(completedAt([{after: 9000, callback: true}], 9000).result.requestId, 'request-123');
});
test('missing callback stays null at the 20-second deadline', () => {
    assert.equal(completedAt([], 20000).result, null);
});
test('callback beyond the deadline cannot become a success', () => {
    assert.equal(completedAt([{after: 21000, callback: true}], 20000).result, null);
});
test('unrelated callback ignored until deadline', () => {
    assert.equal(completedAt([{after: 1000, callback: true, wrongId: true}], 20000).result, null);
});
test('correct late callback succeeds after unrelated callback', () => {
    assert.equal(completedAt([{after: 1000, callback: true, wrongId: true},
        {after: 11000, callback: true}], 11000).result.requestId, 'request-123');
});
for (const visible of ['Security verification', '操作过于频繁，请稍后再试']) {
    test('visible warning terminates observation: ' + visible, () => {
        const value = completedAt([{after: 8000, visible}], 8000);
        assert.equal(value.result, null);
        assert.equal(value.visible_text, visible);
    });
    test('existing warning prevents submission: ' + visible, () => {
        const value = scenario([], {visible});
        assert.equal(value.requests.length, 0);
        assert.ok(value.messages.some(m => m.payload && m.payload.error === 'rate_limit_or_verification_required'));
    });
}
for (const change of ['replaceHandler', 'replaceObservation', 'replaceRequest', 'mutateRequest', 'pageNavigate']) {
    test('changed request context fails closed: ' + change, () => {
        const value = scenario([{after: 8000, [change]: true}, {after: 9000, callback: true}]);
        assert.equal(value.completed.length, 0);
        assert.ok(value.messages.some(m => m.payload && m.payload.error === 'search_observation_changed'));
    });
}
for (const change of ['focusLost', 'nativeNavigate']) {
    test('native view change fails closed: ' + change, () => {
        const value = scenario([{after: 8000, [change]: true}]);
        assert.equal(value.completed.length, 0);
        assert.ok(value.messages.some(m => m.error === 'focused_websearch_changed'));
    });
}
for (const initial of [{focused: false}, {multiple: true}]) {
    test('ambiguous or unfocused search view prevents submission', () => {
        const value = scenario([], initial);
        assert.equal(value.requests.length, 0);
        assert.ok(value.messages.some(m => m.error === 'no_unique_focused_websearch'));
    });
}
process.stdout.write(count + ' Discovery observation tests passed' + (composed ? ' with pinned adapter' : '') + '\n');
