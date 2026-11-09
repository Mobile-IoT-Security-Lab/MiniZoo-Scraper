"""Read search observations without certifying packages or runtime versions."""
import hashlib
import json
from pathlib import Path


def search_fields(manifest, collection_dir):
    result = {"search_status": "unavailable", "search_observed_appid": "", "search_title": "",
              "search_developer": "", "search_published_version": "", "search_candidate_count": 0,
              "search_error": "", "search_reason": ""}
    reference = manifest.get("search_observation")
    if reference is None:
        return result
    try:
        root = Path(collection_dir).absolute()
        path = Path(reference["path"])
        if not path.is_absolute():
            path = root / path
        if root.resolve() != root or path.resolve() != path or root not in path.parents:
            raise ValueError("Observation outside the collection or a symbolic link")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError("Observation hash differs from the checkpoint")
        observation = json.loads(data)
        if type(observation.get("schema_version")) is not int or observation["schema_version"] != 1:
            raise ValueError("Unsupported observation schema")
        for observed, expected in (("expected_appid", "appid"), ("attempt_id", "attempt_id"), ("serial", "device")):
            if not isinstance(manifest.get(expected), str) or not manifest[expected] or observation.get(observed) != manifest[expected]:
                raise ValueError("Observation belongs to another AppID, attempt or device")
        status, candidates = observation.get("status"), observation.get("candidates")
        if not isinstance(status, str) or not status or not isinstance(candidates, list):
            raise ValueError("Invalid observation status or candidates")
        if any(not isinstance(candidate, dict) for candidate in candidates):
            raise ValueError("Invalid candidate list")
        selected = {}
        if status == "matched":
            candidate = observation.get("candidate")
            if not isinstance(candidate, dict) or candidate.get("observed_appid") != manifest["appid"]:
                raise ValueError("Selected candidate does not have the expected AppID")
            for field, column in (("observed_appid", "search_observed_appid"), ("title", "search_title"),
                                  ("developer", "search_developer"), ("published_app_version", "search_published_version")):
                value = candidate.get(field)
                if value is None:
                    value = ""
                if not isinstance(value, str) and not (field == "published_app_version" and type(value) is int):
                    raise ValueError("Invalid candidate field: " + field)
                selected[column] = str(value)
        result.update(search_status=status, search_candidate_count=len(candidates),
                      search_reason=str(observation.get("reason", ""))[:256], **selected)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result.update(search_status="rejected", search_error=str(error))
    return result
