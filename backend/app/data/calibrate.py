"""Turn the sampled real datasets into the statistics the synthetic generator uses.

Outputs (committed, small):
* calibration/calibration.json — fitted parameters + reference distributions, each with its source
* calibration/catalogue.parquet — real Amazon products with Indian ₹ prices and margins
"""

import hashlib
import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import yaml
from scipy.optimize import minimize
from scipy.special import betaln, gammaln

from app.data.ingest_amazon import CATEGORIES
from app.data.paths import (
    AMAZON_RAW_DIR,
    CALIBRATION_FILE,
    CATALOGUE_FILE,
    HILLSTROM_RAW,
    INDIA_FILE,
    RETAIL_RAW,
)

QUANTILES = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]


def load_india() -> dict:
    return yaml.safe_load(INDIA_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- BG/NBD


def bgnbd_neg_loglik(log_params: np.ndarray, x: np.ndarray, t_x: np.ndarray, T: np.ndarray) -> float:
    """Negative log-likelihood of the BG/NBD model (Fader, Hardie & Lee, 2005).

    x = repeat purchases, t_x = time of last purchase, T = observation length (all per customer).
    """
    r, alpha, a, b = np.exp(log_params)
    common = gammaln(r + x) - gammaln(r) + r * np.log(alpha)
    term1 = betaln(a, b + x) - betaln(a, b) + common - (r + x) * np.log(alpha + T)
    with np.errstate(divide="ignore", invalid="ignore"):
        term2 = np.where(
            x > 0,
            betaln(a + 1, b + x - 1) - betaln(a, b) + common - (r + x) * np.log(alpha + t_x),
            -np.inf,
        )
    return -float(np.logaddexp(term1, term2).sum())


def fit_bgnbd(x: np.ndarray, t_x: np.ndarray, T: np.ndarray) -> dict:
    result = minimize(
        bgnbd_neg_loglik,
        x0=np.log([0.5, 10.0, 0.5, 2.0]),
        args=(x, t_x, T),
        method="Nelder-Mead",
        options={"maxiter": 4000, "xatol": 1e-6, "fatol": 1e-6},
    )
    r, alpha, a, b = np.exp(result.x)
    return {"r": r, "alpha": alpha, "a": a, "b": b, "converged": bool(result.success)}


# --------------------------------------------------------------------------- UCI Online Retail II


def retail_customer_summary(retail: pd.DataFrame) -> pd.DataFrame:
    """One row per customer: repeat purchases x, recency t_x and age T, in days (purchase-day level)."""
    clean = retail[
        retail["CustomerID"].notna()
        & ~retail["Invoice"].astype(str).str.startswith("C")
        & (retail["Quantity"] > 0)
        & (retail["Price"] > 0)
    ]
    days = clean.assign(day=clean["InvoiceDate"].dt.normalize())[["CustomerID", "day"]].drop_duplicates()
    end = days["day"].max()
    grouped = days.groupby("CustomerID")["day"]
    first, last, n = grouped.min(), grouped.max(), grouped.nunique()
    return pd.DataFrame(
        {
            "x": (n - 1).astype(float),
            "t_x": (last - first).dt.days.astype(float),
            "T": (end - first).dt.days.astype(float),
        }
    )


def purchase_gaps(days: pd.DataFrame, key: str, time: str) -> np.ndarray:
    ordered = days.sort_values([key, time])
    gaps = ordered.groupby(key)[time].diff().dropna()
    return gaps.to_numpy()


def calibrate_retail() -> dict:
    retail = pd.read_parquet(RETAIL_RAW)
    retail["InvoiceDate"] = pd.to_datetime(retail["InvoiceDate"])
    summary = retail_customer_summary(retail)
    summary = summary[summary["T"] > 0]
    fit = fit_bgnbd(summary["x"].to_numpy(), summary["t_x"].to_numpy(), summary["T"].to_numpy())

    clean = retail[retail["CustomerID"].notna() & ~retail["Invoice"].astype(str).str.startswith("C")]
    orders = clean.groupby("Invoice").agg(customer=("CustomerID", "first"), ts=("InvoiceDate", "min"))
    day_level = orders.assign(day=orders["ts"].dt.normalize())[["customer", "day"]].drop_duplicates()
    gaps = purchase_gaps(day_level, "customer", "day") / np.timedelta64(1, "D")
    orders_per_customer = day_level.groupby("customer").size().to_numpy()

    invoices = retail["Invoice"].astype(str).drop_duplicates()
    return {
        "source": "UCI Online Retail II (UCI dataset 502), transactions 2009-12..2011-12",
        "customers": int(len(summary)),
        "bgnbd_days": fit,
        "repeat_rate": float((summary["x"] > 0).mean()),
        "gap_days_quantiles": dict(
            zip(map(str, QUANTILES), np.quantile(gaps, QUANTILES).round(2), strict=True)
        ),
        "gap_days_mean": float(gaps.mean()),
        "gap_cv": float(gaps.std() / gaps.mean()),
        "orders_per_customer_quantiles": dict(
            zip(map(str, QUANTILES), np.quantile(orders_per_customer, QUANTILES).tolist(), strict=True)
        ),
        "share_single_order": float((orders_per_customer == 1).mean()),
        "cancellation_rate": float(invoices.str.startswith("C").mean()),
        "weekday_share": orders["ts"]
        .dt.dayofweek.value_counts(normalize=True)
        .sort_index()
        .round(4)
        .tolist(),
        "normalised_gaps_sample": np.random.default_rng(0)
        .choice(gaps / gaps.mean(), size=min(4000, len(gaps)), replace=False)
        .round(4)
        .tolist(),
    }


# --------------------------------------------------------------------------- Amazon


def calibrate_amazon_ratings() -> dict:
    per_category = {}
    pooled_counts, pooled_gaps = [], []
    for category in CATEGORIES:
        ratings = pd.read_parquet(AMAZON_RAW_DIR / f"ratings_{category}.parquet")
        ratings["day"] = ratings["timestamp"] / 86_400_000
        counts = ratings.groupby("user_id").size().to_numpy()
        gaps = purchase_gaps(ratings, "user_id", "day")
        gaps = gaps[gaps > 0.5]  # same-day bursts are one shopping trip
        pooled_counts.append(counts)
        pooled_gaps.append(gaps)
        per_category[category] = {
            "users": int(len(counts)),
            "ratings_per_user_median": float(np.median(counts)),
            "gap_days_median": float(np.median(gaps)) if len(gaps) else None,
            "rating_mean": float(ratings["rating"].mean()),
        }
    gaps = np.concatenate(pooled_gaps)
    return {
        "source": "Amazon Reviews 2023 (McAuley Lab), 5-core rating histories, random-slice sample",
        "per_category": per_category,
        "gap_days_quantiles": dict(
            zip(map(str, QUANTILES), np.quantile(gaps, QUANTILES).round(2), strict=True)
        ),
        "gap_cv": float(gaps.std() / gaps.mean()),
        "normalised_gaps_sample": np.random.default_rng(1)
        .choice(gaps / gaps.mean(), size=min(4000, len(gaps)), replace=False)
        .round(4)
        .tolist(),
    }


def category_share() -> dict:
    volumes = json.loads((AMAZON_RAW_DIR / "category_volume_bytes.json").read_text())
    total = sum(volumes.values())
    return {c: round(v / total, 4) for c, v in volumes.items()}


def snap_price(inr: float) -> int:
    """Indian shelf price points: ₹249, ₹1,499, ₹12,999 …"""
    step = 10 if inr < 500 else 50 if inr < 5000 else 100 if inr < 50_000 else 1000
    return int(max(step, round(inr / step) * step) - 1)


def _stable_unit(key: str) -> float:
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def build_catalogue(india: dict) -> pd.DataFrame:
    frames = [pd.read_parquet(AMAZON_RAW_DIR / f"meta_{c}.parquet") for c in CATEGORIES]
    meta = pd.concat(frames, ignore_index=True)
    meta = meta[
        meta["price_usd"].notna()
        & meta["title"].notna()
        & meta["image_url"].notna()
        & (meta["rating_number"].fillna(0) >= 5)
    ].copy()
    factor = meta["category"].map(india["category_price_factor"])
    meta["price_inr"] = (meta["price_usd"] * india["usd_inr"] * factor).map(snap_price)
    meta = meta[(meta["price_inr"] >= 49) & (meta["price_inr"] <= 200_000)]
    meta["margin_pct"] = meta["category"].map(india["category_margin_pct"])
    # Strike-through MRP (an Indian retail convention): a stable per-item markup bounded by the margin.
    markup = meta["parent_asin"].map(_stable_unit) * meta["margin_pct"] / 100 * 1.2
    meta["mrp_inr"] = (meta["price_inr"] * (1 + markup)).map(snap_price).clip(lower=meta["price_inr"])
    meta["subcategory"] = meta["subcategory"].str.split(" > ").str[1].fillna(meta["main_category"])
    meta["title"] = meta["title"].str.slice(0, 140)
    meta["brand"] = meta["brand"].fillna("Generic").str.slice(0, 60)
    columns = [
        "parent_asin",
        "title",
        "brand",
        "category",
        "subcategory",
        "image_url",
        "mrp_inr",
        "price_inr",
        "margin_pct",
        "average_rating",
        "rating_number",
    ]
    catalogue = meta[columns].rename(
        columns={"parent_asin": "product_id", "average_rating": "rating", "rating_number": "rating_count"}
    )
    catalogue["rating_count"] = catalogue["rating_count"].astype(int)
    return catalogue.reset_index(drop=True)


# --------------------------------------------------------------------------- Hillstrom (real RCT)

RECENCY_BUCKETS = [0, 3, 6, 9, 12]
HISTORY_BUCKETS = [0, 100, 300, 700, np.inf]


def calibrate_hillstrom() -> dict:
    df = pd.read_csv(HILLSTROM_RAW)
    df["treated"] = df["segment"] != "No E-Mail"
    df["recency_bucket"] = pd.cut(df["recency"], RECENCY_BUCKETS, labels=["1-3", "4-6", "7-9", "10-12"])
    df["history_bucket"] = pd.cut(
        df["history"], HISTORY_BUCKETS, labels=["<100", "100-300", "300-700", "700+"]
    )

    def effect(frame: pd.DataFrame) -> dict:
        t, c = frame[frame["treated"]], frame[~frame["treated"]]
        return {
            "n": int(len(frame)),
            "conv_control": float(c["conversion"].mean()),
            "conv_treated": float(t["conversion"].mean()),
            "uplift_conv": float(t["conversion"].mean() - c["conversion"].mean()),
            "uplift_visit": float(t["visit"].mean() - c["visit"].mean()),
            "uplift_spend": float(t["spend"].mean() - c["spend"].mean()),
        }

    overall = effect(df)
    # Two-way recency × history cells. Conversions are rare (~1%), so both the control rate and the
    # uplift are shrunk toward the overall values (empirical Bayes, k = 2,000) before taking their ratio.
    cells = {}
    for (rec, hist), frame in df.groupby(["recency_bucket", "history_bucket"], observed=True):
        cell = effect(frame)
        w = cell["n"] / (cell["n"] + 2000)
        cell["uplift_conv_shrunk"] = w * cell["uplift_conv"] + (1 - w) * overall["uplift_conv"]
        cell["conv_control_shrunk"] = w * cell["conv_control"] + (1 - w) * overall["conv_control"]
        relative = cell["uplift_conv_shrunk"] / cell["conv_control_shrunk"]
        cell["relative_lift"] = float(np.clip(relative, -0.5, 3.0))
        cells[f"{rec}|{hist}"] = cell

    return {
        "source": "Kevin Hillstrom MineThatData E-Mail Analytics challenge (2008): 64k customers, randomised",
        "overall": overall,
        "by_segment": {
            col: {str(k): effect(g) for k, g in df.groupby(col, observed=True)}
            for col in ["recency_bucket", "history_bucket", "newbie", "channel", "zip_code"]
        },
        "recency_history_cells": cells,
        "relative_lift_overall": overall["uplift_conv"] / overall["conv_control"],
    }


# --------------------------------------------------------------------------- entry point


def run() -> dict:
    india = load_india()
    catalogue = build_catalogue(india)
    catalogue.to_parquet(CATALOGUE_FILE, index=False)
    print(f"  catalogue {len(catalogue):,} products")
    print(catalogue.groupby("category")["price_inr"].describe()[["count", "50%", "max"]].to_string())

    calibration = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "retail": calibrate_retail(),
        "amazon": calibrate_amazon_ratings(),
        "category_share": category_share(),
        "hillstrom": calibrate_hillstrom(),
        "catalogue": {
            "products": int(len(catalogue)),
            "per_category": catalogue["category"].value_counts().to_dict(),
            "price_inr_quantiles": catalogue.groupby("category")["price_inr"]
            .quantile([0.1, 0.5, 0.9])
            .unstack()
            .rename(columns=str)
            .to_dict(orient="index"),
        },
    }
    CALIBRATION_FILE.write_text(json.dumps(calibration, indent=2, default=float), encoding="utf-8")
    fit = calibration["retail"]["bgnbd_days"]
    print(f"  BG/NBD r={fit['r']:.3f} alpha={fit['alpha']:.2f} a={fit['a']:.3f} b={fit['b']:.3f}")
    print(f"  Hillstrom uplift (conversion) = {calibration['hillstrom']['overall']['uplift_conv']:.4f}")
    return calibration
