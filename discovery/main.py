#!/usr/bin/env python3
"""Discover AppIDs using an explicit external MiniCrawler checkout and one phone.

Only `run` contacts a device. The source mount is read-only; all queue, cursor,
metadata and exports live beside --database. A paused run needs explicit resume.
"""
import argparse
import csv
import fcntl
import hashlib
import importlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from metadata import explicitly_empty, item_record, save_records
from bridge import compose_bridge

SOURCE_COMMIT = "5cd8dff0ea25f8bd52f4898954b8bd8d76ef6958"
SOURCE_HASHES = {
    "pixel_crawler.py": "6b452905ddd1b264eba7dfa383f0509e40668e63abffd5e4a502e6c37e4e6145",
    "pixel_batch_crawler.py": "512096e1f7b355348d8336b97a816566a64cba8880f37869699c11ad0fa06ac3",
    "frida_direct_query.js": "e65472730ca2cea2215574a495d6764121d35ee2691bec2d68e54a6c919aaa11",
}
APPID = re.compile(r"wx[0-9a-f]{16}\Z")
RATE = re.compile(r"too (?:many|frequent) (?:requests|attempts)|rate limit(?:ed| exceeded)?|操作(?:过于|太)?频繁|访问(?:过于|太)?频繁|请求过于频繁", re.I)
WALL = re.compile(r"captcha|security verification|安全验证|帐号被封|账号被封", re.I)
EMPTY_WORDING = {"暂无相关结果", "no results", "no results found", "no relevant results"}
STOPPING = False


class Pause(RuntimeError):
    pass


def load_source(source):
    directory = source.resolve() / "MetadataCrawler"
    for name, expected in SOURCE_HASHES.items():
        path = directory / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(
                f"Missing or modified MiniCrawler source: {name}. Run "
                "python3 tools/fetch_discovery_source.py, or provide the pinned adapter with --source."
            )
    sys.path.insert(0, str(directory))
    return importlib.import_module("pixel_crawler"), importlib.import_module("pixel_batch_crawler")


def state(db, key, default=None):
    row = db.execute("SELECT value FROM discovery_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put_state(db, key, value):
    db.execute("INSERT OR REPLACE INTO discovery_state VALUES (?,?)", (key, json.dumps(value)))


def initialize(path, source, queries):
    crawler, batch = load_source(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = crawler.connect_db(path)
    db.execute("PRAGMA synchronous=FULL")
    batch.ensure_queue(db)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS discovery_cursor(query TEXT PRIMARY KEY, cursor TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS discovery_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS discovery_observations(id INTEGER PRIMARY KEY,
        observed_at TEXT NOT NULL, query TEXT NOT NULL, page INTEGER NOT NULL,
        offset INTEGER NOT NULL, response TEXT NOT NULL);
    """)
    batch.enqueue(db, queries, "seed")
    db.close()


def cursor_for(db, query):
    row = db.execute("SELECT cursor FROM discovery_cursor WHERE query=?", (query,)).fetchone()
    return json.loads(row[0]) if row else {
        "offset": 0, "page": 1, "cookies": "", "search_id": "", "found": 0, "seen_offsets": [],
    }


def inspect_response(response):
    result = response.get("result")
    visible = str(response.get("visible_text") or "")
    if RATE.search(visible) or WALL.search(visible):
        raise Pause("rate_limit_or_verification_required")
    if not isinstance(result, dict):
        raise RuntimeError("No completed WeChat search result")
    raw = result.get("json")
    body = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(body, dict):
        raise RuntimeError("Invalid WeChat result format")
    no_more = body.get("noMoreData")
    wording = no_more.get("noMoreWording", "") if isinstance(no_more, dict) else ""
    terminal_wording = body.get("nomoreText", "")
    messages = "\n".join(str(v) for v in
                         (result.get("errMsg", ""), body.get("errMsg", ""), wording, terminal_wording))
    if RATE.search(messages) or WALL.search(messages):
        raise Pause("rate_limit_or_verification_required")
    if result.get("ret") not in (None, 0) or body.get("ret") not in (None, 0):
        raise RuntimeError("WeChat returned an unsuccessful search response")
    if body.get("data") is None:
        if (result.get("ret") == 0 and body.get("ret") == 0 and "data" in body
                and body.get("continueFlag") == 0
                and (str(wording).strip().lower() in EMPTY_WORDING
                     or str(terminal_wording).strip().lower() == "no more results")):
            return dict(body, data=[])
        raise Pause("unexplained_null_response")
    if not isinstance(body["data"], list):
        raise RuntimeError("Invalid WeChat result data")
    return body


def apply_page(db, crawler, batch, query, cursor, response, max_pages):
    db.execute("INSERT INTO discovery_observations(observed_at,query,page,offset,response) VALUES (?,?,?,?,?)",
               (datetime.now(timezone.utc).isoformat(), query, cursor["page"], cursor["offset"],
                json.dumps(response, ensure_ascii=False)))
    db.commit()
    body = inspect_response(response)
    records = [item_record(item, query, crawler.item_record) for item in crawler.iter_items(body)]
    records = [row for row in records if row is not None]
    if not records and not explicitly_empty(body):
        raise RuntimeError("Nonempty search data contains no valid Mini Program records; inspect the saved response")
    continuation = int(body.get("continueFlag") or 0)
    offset = int(body.get("offset") or 0)
    seen = cursor["seen_offsets"] + [cursor["offset"]]
    if continuation and (not records or offset <= cursor["offset"] or offset in seen):
        raise RuntimeError("Search continuation did not advance; current page cursor retained")
    save_records(db, records, query, cursor["offset"], continuation)
    # Enqueue and cursor completion share a transaction, after the idempotent AppID upsert.
    db.executemany("INSERT OR IGNORE INTO query_queue(query,discovered_from) VALUES (?,?)",
                   ((q, query) for q in batch.discovered_queries(records) if 1 <= len(q) <= 32))
    found = cursor["found"] + len(records)
    complete = not continuation
    capped = not complete and cursor["page"] >= max_pages
    if complete:
        empty = list(dict.fromkeys(state(db, "empty_queries", []) + [query])) if found == 0 else []
        put_state(db, "empty_queries", empty)
        db.execute("UPDATE query_queue SET status='done',attempts=attempts+1,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE query=?", (query,))
        db.execute("DELETE FROM discovery_cursor WHERE query=?", (query,))
        if len(empty) >= 10:
            db.executemany("UPDATE query_queue SET status='pending',last_error='ten empty queries' WHERE query=?", ((q,) for q in empty))
            put_state(db, "paused", "ten_consecutive_empty_queries")
            db.commit()
            raise Pause("ten_consecutive_empty_queries")
    else:
        cookies = body.get("cookies") or ""
        next_cursor = dict(offset=offset, page=cursor["page"] + 1,
                           cookies=cookies if isinstance(cookies, str) else json.dumps(cookies),
                           search_id=str(body.get("searchID") or body.get("searchId") or cursor["search_id"]),
                           found=found, seen_offsets=seen)
        db.execute("INSERT OR REPLACE INTO discovery_cursor VALUES (?,?)", (query, json.dumps(next_cursor)))
        if capped:
            db.execute("UPDATE query_queue SET status='capped',attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE query=?", (query,))
            put_state(db, "empty_queries", [])
    db.commit()
    return {"query": query, "records": len(records), "query_records": found,
            "complete": complete or capped, "capped": capped}


def query_phone(args, crawler, query, cursor):
    if STOPPING:
        raise Pause("operator_stop_requested")
    pid = subprocess.check_output(["adb", "-s", args.serial, "shell", "pidof", "com.tencent.mm"], text=True, timeout=10).strip()
    if not pid.isdigit():
        raise RuntimeError("No unique WeChat main process; open WeChat's Mini Program search page")
    options = dict(query=query, offset=cursor["offset"], cookies=cursor["cookies"],
                   search_id=cursor["search_id"], current_page=cursor["page"], wait_ms=6500)
    # Cursor cookies belong in private storage, never in process arguments or
    # exception messages. A single combined script preserves the bridge's
    # strict mode and options-before-query execution order.
    try:
        bridge = compose_bridge(
            (args.source / "MetadataCrawler/frida_direct_query.js").read_text(encoding="utf-8"),
            SOURCE_HASHES["frida_direct_query.js"],
        )
        with tempfile.TemporaryDirectory(prefix=".minizoo-query-", dir=args.database.parent) as temporary:
            script = Path(temporary) / "query.js"
            fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("'use strict';\nglobalThis.__MC_OPTIONS = " + json.dumps(options) + ";\n")
                stream.write(bridge)
            if STOPPING:
                raise Pause("operator_stop_requested")
            result = subprocess.run([
                args.frida, "-D", args.serial, "-p", pid, "-q", "--no-auto-reload", "-t", "25",
                "-l", str(script),
            ], capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Discovery bridge timed out; page cursor retained") from None
    except subprocess.SubprocessError:
        raise RuntimeError("Discovery bridge process failed; page cursor retained") from None
    except OSError:
        raise RuntimeError("Discovery bridge could not start; page cursor retained") from None
    if STOPPING:
        raise Pause("operator_stop_requested")
    messages = list(crawler.parse_frida_messages(result.stdout))
    if any(value.get("phase") == "error" and value.get("error") == "rate_limit_or_verification_required"
           for value in messages):
        raise Pause("rate_limit_or_verification_required")
    completed = [value for value in messages if value.get("phase") == "completed"]
    if result.returncode or len(completed) != 1:
        # Never print raw Frida output: it can contain request cookies.
        raise RuntimeError(f"Discovery bridge unavailable: exit={result.returncode}, completed_callbacks={len(completed)}")
    value = completed[0]
    request = value.get("request")
    callback = value.get("result")
    if (not isinstance(request, dict)
            or request.get("query") != query or request.get("offset") != cursor["offset"]
            or not isinstance(request.get("requestId"), str) or not request["requestId"]
            or (callback is not None and (not isinstance(callback, dict)
                                         or callback.get("requestId") != request["requestId"]))):
        raise RuntimeError("Search callback does not match the requested query, page and request ID")
    # A missing callback is an observation, never an empty successful page.
    # apply_page persists it before inspect_response pauses with the cursor intact.
    return value


def export(db, directory):
    # Operational input, not a public package release CSV. No invented category/version.
    columns = ("appid", "name", "category", "rating", "url", "description", "icon_url",
               "developer", "app_version", "source_query", "verified_at")
    path = directory / "catalog.csv"
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(db.execute("SELECT " + ",".join(columns) + " FROM miniapps ORDER BY appid"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def status(db):
    return {"role": "discovery", "apps": db.execute("SELECT COUNT(*) FROM miniapps").fetchone()[0],
            "queue": dict(db.execute("SELECT status,COUNT(*) FROM query_queue GROUP BY status")),
            "paused": state(db, "paused"), "empty_queries": state(db, "empty_queries", []),
            "serial": state(db, "serial"), "automatic_restart": False}


def request_stop(signum, frame):
    global STOPPING
    STOPPING = True


def delay(seconds):
    end = time.monotonic() + seconds
    while not STOPPING and time.monotonic() < end:
        time.sleep(min(1, max(0, end - time.monotonic())))


def run(db, args):
    global STOPPING
    STOPPING = False
    db.execute("PRAGMA synchronous=FULL")
    if state(db, "paused"):
        raise Pause(f"Discovery is paused: {state(db, 'paused')}. Use resume after checking the phone.")
    if state(db, "serial") not in (None, args.serial):
        raise RuntimeError("This checkpoint belongs to another device; use its assigned serial")
    crawler, batch = load_source(args.source)
    put_state(db, "serial", args.serial)
    db.execute("UPDATE query_queue SET status='pending' WHERE status='running'")
    db.commit()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    completed = 0
    try:
        while not STOPPING:
            row = db.execute("SELECT query FROM query_queue WHERE status IN ('pending','running') ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,attempts,rowid LIMIT 1").fetchone()
            if not row:
                break
            query = row[0]
            db.execute("UPDATE query_queue SET status='running' WHERE query=?", (query,))
            db.commit()
            outcome = apply_page(db, crawler, batch, query, cursor_for(db, query),
                                 query_phone(args, crawler, query, cursor_for(db, query)), args.max_pages)
            print(json.dumps(outcome, ensure_ascii=False), flush=True)
            if outcome["complete"]:
                export(db, args.database.parent)
                completed += 1
                if args.max_queries and completed >= args.max_queries:
                    break
            delay(args.query_delay if outcome["complete"] else args.page_delay)
    except Exception as exc:
        put_state(db, "paused", str(exc))
        raise
    finally:
        db.execute("UPDATE query_queue SET status='pending' WHERE status='running'")
        db.commit()
        export(db, args.database.parent)


@contextmanager
def exclusive(database):
    with database.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another discovery process is using this database") from exc
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(os.environ.get("MINICRAWLER_SOURCE", str(Path(__file__).resolve().parents[1] / "private" / "dependencies" / "MiniCrawler"))))
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("MINIZOO_DATA_DIR", str(Path(__file__).resolve().parents[1] / "collection_results"))) / "discovery" / "data.db")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Seed a persistent queue without contacting a phone")
    init.add_argument("--queries", type=Path, required=True, help="UTF-8 file, one keyword per line")
    commands.add_parser("status", help="Read queue counts and pause state")
    resume = commands.add_parser("resume", help="Clear a pause without starting acquisition")
    resume.add_argument("--include-capped", action="store_true", help="Also requeue page-capped queries; increase run --max-pages to continue")
    runner = commands.add_parser("run", help="Contact one explicit phone and process the queue")
    runner.add_argument("--serial", required=True)
    runner.add_argument("--frida", default="frida")
    runner.add_argument("--max-queries", type=int, default=1, help="Completed queries; 0 means unlimited")
    runner.add_argument("--max-pages", type=int, default=5, help="Page cap per query; capped queries keep their next-page cursor")
    runner.add_argument("--query-delay", type=float, default=30)
    runner.add_argument("--page-delay", type=float, default=5)
    args = parser.parse_args(argv)
    args.database = args.database.resolve()
    if args.command == "run" and (args.max_queries < 0 or args.max_pages < 1 or args.query_delay < 0 or args.page_delay < 0):
        parser.error("Query limit/delays must be nonnegative and max-pages must be positive")
    try:
        if args.command == "init":
            queries = [line.strip() for line in args.queries.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            if not queries or any(len(query) > 32 for query in queries):
                raise RuntimeError("Seed file needs nonempty queries of at most 32 characters")
            args.database.parent.mkdir(parents=True, exist_ok=True)
            with exclusive(args.database):
                initialize(args.database, args.source, queries)
        if not args.database.is_file():
            raise RuntimeError("Discovery database not found; run init first")
        if args.command in ("init", "status"):
            with sqlite3.connect(args.database.as_uri() + "?mode=ro", uri=True) as db:
                print(json.dumps(status(db), ensure_ascii=False))
        else:
            with exclusive(args.database), sqlite3.connect(args.database) as db:
                if args.command == "resume":
                    put_state(db, "paused", None)
                    put_state(db, "empty_queries", [])
                    if args.include_capped:
                        db.execute("UPDATE query_queue SET status='pending' WHERE status='capped'")
                    db.commit()
                else:
                    run(db, args)
                print(json.dumps(status(db), ensure_ascii=False))
        return 0
    except Pause as exc:
        print(f"PAUSED: {exc}", file=sys.stderr)
        return 75
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
