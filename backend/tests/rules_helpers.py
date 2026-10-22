"""Shared fixtures for rules tests: a fixed policy, measured effects and customer contexts (no DB)."""

from datetime import datetime

from app.rules.effects import MeasuredEffect, OfferEffects
from app.rules.engine import CustomerContext
from app.rules.schema import load_policy

NOW = datetime(2026, 10, 22, 11, 0)  # a Thursday morning, outside quiet hours, outside the Diwali window
POLICY = load_policy()


def effects(trusted: tuple[str, ...] = ("BONUS_POINTS", "FREE_SHIPPING")) -> OfferEffects:
    measured = {
        "CART_NUDGE": (0.038, (0.015, 0.061), 0),
        "BONUS_POINTS": (0.056, (0.032, 0.079), 2),
        "FREE_SHIPPING": (0.055, (0.032, 0.078), 40),
        "PCT_OFF": (0.061, (0.038, 0.085), 10),
        "FLAT_OFF": (0.063, (0.040, 0.087), 100),
    }
    m = {
        t: MeasuredEffect(t, e, ci, v, t in trusted, 2.0 if t in trusted else 0.7)
        for t, (e, ci, v) in measured.items()
    }
    return OfferEffects(m, sum(x.effect for x in m.values()) / len(m), "test")


def ctx(**overrides) -> CustomerContext:
    """A typical, contactable customer whose top category is fashion (35% cap)."""
    base = dict(
        customer_id="C00002",  # not in the 10% holdout (checked in a test)
        name="Simran Kaur",
        language="en",
        lifecycle="active",
        uplift_group="persuadable",
        churn_60d=0.4,
        p_buy=0.15,
        uplift_any=0.10,
        uplift_by_type={"BONUS_POINTS": 0.09, "FREE_SHIPPING": 0.08},
        clv_margin_90d_inr=900.0,
        quarterly_margin_inr=600.0,
        aov_inr=1200.0,
        weighted_margin_pct=40.0,
        top_category="Clothing_Shoes_and_Jewelry",
        tenure_days=400,
        opted_out=False,
        prefers_cod=True,
        points_expiring_30d=0,
        last_order_at=datetime(2026, 10, 1, 18, 0),
    )
    base.update(overrides)
    return CustomerContext(**base)
