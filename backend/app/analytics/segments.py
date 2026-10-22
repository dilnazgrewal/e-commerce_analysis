"""Segments a marketer can read: uplift groups (who to target) and lifecycle stages (where they are)."""

import numpy as np
import pandas as pd

UPLIFT_GROUPS = {
    "persuadable": "Buys noticeably more when given an offer: the people offers are for.",
    "sure_thing": "Likely to buy anyway: a discount mostly gives away margin. Reward with points instead.",
    "lost_cause": "Unlikely to buy, with or without an offer: keep spend minimal.",
    "sleeping_dog": "Every offer we tested looks likely to make them buy less. Leave them alone.",
}
LIFECYCLE = {
    "new": "Joined in the last 30 days or has at most one order in their first 60 days.",
    "vip": "Top 10% by spend in the past year and bought recently.",
    "active": "Buying regularly; last order within 45 days.",
    "at_risk": "Was buying, but has gone quiet and the churn model sees high risk.",
    "lapsed": "No order for over 120 days.",
    "window_shopper": "Browses often but hasn't bought in 90 days.",
    "bargain_hunter": "Mostly buys with coupons or points.",
}

# Uplift groups are relative: offers are budget-limited, so "persuadable" means the customers where an
# offer moves the needle most (top 40% by best-offer uplift, and at least +3pp in 14-day purchase odds).
PERSUADABLE_TOP_SHARE = 0.40
PERSUADABLE_MIN_UPLIFT = 0.03
SLEEPING_DOG_MAX_UPLIFT = -0.03  # even the best offer is estimated to cost ≥ 3pp of purchase probability
SURE_THING_TOP_SHARE = 0.20  # most likely buyers without any offer


def uplift_thresholds(best_uplift: np.ndarray, baseline: np.ndarray) -> dict:
    return {
        "persuadable_min_uplift": float(
            max(PERSUADABLE_MIN_UPLIFT, np.quantile(best_uplift, 1 - PERSUADABLE_TOP_SHARE))
        ),
        "sleeping_dog_max_uplift": SLEEPING_DOG_MAX_UPLIFT,
        "sure_thing_min_baseline": float(np.quantile(baseline, 1 - SURE_THING_TOP_SHARE)),
    }


def uplift_group(best_uplift: np.ndarray, baseline: np.ndarray, thresholds: dict) -> np.ndarray:
    return np.select(
        [
            best_uplift <= thresholds["sleeping_dog_max_uplift"],
            best_uplift >= thresholds["persuadable_min_uplift"],
            baseline >= thresholds["sure_thing_min_baseline"],
        ],
        ["sleeping_dog", "persuadable", "sure_thing"],
        "lost_cause",
    )


def lifecycle(features: pd.DataFrame, churn: pd.Series) -> np.ndarray:
    f = features
    vip_cut = f.loc[f["monetary_365d_inr"] > 0, "monetary_365d_inr"].quantile(0.9)
    conditions = [
        (f["tenure_days"] <= 30) | ((f["tenure_days"] <= 60) & (f["orders_lifetime"] <= 1)),
        f["recency_days"] > 120,
        (f["monetary_365d_inr"] >= vip_cut) & (f["recency_days"] <= 60),
        (churn >= 0.6) & (f["recency_days"] > 30),
        (f["sessions_30d"] >= 3) & (f["orders_90d"] == 0),
        (f["discount_dependency"] >= 0.12) | (f["coupon_share"] >= 0.5),
    ]
    labels = ["new", "lapsed", "vip", "at_risk", "window_shopper", "bargain_hunter"]
    return np.select(conditions, labels, "active")
