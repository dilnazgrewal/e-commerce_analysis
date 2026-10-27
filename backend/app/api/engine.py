"""Engine trace: one customer event in, every step of the decision out.

This is the core of OfferIQ made visible: the platform's event, what the models say about the customer, why
a trigger fired, every offer the rules considered (allowed or blocked, and why), the choice, all rule checks,
and the Decision JSON a platform would receive. Dry runs: nothing is sent or logged.
"""

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.agent.pipeline import decide
from app.api.dashboard import Auth, effects, harness, rows
from app.db import get_engine
from app.rules import policy_store
from app.rules.context import load_activity, load_contexts, load_history
from app.rules.decide import decide_rules_only
from app.rules.engine import CustomerContext, Proposal, contact_checks, fit_value, validate
from app.rules.schema import PERCENT_TYPES, RUPEE_TYPES

router = APIRouter(prefix="/api/engine", dependencies=[Auth])
IST = ZoneInfo("Asia/Kolkata")
TRIGGERS = ["auto", "cart_abandoned", "drifting", "points_expiring", "browsing"]


class TraceRequest(BaseModel):
    customer_id: str | None = Field(None, max_length=20)
    trigger: str = "auto"
    use_ai: bool = False
    event: dict | None = None  # a platform event in the input contract; built from recent activity if omitted
    now: datetime | None = None  # naive IST; defaults to now, or 11:00 today during quiet hours


def demo_clock(now: datetime | None) -> datetime:
    if now is not None:
        return now.replace(tzinfo=None)
    real = datetime.now(IST).replace(tzinfo=None, microsecond=0)
    if 9 <= real.hour < 21:
        return real  # the live clock, so the newest bot activity is included
    return real.replace(hour=11, minute=0, second=0)  # at night, 11:00 today (outside quiet hours)


def random_customer() -> str:
    ids = [r["customer_id"] for r in rows("SELECT customer_id FROM customer_scores")]
    return random.choice(ids)


def profile(customer_id: str) -> dict:
    found = rows(
        "SELECT customer_id, name, age_band, city, state, city_tier, signup_date, prime_member, "
        "preferred_payment, points_balance, points_expiry_date, language_pref, device, opted_out "
        "FROM customers WHERE customer_id = :c",
        c=customer_id,
    )
    if not found:
        raise HTTPException(404, f"No customer {customer_id}.")
    p = found[0]
    return {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in p.items()}


def input_event(customer_id: str, now: datetime, given: dict | None) -> dict:
    """The platform's event (input contract). Default: the customer's last add-to-cart (bot or history),
    replayed as if it was left 95 minutes ago, so the cart-abandoned rule can be shown."""
    if given:
        return given
    last = rows(
        "SELECT e.product_id, e.price_inr, e.session_id, e.device FROM events e "
        "WHERE e.customer_id = :c AND e.type = 'add_to_cart' ORDER BY e.ts DESC LIMIT 1",
        c=customer_id,
    )
    item = last[0] if last else {"product_id": None, "price_inr": None, "session_id": None, "device": None}
    return {
        "event_id": f"E-trace-{random.randrange(16**8):08x}",
        "customer_id": customer_id,
        "type": "add_to_cart",
        "ts": (now - timedelta(minutes=95)).isoformat() + "+05:30",
        "product_id": item["product_id"],
        "price_inr": item["price_inr"],
        "session_id": item["session_id"] or "s-trace",
        "device": item["device"] or "android",
    }


def detect_trigger(requested: str, event: dict, ctx: CustomerContext, now: datetime) -> tuple[str, str]:
    """Which moment this is, and why (the trigger detector's rule, in words)."""
    if requested != "auto":
        return requested, f"Chosen by hand for this trace: {requested.replace('_', ' ')}."
    if event.get("type") == "add_to_cart":
        ts = datetime.fromisoformat(str(event["ts"])[:19])
        minutes = int((now - ts).total_seconds() // 60)
        if minutes >= 60:
            return (
                "cart_abandoned",
                f"Item added to cart {minutes} min ago and no order since (rule: ≥ 60 min).",
            )
    if ctx.points_expiring_30d > 0:
        return "points_expiring", f"{ctx.points_expiring_30d:,} reward points expire within 30 days."
    if ctx.churn_60d >= 0.6:
        return "drifting", f"Churn risk {ctx.churn_60d:.0%} (rule: ≥ 60%) — the customer is drifting away."
    return "browsing", "Active session, no special moment: a routine check."


def considered_offers(ctx, policy, now, history) -> list[dict]:
    """Every offer on the menu, with whether the rules allow it for this customer and why not."""
    gate_rules = {c.rule for c in contact_checks(ctx, policy, now, history)}
    out = []
    for t in policy.offers:
        row = {"offer_id": t.id, "type": t.type, "label": t.label}
        category = ctx.top_category if t.type in PERCENT_TYPES | RUPEE_TYPES else None
        if not t.enabled:
            out.append({**row, "allowed": False, "blocked_by": ["switched off by the marketing team"]})
            continue
        value = fit_value(ctx, t, category, policy, now)
        if value is None:
            reason = (
                f"even the minimum discount breaks the {category} cap of "
                f"{policy.category_cap(category, now.date()):g}% of the order"
                if category
                else "no purchase history to size a discount on"
            )
            out.append({**row, "allowed": False, "blocked_by": [reason]})
            continue
        verdict = validate(
            ctx,
            Proposal(t.id, value, category, now + timedelta(days=t.validity_days)),
            policy,
            effects(),
            now,
            history,
        )
        e = verdict.economics
        out.append(
            {
                **row,
                "value": value,
                "category": category,
                "allowed": verdict.passed,
                "blocked_by": [f"{c.rule}: {c.reason}" for c in verdict.failures if c.rule not in gate_rules]
                or ([] if verdict.passed else ["contact limits (step 5)"]),
                "uplift_pp": round(e.uplift * 100, 1) if e else None,
                "p_buy_with_offer": round(e.p_buy_with_offer, 3) if e else None,
                "cost_if_used_inr": round(e.cost_if_used_inr, 1) if e else None,
                "expected_cost_inr": round(e.expected_cost_inr, 1) if e else None,
                "expected_profit_inr": round(e.expected_profit_inr, 1) if e else None,
                "uplift_basis": e.basis if e else None,
            }
        )
    return sorted(out, key=lambda o: (not o["allowed"], -(o.get("expected_profit_inr") or -1e9)))


def contract(d: dict, ctx: CustomerContext, policy_holdout: bool) -> dict:
    """The Decision JSON a platform receives (output contract in CLAUDE.md)."""
    offer, econ, ai = d.get("offer") or {}, d.get("economics") or {}, d.get("ai") or {}
    calls = ai.get("calls") or []
    return {
        "decision_id": d["decision_id"],
        "customer_id": d["customer_id"],
        "trigger": d["trigger"],
        "scores": {
            "churn_60d": round(ctx.churn_60d, 3),
            "uplift": round(econ.get("uplift", ctx.uplift_any), 4),
            "clv_90d_inr": round(ctx.clv_margin_90d_inr),
            "uplift_group": ctx.uplift_group,
            "lifecycle": ctx.lifecycle,
        },
        "offer": {
            k: offer.get(k) for k in ("type", "value", "min_order_inr", "category", "code", "valid_until")
        }
        if offer
        else None,
        "message": d.get("message"),
        "ai": {
            "model": calls[-1]["model"] if calls else None,
            "reason": d["explanation"].get("why"),
            "attempts": len(calls),
            "tier": d["tier"],
        },
        "rule_checks": d["rule_checks"],
        "status": d["status"],
        "channel": "api",
        "holdout": policy_holdout,
    }


@router.post("/trace")
def trace(body: TraceRequest) -> dict:
    engine = get_engine()
    policy = policy_store.active(engine)
    now = demo_clock(body.now)
    customer_id = body.customer_id or random_customer()
    person = profile(customer_id)
    ctx = load_contexts(engine, [customer_id]).get(customer_id)
    if ctx is None:
        raise HTTPException(404, f"{customer_id} has no model scores.")
    event = input_event(customer_id, now, body.event)
    trigger, trigger_why = detect_trigger(body.trigger, event, ctx, now)
    history = load_history(engine, customer_id, now)
    activity = load_activity(engine, customer_id, now)
    gates = contact_checks(ctx, policy, now, history)
    if body.use_ai:
        decision = decide(ctx, policy, effects(), now, history, harness(), activity, None, trigger)
    else:
        decision = decide_rules_only(ctx, policy, effects(), now, history, trigger)
    d = decision.to_dict()
    return {
        "clock": now.isoformat() + "+05:30",
        "policy": {"version": policy.version, "name": policy.name},
        "event": event,
        "customer": person,
        "activity": activity,
        "scores": {
            "churn_60d": ctx.churn_60d,
            "p_buy_14d_without_offer": ctx.p_buy,
            "uplift_any_offer": ctx.uplift_any,
            "uplift_group": ctx.uplift_group,
            "lifecycle": ctx.lifecycle,
            "clv_margin_90d_inr": ctx.clv_margin_90d_inr,
            "aov_inr": ctx.aov_inr,
            "top_category": ctx.top_category,
            "points_expiring_30d": ctx.points_expiring_30d,
            "tenure_days": ctx.tenure_days,
            "prefers_cod": ctx.prefers_cod,
        },
        "trigger": {"name": trigger, "why": trigger_why},
        "history": {"offers_last_30d": len(history.offers), "spent_today_inr": history.spent_today_inr},
        "contact_checks": [{"rule": c.rule, "passed": c.passed, "reason": c.reason} for c in gates],
        "considered": considered_offers(ctx, policy, now, history),
        "decision": d,
        "output": contract(d, ctx, d["status"] == "holdout"),
    }


class BatchRequest(BaseModel):
    n: int = Field(20, ge=1, le=100)


@router.post("/batch")
def batch(body: BatchRequest) -> dict:
    """A burst of platform traffic: n random customers decided by the rules engine (no AI, nothing sent)."""
    engine = get_engine()
    policy = policy_store.active(engine)
    now = demo_clock(None)
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(text("SELECT customer_id FROM customer_scores"))]
    picked = random.sample(ids, min(body.n, len(ids)))
    contexts = load_contexts(engine, picked)
    names = {r["customer_id"]: r for r in rows("SELECT customer_id, name, city, city_tier FROM customers")}
    out = []
    for cid in picked:
        ctx = contexts[cid]
        trigger = "drifting" if ctx.churn_60d >= 0.6 else "cart_abandoned"
        d = decide_rules_only(ctx, policy, effects(), now, load_history(engine, cid, now), trigger).to_dict()
        offer, econ = d.get("offer") or {}, d.get("economics") or {}
        out.append(
            {
                "customer_id": cid,
                "name": names.get(cid, {}).get("name"),
                "city": names.get(cid, {}).get("city"),
                "lifecycle": ctx.lifecycle,
                "uplift_group": ctx.uplift_group,
                "churn_60d": round(ctx.churn_60d, 2),
                "trigger": trigger,
                "tier": d["tier"],
                "status": d["status"],
                "offer": offer.get("label"),
                "value": offer.get("value"),
                "expected_cost_inr": econ.get("expected_cost_inr"),
                "expected_profit_inr": econ.get("expected_profit_inr"),
                "checks_passed": sum(c["passed"] for c in d["rule_checks"]),
                "checks_total": len(d["rule_checks"]),
                "why": d["explanation"].get("why"),
            }
        )
    return {"clock": now.isoformat() + "+05:30", "items": out}
