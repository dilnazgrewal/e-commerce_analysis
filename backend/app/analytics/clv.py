"""Customer lifetime value: BG/NBD (refitted on the platform's own orders) × shrunk order value × margin.

P(alive) is the exact BG/NBD posterior. Expected purchases over the next t days use the posterior mean
purchase rate (r + x) / (alpha + T) scaled by P(alive) — a close, numerically stable approximation that
avoids the hypergeometric term (which is unstable when the fitted a < 1).
"""

from datetime import datetime

import numpy as np
import pandas as pd

from app.analytics.features import Tables
from app.data.calibrate import fit_bgnbd

AOV_SHRINK_ORDERS = 3  # pseudo-orders pulling a customer's average order value toward the population mean


def rfm_inputs(tables: Tables, as_of: datetime) -> pd.DataFrame:
    """x (repeat purchases), t_x (days from first to last purchase), T (days since first purchase)."""
    as_of = pd.Timestamp(as_of)
    c = tables.customers[tables.customers["signup_date"] < as_of].set_index("customer_id")
    orders = tables.orders[tables.orders["ts"] < as_of]
    days = orders.assign(day=orders["ts"].dt.normalize()).groupby("customer_id")["day"]
    first_db, last_db, n_db = days.min(), days.max(), days.nunique()
    n_pre = c["orders_before_window"]
    # Customers with pre-window orders started buying around signup; otherwise at their first DB order.
    first = pd.Series(np.where(n_pre > 0, c["signup_date"], first_db.reindex(c.index)), index=c.index)
    first = pd.to_datetime(first)
    last = last_db.reindex(c.index).fillna(c["last_order_before_window"])
    total = n_pre + n_db.reindex(c.index, fill_value=0)
    out = pd.DataFrame(index=c.index)
    out["x"] = (total - 1).clip(lower=0).astype(float)
    out["t_x"] = (last - first).dt.days.clip(lower=0).fillna(0).astype(float)
    out["T"] = (as_of - first).dt.days.astype(float)
    out["has_purchased"] = total > 0
    return out


def p_alive(params: dict, x: np.ndarray, t_x: np.ndarray, T: np.ndarray) -> np.ndarray:
    r, alpha, a, b = params["r"], params["alpha"], params["a"], params["b"]
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        ratio = (a / (b + x - 1)) * ((alpha + T) / (alpha + t_x)) ** (r + x)
    return np.where(x > 0, 1 / (1 + np.nan_to_num(ratio, posinf=1e12)), 1.0)


def score_clv(
    tables: Tables, as_of: datetime, features: pd.DataFrame, horizon_days: int = 90
) -> tuple[pd.DataFrame, dict]:
    rfm = rfm_inputs(tables, as_of)
    buyers = rfm[rfm["has_purchased"] & (rfm["T"] > 0)]
    params = fit_bgnbd(buyers["x"].to_numpy(), buyers["t_x"].to_numpy(), buyers["T"].to_numpy())

    f = features.set_index("customer_id")
    rfm = rfm.reindex(f.index)
    alive = p_alive(params, rfm["x"].to_numpy(), rfm["t_x"].to_numpy(), rfm["T"].to_numpy())
    alive = np.where(rfm["has_purchased"], alive, 0.5)  # registered but never bought: uninformative
    # Never-bought customers have no purchase history: fall back to the population prior rate r / alpha.
    rate = ((params["r"] + rfm["x"]) / (params["alpha"] + rfm["T"].clip(lower=1))).where(
        rfm["has_purchased"], params["r"] / params["alpha"]
    )
    expected = alive * rate * horizon_days

    population_aov = float(f.loc[f["orders_365d"] > 0, "aov_inr"].mean())
    n = f["orders_365d"]
    aov = (n * f["aov_inr"] + AOV_SHRINK_ORDERS * population_aov) / (n + AOV_SHRINK_ORDERS)
    aov = aov.fillna(population_aov)
    out = pd.DataFrame(
        {
            "p_alive": alive,
            "expected_orders_90d": expected.to_numpy(),
            "clv_90d_inr": (expected * aov).round(0).to_numpy(),
            "clv_margin_90d_inr": (expected * aov * f["weighted_margin_pct"] / 100).round(0).to_numpy(),
        },
        index=f.index,
    ).reset_index()
    return out, {"bgnbd_days": params, "population_aov_inr": population_aov, "horizon_days": horizon_days}
