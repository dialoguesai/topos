"""Persist explicit routine query paths without silently broadening legacy routines.

The control plane compiles grant drafts and the node's signed permissions runtime
executes them. Storage validation is defense in depth: unknown paths and incomplete
policies cannot survive a local round trip. No draft is treated as query authority.
"""
from copy import deepcopy

DRAFT_FIELDS = frozenset({"version", "profile", "client_id", "source_ids", "domains", "excluded_domains", "sensitivities",
    "lookback_days", "ceiling", "release_event_time", "result_types", "questions_per_day", "expires_at", "answers", "writer_disclosure"})


def validate_query_permissions(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("targets", []), list):
        raise ValueError("INVALID_QUERY_PERMISSIONS")
    result = deepcopy(payload)
    for target in result.get("targets", []):
        if not isinstance(target, dict):
            raise ValueError("INVALID_QUERY_PERMISSIONS")
        path = target.get("query_path")
        if path is None:
            continue
        if not isinstance(path, str) or path not in {"owner", "grant"}:
            raise ValueError("INVALID_QUERY_PERMISSIONS")
        target["access_mode"], target["filters"] = "raw_access", {}
        if path == "owner":
            target.pop("grant_permissions", None)
            continue
        draft = target.get("grant_permissions")
        if (not isinstance(draft, dict) or set(draft) - DRAFT_FIELDS
            or draft.get("version") != "uma-policy-draft/v1" or draft.get("profile") != "knowledge_search"
            or not draft.get("client_id") or any(not isinstance(draft.get(key), list) or not draft[key]
                for key in ("source_ids", "domains", "sensitivities", "result_types"))
            or "fact" in draft["result_types"] or result.get("save_to_topos")):
            raise ValueError("INVALID_QUERY_PERMISSIONS")
        if "answers" in draft and draft["answers"] not in ("only", "with_sources", "records"):
            raise ValueError("INVALID_QUERY_PERMISSIONS")
        if draft.get("answers") in ("only", "with_sources") and len(str(result.get("prompt", ""))) > 4000:
            raise ValueError("INVALID_QUERY_PERMISSIONS")
    return result
