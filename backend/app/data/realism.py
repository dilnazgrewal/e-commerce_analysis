"""Realism report: does the synthetic population behave like the real datasets it was calibrated on?

Each check compares a synthetic statistic with its real counterpart and records pass/fail against a
threshold. Distribution shapes are compared after normalising by the mean (Indian consumer purchase
frequency is deliberately rescaled; the *shape* of heterogeneity must still match the real data).
"""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

BINS = np.linspace(0, 5, 26)


def _hist(values: np.ndarray) -> list[float]:
    counts, _ = np.histogram(np.clip(values, 0, BINS[-1]), bins=BINS)
    return (counts / max(counts.sum(), 1)).round(4).tolist()


def _check(name: str, synthetic: float, real: float, stat: float, threshold: float, detail: str) -> dict:
    return {
        "name": name,
        "synthetic": round(float(synthetic), 4),
        "real": round(float(real), 4),
        "statistic": round(float(stat), 4),
        "threshold": threshold,
        "passed": bool(stat <= threshold),
        "detail": detail,
    }


def order_gaps(orders: pd.DataFrame) -> np.ndarray:
    days = orders.assign(day=pd.to_datetime(orders["ts"]).dt.normalize())[
        ["customer_id", "day"]
    ].drop_duplicates()
    days = days.sort_values(["customer_id", "day"])
    gaps = days.groupby("customer_id")["day"].diff().dropna() / np.timedelta64(1, "D")
    return gaps.to_numpy()


def build_report(
    customers: pd.DataFrame,
    events: pd.DataFrame,
    orders: pd.DataFrame,
    items: pd.DataFrame,
    catalogue: pd.DataFrame,
    calibration: dict,
    india: dict,
) -> dict:
    checks, charts = [], {}
    retail, amazon = calibration["retail"], calibration["amazon"]

    # 1. Shape of time between purchases vs real retail transactions and real Amazon histories
    gaps = order_gaps(orders)
    synth_norm = gaps / gaps.mean()
    real_retail = np.array(retail["normalised_gaps_sample"])
    real_amazon = np.array(amazon["normalised_gaps_sample"])
    ks_retail = ks_2samp(synth_norm, real_retail).statistic
    checks.append(
        _check(
            "purchase_gap_shape_vs_uci",
            synth_norm.std(),
            real_retail.std(),
            ks_retail,
            0.15,
            "KS distance between mean-normalised inter-purchase gaps (UCI Online Retail II)",
        )
    )
    ks_amazon = ks_2samp(synth_norm, real_amazon).statistic
    checks.append(
        _check(
            "purchase_gap_shape_vs_amazon",
            synth_norm.std(),
            real_amazon.std(),
            ks_amazon,
            0.20,
            "KS distance between mean-normalised gaps (Amazon Reviews 2023 user histories)",
        )
    )
    charts["purchase_gaps"] = {
        "bins": BINS.round(2).tolist(),
        "synthetic": _hist(synth_norm),
        "uci_real": _hist(real_retail),
        "amazon_real": _hist(real_amazon),
    }

    # 2. Repeat-purchase rate among customers who bought in the window vs real
    per_customer = orders.groupby("customer_id").size()
    repeat = float((per_customer > 1).mean())
    checks.append(
        _check(
            "repeat_purchase_rate",
            repeat,
            retail["repeat_rate"],
            abs(repeat - retail["repeat_rate"]),
            0.12,
            "Share of buyers with 2+ orders (synthetic 12-month window vs UCI)",
        )
    )

    # 3. Category mix of orders vs real Amazon category volumes
    synth_mix = items["category"].value_counts(normalize=True)
    real_mix = pd.Series(calibration["category_share"])
    tvd = 0.5 * (synth_mix.reindex(real_mix.index, fill_value=0) - real_mix).abs().sum()
    checks.append(
        _check(
            "category_mix",
            1 - tvd,
            1.0,
            tvd,
            0.08,
            "Total-variation distance between order category mix and Amazon category volumes",
        )
    )
    charts["category_mix"] = {
        "categories": real_mix.index.tolist(),
        "synthetic": synth_mix.reindex(real_mix.index, fill_value=0).round(4).tolist(),
        "amazon_real": real_mix.round(4).tolist(),
    }

    # 4. Item price distribution vs real catalogue (popularity-weighted, log scale)
    weights = catalogue["rating_count"].to_numpy(dtype=float) ** 0.7
    rng = np.random.default_rng(0)
    real_prices = rng.choice(catalogue["price_inr"].to_numpy(), size=20_000, p=weights / weights.sum())
    ks_price = ks_2samp(np.log(items["price_inr"]), np.log(real_prices)).statistic
    checks.append(
        _check(
            "item_price_distribution",
            items["price_inr"].median(),
            np.median(real_prices),
            ks_price,
            0.08,
            "KS on log ₹ price: purchased items vs popularity-weighted real catalogue",
        )
    )

    # 5. Funnel: cart abandonment and session conversion
    carts = events[events["type"] == "add_to_cart"]
    cart_sessions = carts["session_id"].nunique()
    order_sessions = events.loc[events["type"] == "order", "session_id"].nunique()
    abandonment = 1 - order_sessions / max(cart_sessions, 1)
    target = india["funnel"]["cart_abandonment_rate"]
    checks.append(
        _check(
            "cart_abandonment_rate",
            abandonment,
            target,
            abs(abandonment - target),
            0.05,
            "Share of sessions with an add-to-cart that end without an order",
        )
    )
    sessions = events["session_id"][events["type"] == "session_start"].nunique()
    conversion = order_sessions / max(sessions, 1)
    expected = 1 / india["funnel"]["sessions_per_order"]
    checks.append(
        _check(
            "session_conversion", conversion, expected, abs(conversion - expected), 0.04, "Orders per session"
        )
    )

    # 6. Returns and RTO
    rto_cod = (orders["status"] == "rto")[orders["payment_mode"] == "COD"].mean()
    checks.append(
        _check(
            "cod_rto_rate",
            rto_cod,
            india["cod_rto_rate"],
            abs(rto_cod - india["cod_rto_rate"]),
            0.03,
            "COD orders returned to origin",
        )
    )

    # 7. Activity spread: share of customers active (any order) in the last 90 days
    last_ts = pd.to_datetime(orders["ts"]).max()
    recent = orders[pd.to_datetime(orders["ts"]) >= last_ts - pd.Timedelta(days=90)]["customer_id"].nunique()
    summary = {
        "customers": int(len(customers)),
        "events": int(len(events)),
        "orders": int(len(orders)),
        "buyers_in_window": int(per_customer.size),
        "active_last_90d_share": round(recent / len(customers), 4),
        "gmv_inr": int(orders["gross_inr"].sum()),
        "median_order_inr": int(orders["gross_inr"].median()),
        "event_types": events["type"].value_counts().to_dict(),
        "city_tier_mix": customers["city_tier"].value_counts(normalize=True).round(3).to_dict(),
        "payment_mix": orders["payment_mode"].value_counts(normalize=True).round(3).to_dict(),
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "summary": summary,
        "charts": charts,
    }
