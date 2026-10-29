"""The exact model-visible content shared by pre-review and transactional guards."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

QUOTE_LIMIT = 600
CONTENT_CHANGED_REASON = "审核期间候选已被修改，需重新审核。"


class CandidateContentChanged(ValueError):
    """A verdict no longer describes the candidate being decided."""

    def __init__(self) -> None:
        super().__init__(CONTENT_CHANGED_REASON)


def review_row(item: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    candidate = item["candidate"]
    row = {
        "candidate_id": candidate["id"],
        "kind": "实体" if item["kind"] == "entity" else "关系",
        "type": candidate["type"],
        "summary": candidate["summary"],
        "evidence_quote": str(candidate["evidence"]["quote"])[:QUOTE_LIMIT],
    }
    if item["kind"] == "entity":
        row["name"] = candidate["name"]
    else:
        row["source"] = names.get(candidate["source_candidate_id"], "")
        row["target"] = names.get(candidate["target_candidate_id"], "")
    return row


def review_fingerprint(item: dict[str, Any], names: dict[str, str]) -> str:
    # The ID routes a verdict; status, metadata, confidence and aliases are not
    # shown to the model. Do not invalidate verdicts for changes to those fields.
    content = review_row(item, names)
    del content["candidate_id"]
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
