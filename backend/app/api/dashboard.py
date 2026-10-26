"""Dashboard API for the marketing team: overview, decision log, Policy Lab, Rules Studio, evidence.

Everything except /api/login needs the dashboard token (DASHBOARD_PASSWORD). With no password configured
(local development) the dashboard is open. Writing rules always goes validate → dry run → a person publishes.
"""

import contextlib
import hashlib
import hmac
import json
import os
import threading
import time
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import inspect, text

from app.agent import rule_author
from app.agent.harness import Harness
from app.config import REPO_ROOT, get_settings
from app.data.calibrate import load_india
from app.db import get_engine
from app.experiments import policy_lab
from app.rules import policy_store
from app.rules.effects import OfferEffects
from app.rules.schema import Policy

router = APIRouter(prefix="/api")
CALIBRATION = REPO_ROOT / "calibration"


# --------------------------------------------------------------------------- login


def dashboard_token() -> str | None:
    """A stateless token derived from the password: changing the password signs everyone out."""
    password = get_settings().dashboard_password
    if password is None or not password.get_secret_value():
        return None
    return hmac.new(password.get_secret_value().encode(), b"offeriq-dashboard-v1", hashlib.sha256).hexdigest()


def require_login(authorization: Annotated[str, Header()] = "") -> None:
    expected = dashboard_token()
    if expected is None:
        return  # local development: no password configured
    if not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
        raise HTTPException(401, "Please sign in to the dashboard.")


Auth = Depends(require_login)


class Login(BaseModel):
    password: str = Field(max_length=200)


@router.post("/login")
def login(body: Login) -> dict:
    expected = dashboard_token()
    if expected is None:
        return {"token": "", "open": True}
    password = get_settings().dashboard_password.get_secret_value()
    if not hmac.compare_digest(body.password.encode(), password.encode()):
        time.sleep(1)  # slows down password guessing
        raise HTTPException(401, "Wrong password.")
    return {"token": expected, "open": False}


@router.get("/session", dependencies=[Auth])
def session() -> dict:
    return {"ok": True, "login_required": dashboard_token() is not None}


# --------------------------------------------------------------------------- shared helpers


@lru_cache
def effects() -> OfferEffects:
    return OfferEffects.from_report()


@lru_cache
def margins() -> dict[str, float]:
    return load_india()["category_margin_pct"]


_harness: Harness | None = None


def harness() -> Harness:
    global _harness
    if _harness is None:
        _harness = Harness()
    return _harness


def rows(sql: str, **params) -> list[dict]:
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def has_table(name: str) -> bool:
    return inspect(get_engine()).has_table(name)


def failed_checks(rule_checks: str | None) -> list[dict]:
    try:
        return [c for c in json.loads(rule_checks or "[]") if not c.get("passed", True)]
    except json.JSONDecodeError:
        return []


# --------------------------------------------------------------------------- overview


@router.get("/overview", dependencies=[Auth])
def overview() -> dict:
    by_status = rows("SELECT status, COUNT(*) AS n FROM offer_decisions GROUP BY status")
    by_tier = rows(
        "SELECT tier, COUNT(*) AS n FROM offer_decisions "
        "WHERE status IN ('sent', 'claimed', 'dismissed') GROUP BY tier"
    )
    by_channel = rows(
        "SELECT COALESCE(channel, 'cli') AS channel, COUNT(*) AS n FROM offer_decisions GROUP BY 1"
    )
    by_type = rows(
        "SELECT offer_type, COUNT(*) AS n, SUM(expected_cost_inr) AS cost FROM offer_decisions "
        "WHERE status IN ('sent', 'claimed', 'dismissed') AND offer_type IS NOT NULL GROUP BY offer_type"
    )
    sent = rows(
        "SELECT rule_checks, expected_cost_inr, expected_profit_inr FROM offer_decisions "
        "WHERE status IN ('sent', 'claimed', 'dismissed')"
    )
    daily = rows(
        "SELECT substr(CAST(created_at AS TEXT), 1, 10) AS day, status, COUNT(*) AS n FROM offer_decisions "
        "GROUP BY 1, 2 ORDER BY 1"
    )
    bot = {"users": 0, "holdout": 0, "opted_out": 0, "feedback": {}}
    if has_table("bot_users"):
        b = rows(
            "SELECT COUNT(*) AS users, SUM(CASE WHEN holdout THEN 1 ELSE 0 END) AS holdout, "
            "SUM(CASE WHEN opted_out THEN 1 ELSE 0 END) AS opted_out FROM bot_users"
        )[0]
        bot = {k: int(v or 0) for k, v in b.items()}
        bot["feedback"] = {
            r["action"]: r["n"] for r in rows("SELECT action, COUNT(*) AS n FROM feedback GROUP BY action")
        }
    days = sorted({d["day"] for d in daily})[-21:]
    series = [{"day": day, **{d["status"]: d["n"] for d in daily if d["day"] == day}} for day in days]
    policy = policy_store.active(get_engine())
    return {
        "decisions": sum(r["n"] for r in by_status),
        "by_status": {r["status"]: r["n"] for r in by_status},
        "by_tier": {r["tier"]: r["n"] for r in by_tier},
        "by_channel": {r["channel"]: r["n"] for r in by_channel},
        "by_offer_type": [
            {"type": r["offer_type"], "n": r["n"], "cost_inr": round(r["cost"] or 0)} for r in by_type
        ],
        "offers_sent": len(sent),
        "rule_violations_in_sent": sum(1 for r in sent if failed_checks(r["rule_checks"])),
        "expected_cost_inr": round(sum(r["expected_cost_inr"] or 0 for r in sent)),
        "expected_profit_inr": round(sum(r["expected_profit_inr"] or 0 for r in sent)),
        "daily": series,
        "bot": bot,
        "policy": {"version": policy.version, "name": policy.name},
        "ai_enabled": os.environ.get("OFFERIQ_AI_ENABLED", "1") not in ("0", "false", "off"),
    }


# --------------------------------------------------------------------------- decision log


@router.get("/decisions", dependencies=[Auth])
def decisions(
    status: str | None = None,
    tier: str | None = None,
    channel: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where, params = [], {"limit": min(max(limit, 1), 200), "offset": max(offset, 0)}
    for column, value in (("status", status), ("tier", tier), ("channel", channel)):
        if value:
            where.append(f"{column} = :{column}")
            params[column] = value
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = rows(f"SELECT COUNT(*) AS n FROM offer_decisions {clause}", **params)[0]["n"]
    items = rows(
        "SELECT decision_id, customer_id, created_at, trigger, tier, status, offer_type, offer_value, "
        "category, message_text, message_lang, expected_cost_inr, expected_profit_inr, uplift, "
        f"policy_version, channel, rule_checks FROM offer_decisions {clause} "
        "ORDER BY created_at DESC LIMIT :limit OFFSET :offset",
        **params,
    )
    for item in items:
        checks = json.loads(item.pop("rule_checks") or "[]")
        item["checks_total"] = len(checks)
        item["checks_failed"] = sum(1 for c in checks if not c.get("passed", True))
        item["created_at"] = str(item["created_at"])
    return {"total": total, "items": items}


@router.get("/decisions/{decision_id}", dependencies=[Auth])
def decision(decision_id: str) -> dict:
    found = rows("SELECT * FROM offer_decisions WHERE decision_id = :d", d=decision_id)
    if not found:
        raise HTTPException(404, "No such decision.")
    d = found[0]
    for key in ("rule_checks", "explanation", "ai"):
        with contextlib.suppress(json.JSONDecodeError):
            d[key] = json.loads(d[key]) if d[key] else None
    for key in ("created_at", "valid_until"):
        d[key] = str(d[key]) if d[key] else None
    d["feedback"] = (
        [
            {"action": f["action"], "detail": f["detail"], "created_at": str(f["created_at"])}
            for f in rows(
                "SELECT action, detail, created_at FROM feedback WHERE decision_id = :d", d=decision_id
            )
        ]
        if has_table("feedback")
        else []
    )
    return d


# --------------------------------------------------------------------------- Policy Lab

_LAB_LOCK = threading.Lock()  # one simulation at a time (the free cloud host has a fraction of a CPU)


def lab_sample() -> int | None:
    value = os.environ.get("OFFERIQ_LAB_SAMPLE")
    return int(value) if value else None


class LabRequest(BaseModel):
    discount_strength: float = Field(1.0, ge=0.25, le=3.0)
    reward_strength: float = Field(1.0, ge=0.25, le=3.0)
    days: int = Field(14, ge=7, le=30)


@router.post("/lab/compare", dependencies=[Auth])
def lab_compare(body: LabRequest) -> dict:
    with _LAB_LOCK:
        started = time.perf_counter()
        result = policy_lab.compare(
            get_engine(),
            policy_store.active(get_engine()),
            effects(),
            list(policy_lab.STRATEGIES),
            days=body.days,
            discount_strength=body.discount_strength,
            reward_strength=body.reward_strength,
            sample=lab_sample(),
        )
    result["seconds"] = round(time.perf_counter() - started, 1)
    return result


# --------------------------------------------------------------------------- Rules Studio


class PolicyBody(BaseModel):
    policy: dict


class PublishBody(PolicyBody):
    note: str = Field("", max_length=300)
    author: str = Field("marketing team", max_length=60)


class InstructionBody(BaseModel):
    instruction: str = Field(min_length=3, max_length=1000)


def margin_warnings(policy: Policy) -> list[str]:
    out = []
    for category, rule in policy.categories.items():
        margin = margins().get(category)
        if margin is not None and rule.max_discount_pct >= margin:
            out.append(
                f"{category}: a {rule.max_discount_pct:g}% cap is at or above the {margin:g}% gross margin, "
                "so every discounted order there loses money."
            )
    for festive in policy.festive:
        for category, cap in festive.category_caps.items():
            margin = margins().get(category)
            if margin is not None and cap >= margin:
                out.append(f"{festive.name}: {category} cap {cap:g}% is at or above its {margin:g}% margin.")
    return out


def check_policy(proposed: dict) -> tuple[Policy | None, dict]:
    current = policy_store.active(get_engine())
    base = json.loads(current.model_dump_json())
    candidate = {**proposed, **{k: base[k] for k in ("version", "updated_by", "updated_at")}}
    try:
        policy = Policy.model_validate(candidate)
    except ValidationError as e:
        errors = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:20]]
        return None, {"ok": False, "errors": errors, "warnings": [], "diff": []}
    after = json.loads(policy.model_dump_json())
    return policy, {
        "ok": True,
        "errors": [],
        "warnings": margin_warnings(policy),
        "diff": policy_store.diff(base, after),
    }


@router.get("/policy", dependencies=[Auth])
def get_policy() -> dict:
    policy = policy_store.active(get_engine())
    return {
        "policy": json.loads(policy.model_dump_json()),
        "history": [{**h, "created_at": str(h["created_at"])} for h in policy_store.history(get_engine())],
        "margins": margins(),
        "ai_available": harness().unavailable_reason() is None,
    }


@router.post("/policy/check", dependencies=[Auth])
def policy_check(body: PolicyBody) -> dict:
    return check_policy(body.policy)[1]


@router.post("/policy/dry-run", dependencies=[Auth])
def policy_dry_run(body: PolicyBody) -> dict:
    """What would this edit do? OfferIQ under the live vs the proposed policy, on the simulated customers."""
    proposed, report = check_policy(body.policy)
    if proposed is None:
        raise HTTPException(422, {"errors": report["errors"]})
    engine = get_engine()
    current = policy_store.active(engine)
    with _LAB_LOCK:
        before = policy_lab.compare(engine, current, effects(), ["offeriq"], sample=lab_sample())
        after = policy_lab.compare(engine, proposed, effects(), ["offeriq"], sample=lab_sample())
    pick = lambda r: next(x for x in r["results"] if x["strategy"] == "offeriq")  # noqa: E731
    return {
        **report,
        "customers": before["customers"],
        "days": before["days"],
        "current": {**pick(before), "mix": before["offeriq_offer_mix"]},
        "proposed": {**pick(after), "mix": after["offeriq_offer_mix"]},
        "fairness_gap_pp": {
            k: {
                "current": before["fairness"][k]["max_gap_pp"],
                "proposed": after["fairness"][k]["max_gap_pp"],
            }
            for k in before["fairness"]
        },
    }


@router.post("/policy/publish", dependencies=[Auth])
def policy_publish(body: PublishBody) -> dict:
    proposed, report = check_policy(body.policy)
    if proposed is None:
        raise HTTPException(422, {"errors": report["errors"]})
    if not report["diff"]:
        raise HTTPException(400, "Nothing changed.")
    published = policy_store.publish(
        get_engine(),
        json.loads(proposed.model_dump_json()),
        body.author,
        body.note or "Edited in Rules Studio",
    )
    return {"version": published.version, "changes": len(report["diff"])}


@router.post("/policy/ai-edit", dependencies=[Auth])
def policy_ai_edit(body: InstructionBody) -> dict:
    """Claude Opus turns plain English into a proposed policy. It is never published from here."""
    current = policy_store.active(get_engine())
    proposal = rule_author.propose(harness(), current, body.instruction, margins())
    if not proposal.ok:
        return {
            "ok": False,
            "errors": proposal.errors,
            "summary": proposal.summary,
            "attempts": proposal.attempts,
        }
    _, report = check_policy(proposal.policy)
    return {
        "ok": True,
        "summary": proposal.summary,
        "policy": proposal.policy,
        "warnings": proposal.warnings + report["warnings"],
        "diff": report["diff"],
        "attempts": proposal.attempts,
        "tokens": proposal.tokens,
    }


# --------------------------------------------------------------------------- evidence


def read_report(name: str) -> dict:
    path = CALIBRATION / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@router.get("/evidence", dependencies=[Auth])
def evidence() -> dict:
    model = read_report("model_report.json")
    realism = read_report("realism_report.json")
    live = {"treated_users": 0, "holdout_users": 0, "offers": 0, "claimed": 0, "dismissed": 0, "why": 0}
    if has_table("bot_users"):
        users = rows(
            "SELECT SUM(CASE WHEN holdout THEN 0 ELSE 1 END) AS t, "
            "SUM(CASE WHEN holdout THEN 1 ELSE 0 END) AS h FROM bot_users WHERE twin_customer_id IS NOT NULL"
        )[0]
        live["treated_users"], live["holdout_users"] = int(users["t"] or 0), int(users["h"] or 0)
        live["offers"] = rows(
            "SELECT COUNT(*) AS n FROM offer_decisions WHERE channel = 'telegram' "
            "AND status IN ('sent', 'claimed', 'dismissed')"
        )[0]["n"]
        for action, n in (
            (r["action"], r["n"]) for r in rows("SELECT action, COUNT(*) AS n FROM feedback GROUP BY action")
        ):
            if action in live:
                live[action] = n
    return {
        "models": {
            "as_of": model.get("as_of"),
            "churn": {
                k: model.get("churn", {}).get(k)
                for k in ("auc", "brier", "brier_baseline", "base_rate", "calibration")
            },
            "uplift_arms": model.get("uplift", {}).get("arms", {}),
            "hillstrom": model.get("hillstrom_validation", {}),
            "segments": model.get("segments", {}),
            "ground_truth": model.get("ground_truth_check", {}),
        },
        "realism": {
            "passed": realism.get("passed"),
            "checks": realism.get("checks", []),
            "summary": realism.get("summary", {}),
            "charts": realism.get("charts", {}),
        },
        "live": live,
    }
