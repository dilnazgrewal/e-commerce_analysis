"""Property tests: across thousands of random customers, histories, times and (often bad) proposals,
nothing the engine approves ever breaks a rule.

The assertions below are a second, deliberately simple implementation of the key limits, written straight
from the policy file. They don't reuse the engine's checks, so a bug in the engine can't hide itself.
Half the proposals are "near-valid": a real menu offer with fields pushed past their limits, so the test
spends its time at the rule boundaries, not on obviously broken input.
"""

from datetime import datetime, timedelta

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from app.rules.decide import decide_rules_only
from app.rules.effects import cost_if_used, order_value
from app.rules.engine import CustomerContext, History, PastOffer, Proposal, in_holdout, menu, validate
from app.rules.message_checks import NUMBER, allowed_numbers
from tests.rules_helpers import POLICY, effects

E = effects()
CATEGORIES = list(POLICY.categories)
MONETARY = {"PCT_OFF", "WIN_BACK", "FLAT_OFF", "SECOND_ORDER", "UPI_CASHBACK"}
BASE = datetime(2026, 10, 22, 0, 0)

customers = st.builds(
    CustomerContext,
    customer_id=st.integers(1, 9999).map(lambda i: f"C{i:05d}"),
    name=st.sampled_from(["Simran Kaur", "Ishwar Jain", "Gurpreet Gill", "Meera"]),
    language=st.sampled_from(["en", "hi-Latn", "pa"]),
    lifecycle=st.sampled_from(
        ["new", "vip", "active", "at_risk", "lapsed", "window_shopper", "bargain_hunter"]
    ),
    uplift_group=st.sampled_from(["persuadable"] * 4 + ["sure_thing", "lost_cause", "sleeping_dog"]),
    churn_60d=st.floats(0, 1),
    p_buy=st.floats(0, 0.95),
    uplift_any=st.floats(-0.1, 0.4),
    uplift_by_type=st.fixed_dictionaries(
        {"BONUS_POINTS": st.floats(-0.2, 0.4), "FREE_SHIPPING": st.floats(-0.2, 0.4)}
    ),
    clv_margin_90d_inr=st.floats(0, 20_000),
    quarterly_margin_inr=st.floats(0, 20_000),
    aov_inr=st.one_of(st.floats(200, 5000), st.floats(0, 60_000)),
    weighted_margin_pct=st.floats(4, 45),
    top_category=st.one_of(st.none(), st.sampled_from(CATEGORIES)),
    tenure_days=st.one_of(st.integers(30, 3000), st.integers(0, 30)),
    opted_out=st.sampled_from([False] * 9 + [True]),
    prefers_cod=st.booleans(),
    points_expiring_30d=st.integers(0, 3000),
    last_order_at=st.one_of(st.none(), st.integers(0, 24 * 120).map(lambda h: BASE - timedelta(hours=h))),
)
# Mostly daytime (outside quiet hours), any hour, and the Diwali override window (1–8 Nov).
daytime = st.tuples(st.integers(0, 19), st.integers(9, 20), st.integers(0, 59)).map(
    lambda t: BASE + timedelta(days=t[0], hours=t[1], minutes=t[2])
)
any_hour = st.integers(0, 24 * 20 - 1).map(lambda h: BASE + timedelta(hours=h))
diwali = st.integers(0, 24 * 8 - 1).map(lambda h: datetime(2026, 11, 1) + timedelta(hours=h))
times = st.one_of(daytime, any_hour, diwali)

past = st.builds(
    PastOffer,
    created_at=st.integers(1, 24 * 20).map(lambda h: BASE - timedelta(hours=h)),
    valid_until=st.integers(-24 * 10, 24 * 10).map(lambda h: BASE + timedelta(hours=h)),
    offer_type=st.sampled_from(["PCT_OFF", "CART_NUDGE"]),
    status=st.sampled_from(["sent", "claimed", "holdout", "suppressed"]),
)
histories = st.builds(
    History,
    offers=st.one_of(st.just([]), st.lists(past, max_size=4)),
    spent_today_inr=st.one_of(st.floats(0, 30_000), st.floats(0, 120_000)),
)
values = st.one_of(st.sampled_from([0, 2, 5, 10, 15, 25, 40, 50, 100, 150, 200]), st.floats(-50, 500))
proposals = st.builds(
    Proposal,
    offer_id=st.sampled_from([o.id for o in POLICY.offers] + ["bogus"]),
    value=values,
    category=st.one_of(st.none(), st.sampled_from(CATEGORIES + ["Cars"])),
    valid_until=st.one_of(st.none(), st.integers(-48, 24 * 40).map(lambda h: BASE + timedelta(hours=h))),
    code=st.just("OIQ-TEST-ABCDE"),
)


def sent_offers(history: History) -> list[PastOffer]:
    return [o for o in history.offers if o.status in ("sent", "claimed")]


def assert_independent_invariants(c: CustomerContext, p: Proposal, now: datetime, history: History) -> None:
    t = POLICY.offer(p.offer_id)
    assert t is not None and t.enabled
    assert not c.opted_out
    assert not in_holdout(c.customer_id, POLICY.contact.holdout_share)
    assert c.uplift_group != "sleeping_dog"
    qh = POLICY.contact.quiet_hours
    assert not (now.time() >= qh.start or now.time() < qh.end), "inside quiet hours"
    assert t.value.min <= p.value <= t.value.max
    week = [o for o in sent_offers(history) if o.created_at > now - timedelta(days=7)]
    assert len(week) < POLICY.contact.max_offers_per_7_days
    assert not [o for o in sent_offers(history) if o.valid_until > now], "stacked on an active offer"
    if c.last_order_at is not None:
        assert now - c.last_order_at >= timedelta(days=POLICY.contact.cooldown_after_purchase_days)
    cost = cost_if_used(c, t, p.value)
    assert cost <= POLICY.budget.max_cost_per_offer_inr
    if t.type in MONETARY:
        festive = POLICY.festive_on(now.date())
        cap = POLICY.categories[p.category].max_discount_pct
        if festive and p.category in festive.category_caps:
            cap = festive.category_caps[p.category]
        assert 100 * cost / order_value(c, t) <= cap + 1e-9, "discount above category cap"
    if c.uplift_group == "sure_thing":
        assert t.type not in MONETARY and t.type != "FREE_SHIPPING"
    if t.only_lifecycles:
        assert c.lifecycle in t.only_lifecycles
    if c.tenure_days < POLICY.targeting.new_account_days:
        assert cost <= POLICY.targeting.new_account_max_cost_inr


@settings(max_examples=10_000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(c=customers, now=times, history=histories, data=st.data())
def test_anything_validate_approves_obeys_the_policy(c, now, history, data):
    items = menu(c, POLICY, E, now, history)
    if items and data.draw(st.booleans()):
        base = data.draw(st.sampled_from(items)).proposal
        later = st.integers(-48, 24 * 40).map(lambda h: now + timedelta(hours=h))
        p = Proposal(
            base.offer_id,
            data.draw(st.one_of(st.just(base.value), values)),
            data.draw(st.one_of(st.just(base.category), st.sampled_from(CATEGORIES))),
            data.draw(st.one_of(st.just(base.valid_until), later)),
            "OIQ-TEST-ABCDE",
        )
        event("near-valid proposal")
    else:
        p = data.draw(proposals)
        event("random proposal")
    verdict = validate(c, p, POLICY, E, now, history)
    event("approved" if verdict.passed else "rejected")
    if verdict.passed:
        assert_independent_invariants(c, p, now, history)
        assert verdict.economics.expected_profit_inr >= POLICY.targeting.min_expected_profit_inr


@settings(max_examples=2_000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(c=customers, now=times, history=histories)
def test_every_sent_decision_obeys_the_policy_and_its_message_is_truthful(c, now, history):
    d = decide_rules_only(c, POLICY, E, now, history)
    event(f"{d.tier} {d.status}")
    assert d.tier in {"T4", "T5", "T6"}
    if d.status != "sent":
        assert d.offer is None and d.message is None
        return
    o = d.offer
    p = Proposal(o["offer_id"], o["value"], o["category"], o["valid_until"], o["code"])
    assert_independent_invariants(c, p, now, history)
    assert all(check["passed"] for check in d.rule_checks)
    text = d.message["text"]
    body = text.replace(o["code"], " ") if o["code"] else text
    allowed = allowed_numbers(c, p, POLICY.offer(o["offer_id"]))
    assert all(float(n.replace(",", "")) in allowed for n in NUMBER.findall(body)), text
    assert len(text) <= POLICY.messages.max_length
