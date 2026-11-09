"""Decode public WeChat attributes only after a runtime AppID has been observed.

Raw JSON and its field/DB provenance stay in the acquisition observations.
Missing, malformed and conflicting values are reported, never guessed.
"""
import json
import re

SCHEMA = "minizoo_public_metadata_v2"
FIELDS = ("name", "description", "category", "developer", "icon_url")
CLASS = "com.tencent.mm.plugin.appbrand.config.WxaAttributes"
DB_PATH = re.compile(r"/data/(?:data|user/\d+)/com\.tencent\.mm/MicroMsg/[0-9a-f]{32}/AppBrandComm\.db\Z")
TABLES = ("WxaAttributesTable", "WxaAttrAvailableBackupTable")
COLUMNS = {"name": "nickname", "username": "username", "description": "signature",
           "icon_url": "brandIconURL", "raw_dynamic_info": "dynamicInfo",
           "raw_register_source": "registerSource"}
RAW_LIMIT = 65536


def text(value):
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        return None
    return value.strip() or None


def json_object(value):
    if not isinstance(value, str) or len(value) > RAW_LIMIT or "\x00" in value:
        raise ValueError("Invalid public attribute JSON")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("Public attribute JSON must be an object")
    return result


def categories(raw):
    values = json_object(raw).get("NewCategories")
    if values is None:
        return None
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("Invalid service category list")
    names = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Invalid service category entry")
        first, second = text(item.get("first")), text(item.get("second"))
        if not first or not second:
            raise ValueError("Service category requires named first and second levels")
        parts = [first, second]
        third = text(item.get("third"))
        if third:
            parts.append(third)
        value = " / ".join(parts)
        if value not in names:
            names.append(value)
    result = "; ".join(names) or None
    if result and len(result) > 4096:
        raise ValueError("Service category text too large")
    return result


def select_records(messages, appid, attempt_id, process_id):
    """Require exact runtime identity and a completed bounded DB query."""
    def bound(record):
        if record.get("appid") != appid or record.get("attempt_id") != attempt_id:
            return False
        if record.get("metadata_source") == "wechat_main_cache":
            return (record.get("runtime_process_id") == process_id
                    and record.get("metadata_process_name") == "com.tencent.mm"
                    and type(record.get("process_id")) is int and record["process_id"] > 0)
        return record.get("process_id") == process_id
    def nested_identity(record):
        container = record.get("row", {}) if record.get("kind") == "runtime-cached-public-metadata" else record.get("fields", {})
        if not isinstance(container, dict):
            return False
        raw = container.get("appInfo" if record.get("kind") == "runtime-cached-public-metadata" else "raw_app_info")
        if raw in (None, ""):
            return True
        try:
            return json_object(raw).get("Appid") == appid
        except (ValueError, TypeError, RecursionError):
            return False
    result = []
    for record in messages:
        if not bound(record) or not nested_identity(record):
            continue
        identity = record.get("identity_field", {})
        if not isinstance(identity, dict):
            identity = {}
        if (record.get("kind") == "runtime-public-metadata"
                and record.get("metadata_source") != "wechat_main_cache"
                and record.get("metadata_class") == CLASS
                and identity.get("field") == "field_appId"
                and identity.get("value") == appid):
            result.append(record)
        elif (record.get("kind") == "runtime-cached-public-metadata"
              and record.get("metadata_schema") == SCHEMA
              and DB_PATH.fullmatch(str(record.get("database_path", "")))
              and record.get("table") in TABLES
              and isinstance(record.get("row"), dict) and record["row"].get("appId") == appid):
            query = (record["process_id"], record["database_path"], record["table"])
            rows = [r for r in messages if bound(r) and r.get("kind") == record["kind"]
                    and (r.get("process_id"), r.get("database_path"), r.get("table")) == query]
            done = [r for r in messages if bound(r) and r.get("kind") == "metadata-public-table-complete"
                    and (r.get("process_id"), r.get("database_path"), r.get("table")) == query]
            if (len(done) == 1 and done[0].get("count") == len(rows) <= 2
                    and done[0].get("truncated") is False):
                result.append(record)
    return result


def decode_records(records, runtime_values):
    """Merge observed sources conservatively; differing nonempty values conflict."""
    values = {key: set(items) for key, items in runtime_values.items()}
    invalid = set()
    for record in records:
        database = record.get("kind") == "runtime-cached-public-metadata"
        fields, proofs = record.get("fields", {}), record.get("provenance", {})
        if not isinstance(fields, dict) or not isinstance(proofs, dict):
            invalid.update(FIELDS)
            continue
        if database:
            reverse = {column: {"raw_dynamic_info": "category", "raw_register_source": "developer"}.get(key, key)
                       for key, column in COLUMNS.items()}
            invalid.update(reverse[column] for column in record.get("invalid_fields", []) if column in reverse)
        for key, column in COLUMNS.items():
            if database:
                raw = record["row"].get(column)
            else:
                raw = fields.get(key)
                proof = proofs.get(key, {})
                if not isinstance(proof, dict):
                    proof = {}
                if raw is not None and (proof.get("field") != "field_" + column or proof.get("value") != raw):
                    invalid.add({"raw_dynamic_info": "category", "raw_register_source": "developer"}.get(key, key))
                    continue
            if raw in (None, ""):
                continue
            target = {"raw_dynamic_info": "category", "raw_register_source": "developer"}.get(key, key)
            try:
                if key == "raw_dynamic_info":
                    value = categories(raw)
                elif key == "raw_register_source":
                    body = json_object(raw).get("RegisterBody")
                    value = text(body)
                    if body is not None and not isinstance(body, str):
                        raise ValueError("Invalid registration body")
                else:
                    value = text(raw)
                    if not isinstance(raw, str) or len(raw) > 4096 or "\x00" in raw:
                        raise ValueError("Invalid public text")
                if target == "icon_url" and value and not re.match(r"https?://[^/\s]+", value):
                    raise ValueError("Invalid public icon URL")
                if value:
                    values.setdefault(target, set()).add(value)
            except (ValueError, TypeError, RecursionError):
                invalid.add(target)
    conflicts = {key for key, items in values.items() if len(items) > 1}
    result = {key: next(iter(items)) for key, items in values.items() if len(items) == 1}
    return result, status(result, conflicts, invalid, len(records))


def status(values, conflicts=(), invalid=(), source_count=0):
    missing = [key for key in FIELDS if not values.get(key)]
    return {"schema": SCHEMA, "status": "incomplete" if missing else "complete",
            "missing_fields": missing, "conflicting_fields": sorted(set(conflicts) & set(FIELDS)),
            "invalid_fields": sorted(set(invalid) & set(FIELDS)), "source_count": source_count}


def combine(first, last):
    """Use both identity-verified observations; keep disagreements explicit."""
    conflicts, invalid, values = set(), set(), {}
    for binding in (first, last):
        report = binding.get("public_metadata_status", {})
        conflicts.update(report.get("conflicting_fields", []))
        invalid.update(report.get("invalid_fields", []))
        for key, value in binding["metadata"].items():
            values.setdefault(key, set()).add(value)
    conflicts.update(key for key, items in values.items() if len(items) > 1)
    result = {key: next(iter(items)) for key, items in values.items() if len(items) == 1 and key not in conflicts}
    return result, status(result, conflicts, invalid,
        sum(b.get("public_metadata_status", {}).get("source_count", 0) for b in (first, last)))
