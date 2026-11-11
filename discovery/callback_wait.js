'use strict';
// First-party overlay: request construction stays in the external pinned
// adapter. Observe its ONE submitted request; never retry or navigate the UI.
globalThis.__MINIZOO_DISCOVERY = (function () {
    const deadlineMs = 20000;
    const warning = 'too (?:many|frequent) (?:requests|attempts)|rate limit(?:ed| exceeded)?|操作(?:过于|太)?频繁|访问(?:过于|太)?频繁|请求过于频繁|captcha|security verification|安全验证|帐号被封|账号被封';
    function error(reason) {
        send({kind: 'direct-query-error', error: reason});
    }
    function available(view) {
        return view.isShown() && view.hasWindowFocus() && String(view.getUrl()).includes('websearch');
    }
    function focused(views) {
        const matches = views.filter(available);
        if (matches.length !== 1) {
            error('no_unique_focused_websearch');
            return [];
        }
        return matches;
    }
    function start(view, expression, query, offset, initialWait) {
        if (!available(view)) { error('focused_websearch_changed'); return; }
        const url = String(view.getUrl());
        const token = 'minizoo-' + Date.now() + '-' + Math.random().toString(16).slice(2);
        let finished = false;
        let submitted = false;
        const waitMs = Number.isFinite(initialWait) ? Math.min(Math.max(initialWait, 0), deadlineMs) : 6500;
        const callback = Java.registerClass({
            name: 'org.minizoo.DiscoveryObservation' + Date.now(),
            implements: [Java.use('android.webkit.ValueCallback')],
            methods: {onReceiveValue(value) {
                if (finished) return;
                let payload;
                try {
                    payload = JSON.parse(String(value));
                    if (typeof payload === 'string') payload = JSON.parse(payload);
                } catch (_) {}
                if (payload && payload.phase === 'submitted' && !submitted) {
                    submitted = true;
                    setTimeout(poll, waitMs);
                } else if (payload && payload.phase === 'pending' && submitted) {
                    setTimeout(poll, Math.min(500, Math.max(0, deadlineMs - payload.callback_wait_ms)));
                } else {
                    finished = true;
                    if (!payload || (payload.phase !== 'completed' && payload.phase !== 'error')) {
                        error('invalid_search_observation');
                        return;
                    }
                }
                // Pending/submitted envelopes contain no cookies. Completed
                // envelopes expose only the request identity, not the builder.
                send({kind: 'direct-query-result', query: query, offset: offset, value: String(value)});
            }}
        });
        view.evaluateJavascript(`(function () {
            var visible = (document.body.innerText || '').slice(0, 1000);
            if (new RegExp(${JSON.stringify(warning)}, 'i').test(visible)) {
                return JSON.stringify({phase: 'error', error: 'rate_limit_or_verification_required'});
            }
            var previous = window.onSearchDataReady;
            var startedAt = Date.now();
            var submitted = JSON.parse(${expression});
            var request = window.__MC_DIRECT_REQUEST;
            if (!request || !submitted || submitted.phase !== 'submitted'
                    || !submitted.request || request.requestId !== submitted.request.requestId
                    || request.query !== ${JSON.stringify(query)} || request.offset !== ${JSON.stringify(offset)}
                    || typeof request.requestId !== 'string' || !request.requestId) {
                window.onSearchDataReady = previous;
                return JSON.stringify({phase: 'error', error: 'submitted_request_changed'});
            }
            var observation = {token: ${JSON.stringify(token)}, startedAt: startedAt,
                request: request, identity: {query: request.query, offset: request.offset, requestId: request.requestId},
                previous: previous, result: null, url: location.href};
            // Preserve an immediate callback delivered inside the native call.
            var immediate = window.__MC_DIRECT_RESULT;
            if (immediate && immediate.requestId === observation.identity.requestId
                    && Date.now() - startedAt <= ${deadlineMs}) observation.result = immediate;
            observation.handler = function (data) {
                if (data && data.requestId === observation.identity.requestId
                        && Date.now() - startedAt <= ${deadlineMs}) observation.result = data;
                return typeof previous === 'function' ? previous.apply(this, arguments) : undefined;
            };
            window.__MINIZOO_OBSERVATION = observation;
            window.onSearchDataReady = observation.handler;
            return JSON.stringify({phase: 'submitted', request: observation.identity});
        })()`, callback.$new());
        function poll() {
            if (finished) return;
            Java.scheduleOnMainThread(function () {
                if (!available(view) || String(view.getUrl()) !== url) {
                    finished = true;
                    error('focused_websearch_changed');
                    return;
                }
                view.evaluateJavascript(`(function () {
                    var observation = window.__MINIZOO_OBSERVATION;
                    if (!observation || observation.token !== ${JSON.stringify(token)}
                            || window.onSearchDataReady !== observation.handler
                            || window.__MC_DIRECT_REQUEST !== observation.request
                            || observation.request.requestId !== observation.identity.requestId
                            || observation.request.query !== observation.identity.query
                            || observation.request.offset !== observation.identity.offset
                            || location.href !== observation.url) {
                        return JSON.stringify({phase: 'error', error: 'search_observation_changed'});
                    }
                    var elapsed = Date.now() - observation.startedAt;
                    var visible = (document.body.innerText || '').slice(0, 1000);
                    var blocked = new RegExp(${JSON.stringify(warning)}, 'i').test(visible);
                    if (!observation.result && !blocked && elapsed < ${deadlineMs}) {
                        return JSON.stringify({phase: 'pending', callback_wait_ms: elapsed});
                    }
                    window.onSearchDataReady = observation.previous;
                    return JSON.stringify({phase: 'completed', request: observation.identity,
                        result: observation.result, visible_text: visible, webViewUrl: location.href,
                        callback_wait_ms: elapsed, callback_deadline_ms: ${deadlineMs}});
                })()`, callback.$new());
            });
        }
    }
    return {focused: focused, start: start};
})();
