"""Churn model: P(no order in the next 60 days), with plain-English reason codes.

Training uses a point-in-time snapshot 60 days before `as_of` (features from data before T0, label
from orders in (T0, T0+60d]); the fitted model then scores the current snapshot.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import train_test_split

from app.analytics.features import Tables, build_features, model_columns

HORIZON_DAYS = 60


# Reason templates for the features a marketer understands. {v} is the customer's value.
def _count(v: float, noun: str, none: str) -> str:
    n = int(round(v))
    return none if n == 0 else f"Only {n} {noun}{'' if n == 1 else 's'}"


# Reason text for the features a marketer understands, keyed by feature, with the risky end of its range.
REASONS = {
    "recency_days": ("high", lambda v: "Has never ordered" if v >= 3000 else f"No order in {v:.0f} days"),
    "days_since_last_session": ("high", lambda v: f"Hasn't visited in {v:.0f} days"),
    "sessions_30d": ("low", lambda v: _count(v, "visit", "No visits") + " in the last 30 days"),
    "sessions_90d": ("low", lambda v: _count(v, "visit", "No visits") + " in the last 90 days"),
    "orders_90d": ("low", lambda v: _count(v, "order", "No orders") + " in the last 90 days"),
    "orders_365d": ("low", lambda v: _count(v, "order", "No orders") + " in the past year"),
    "monetary_365d_inr": (
        "low",
        lambda v: "No spend in the past year" if v <= 0 else f"Spent only ₹{v:,.0f} in the past year",
    ),
    "cart_abandon_rate_30d": ("high", lambda v: f"Abandoned {v:.0%} of recent carts"),
    "return_rate": ("high", lambda v: f"Returned {v:.0%} of orders"),
    "rto_count_365d": (
        "high",
        lambda v: f"{v:.0f} COD order{'' if round(v) == 1 else 's'} refused at delivery (RTO)",
    ),
    "views_30d": ("low", lambda v: _count(v, "product view", "No product views") + " this month"),
    "tenure_days": ("low", lambda v: f"New customer ({v:.0f} days)"),
    "orders_lifetime": ("low", lambda v: "Has never ordered" if v == 0 else _count(v, "lifetime order", "")),
}


def churn_labels(tables: Tables, t0: datetime, customers: pd.Series) -> pd.Series:
    future = tables.orders[
        (tables.orders["ts"] >= t0) & (tables.orders["ts"] < t0 + timedelta(days=HORIZON_DAYS))
    ]
    bought = set(future["customer_id"])
    return customers.map(lambda c: int(c not in bought))


def _model() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.05,
        max_leaf_nodes=15,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=0,
    )


@dataclass
class ChurnModel:
    model: HistGradientBoostingClassifier
    columns: list[str]
    metrics: dict
    reference: pd.DataFrame  # training features, for percentile-based reason codes
    importance: pd.Series  # permutation importance (AUC drop)
    direction: pd.Series  # +1 if higher values raise churn risk


def train_churn(tables: Tables, as_of: datetime) -> ChurnModel:
    t0 = pd.Timestamp(as_of) - timedelta(days=HORIZON_DAYS)
    snap = build_features(tables, t0)
    cols = model_columns(snap)
    y = churn_labels(tables, t0, snap["customer_id"])
    X_tr, X_va, y_tr, y_va = train_test_split(snap[cols], y, test_size=0.25, random_state=0, stratify=y)

    model = _model().fit(X_tr, y_tr)
    p_va = model.predict_proba(X_va)[:, 1]
    bins = pd.qcut(p_va, 10, duplicates="drop")
    calibration = pd.DataFrame({"pred": p_va, "obs": y_va.to_numpy()}).groupby(bins, observed=True).mean()
    perm = permutation_importance(model, X_va, y_va, scoring="roc_auc", n_repeats=3, random_state=0)
    importance = pd.Series(perm.importances_mean, index=cols).sort_values(ascending=False)
    metrics = {
        "snapshot": str(t0.date()),
        "horizon_days": HORIZON_DAYS,
        "train_rows": int(len(X_tr)),
        "validation_rows": int(len(X_va)),
        "base_rate": float(y.mean()),
        "auc": float(roc_auc_score(y_va, p_va)),
        "brier": float(brier_score_loss(y_va, p_va)),
        "brier_baseline": float(brier_score_loss(y_va, np.full(len(y_va), y_tr.mean()))),
        "calibration": [{"pred": round(r.pred, 3), "obs": round(r.obs, 3)} for r in calibration.itertuples()],
        "top_features": importance.head(10).round(4).to_dict(),
    }
    model = _model().fit(snap[cols], y)  # refit on all labelled rows
    risk = model.predict_proba(snap[cols])[:, 1]
    direction = pd.Series(
        {c: np.sign(spearmanr(snap[c], risk).statistic or 0) for c in cols}, dtype=float
    ).fillna(0)
    return ChurnModel(model, cols, metrics, snap[cols], importance, direction)


def score_churn(churn: ChurnModel, features: pd.DataFrame) -> pd.DataFrame:
    X = features[churn.columns]
    out = pd.DataFrame(
        {"customer_id": features["customer_id"], "churn_60d": churn.model.predict_proba(X)[:, 1]}
    )
    out["churn_reasons"] = reason_codes(churn, X)
    return out


def reason_codes(churn: ChurnModel, X: pd.DataFrame, top: int = 3) -> list[list[str]]:
    """Top risk-raising reasons: important features where the customer sits at the risky end."""
    candidates = [c for c in churn.importance.index[:15] if c in REASONS and churn.importance[c] > 0]
    sorted_ref = {c: np.sort(churn.reference[c].to_numpy()) for c in candidates}
    reasons = []
    for _, row in X.iterrows():
        scored = []
        for c in candidates:
            q = np.searchsorted(sorted_ref[c], row[c], side="right") / len(sorted_ref[c])
            risky_end, template = REASONS[c]
            tail = q if risky_end == "high" else 1 - q
            if churn.direction[c] == (1 if risky_end == "high" else -1) and tail > 0.7:
                scored.append((churn.importance[c] * tail, template(row[c])))
        reasons.append([text for _, text in sorted(scored, reverse=True)[:top]])
    return reasons
