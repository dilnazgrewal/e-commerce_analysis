"""Each business rule, one at a time: a compliant proposal passes; each violation fails its rule."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.rules.decide import decide_rules_only
from app.rules.engine import History, PastOffer, Proposal, in_holdout, menu, validate
from app.rules.schema import Policy, load_policy
from tests.rules_helpers import NOW, POLICY, ctx, effects

E = effects()


def proposal(offer_id="pct10", value=10, category="Clothing_Shoes_and_Jewelry", days=7):
    return Proposal(offer_id, value, category, NOW + timedelta(days=days), "OIQ-PCT-ABCDE")


def failed(verdict) -> set[str]:
    return {c.rule for c in verdict.failures}


def test_helper_customer_is_not_in_holdout():
    assert not in_holdout("C00002", POLICY.contact.holdout_share)


def test_compliant_offer_passes_every_rule():
    verdict = validate(ctx(), proposal(), POLICY, E, NOW, History())
    assert verdict.passed, verdict.reasons()


@pytest.mark.parametrize(
    ("customer", "prop", "now", "history", "rule"),
    [
        (ctx(opted_out=True), proposal(), NOW, History(), "not_opted_out"),
        (ctx(uplift_group="sleeping_dog"), proposal(), NOW, History(), "uplift_group_contactable"),
        (ctx(), proposal(), NOW.replace(hour=22), History(), "quiet_hours"),
        (ctx(), proposal(), NOW.replace(hour=7), History(), "quiet_hours"),
        (ctx(last_order_at=NOW - timedelta(hours=20)), proposal(), NOW, History(), "purchase_cooldown"),
        (ctx(), proposal(value=40), NOW, History(), "value_in_range"),
        (ctx(), proposal("nope"), NOW, History(), "offer_exists"),
        (
            ctx(top_category="Electronics"),
            proposal(category="Electronics"),
            NOW,
            History(),
            "category_discount_cap",
        ),
        (ctx(), proposal(category=None), NOW, History(), "category_known"),
        (ctx(), proposal(days=9), NOW, History(), "validity_window"),
        (ctx(uplift_group="sure_thing"), proposal(), NOW, History(), "sure_thing_no_discount"),
        (ctx(lifecycle="active"), proposal("winback15", 15), NOW, History(), "lifecycle_allowed"),
        (ctx(prefers_cod=False), proposal("upi50", 50), NOW, History(), "cod_customers_only"),
        (ctx(), proposal("pointsexpiry", 0, None), NOW, History(), "has_expiring_points"),
        (ctx(tenure_days=3, aov_inr=3000), proposal("pct10", 10), NOW, History(), "new_account_guard"),
        (ctx(), proposal(), NOW, History(spent_today_inr=49_999), "daily_budget"),
        (ctx(uplift_any=0.0, uplift_by_type={}), proposal(), NOW, History(), "min_expected_profit"),
        (ctx(clv_margin_90d_inr=0, aov_inr=3000), proposal(), NOW, History(), "clv_cost_cap"),
    ],
)
def test_each_rule_blocks_its_violation(customer, prop, now, history, rule):
    verdict = validate(customer, prop, POLICY, E, now, history)
    assert rule in failed(verdict), verdict.reasons()


def test_holdout_customer_is_never_contacted():
    holdout_id = next(f"C{i:05d}" for i in range(1, 1000) if in_holdout(f"C{i:05d}", 0.10))
    verdict = validate(ctx(customer_id=holdout_id), proposal(), POLICY, E, NOW, History())
    assert "not_in_holdout" in failed(verdict)
    share = sum(in_holdout(f"C{i:05d}", 0.10) for i in range(1, 5001)) / 5000
    assert 0.08 < share < 0.12


def test_frequency_cap_gap_and_stacking():
    old = PastOffer(NOW - timedelta(days=1), NOW + timedelta(days=3), "PCT_OFF", "sent")
    verdict = validate(ctx(), proposal(), POLICY, E, NOW, History([old]))
    assert {"min_gap_between_offers", "no_stacking"} <= failed(verdict)
    week = [
        PastOffer(NOW - timedelta(days=d), NOW - timedelta(days=d - 1), "CART_NUDGE", "sent") for d in (3, 5)
    ]
    assert "frequency_cap_7d" in failed(validate(ctx(), proposal(), POLICY, E, NOW, History(week)))
    # holdout/suppressed records don't count as offers received
    quiet = [PastOffer(NOW - timedelta(days=1), NOW, "", "suppressed")]
    assert validate(ctx(), proposal(), POLICY, E, NOW, History(quiet)).passed


def test_diwali_override_raises_electronics_cap():
    electronics = ctx(
        top_category="Electronics", aov_inr=2000, weighted_margin_pct=8, clv_margin_90d_inr=2000
    )
    p = proposal(value=7, category="Electronics")
    assert "category_discount_cap" in failed(validate(electronics, p, POLICY, E, NOW, History()))
    diwali = datetime(2026, 11, 5, 11, 0)
    p_diwali = replace(p, valid_until=diwali + timedelta(days=7))
    assert "category_discount_cap" not in failed(
        validate(electronics, p_diwali, POLICY, E, diwali, History())
    )


def test_menu_only_contains_fully_valid_offers_sorted_by_profit():
    items = menu(ctx(), POLICY, E, NOW, History())
    assert items, "a persuadable fashion customer should have options"
    profits = [m.verdict.economics.expected_profit_inr for m in items]
    assert profits == sorted(profits, reverse=True)
    assert all(m.verdict.passed for m in items)
    # electronics: % values are cut to fit the 5% cap
    electronics = menu(
        ctx(top_category="Electronics", aov_inr=3000, clv_margin_90d_inr=3000), POLICY, E, NOW, History()
    )
    pct = [m for m in electronics if m.template.id == "pct10"]
    assert not pct or pct[0].proposal.value <= 5


def test_option_a_uses_measured_effects_except_trusted_types():
    from app.rules.effects import economics

    c = ctx()
    pct = economics(c, POLICY.offer("pct10"), 10, E, POLICY)
    assert pct.uplift == pytest.approx(c.uplift_any * 0.061 / E.average_effect)
    assert "measured PCT_OFF" in pct.basis
    points = economics(c, POLICY.offer("points2x"), 2, E, POLICY)
    assert points.uplift == pytest.approx(0.09) and "per-person model" in points.basis
    upi = economics(c, POLICY.offer("upi50"), 50, E, POLICY)
    assert "assumed like FLAT_OFF" in upi.basis


def test_decide_tiers():
    sent = decide_rules_only(ctx(), POLICY, E, NOW, History())
    assert sent.status == "sent" and sent.tier == "T4" and all(c["passed"] for c in sent.rule_checks)
    assert sent.message["text"] and sent.offer["code"] in sent.message["text"]

    night = decide_rules_only(ctx(), POLICY, E, NOW.replace(hour=23), History())
    assert night.tier == "T6" and night.status == "queued"

    dog = decide_rules_only(ctx(uplift_group="sleeping_dog"), POLICY, E, NOW, History())
    assert dog.status == "suppressed" and dog.offer is None

    # nothing profitable + at_risk → marketer's fallback (free shipping)
    at_risk = ctx(lifecycle="at_risk", uplift_any=0.0, uplift_by_type={}, p_buy=0.05, aov_inr=300)
    fallback = decide_rules_only(at_risk, POLICY, E, NOW, History())
    assert fallback.tier == "T5" and fallback.offer["offer_id"] == "freeship"

    # nothing profitable + active (fallback "none") → suppressed
    none = decide_rules_only(replace(at_risk, lifecycle="active"), POLICY, E, NOW, History())
    assert none.tier == "T6" and none.status == "suppressed"


def test_policy_rejects_typos_and_inconsistent_values():
    raw = load_policy().model_dump(mode="json")
    raw["contact"]["max_offers_per_7_dayz"] = 5
    with pytest.raises(ValidationError):
        Policy.model_validate(raw)
    raw = load_policy().model_dump(mode="json")
    raw["fallbacks"]["vip"] = "does-not-exist"
    with pytest.raises(ValidationError):
        Policy.model_validate(raw)
    raw = load_policy().model_dump(mode="json")
    raw["offers"][3]["value"] = {"min": 5, "max": 15, "default": 30}
    with pytest.raises(ValidationError):
        Policy.model_validate(raw)
