"""Read public WeChat search cards; return an exact AppID candidate for Appium.

This standalone sidecar never queries, clicks, enables debugging or proves the
identity/version of captured packages. Appium owns navigation and tap actions.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import uuid


APPID = re.compile(r"wx[0-9a-f]{16}")


def now():
    return datetime.now(timezone.utc).isoformat()


def frida_source(appid, attempt_id):
    expression = "JSON.stringify((" + Path(__file__).with_suffix(".js").read_text() + ")(" + json.dumps(appid) + "))"
    # Known MMWebView API from the existing public DOM adapter. Java.choose is
    # bounded; evaluateJavascript and callback execute on Android's UI thread.
    source = r"""'use strict';
setImmediate(() => Java.perform(() => {
  const views = [], sent = new Set(); let scheduled = false, overflow = false;
  const Rect = Java.use('android.graphics.Rect');
  const System = Java.use('java.lang.System');
  function eligible(view) {
    return view.isShown() && view.hasWindowFocus()
      && /(?:^|\/)websearch(?:\/|[?#]|$)/i.test(String(view.getUrl()).split('?')[0]);
  }
  function geometry(view) {
    const location = Java.array('int', [0, 0]); view.getLocationOnScreen(location);
    const visible = Rect.$new();
    if (!view.getLocalVisibleRect(visible) || view.getRotation() !== 0 || view.getScaleX() !== 1 || view.getScaleY() !== 1)
      throw new Error('unsupported_native_view_geometry');
    return {x: location[0], y: location[1], width: view.getWidth(), height: view.getHeight(),
      visible: {left: visible.left.value, top: visible.top.value, right: visible.right.value, bottom: visible.bottom.value}};
  }
  function emit(payload) {
    payload.kind = 'search-snapshot'; payload.appid = %%APPID%%; payload.attempt_id = %%ATTEMPT%%;
    const encoded = JSON.stringify(payload); if (sent.has(encoded)) return; sent.add(encoded); send(payload);
  }
  Java.choose('com.tencent.mm.ui.widget.MMWebView', {
    onMatch(view) {
      if (views.length >= 32) { overflow = true; return 'stop'; }
      views.push(Java.retain(view));
    },
    onComplete() {
      if (scheduled) return; scheduled = true;
      Java.scheduleOnMainThread(() => {
        try {
          if (overflow) { emit({error: 'webview_scan_bound'}); return; }
          const focused = views.filter(eligible);
          if (focused.length !== 1) { emit({error: 'no_unique_focused_public_search_webview', count: focused.length}); return; }
          const view = focused[0], initial = geometry(view), url = String(view.getUrl());
          const C = Java.registerClass({name: 'org.hermes.SearchMetadata' + Date.now(),
            implements: [Java.use('android.webkit.ValueCallback')], methods: {onReceiveValue(value) {
              try {
                if (!eligible(view) || String(view.getUrl()) !== url || JSON.stringify(geometry(view)) !== JSON.stringify(initial)) {
                  emit({error: 'search_view_changed_during_read'}); return;
                }
                emit({value: String(value), native_view: initial, view_identity: String(System.identityHashCode(view))});
              } catch (_) { emit({error: 'search_view_callback_unavailable'}); }
            }}});
          view.evaluateJavascript(%%EXPRESSION%%, C.$new());
        } catch (_) { emit({error: 'public_search_read_unavailable'}); }
      });
    }
  });
}));
"""
    return source.replace("%%APPID%%", json.dumps(appid)).replace("%%ATTEMPT%%", json.dumps(attempt_id)).replace("%%EXPRESSION%%", json.dumps(expression))


def parse_callback(raw, appid, attempt_id):
    records = {}
    for line in raw.splitlines():
        try:
            if "message: " in line and " data: " in line:
                envelope = ast.literal_eval(line.split("message: ", 1)[1].rsplit(" data: ", 1)[0])
            else:
                envelope = json.loads(line)
            value = envelope.get("payload", envelope)
            if value.get("kind") != "search-snapshot" or value.get("appid") != appid or value.get("attempt_id") != attempt_id:
                continue
            records[json.dumps(value, sort_keys=True)] = value
        except (ValueError, SyntaxError, AttributeError):
            continue
    if len(records) != 1:
        return {"error": "no_unique_public_search_callback"}
    record = next(iter(records.values()))
    if record.get("error"):
        return record
    dom = record.get("value")
    try:
        for _ in range(2):
            if isinstance(dom, str):
                dom = json.loads(dom)
        if not isinstance(dom, dict) or dom.get("target_appid") != appid:
            raise ValueError("wrong_target")
    except (ValueError, TypeError):
        return {"error": "invalid_public_search_callback"}
    return dict(record, dom=dom)


def classify(record, appid):
    """Validate public identity and convert CSS bounds to Android screen pixels."""
    if record.get("error"):
        return {"status": "unavailable", "reason": record["error"], "candidate": None}
    dom = record["dom"]
    reason = dom.get("reason", "")
    cards = dom.get("cards", [])
    if reason == "public_ui_wall":
        return {"status": "blocked", "reason": reason, "candidate": None,
                "wall_kind": dom.get("wall_kind", "security"), "retry_after": dom.get("retry_after", 0)}
    if reason in ("ambiguous_target_cards", "target_identity_conflict"):
        return {"status": "ambiguous", "reason": reason, "candidate": None}
    if dom.get("status") != "ready":
        supported = dom.get("page_supported") is True
        status = "absent" if reason == "no_exact_selectable_card" and supported else "unavailable"
        return {"status": status, "reason": reason or "unsupported_public_search_dom", "candidate": None}
    matches = [card for card in cards if card.get("appid") == appid and card.get("selectable") is True]
    if len(matches) != 1 or dom.get("selected") != matches[0]:
        return {"status": "ambiguous", "reason": "non_unique_exact_card", "candidate": None}
    card = matches[0]
    if (card.get("identity_conflict") or card.get("visible") is not True or card.get("bound_to_open_weapp") is not True
            or type(card.get("app_version")) is not int or card["app_version"] < 0 or card.get("jump_type") != 2):
        return {"status": "unavailable", "reason": "invalid_card_binding", "candidate": None}
    try:
        viewport, view, rect = dom["viewport"], record["native_view"], card["rect_css"]
        if viewport["scale"] != 1 or viewport["offset_left"] != 0 or viewport["offset_top"] != 0:
            raise ValueError("unsupported_viewport")
        values = [viewport[k] for k in ("width", "height")] + [view[k] for k in ("x", "y", "width", "height")] + list(rect.values())
        if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
            raise ValueError("nonfinite_geometry")
        sx, sy = view["width"] / viewport["width"], view["height"] / viewport["height"]
        if sx <= 0 or sy <= 0 or abs(sx - sy) / sx > .03:
            raise ValueError("inconsistent_viewport_scale")
        if not (0 <= rect["left"] < rect["right"] <= viewport["width"] and 0 <= rect["top"] < rect["bottom"] <= viewport["height"]):
            raise ValueError("card_outside_viewport")
        local = {key: rect[key] * (sx if key in ("left", "right") else sy) for key in rect}
        visible = view["visible"]
        if not (visible["left"] <= local["left"] < local["right"] <= visible["right"]
                and visible["top"] <= local["top"] < local["bottom"] <= visible["bottom"]):
            raise ValueError("card_clipped_by_native_view")
        bounds = {key: round(value + view["x" if key in ("left", "right") else "y"]) for key, value in local.items()}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {"status": "unavailable", "reason": "unverified_screen_geometry", "candidate": None}
    candidate = {"observed_appid": appid, "title": card.get("title", ""), "developer": card.get("developer", ""),
                 "published_app_version": card["app_version"], "physical_bounds": bounds,
                 "bounds": [bounds[key] for key in ("left", "top", "right", "bottom")],
                 "tap": {"x": round((bounds["left"] + bounds["right"]) / 2), "y": round((bounds["top"] + bounds["bottom"]) / 2)},
                 "component_path": card.get("component_path"), "selector": card.get("selector")}
    return {"status": "matched", "reason": "exact_public_card_identity", "candidate": candidate}


def public_candidates(record):
    """Keep unknown/conflicting IDs as diagnostics, never as tap candidates."""
    candidates = []
    for card in record.get("dom", {}).get("cards", [])[:64]:
        diagnostic = {"observed_appid": card.get("appid"), "title": card.get("title", ""),
                      "developer": card.get("developer", ""), "published_app_version": card.get("app_version"),
                      "bounds": None, "app_id_aliases": card.get("app_id_aliases", {}),
                      "identity_conflict": bool(card.get("identity_conflict")), "visible": card.get("visible") is True}
        # Reuse the same geometry validation for each otherwise selectable card.
        if card.get("appid") and card.get("selectable") is True:
            dom = dict(record["dom"], status="ready", reason="", cards=[card], selected=card)
            selected = classify(dict(record, dom=dom), card["appid"])
            if selected["status"] == "matched":
                diagnostic["bounds"] = selected["candidate"]["bounds"]
        candidates.append(diagnostic)
    return candidates


def collect(serial, appid, attempt_id, output, frida, adb="adb", pid=None):
    if not APPID.fullmatch(appid) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", attempt_id):
        raise ValueError("Invalid AppID or attempt ID")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = "search-metadata-" + uuid.uuid4().hex[:12]
    script, log = output.parent / (stem + ".js"), output.parent / (stem + ".log")
    script.write_text(frida_source(appid, attempt_id))
    started = now()
    try:
        if pid is None:
            found = subprocess.check_output([adb, "-s", serial, "shell", "pidof", "com.tencent.mm"], text=True, timeout=10).strip()
            if not found.isdigit():
                raise ValueError("No unique WeChat main PID")
            pid = int(found)
        run = subprocess.run([frida, "-D", serial, "-p", str(pid), "-q", "--no-auto-reload", "-t", "6", "-l", str(script)],
                             capture_output=True, text=True, timeout=22)
        raw = run.stdout + run.stderr
        log.write_text(raw)
        record = parse_callback(raw, appid, attempt_id) if run.returncode == 0 else {"error": "search_metadata_process_failed"}
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        record = {"error": "search_metadata_transport_unavailable"}
        log.write_text(type(error).__name__ + "\n")
    view = record.get("native_view", {})
    webview_bounds = ([view["x"], view["y"], view["x"] + view["width"], view["y"] + view["height"]]
                      if all(type(view.get(k)) in (int, float) for k in ("x", "y", "width", "height")) else None)
    result = {"kind": "verified_snapshot", "schema_version": 1, "serial": serial, "expected_appid": appid,
              "attempt_id": attempt_id, "observed_at": started, "read_started": started,
              "read_finished": now(), "max_age_seconds": 15,
              "source": "visible_public_search_card", "package_identity_verified": False,
              "page_supported": record.get("dom", {}).get("page_supported") is True,
              "candidates": public_candidates(record), "webview_bounds": webview_bounds,
              "metadata_sources": [{"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                   for path in (script, log)]}
    result.update(classify(record, appid))
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("serial", "appid", "attempt-id", "output", "frida"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    result = collect(args.serial, args.appid, args.attempt_id, args.output, args.frida, args.adb, args.pid)
    print(json.dumps({"status": result["status"], "reason": result["reason"], "candidate": result["candidate"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
