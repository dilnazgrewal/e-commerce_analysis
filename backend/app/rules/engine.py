"""The rules engine: which offers a customer may get (the menu) and whether a proposed offer is allowed.

Everything here is deterministic and free of I/O, so it can be property-tested. `validate` never stops at the
first failure: it returns every check with a plain-English reason, which is what the decision log, the
"Why me?" button and Claude's retry loop all show.
"""

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.rules.check import Check
from app.rules.effects import Economics, OfferEffects, discount_pct_of_order, economics
from app.rules.message_checks import check_message
from app.rules.schema import PERCENT_TYPES, RUPEE_TYPES, OfferTemplate, Policy


@dataclass
class CustomerContext:
    """What the engine knows about one customer (built from customer_scores + customers)."""

    customer_id: str
    name: str = "there"
    language: str = "en"
    lifecycle: str = "active"
    uplift_group: str = "lost_cause"
    churn_60d: float = 0.5
    p_buy: float = 0.1  # 14-day purchase chance with no offer
    uplift_any: float = 0.05  # pooled model: extra chance from "some offer"
    uplift_by_type: dict[str, float] = field(default_factory=dict)  # per-person model outputs
    clv_margin_90d_inr: float = 0.0
    quarterly_margin_inr: float = 0.0  # usual 90-day margin when buying normally (past year / 4)
    aov_inr: float = 0.0
    weighted_margin_pct: float = 25.0
    top_category: str | None = None
    tenure_days: int = 365
    opted_out: bool = False
    prefers_cod: bool = False
    points_expiring_30d: int = 0
    last_order_at: datetime | None = None


@dataclass
class PastOffer:
    created_at: datetime
    valid_until: datetime
    offer_type: str
    status: str  # sent | claimed | holdout | suppressed | rejected


@dataclass
class History:
    offers: list[PastOffer] = field(default_factory=list)
    spent_today_inr: float = 0.0  # expected cost already committed today

    def sent(self) -> list[PastOffer]:
        return [o for o in self.offers if o.status in ("sent", "claimed")]


@dataclass
class Proposal:
    offer_id: str
    value: float
    category: str | None = None
    valid_until: datetime | None = None
    code: str | None = None


@dataclass
class Verdict:
    checks: list[Check]
    economics: Economics | None = None

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def reasons(self) -> list[str]:
        return [f"{c.rule}: {c.reason}" for c in self.failures]


def in_holdout(customer_id: str, share: float, salt: str = "offeriq-holdout-v1") -> bool:
    """Stable random assignment: the same customer is always in (or out of) the holdout group."""
    digest = hashlib.sha256(f"{salt}:{customer_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF < share


def contact_checks(ctx: CustomerContext, policy: Policy, now: datetime, history: History) -> list[Check]:
    """Rules about contacting this customer at all, independent of which offer."""
    c = policy.contact
    sent = history.sent()
    week = [o for o in sent if o.created_at > now - timedelta(days=7)]
    last = max((o.created_at for o in sent), default=None)
    active = [o for o in sent if o.valid_until > now]
    checks = [
        Check("not_opted_out", not ctx.opted_out, "customer opted out of offers" if ctx.opted_out else "ok"),
        Check(
            "not_in_holdout",
            not in_holdout(ctx.customer_id, c.holdout_share),
            "in the random holdout group (no offers, used to measure true lift)"
            if in_holdout(ctx.customer_id, c.holdout_share)
            else "ok",
        ),
        Check(
            "uplift_group_contactable",
            ctx.uplift_group not in policy.targeting.never_contact_uplift_groups,
            f"{ctx.uplift_group}: contacting them is expected to reduce purchases"
            if ctx.uplift_group in policy.targeting.never_contact_uplift_groups
            else "ok",
        ),
        Check(
            "frequency_cap_7d",
            len(week) < c.max_offers_per_7_days,
            f"{len(week)} offers in the last 7 days (max {c.max_offers_per_7_days})",
        ),
        Check(
            "min_gap_between_offers",
            last is None or now - last >= timedelta(days=c.min_days_between_offers),
            "no previous offer"
            if last is None
            else f"last offer {(now - last).days} days ago (min {c.min_days_between_offers})",
        ),
        Check("no_stacking", not active, f"{len(active)} offer(s) still active" if active else "ok"),
        Check(
            "purchase_cooldown",
            ctx.last_order_at is None
            or now - ctx.last_order_at >= timedelta(days=c.cooldown_after_purchase_days),
            "ok"
            if ctx.last_order_at is None
            else f"last order {(now - ctx.last_order_at).days} days ago "
            f"(cool-down {c.cooldown_after_purchase_days} days)",
        ),
        Check(
            "quiet_hours",
            not c.quiet_hours.contains(now.time()),
            f"{now:%H:%M} IST is inside quiet hours {c.quiet_hours.start:%H:%M}–{c.quiet_hours.end:%H:%M}"
            if c.quiet_hours.contains(now.time())
            else "ok",
        ),
    ]
    return checks


def offer_checks(
    ctx: CustomerContext,
    proposal: Proposal,
    template: OfferTemplate,
    policy: Policy,
    now: datetime,
    history: History,
    econ: Economics,
) -> list[Check]:
    v = proposal.value
    checks = [
        Check(
            "offer_enabled", template.enabled, "ok" if template.enabled else f"{template.id} is switched off"
        ),
        Check(
            "value_in_range",
            template.value.min <= v <= template.value.max,
            f"value {v:g} (allowed {template.value.min:g}–{template.value.max:g})",
        ),
    ]
    if template.only_lifecycles:
        ok = ctx.lifecycle in template.only_lifecycles
        checks.append(
            Check("lifecycle_allowed", ok, f"{ctx.lifecycle} (offer is for {template.only_lifecycles})")
        )
    if template.only_cod_customers:
        checks.append(
            Check(
                "cod_customers_only",
                ctx.prefers_cod,
                "prefers COD" if ctx.prefers_cod else "customer already pays online",
            )
        )
    if template.requires_expiring_points:
        checks.append(
            Check(
                "has_expiring_points",
                ctx.points_expiring_30d > 0,
                f"{ctx.points_expiring_30d} points expiring in 30 days",
            )
        )
    if ctx.uplift_group == "sure_thing":
        ok = template.type in policy.targeting.sure_thing_allowed_types
        checks.append(
            Check(
                "sure_thing_no_discount",
                ok,
                "likely to buy anyway: only non-discount rewards" if not ok else "ok",
            )
        )

    monetary = template.type in PERCENT_TYPES | RUPEE_TYPES
    if monetary:
        category = proposal.category
        cap = policy.category_cap(category, now.date()) if category else 0.0
        pct = discount_pct_of_order(ctx, template, v)
        checks.append(
            Check(
                "category_known",
                category in policy.categories,
                f"category {category!r}" if category else "monetary offer needs a category",
            )
        )
        checks.append(
            Check(
                "category_discount_cap",
                pct <= cap + 1e-9,
                f"{pct:.1f}% of the order (cap for {category} is {cap:g}%)",
            )
        )
    if template.max_discount_inr is not None:
        checks.append(
            Check(
                "max_discount_inr",
                econ.cost_if_used_inr <= template.max_discount_inr + 1e-9,
                f"₹{econ.cost_if_used_inr:,.0f} (max ₹{template.max_discount_inr:,.0f})",
            )
        )

    b = policy.budget
    checks.append(
        Check(
            "max_cost_per_offer",
            econ.cost_if_used_inr <= b.max_cost_per_offer_inr,
            f"₹{econ.cost_if_used_inr:,.0f} if used (max ₹{b.max_cost_per_offer_inr:,.0f})",
        )
    )
    clv_cap = max(b.max_cost_share_of_clv * ctx.clv_margin_90d_inr, b.clv_cap_floor_inr)
    checks.append(
        Check(
            "clv_cost_cap",
            econ.expected_cost_inr <= clv_cap + 1e-9,
            f"expected cost ₹{econ.expected_cost_inr:,.0f} (cap ₹{clv_cap:,.0f} from 90-day value)",
        )
    )
    if ctx.tenure_days < policy.targeting.new_account_days:
        ok = econ.cost_if_used_inr <= policy.targeting.new_account_max_cost_inr
        checks.append(
            Check(
                "new_account_guard",
                ok,
                f"account {ctx.tenure_days} days old: max ₹{policy.targeting.new_account_max_cost_inr:,.0f}",
            )
        )
    budget = policy.daily_budget(now.date())
    checks.append(
        Check(
            "daily_budget",
            history.spent_today_inr + econ.expected_cost_inr <= budget + 1e-9,
            f"₹{history.spent_today_inr:,.0f} committed today + ₹{econ.expected_cost_inr:,.0f} "
            f"(budget ₹{budget:,.0f})",
        )
    )
    checks.append(
        Check(
            "min_expected_profit",
            econ.expected_profit_inr >= policy.targeting.min_expected_profit_inr,
            f"expected profit ₹{econ.expected_profit_inr:,.1f} (min "
            f"₹{policy.targeting.min_expected_profit_inr:,.0f})",
        )
    )
    if proposal.valid_until is not None:
        latest = now + timedelta(days=template.validity_days)
        ok = now < proposal.valid_until <= latest + timedelta(minutes=1)
        checks.append(
            Check(
                "validity_window",
                ok,
                f"valid until {proposal.valid_until:%d %b %H:%M} (max {template.validity_days} days)",
            )
        )
    return checks


def validate(
    ctx: CustomerContext,
    proposal: Proposal,
    policy: Policy,
    effects: OfferEffects,
    now: datetime,
    history: History,
    message: str | None = None,
    skip: frozenset[str] = frozenset(),
) -> Verdict:
    """Check a proposed offer (and its message, if given) against every rule in the policy."""
    template = policy.offer(proposal.offer_id)
    if template is None:
        return Verdict([Check("offer_exists", False, f"no offer {proposal.offer_id!r} in the policy")])
    econ = economics(ctx, template, proposal.value, effects, policy)
    checks = [Check("offer_exists", True, template.id)]
    checks += contact_checks(ctx, policy, now, history)
    checks += offer_checks(ctx, proposal, template, policy, now, history, econ)
    if message is not None:
        checks += check_message(message, ctx, proposal, template, policy)
    checks = [c for c in checks if c.rule not in skip]
    return Verdict(checks, econ)


def fit_value(
    ctx: CustomerContext, template: OfferTemplate, category: str | None, policy: Policy, now: datetime
) -> float | None:
    """Default value, reduced if needed to fit the category cap; None if even the minimum doesn't fit."""
    v = template.value.default
    if template.type not in PERCENT_TYPES | RUPEE_TYPES:
        return v
    cap = policy.category_cap(category, now.date()) if category else 0.0
    if discount_pct_of_order(ctx, template, v) <= cap:
        return v
    if template.type in PERCENT_TYPES:
        v = math.floor(cap)
    else:  # ₹ offers: largest multiple of ₹10 within the cap
        v = math.floor(cap / 100 * max(ctx.aov_inr, template.min_order_inr) / 10) * 10
    return float(v) if v >= template.value.min and discount_pct_of_order(ctx, template, v) <= cap else None


@dataclass
class MenuItem:
    template: OfferTemplate
    proposal: Proposal
    verdict: Verdict


def menu(
    ctx: CustomerContext, policy: Policy, effects: OfferEffects, now: datetime, history: History
) -> list[MenuItem]:
    """Every offer this customer may receive right now (all rules pass), best expected profit first."""
    items = []
    for template in policy.offers:
        if not template.enabled:
            continue
        category = ctx.top_category if template.type in PERCENT_TYPES | RUPEE_TYPES else None
        value = fit_value(ctx, template, category, policy, now)
        if value is None:
            continue
        proposal = Proposal(template.id, value, category, now + timedelta(days=template.validity_days))
        verdict = validate(ctx, proposal, policy, effects, now, history)
        if verdict.passed:
            items.append(MenuItem(template, proposal, verdict))
    return sorted(items, key=lambda m: m.verdict.economics.expected_profit_inr, reverse=True)
