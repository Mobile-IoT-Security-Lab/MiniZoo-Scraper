"""Add first-party observation guards to the separately supplied pinned adapter.

The external request builder is never copied or changed on disk. Fail closed if
its hash or the two integration points differ from the supported revision.
"""
import hashlib
from pathlib import Path


def compose_bridge(source, expected_hash):
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != expected_hash:
        raise RuntimeError("Modified MiniCrawler bridge; use the pinned adapter")
    selection = "instances.forEach(function (instance) {"
    submission = "instance.evaluateJavascript(expression, Callback.$new());"
    if source.count(selection) != 1 or source.count(submission) != 1:
        raise RuntimeError("Unsupported MiniCrawler bridge integration points")
    source = source.replace(selection, "globalThis.__MINIZOO_DISCOVERY.focused(instances).forEach(function (instance) {")
    start = source.index(submission)
    end = source.index("\n                });", start)
    source = (source[:start]
              + "globalThis.__MINIZOO_DISCOVERY.start(instance, expression, query, offset, waitMs);"
              + source[end:])
    overlay = Path(__file__).with_name("callback_wait.js").read_text(encoding="utf-8")
    return overlay + "\n" + source
