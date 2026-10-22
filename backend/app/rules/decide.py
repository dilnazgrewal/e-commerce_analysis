"""Rules-only decision pipeline: tiers T4 → T5 → T6. No AI involved, so it works even if Claude is down.

T4  next-best offer: the eligible offer with the highest expected profit, sent with a pre-approved template
T5  the marketer's fallback offer for the customer's lifecycle stage (when nothing clears the profit bar)
T6  no offer: holdout, opted out, quiet hours (queued), contact limits, or nothing allowed — always logged

Step 4 puts Claude (T1–T3) in front of this; whatever Claude proposes goes through the same `validate`.
"""

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from app.rules.effects import OfferEffects
from app.rules.engine import (
    CustomerContext,
    History,
    Proposal,
    Verdict,
    contact_checks,
    fit_value,
    menu,
    validate,
)
from app.rules.schema import PERCENT_TYPES, RUPEE_TYPES, Policy
from app.rules.templates import NO_CODE_TYPES, new_code, render


@dataclass
class Decision:
    decision_id: str
    customer_id: str
    created_at: datetime
    trigger: str
    tier: str
    status: str
    policy_version: int
    offer: dict | None = None
    message: dict | None = None
    economics: dict | None = None
    rule_checks: list[dict] = field(default_factory=list)
    explanation: dict = field(default_factory=dict)
    ai: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        if self.offer and self.offer.get("valid_until"):
            d["offer"]["valid_until"] = self.offer["valid_until"].isoformat()
        return d

    def to_row(self) -> dict:
        offer, message, econ = self.offer or {}, self.message or {}, self.economics or {}
        return {
            "decision_id": self.decision_id,
            "customer_id": self.customer_id,
            "created_at": self.created_at,
            "trigger": self.trigger,
            "tier": self.tier,
            "status": self.status,
            "offer_id": offer.get("offer_id"),
            "offer_type": offer.get("type"),
            "offer_value": offer.get("value"),
            "category": offer.get("category"),
            "code": offer.get("code"),
            "valid_until": offer.get("valid_until"),
            "message_lang": message.get("lang"),
            "message_text": message.get("text"),
            "expected_cost_inr": econ.get("expected_cost_inr"),
            "expected_profit_inr": econ.get("expected_profit_inr"),
            "uplift": econ.get("uplift"),
            "policy_version": self.policy_version,
            "rule_checks": json.dumps(self.rule_checks, ensure_ascii=False),
            "explanation": json.dumps(self.explanation, ensure_ascii=False),
            "ai": json.dumps(self.ai) if self.ai else None,
        }


def _checks(verdict_or_list) -> list[dict]:
    checks = verdict_or_list.checks if isinstance(verdict_or_list, Verdict) else verdict_or_list
    return [{"rule": c.rule, "passed": c.passed, "reason": c.reason} for c in checks]


def _decision(ctx, now, trigger, tier, status, policy, **kw) -> Decision:
    return Decision(
        f"D-{uuid.uuid4().hex[:12]}", ctx.customer_id, now, trigger, tier, status, policy.version, **kw
    )


def build_sent(ctx, proposal, template, verdict, message, now, trigger, tier, policy, why: str) -> Decision:
    econ = verdict.economics
    return _decision(
        ctx,
        now,
        trigger,
        tier,
        "sent",
        policy,
        offer={
            "offer_id": template.id,
            "type": template.type,
            "label": template.label,
            "value": proposal.value,
            "category": proposal.category,
            "min_order_inr": template.min_order_inr,
            "code": proposal.code,
            "valid_until": proposal.valid_until,
        },
        message={"lang": ctx.language, "text": message},
        economics={
            "uplift": round(econ.uplift, 4),
            "p_buy_without": round(ctx.p_buy, 4),
            "p_buy_with": round(econ.p_buy_with_offer, 4),
            "cost_if_used_inr": econ.cost_if_used_inr,
            "expected_cost_inr": econ.expected_cost_inr,
            "value_if_buy_inr": econ.value_if_buy_inr,
            "expected_profit_inr": econ.expected_profit_inr,
            "uplift_basis": econ.basis,
        },
        rule_checks=_checks(verdict),
        explanation={"why": why, "segment": ctx.uplift_group, "lifecycle": ctx.lifecycle},
    )


def prepare(template, value: float, category: str | None, now: datetime) -> Proposal:
    code = None if template.type in NO_CODE_TYPES else new_code(template.id)
    return Proposal(template.id, value, category, now + timedelta(days=template.validity_days), code)


def decide_rules_only(
    ctx: CustomerContext,
    policy: Policy,
    effects: OfferEffects,
    now: datetime,
    history: History,
    trigger: str = "scheduled",
) -> Decision:
    # T6 gates: reasons not to contact this customer at all right now
    gates = contact_checks(ctx, policy, now, history)
    failed = [c for c in gates if not c.passed]
    if failed:
        rules = {c.rule for c in failed}
        status = (
            "holdout" if "not_in_holdout" in rules else "queued" if rules == {"quiet_hours"} else "suppressed"
        )
        why = "; ".join(c.reason for c in failed)
        return _decision(
            ctx,
            now,
            trigger,
            "T6",
            status,
            policy,
            rule_checks=_checks(gates),
            explanation={"why": f"No offer: {why}", "segment": ctx.uplift_group, "lifecycle": ctx.lifecycle},
        )

    # T4: best eligible offer by expected profit
    options = menu(ctx, policy, effects, now, history)
    if options:
        best = options[0]
        proposal = prepare(best.template, best.proposal.value, best.proposal.category, now)
        message = render(ctx, proposal, best.template)
        verdict = validate(ctx, proposal, policy, effects, now, history, message=message)
        if verdict.passed:
            econ = verdict.economics
            why = (
                f"{best.template.label} has the highest expected profit of {len(options)} allowed offers: "
                f"+{econ.uplift * 100:.1f} pp chance to buy, expected cost ₹{econ.expected_cost_inr:,.0f}, "
                f"expected profit ₹{econ.expected_profit_inr:,.0f}."
            )
            return build_sent(ctx, proposal, best.template, verdict, message, now, trigger, "T4", policy, why)

    # T5: marketer's fallback for this lifecycle stage (profit bar not required, every other rule is)
    fallback_id = policy.fallbacks.get(ctx.lifecycle, "none")
    fallback = policy.offer(fallback_id) if fallback_id != "none" else None
    notes = []
    if fallback is not None:
        category = ctx.top_category if fallback.type in PERCENT_TYPES | RUPEE_TYPES else None
        value = fit_value(ctx, fallback, category, policy, now)
        if value is not None:
            proposal = prepare(fallback, value, category, now)
            message = render(ctx, proposal, fallback)
            verdict = validate(
                ctx,
                proposal,
                policy,
                effects,
                now,
                history,
                message=message,
                skip=frozenset({"min_expected_profit"}),
            )
            if verdict.passed:
                why = (
                    f"No offer cleared the profit bar, so the marketing team's fallback for "
                    f"{ctx.lifecycle} customers was used: {fallback.label}."
                )
                return build_sent(ctx, proposal, fallback, verdict, message, now, trigger, "T5", policy, why)
            notes = verdict.reasons()
        else:
            notes = [f"{fallback.id} does not fit the {category} discount cap"]

    why = "No allowed offer is expected to make money"
    why += (
        f", and the fallback failed ({'; '.join(notes)})"
        if notes
        else f", and there is no fallback for {ctx.lifecycle}"
    )
    return _decision(
        ctx,
        now,
        trigger,
        "T6",
        "suppressed",
        policy,
        rule_checks=_checks(gates),
        explanation={"why": why + ".", "segment": ctx.uplift_group, "lifecycle": ctx.lifecycle},
    )
