"""Preserve AppID-bound search metadata across sparse native responses.

The caller saves the complete response before parsing. The current catalogue
is a projection: absent fields never erase an earlier observation, while the
raw_json column remains the actual latest item, without invented additions.
"""
from datetime import datetime, timezone
import json
import re

APPID = re.compile(r"wx[0-9a-f]{16}\Z")
COLUMNS = ("appid", "doc_id", "name", "category", "rating", "url", "description",
           "icon_url", "username", "developer", "app_version", "source_query",
           "verified_at", "raw_json")
RETAIN_IF_ABSENT = COLUMNS[1:11]


def explicitly_empty(body):
    """Recognize empty native result containers, not malformed result items."""
    return all(isinstance(box, dict) and isinstance(box.get("subBoxes"), list)
               and all(isinstance(sub, dict) and sub.get("items") == []
                       for sub in box["subBoxes"]) for box in body["data"])


def item_record(item, query, parser):
    """Reject conflicting identities; classification must come from the item."""
    if not isinstance(item, dict):
        return None
    jump = item.get("jumpInfo")
    if jump is not None and not isinstance(jump, dict):
        return None
    jump = jump or {}
    identity = jump.get("appID") or item.get("appID")
    if not isinstance(identity, str) or not APPID.fullmatch(identity):
        return None
    if item.get("appID") and item["appID"] != identity:
        return None
    record = parser(item, query)
    if record.get("appid") != identity:
        return None
    for key, source in (("name", "title"), ("description", "desc")):
        if not isinstance(item.get(source), str):
            record[key] = ""
    icon = item.get("imgUrl") or item.get("iconUrl")
    if not isinstance(icon, str):
        record["icon_url"] = ""
    developer = item.get("source") or item.get("sourceName")
    if isinstance(developer, dict):
        developer = developer.get("title") or developer.get("name")
    if not isinstance(developer, str):
        record["developer"] = ""
    # The upstream parser stringifies objects. Only literal category labels or
    # the explicit native Mini Game flag provide a supported classification.
    record["category"] = next((v.strip() for key in ("category", "categoryName", "typeName")
                               if isinstance((v := item.get(key)), str) and v.strip()), "")
    if not record["category"] and type(item.get("isMinigame")) is int and item["isMinigame"] == 1:
        record["category"] = "Mini game"
    record["raw_json"] = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    return record


def save_records(db, records, query, offset, continuation):
    """Upsert without losing prior values; preserve even a nameless AppID."""
    assignments = [f"{key}=COALESCE(NULLIF(TRIM(excluded.{key}), ''),miniapps.{key})"
                   for key in RETAIN_IF_ABSENT]
    assignments += [f"{key}=excluded.{key}" for key in ("source_query", "verified_at", "raw_json")]
    sql = ("INSERT INTO miniapps (" + ",".join(COLUMNS) + ") VALUES ("
           + ",".join(":" + key for key in COLUMNS) + ") ON CONFLICT(appid) DO UPDATE SET "
           + ",".join(assignments))
    for record in records:
        if not APPID.fullmatch(str(record.get("appid", ""))):
            raise ValueError("Discovery metadata requires an exact AppID")
        values = {key: record.get(key) or "" for key in COLUMNS}
        db.execute(sql, values)
    db.execute("INSERT OR REPLACE INTO searches "
               "(query,offset,item_count,continue_flag,searched_at) VALUES (?,?,?,?,?)",
               (query, offset, len(records), continuation, datetime.now(timezone.utc).isoformat()))
    db.commit()
