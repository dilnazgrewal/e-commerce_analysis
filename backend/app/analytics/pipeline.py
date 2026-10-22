"""Train all models and score every customer as of a date.

Writes:
* `customer_scores` — one row per customer: features + churn + uplift per offer type + CLV + segments
* `model_runs` — one row per training run with the full metrics report (JSON)
"""

import json
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import Engine, text

from app.analytics.churn import score_churn, train_churn
from app.analytics.clv import score_clv
from app.analytics.features import build_features, load_tables
from app.analytics.segments import lifecycle, uplift_group, uplift_thresholds
from app.analytics.uplift import train_uplift, validate_on_hillstrom
from app.data.calibrate import load_india
from app.data.paths import HILLSTROM_RAW


def run_pipeline(engine: Engine, as_of: datetime, log=print) -> tuple[pd.DataFrame, dict]:
    tables = load_tables(engine, load_india()["festive_calendar"])
    log("features (now)…")
    features = build_features(tables, as_of)

    log("churn model…")
    churn_model = train_churn(tables, as_of)
    churn = score_churn(churn_model, features)

    log("uplift models (past campaigns)…")
    uplift_model = train_uplift(tables)
    uplift = uplift_model.predict(features)
    baseline = uplift_model.baseline(features)

    log("CLV (BG/NBD)…")
    clv, clv_report = score_clv(tables, as_of, features)

    scores = features.merge(churn, on="customer_id").merge(clv, on="customer_id")
    scores["p_buy_14d_no_offer"] = baseline
    for offer_type in uplift.columns:
        scores[f"uplift_{offer_type}"] = uplift[offer_type].to_numpy()
    scores["best_offer_type"] = uplift.idxmax(axis=1).to_numpy()
    scores["uplift_best"] = uplift.max(axis=1).to_numpy()
    thresholds = uplift_thresholds(scores["uplift_best"].to_numpy(), baseline)
    scores["uplift_group"] = uplift_group(scores["uplift_best"].to_numpy(), baseline, thresholds)
    scores["lifecycle"] = lifecycle(scores, scores["churn_60d"])
    scores["churn_reasons"] = scores["churn_reasons"].map(json.dumps)
    scores["scored_at"] = pd.Timestamp(as_of)

    report = {
        "as_of": str(pd.Timestamp(as_of).date()),
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "churn": churn_model.metrics,
        "uplift": uplift_model.metrics,
        "clv": clv_report,
        "segments": {
            "thresholds": thresholds,
            "uplift_group": scores["uplift_group"].value_counts().to_dict(),
            "lifecycle": scores["lifecycle"].value_counts().to_dict(),
            "best_offer_type": scores["best_offer_type"].value_counts().to_dict(),
        },
    }
    if HILLSTROM_RAW.exists():
        log("uplift method validation on Hillstrom…")
        report["hillstrom_validation"] = validate_on_hillstrom(HILLSTROM_RAW)
    return scores, report


def save(engine: Engine, scores: pd.DataFrame, report: dict) -> None:
    scores.to_sql("customer_scores", engine, if_exists="replace", index=False, chunksize=5000)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS model_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "as_of TEXT, trained_at TEXT, report TEXT)"
            )
        )
        conn.execute(
            text("INSERT INTO model_runs (as_of, trained_at, report) VALUES (:a, :t, :r)"),
            {"a": report["as_of"], "t": report["trained_at"], "r": json.dumps(report, default=float)},
        )
