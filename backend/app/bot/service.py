"""Decide an offer for a Telegram user: their twin's context, the AI pipeline, then L5 and the log."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from app.agent.harness import Harness
from app.agent.pipeline import decide
from app.bot import store
from app.models import BotUser
from app.rules.context import load_activity, load_contexts, load_history
from app.rules.decide import Decision, _decision
from app.rules.effects import OfferEffects
from app.rules.schema import Policy

IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def next_morning(now: datetime, policy: Policy) -> datetime:
    """When quiet hours end (e.g. 09:00 IST), today or tomorrow."""
    end = policy.contact.quiet_hours.end
    candidate = now.replace(hour=end.hour, minute=end.minute, second=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


@dataclass
class Services:
    engine: Engine
    policy: Policy
    effects: OfferEffects
    harness: Harness


def offer_for(
    svc: Services,
    user: BotUser,
    trigger: str,
    now: datetime,
    customer_text: str | None = None,
    idempotency_key: str | None = None,
) -> Decision:
    ctx = load_contexts(svc.engine, [user.twin_customer_id])[user.twin_customer_id]
    previous = store.already_handled(svc.engine, idempotency_key)
    if previous:  # the same trigger fired twice: don't decide (or pay for Claude) again
        decision = _decision(
            ctx,
            now,
            trigger,
            "T6",
            "duplicate",
            svc.policy,
            explanation={"why": f"Already handled as {previous} (idempotency key)."},
        )
        return decision
    ctx = replace(ctx, name=user.first_name, language=user.language, opted_out=user.opted_out)
    if user.holdout:
        decision = _decision(
            ctx,
            now,
            trigger,
            "T6",
            "holdout",
            svc.policy,
            explanation={
                "why": "This chat is in the live control group (10% of people get no offers, so we can "
                "measure whether offers really change behaviour).",
                "segment": ctx.uplift_group,
                "lifecycle": ctx.lifecycle,
            },
        )
        decision.ai = {"used": False, "skipped": "live holdout group"}
    else:
        history = load_history(svc.engine, ctx.customer_id, now)
        activity = load_activity(svc.engine, ctx.customer_id, now)
        decision = decide(
            ctx, svc.policy, svc.effects, now, history, svc.harness, activity, customer_text, trigger
        )
    return store.persist(svc.engine, decision, svc.policy, "telegram", idempotency_key)
