"""Versioned policy storage: the marketing team publishes versions from the dashboard; the bot picks them up.

Versions are append-only (nothing is overwritten), so every decision's `policy_version` can be traced back to
the exact rules it was checked against. With no published version yet, the default YAML file is live.
"""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, inspect, text

from app.rules.schema import Policy, load_policy

_CACHE: dict[str, tuple[float, Policy]] = {}
CACHE_SECONDS = 15


def active(engine: Engine, fallback: Policy | None = None) -> Policy:
    """The newest published policy (cached for a few seconds), or the default file if none is published."""
    key = str(engine.url)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    policy = fallback or load_policy()
    if inspect(engine).has_table("policy_versions"):
        with engine.connect() as conn:
            row = conn.execute(text("SELECT body FROM policy_versions ORDER BY version DESC LIMIT 1")).first()
        if row:
            policy = Policy.model_validate(json.loads(row[0]))
    _CACHE[key] = (time.monotonic(), policy)
    return policy


def history(engine: Engine, limit: int = 20) -> list[dict]:
    if not inspect(engine).has_table("policy_versions"):
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT version, author, note, created_at FROM policy_versions ORDER BY version DESC LIMIT :n"
            ),
            {"n": limit},
        ).mappings()
        return [dict(r) for r in rows]


def publish(engine: Engine, proposed: dict, author: str, note: str) -> Policy:
    """Validate and store a new version (version number and stamp are set here, not by the caller)."""
    current = active(engine)
    now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None, microsecond=0)
    policy = Policy.model_validate(
        {**proposed, "version": current.version + 1, "updated_by": author, "updated_at": now.date()}
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy_versions (version, body, author, note, created_at) "
                "VALUES (:v, :b, :a, :n, :t)"
            ),
            {"v": policy.version, "b": policy.model_dump_json(), "a": author, "n": note, "t": now},
        )
    _CACHE.pop(str(engine.url), None)
    return policy


def diff(old: dict, new: dict, prefix: str = "") -> list[dict]:
    """Plain list of changed values (dotted paths), for showing a marketer exactly what an edit does."""
    changes = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in old:
                changes.append({"path": path, "before": None, "after": new[key]})
            elif key not in new:
                changes.append({"path": path, "before": old[key], "after": None})
            else:
                changes += diff(old[key], new[key], path)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new, strict=True)):
            label = a.get("id", i) if isinstance(a, dict) else i
            changes += diff(a, b, f"{prefix}[{label}]")
    elif old != new:
        changes.append({"path": prefix, "before": old, "after": new})
    return changes
