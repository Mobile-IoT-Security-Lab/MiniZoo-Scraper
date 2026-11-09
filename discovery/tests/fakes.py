"""Small synthetic adapter: unit tests never need upstream code or a phone."""
import ast
import json
import re
import sqlite3

from metadata import COLUMNS


class Crawler:
    @staticmethod
    def connect_db(path):
        db = sqlite3.connect(path)
        fields = ["appid TEXT PRIMARY KEY", *[name + " TEXT" for name in COLUMNS if name != "appid"]]
        db.execute("CREATE TABLE IF NOT EXISTS miniapps (" + ",".join(fields) + ")")
        db.execute("CREATE TABLE IF NOT EXISTS searches(query TEXT, offset INTEGER, item_count INTEGER, continue_flag INTEGER, searched_at TEXT, PRIMARY KEY(query,offset))")
        return db

    @staticmethod
    def iter_items(body):
        for box in body.get('data', []):
            for sub in box.get('subBoxes', []):
                yield from sub.get('items', [])

    @staticmethod
    def item_record(item, query):
        source = item.get('source') or item.get('sourceName') or ''
        if isinstance(source, dict):
            source = source.get('title') or source.get('name') or ''
        return dict(appid=item.get('jumpInfo', {}).get('appID') or item.get('appID'),
                    name=item.get('title', ''), description=item.get('desc', ''),
                    icon_url=item.get('imgUrl') or item.get('iconUrl') or '', developer=source,
                    app_version=str(item.get('jumpInfo', {}).get('appVersion') or ''), source_query=query)

    @staticmethod
    def parse_frida_messages(output):
        for line in output.splitlines():
            value = ast.literal_eval(line.removeprefix('message: ').removesuffix(' data: None'))
            yield json.loads(value['payload']['value'])


class Batch:
    @staticmethod
    def ensure_queue(db):
        db.execute("CREATE TABLE IF NOT EXISTS query_queue(query TEXT PRIMARY KEY, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, discovered_from TEXT, last_error TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")

    @staticmethod
    def enqueue(db, queries, source):
        db.executemany('INSERT OR IGNORE INTO query_queue(query,discovered_from) VALUES (?,?)', ((q, source) for q in queries))
        db.commit()

    @staticmethod
    def discovered_queries(records):
        result = set()
        for record in records:
            name = record.get('name') or ''
            if name:
                result.add(name)
            for text in re.findall(r'[\u4e00-\u9fff]+', name):
                for size in range(2, 5):
                    result.update(text[i:i+size] for i in range(len(text)-size+1))
        return sorted(result)
