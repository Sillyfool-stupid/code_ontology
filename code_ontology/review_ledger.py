"""Auditable AI issue lifecycle; never produces human labels."""
from .util import digest, now


def update_ledger(ledger, issues, schema, round_number):
    version = digest(schema.model_dump(), 40)
    for issue in issues:
        row = issue.model_dump()
        key = row.get("issue_id") or "issue_" + digest({k: row[k] for k in ("source_ids", "target_ids", "description")}, 24)
        previous = ledger.get(key, {})
        if previous and (set(previous["source_ids"]) != set(row["source_ids"]) or set(previous["target_ids"]) != set(row["target_ids"])):
            raise ValueError("Review issue ID cannot be reassigned to unrelated items")
        ledger[key] = {**previous, **row, "issue_id": key,
                       "first_seen": previous.get("first_seen", now()), "last_seen": now(),
                       "first_version": previous.get("first_version", version), "last_version": version,
                       "round": round_number, "verification": "ai_not_human_verified"}
    return list(ledger.values())
