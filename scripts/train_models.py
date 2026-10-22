"""Train churn / uplift / CLV models, score every customer, and check against the simulator's ground truth.

Usage (from backend/): uv run python ../scripts/train_models.py [--as-of YYYY-MM-DD]

The ground-truth check reads `sim_hidden_traits`. That is only possible because the data is synthetic,
and it happens here, outside app/, so no model can ever depend on it.
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

from app.analytics.pipeline import run_pipeline, save  # noqa: E402
from app.data.paths import CALIBRATION_DIR  # noqa: E402
from app.data.simulator import offer_effect  # noqa: E402
from app.db import get_engine  # noqa: E402

MODEL_REPORT = CALIBRATION_DIR / "model_report.json"


def ground_truth_check(engine, scores: pd.DataFrame) -> dict:
    truth = pd.read_sql("SELECT * FROM sim_hidden_traits", engine).set_index("customer_id")
    s = scores.set_index("customer_id").join(truth, how="inner")
    s["sleeping_dog"], s["alive"] = s["sleeping_dog"].astype(bool), s["alive"].astype(bool)
    true_pct10 = [
        offer_effect("PCT_OFF", 10, ps, r)
        for ps, r in zip(s["price_sensitivity"], s["offer_responsiveness"], strict=True)
    ]
    true_abs = np.array(true_pct10) * s["p_buy_14d_no_offer"]  # relative effect × baseline ≈ absolute uplift
    return {
        "spearman_pred_vs_true_uplift_pct10": float(spearmanr(s["uplift_PCT_OFF"], true_abs).statistic),
        "spearman_churn_vs_true_dead": float(spearmanr(s["churn_60d"], ~s["alive"]).statistic),
        "mean_pred_uplift_true_sleeping_dogs": float(s.loc[s["sleeping_dog"], "uplift_best"].mean()),
        "mean_pred_uplift_others": float(s.loc[~s["sleeping_dog"], "uplift_best"].mean()),
        "true_sleeping_dogs_flagged_share": float(
            (s.loc[s["sleeping_dog"], "uplift_group"] == "sleeping_dog").mean()
        ),
        "flagged_sleeping_dogs_precision": float(
            s.loc[s["uplift_group"] == "sleeping_dog", "sleeping_dog"].mean()
        )
        if (s["uplift_group"] == "sleeping_dog").any()
        else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 22))
    args = parser.parse_args()
    started = time.perf_counter()
    engine = get_engine()
    scores, report = run_pipeline(engine, datetime.combine(args.as_of, datetime.min.time()))
    report["ground_truth_check"] = ground_truth_check(engine, scores)
    save(engine, scores, report)
    MODEL_REPORT.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")

    churn, uplift = report["churn"], report["uplift"]
    print(
        f"\nChurn: AUC {churn['auc']:.3f}, Brier {churn['brier']:.3f} "
        f"(baseline {churn['brier_baseline']:.3f}), "
        f"base rate {churn['base_rate']:.1%}"
    )
    print("Uplift by arm (14-day purchase rate, treated − control):")
    for arm, a in uplift["arms"].items():
        print(f"  {arm:9} {a['effect']:+.3f}  CI [{a['ci95'][0]:+.3f}, {a['ci95'][1]:+.3f}]")
    any_offer = uplift["holdout"]["ANY_OFFER"]
    print(
        f"Holdout: uplift in model's top 30% {any_offer['uplift_top30']:+.3f} vs overall "
        f"{any_offer['uplift_overall']:+.3f} (×{any_offer['targeting_multiplier']})"
    )
    if "hillstrom_validation" in report:
        h = report["hillstrom_validation"]
        for outcome in ("visit", "conversion"):
            print(
                f"Hillstrom {outcome}: top-30% uplift {h[outcome]['uplift_top30']:+.4f} vs overall "
                f"{h[outcome]['uplift_overall']:+.4f} (×{h[outcome]['targeting_multiplier']})"
            )
    print("Segments:", report["segments"]["uplift_group"])
    print("Lifecycle:", report["segments"]["lifecycle"])
    print(
        "Ground truth:",
        {k: round(v, 3) if isinstance(v, float) else v for k, v in report["ground_truth_check"].items()},
    )
    print(f"Done in {time.perf_counter() - started:.0f}s → customer_scores, model_runs, {MODEL_REPORT.name}")


if __name__ == "__main__":
    main()
