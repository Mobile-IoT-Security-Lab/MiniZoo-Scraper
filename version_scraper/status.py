"""Read-only status that keeps independent pause evidence when progress fails.

An unavailable observation is not evidence that acquisition has stopped. Each
call opens the checkpoint again; it never resumes a pause or repairs the queue.
"""
import json
import sqlite3

from store import now


def scraping_pause(store):
    """Read the persisted stop marker without automatically resuming searches."""
    path = store.output / "checkpoints" / "rate_limit_stop.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Invalid rate limit checkpoint")
    return value if value.get("active") else None


def empty_search_streak(store):
    path = store.output / "checkpoints" / "empty_search_streak.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "schema_version": 1, "limit": 10, "entries": [], "count": 0}
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("entries"), list):
        raise ValueError("Invalid empty-search checkpoint")
    value["count"] = len(value["entries"])
    return value


def read_status(store):
    """Return fresh available data and explicit errors, without invented counts."""
    result = {"generated_at": now()}
    errors = {}
    # A database failure must not hide a saved pause or prevent its inspection.
    for key, reader in (("scraping_pause", scraping_pause),
                        ("empty_search_streak", empty_search_streak)):
        try:
            result[key] = reader(store)
        except (OSError, ValueError) as error:
            errors[key] = {"error_type": type(error).__name__, "error": str(error)}
    result["pause_state"] = ("unavailable" if "scraping_pause" in errors else
                             "paused" if result["scraping_pause"] else "clear")
    try:
        store.open_readonly()
        report = store.report()
        result.update({key: value for key, value in report.items()
                       if key not in ("tasks", "collections", "csv_decisions")})
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        errors["progress"] = {"error_type": type(error).__name__, "error": str(error)}
    finally:
        # open_readonly can fail after creating its connection (e.g. PRAGMA).
        store.close()
    result["progress_available"] = "progress" not in errors
    result["available"] = not errors
    if errors:
        result["errors"] = errors
    return result
