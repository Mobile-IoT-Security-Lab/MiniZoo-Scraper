"""Unverified alternative names help searches but never establish identity."""
import hashlib
import json
from pathlib import Path
import re

from search_observation import search_fields
from store import sha256


def valid_query(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 100
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def load_search_hints(filename):
    if not filename:
        return {}
    path = Path(filename).resolve()
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Alternative-name file too large")
    raw = path.read_bytes()
    document = json.loads(raw)
    if (not isinstance(document, dict)
            or type(document.get("schema_version")) is not int or document["schema_version"] != 1
            or not isinstance(document.get("entries"), dict)):
        raise ValueError("Invalid alternative-name schema")
    provenance = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    checked, hints = {}, {}
    for appid, entry in document["entries"].items():
        if (not re.fullmatch(r"wx[0-9a-f]{16}", appid) or not isinstance(entry, dict)
                or entry.get("identity_verified") is not False or not valid_query(entry.get("query"))):
            raise ValueError("Invalid alternative name or AppID")
        source = entry.get("source")
        if (not isinstance(source, dict) or not isinstance(source.get("path"), str) or not source["path"]
                or any(not isinstance(source.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", source[key])
                       for key in ("sha256", "package_sha256"))):
            raise ValueError("Invalid alternative-name provenance")
        source_path = Path(source["path"])
        if not source_path.is_absolute():
            source_path = path.parent / source_path
        source_path = source_path.resolve()
        if source_path not in checked:
            checked[source_path] = sha256(source_path)
        if checked[source_path] != source["sha256"]:
            raise ValueError("Alternative name source changed: " + str(source_path))
        hints[appid] = {"query": entry["query"].strip(), "query_source": {
            "kind": "package_name_hint", "identity_verified": False,
            "source": dict(source, path=str(source_path)), "hints_file": provenance}}
    return hints


def supported_absence(manifest, directory, attempt):
    """Use an alternative name only after a persisted, verified public absence."""
    reference = attempt.get("search_observation")
    checkpoint = dict(manifest, search_observation=reference, device=attempt.get("device"))
    if search_fields(checkpoint, directory)["search_status"] != "absent":
        return False
    try:
        path = Path(reference["path"])
        raw = (path if path.is_absolute() else Path(directory) / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            return False
        observation = json.loads(raw)
        return (observation.get("page_supported") is True
                and observation.get("reason") == "no_exact_selectable_card"
                and not any(candidate.get("observed_appid") == manifest["appid"]
                            for candidate in observation["candidates"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False
