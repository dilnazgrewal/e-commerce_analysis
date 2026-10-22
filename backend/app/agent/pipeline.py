"""Full decision pipeline: T1 → T2 → T3 (Claude) → T4 → T5 → T6 (rules only).

    contact gates fail ─────────────────────────────▶ T6 (no AI call: holdout, quiet hours, caps …)
    AI unavailable (kill switch, no key, breaker, budget) ▶ rules-only T4/T5/T6
    menu empty ─────────────────────────────────────▶ rules-only (T5 fallback or T6)
    T1 Sonnet agent ─ rejected ─▶ T2 retry with reasons ─ rejected/error ─▶ T3 Haiku ─ fails ─▶ rules-only

Whatever Claude proposes is re-validated by the rules engine before it is accepted, and once more here before
the decision is returned. The `ai` field of every decision records each model call (tier, latency, tokens,
outcome), every rejection reason, and any prompt-injection flags.
"""

from dataclasses import replace
from datetime import datetime

from app.agent.guard import guard_fields
from app.agent.harness import Harness
from app.agent.offer_agent import AgentMenuItem, AgentOutcome, run_agent, run_haiku
from app.rules.decide import Decision, _checks, _decision, build_sent, decide_rules_only, prepare
from app.rules.effects import OfferEffects
from app.rules.engine import CustomerContext, History, contact_checks, menu, validate
from app.rules.message_checks import allowed_numbers
from app.rules.schema import Policy
from app.rules.templates import CATEGORY_LABELS


def _profile(ctx: CustomerContext, now: datetime, customer_text: str | None) -> tuple[dict, str | None, dict]:
    fields, flags = guard_fields({"name": ctx.name, "customer_text": customer_text})
    profile = {
        "first_name": (fields["name"] or "there").split()[0],
        "language": ctx.language,
        "lifecycle": ctx.lifecycle,
        "uplift_group": ctx.uplift_group,
        "churn_risk_60d": round(ctx.churn_60d, 2),
        "chance_to_buy_14d_without_offer": round(ctx.p_buy, 3),
        "usual_order_inr": round(ctx.aov_inr),
        "favourite_category": CATEGORY_LABELS.get(ctx.top_category or "", None),
        "prefers_cash_on_delivery": ctx.prefers_cod,
        "points_expiring_30d": ctx.points_expiring_30d,
        "days_since_last_order": (now - ctx.last_order_at).days if ctx.last_order_at else None,
        "account_age_days": ctx.tenure_days,
    }
    return profile, fields["customer_text"] or None, flags


def _with_ai(decision: Decision, record: dict) -> Decision:
    decision.ai = record
    return decision


def decide(
    ctx: CustomerContext,
    policy: Policy,
    effects: OfferEffects,
    now: datetime,
    history: History,
    harness: Harness,
    activity: dict | None = None,
    customer_text: str | None = None,
    trigger: str = "scheduled",
) -> Decision:
    if not all(c.passed for c in contact_checks(ctx, policy, now, history)):
        return _with_ai(
            decide_rules_only(ctx, policy, effects, now, history, trigger),
            {"used": False, "skipped": "contact rules block any offer right now"},
        )
    unavailable = harness.unavailable_reason()
    if unavailable:
        return _with_ai(
            decide_rules_only(ctx, policy, effects, now, history, trigger),
            {"used": False, "skipped": unavailable},
        )
    options = menu(ctx, policy, effects, now, history)
    if not options:
        return _with_ai(
            decide_rules_only(ctx, policy, effects, now, history, trigger),
            {"used": False, "skipped": "no offer passes the rules, so there is nothing for AI to choose"},
        )

    items = []
    for m in options:
        proposal = prepare(m.template, m.proposal.value, m.proposal.category, now)
        items.append(
            AgentMenuItem(m.template, proposal, m.verdict, sorted(allowed_numbers(ctx, proposal, m.template)))
        )
    by_id = {i.template.id: i for i in items}
    profile, text, flags = _profile(ctx, now, customer_text)

    def check(offer_id: str, value: float, message: str) -> tuple[bool, list[str]]:
        item = by_id[offer_id]
        verdict = validate(
            ctx, replace(item.proposal, value=value), policy, effects, now, history, message=message
        )
        return verdict.passed, verdict.reasons()

    outcome: AgentOutcome = run_agent(harness, profile, items, activity or {}, check, text, flags)
    t1_error = outcome.error
    if not outcome.decided:
        outcome = run_haiku(harness, profile, items, check, outcome)
    record = {
        "used": True,
        "tier": outcome.tier,
        "calls": outcome.calls,
        "tokens": outcome.tokens(),
        "rejections": outcome.rejections,
        "t1_t2_error": t1_error,
        "error": outcome.error if not outcome.decided else None,
        "injection_flags": flags,
        "reason": outcome.decision.reason if outcome.decision else None,
    }

    if outcome.decided and outcome.decision.offer_id == "none":
        gates = contact_checks(ctx, policy, now, history)
        return _decision(
            ctx,
            now,
            trigger,
            outcome.tier,
            "suppressed",
            policy,
            rule_checks=_checks(gates),
            explanation={
                "why": f"Claude chose not to send an offer: {outcome.decision.reason}",
                "segment": ctx.uplift_group,
                "lifecycle": ctx.lifecycle,
            },
            ai=record,
        )
    if outcome.decided:
        item = by_id[outcome.decision.offer_id]
        proposal = replace(item.proposal, value=outcome.decision.value)
        verdict = validate(ctx, proposal, policy, effects, now, history, message=outcome.decision.message)
        if verdict.passed:  # final re-check: nothing leaves without a passing verdict
            econ = verdict.economics
            why = (
                f"Claude chose {item.template.label}: {outcome.decision.reason} "
                f"(+{econ.uplift * 100:.1f} pp chance to buy, "
                f"expected profit ₹{econ.expected_profit_inr:,.0f})."
            )
            decision = build_sent(
                ctx,
                proposal,
                item.template,
                verdict,
                outcome.decision.message,
                now,
                trigger,
                outcome.tier,
                policy,
                why,
            )
            return _with_ai(decision, record)
        record["error"] = "final re-check failed: " + "; ".join(verdict.reasons())

    record["fallback"] = "rules-only"
    return _with_ai(decide_rules_only(ctx, policy, effects, now, history, trigger), record)
