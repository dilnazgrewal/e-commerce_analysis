"""Fast unit tests for the analytics layer, on small hand-built tables (no database needed)."""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.analytics.clv import p_alive
from app.analytics.features import Tables, build_features, model_columns
from app.analytics.segments import lifecycle, uplift_group, uplift_thresholds
from app.analytics.uplift import ate, qini

AS_OF = datetime(2026, 10, 22)


def tiny_tables() -> Tables:
    customers = pd.DataFrame(
        {
            "customer_id": ["C1", "C2"],
            "signup_date": pd.to_datetime(["2025-01-01", "2026-10-01"]),
            "orders_before_window": [3, 0],
            "last_order_before_window": pd.to_datetime(["2025-09-01", None]),
            "city_tier": ["tier1", "tier3"],
            "device": ["android", "ios"],
            "language_pref": ["en", "pa"],
            "prime_member": [True, False],
            "preferred_payment": ["UPI", "COD"],
            "points_balance": [120, 0],
            "points_expiry_date": pd.to_datetime(["2026-11-10", None]),
            "age_band": ["25-34", "18-24"],
            "acquisition_channel": ["organic", "ads"],
            "opted_out": [False, False],
        }
    )
    orders = pd.DataFrame(
        {
            "order_id": ["O1", "O2", "O3"],
            "customer_id": ["C1", "C1", "C1"],
            "ts": pd.to_datetime(["2026-09-01", "2026-10-10", "2026-11-05"]),  # O3 is in the future
            "gross_inr": [1000, 2000, 99_999],
            "discount_inr": [100, 0, 0],
            "coupon_code": ["SAVE10", None, None],
            "payment_mode": ["UPI", "UPI", "UPI"],
            "status": ["delivered", "returned", "delivered"],
            "return_reason": [None, "changed mind", None],
            "points_earned": [9, 0, 999],
            "points_redeemed": [0, 0, 0],
        }
    )
    events = pd.DataFrame(
        {
            "customer_id": ["C2", "C2", "C2", "C1"],
            "type": ["session_start", "add_to_cart", "view", "session_start"],
            "ts": pd.to_datetime(
                ["2026-10-20 20:00", "2026-10-20 20:05", "2026-10-20 20:01", "2026-10-25 09:00"]
            ),
            "session_id": ["S1", "S1", "S1", "S9"],
            "price_inr": [None, 1499, 1499, None],
            "points": [None, None, None, None],
        }
    )
    items = pd.DataFrame(
        {
            "order_id": ["O1", "O2", "O3"],
            "category": ["Electronics", "Books", "Electronics"],
            "price_inr": [1000, 2000, 99_999],
            "customer_id": ["C1", "C1", "C1"],
            "ts": orders["ts"],
        }
    )
    assignments = pd.DataFrame(
        columns=["campaign_id", "customer_id", "arm", "offer_type", "offer_value", "sent_at", "valid_until"]
    ).astype({"sent_at": "datetime64[ns]", "valid_until": "datetime64[ns]"})
    return Tables(
        customers,
        events,
        orders,
        items,
        assignments,
        {"Electronics": 8, "Books": 22},
        ["Books", "Electronics"],
        [],
    )


def test_features_never_use_future_rows():
    f = build_features(tiny_tables(), AS_OF).set_index("customer_id")
    c1 = f.loc["C1"]
    assert c1["orders_365d"] == 2  # O3 (future) excluded
    assert c1["monetary_365d_inr"] == 900 + 2000
    assert c1["recency_days"] == 12
    assert c1["orders_lifetime"] == 5
    assert c1["return_rate"] == 0.5
    assert c1["sessions_30d"] == 0  # the C1 session is after as_of


def test_features_capture_open_cart_and_profile():
    f = build_features(tiny_tables(), AS_OF).set_index("customer_id")
    c2 = f.loc["C2"]
    assert c2["never_bought"] == 1
    assert c2["open_cart_value_7d_inr"] == 1499
    assert c2["cart_abandon_rate_30d"] == 1.0
    assert c2["city_tier"] == 3 and c2["prefers_cod"] == 1 and c2["lang_pa"] == 1
    assert "gender" not in f.columns
    assert "top3_categories" not in model_columns(f.reset_index())


def test_qini_rewards_a_model_that_ranks_true_responders_first():
    rng = np.random.default_rng(0)
    n = 20_000
    responsive = rng.random(n) < 0.3
    treated = rng.random(n) < 0.5
    outcome = (rng.random(n) < 0.05 + 0.20 * (treated & responsive)).astype(int)
    good = qini(responsive.astype(float) + rng.normal(0, 0.1, n), treated, outcome)
    noise = qini(rng.normal(0, 1, n), treated, outcome)
    assert good["qini_gain"] > noise["qini_gain"]
    assert good["targeting_multiplier"] > 2


def test_ate_confidence_interval_covers_true_effect():
    rng = np.random.default_rng(1)
    treated = rng.random(40_000) < 0.5
    outcome = (rng.random(40_000) < 0.10 + 0.03 * treated).astype(float)
    result = ate(outcome, treated)
    assert result["ci95"][0] < 0.03 < result["ci95"][1]


def test_uplift_groups():
    best = np.array([-0.05, 0.10, 0.01, 0.0, 0.08])
    baseline = np.array([0.2, 0.1, 0.9, 0.05, 0.3])
    thresholds = {
        "persuadable_min_uplift": 0.05,
        "sleeping_dog_max_uplift": -0.03,
        "sure_thing_min_baseline": 0.5,
    }
    assert uplift_group(best, baseline, thresholds).tolist() == [
        "sleeping_dog",
        "persuadable",
        "sure_thing",
        "lost_cause",
        "persuadable",
    ]
    t = uplift_thresholds(np.linspace(-0.1, 0.2, 101), np.linspace(0, 1, 101))
    assert t["persuadable_min_uplift"] == pytest.approx(0.08)


def test_p_alive_drops_when_customer_goes_quiet():
    params = {"r": 0.66, "alpha": 63.0, "a": 0.11, "b": 2.4}
    recent = p_alive(params, np.array([10.0]), np.array([300.0]), np.array([310.0]))
    quiet = p_alive(params, np.array([10.0]), np.array([100.0]), np.array([310.0]))
    assert 0 < quiet[0] < recent[0] <= 1
    assert p_alive(params, np.array([0.0]), np.array([0.0]), np.array([50.0]))[0] == 1.0


def test_lifecycle_labels():
    f = pd.DataFrame(
        {
            "tenure_days": [10, 400, 400, 400],
            "orders_lifetime": [0, 10, 10, 3],
            "recency_days": [10, 200, 5, 40],
            "monetary_365d_inr": [0, 100, 90_000, 500],
            "sessions_30d": [1, 0, 5, 1],
            "orders_90d": [0, 0, 4, 1],
            "discount_dependency": [0, 0, 0, 0],
            "coupon_share": [0, 0, 0, 0],
        }
    )
    assert lifecycle(f, pd.Series([0.1, 0.9, 0.1, 0.2])).tolist() == ["new", "lapsed", "vip", "active"]


def test_reason_text_reads_naturally():
    from app.analytics.churn import REASONS

    assert REASONS["sessions_30d"][1](0) == "No visits in the last 30 days"
    assert REASONS["sessions_30d"][1](1) == "Only 1 visit in the last 30 days"
    assert REASONS["orders_lifetime"][1](0) == "Has never ordered"
    assert REASONS["monetary_365d_inr"][1](1499) == "Spent only ₹1,499 in the past year"
