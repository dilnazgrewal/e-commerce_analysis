"""Uplift models: how much more likely is this customer to buy *because of* an offer?

Two parts:
1. `validate_on_hillstrom` — the method is first proven on a real randomised experiment (Hillstrom, 64k
   customers): T-learner trained on 70%, Qini evaluation on the held-out 30%.
2. `train_uplift` — the same method on the platform's own past randomised campaigns. Training rows are
   (customer, campaign) pairs with features as of the send time and outcome = ordered during the 14-day
   validity window. Per-arm effects are noisy with ~1k customers per arm, so each arm's T-learner
   estimate is blended with the pooled "any offer" estimate rescaled to that arm's measured average effect.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import train_test_split

from app.analytics.features import Tables, build_features, model_columns

ARM_OFFER_TYPE = {
    "nudge": "CART_NUDGE",
    "points2x": "BONUS_POINTS",
    "freeship": "FREE_SHIPPING",
    "pct10": "PCT_OFF",
    "flat100": "FLAT_OFF",
}


def _classifier(large: bool = False) -> HistGradientBoostingClassifier:
    # Small by default: uplift is a difference of two noisy predictions, so variance matters more than
    # bias at ~1–2k rows per arm. Chosen against the simulator ground truth (holdout targeting ×2.5 vs ×2.1
    # for depth-3 trees). Tens of thousands of rows (Hillstrom) support a larger model.
    if large:
        return HistGradientBoostingClassifier(
            max_iter=200,
            learning_rate=0.05,
            max_depth=3,
            min_samples_leaf=200,
            l2_regularization=2.0,
            random_state=0,
        )
    return HistGradientBoostingClassifier(
        max_iter=60,
        learning_rate=0.05,
        max_depth=2,
        min_samples_leaf=100,
        l2_regularization=5.0,
        random_state=0,
    )


# --------------------------------------------------------------------------- evaluation


def qini(uplift: np.ndarray, treated: np.ndarray, outcome: np.ndarray, points: int = 10) -> dict:
    """Qini curve at deciles + summary: how many extra conversions targeting by model rank buys vs random."""
    order = np.argsort(-uplift)
    t, y = treated[order].astype(bool), outcome[order].astype(float)
    n = len(order)
    curve = []
    for k in np.linspace(0, n, points + 1).astype(int):
        nt, nc = t[:k].sum(), (~t[:k]).sum()
        yt, yc = y[:k][t[:k]].sum(), y[:k][~t[:k]].sum()
        curve.append(float(yt - yc * nt / nc) if nc else 0.0)
    total = curve[-1]
    random_line = [total * i / points for i in range(points + 1)]
    gain = float(np.mean(np.array(curve) - np.array(random_line)))
    top = order[: max(1, int(0.3 * n))]
    tt = treated[top].astype(bool)
    top30 = float(outcome[top][tt].mean() - outcome[top][~tt].mean()) if tt.any() and (~tt).any() else 0.0
    overall = float(outcome[treated.astype(bool)].mean() - outcome[~treated.astype(bool)].mean())
    return {
        "qini_curve": [round(v, 2) for v in curve],
        "random_curve": [round(v, 2) for v in random_line],
        "qini_gain": round(gain, 3),
        "uplift_top30": round(top30, 4),
        "uplift_overall": round(overall, 4),
        "targeting_multiplier": round(top30 / overall, 2) if overall > 0 else None,
    }


def ate(outcome: np.ndarray, treated: np.ndarray) -> dict:
    t, c = outcome[treated], outcome[~treated]
    diff = t.mean() - c.mean()
    se = np.sqrt(t.var(ddof=1) / len(t) + c.var(ddof=1) / len(c))
    return {
        "effect": float(diff),
        "ci95": [float(diff - 1.96 * se), float(diff + 1.96 * se)],
        "treated_rate": float(t.mean()),
        "control_rate": float(c.mean()),
        "n_treated": int(len(t)),
        "n_control": int(len(c)),
    }


# --------------------------------------------------------------------------- Hillstrom validation


def validate_on_hillstrom(path) -> dict:
    df = pd.read_csv(path)
    X = pd.get_dummies(
        df[["recency", "history", "mens", "womens", "newbie", "zip_code", "channel"]], dtype=int
    )
    treated = (df["segment"] != "No E-Mail").to_numpy()
    results = {}
    for outcome in ("visit", "conversion"):
        y = df[outcome].to_numpy()
        idx_tr, idx_te = train_test_split(np.arange(len(df)), test_size=0.3, random_state=0, stratify=treated)
        tr_t, tr_c = idx_tr[treated[idx_tr]], idx_tr[~treated[idx_tr]]
        m_t = _classifier(large=True).fit(X.iloc[tr_t], y[tr_t])
        m_c = _classifier(large=True).fit(X.iloc[tr_c], y[tr_c])
        pred = m_t.predict_proba(X.iloc[idx_te])[:, 1] - m_c.predict_proba(X.iloc[idx_te])[:, 1]
        results[outcome] = {
            **qini(pred, treated[idx_te], y[idx_te]),
            "ate_test": ate(y[idx_te].astype(float), treated[idx_te]),
        }
    return {"source": "Hillstrom MineThatData (real RCT), 70/30 split", "rows": int(len(df)), **results}


# --------------------------------------------------------------------------- platform campaigns


def campaign_training_rows(tables: Tables) -> pd.DataFrame:
    """One row per (campaign, customer): point-in-time features + arm + 14-day outcome."""
    frames = []
    for campaign_id, a in tables.assignments.groupby("campaign_id"):
        sent, until = a["sent_at"].iloc[0], a["valid_until"].iloc[0]
        feats = build_features(tables, sent)
        window = tables.orders[(tables.orders["ts"] >= sent) & (tables.orders["ts"] < until)]
        bought = set(window["customer_id"])
        rows = a[["customer_id", "arm"]].merge(feats, on="customer_id", how="inner")
        rows["campaign_id"] = campaign_id
        rows["outcome"] = rows["customer_id"].isin(bought).astype(int)
        frames.append(rows)
    return pd.concat(frames, ignore_index=True)


@dataclass
class UpliftModel:
    control: HistGradientBoostingClassifier
    pooled: HistGradientBoostingClassifier
    arms: dict[str, HistGradientBoostingClassifier]
    arm_scale: dict[str, float]
    columns: list[str]
    metrics: dict

    def baseline(self, X: pd.DataFrame) -> np.ndarray:
        return self.control.predict_proba(X[self.columns])[:, 1]

    def predict_any(self, X: pd.DataFrame) -> np.ndarray:
        """Uplift of "some offer" vs none (pooled model): the targeting score used for ranking."""
        Xc = X[self.columns]
        return self.pooled.predict_proba(Xc)[:, 1] - self.control.predict_proba(Xc)[:, 1]

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """Absolute uplift in 14-day purchase probability, per offer type."""
        Xc = X[self.columns]
        p0 = self.control.predict_proba(Xc)[:, 1]
        pooled = self.pooled.predict_proba(Xc)[:, 1] - p0
        out = {}
        for arm, model in self.arms.items():
            own = model.predict_proba(Xc)[:, 1] - p0
            out[ARM_OFFER_TYPE[arm]] = 0.5 * own + 0.5 * pooled * self.arm_scale[arm]
        return pd.DataFrame(out, index=X.index)


def train_uplift(tables: Tables) -> UpliftModel:
    rows = campaign_training_rows(tables)
    cols = model_columns(rows.drop(columns=["arm", "campaign_id", "outcome"]))
    treated = (rows["arm"] != "control").to_numpy()
    y = rows["outcome"].to_numpy()

    # Held-out evaluation first (customers split so nobody is in both sides).
    customers = rows["customer_id"].unique()
    test_customers = set(
        np.random.default_rng(0).choice(customers, size=int(0.3 * len(customers)), replace=False)
    )
    is_test = rows["customer_id"].isin(test_customers).to_numpy()

    def fit(mask: np.ndarray):
        control = _classifier().fit(rows.loc[mask & ~treated, cols], y[mask & ~treated])
        pooled = _classifier().fit(rows.loc[mask & treated, cols], y[mask & treated])
        arms, scale = {}, {}
        pooled_effect = y[mask & treated].mean() - y[mask & ~treated].mean()
        for arm in ARM_OFFER_TYPE:
            in_arm = mask & (rows["arm"] == arm).to_numpy()
            arms[arm] = _classifier().fit(rows.loc[in_arm, cols], y[in_arm])
            effect = y[in_arm].mean() - y[mask & ~treated].mean()
            scale[arm] = float(np.clip(effect / pooled_effect, 0, 3)) if pooled_effect > 0 else 1.0
        return UpliftModel(control, pooled, arms, scale, cols, {})

    held = fit(~is_test)
    test = rows[is_test]
    preds = held.predict(test)
    evaluation = {}
    for arm, offer_type in ARM_OFFER_TYPE.items():
        m = (test["arm"].isin([arm, "control"])).to_numpy()
        evaluation[offer_type] = {
            **qini(
                preds[offer_type].to_numpy()[m],
                (test["arm"] == arm).to_numpy()[m],
                test["outcome"].to_numpy()[m],
            ),
            "ate": ate(y[is_test][m].astype(float), (test["arm"] == arm).to_numpy()[m]),
        }
    evaluation["ANY_OFFER"] = qini(held.predict_any(test), treated[is_test], y[is_test])

    final = fit(np.ones(len(rows), dtype=bool))
    final.metrics = {
        "training_rows": int(len(rows)),
        "campaigns": sorted(rows["campaign_id"].unique().tolist()),
        "arms": {
            arm: ate(
                y[(rows["arm"] == arm).to_numpy() | ~treated].astype(float),
                (rows["arm"] == arm).to_numpy()[(rows["arm"] == arm).to_numpy() | ~treated],
            )
            for arm in ARM_OFFER_TYPE
        },
        "arm_scale": final.arm_scale,
        "holdout": evaluation,
    }
    return final
