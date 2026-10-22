"""Customer features computed from platform data only (profiles, events, orders, past campaigns).

`build_features(tables, as_of)` uses strictly the rows with timestamps before `as_of`, so the same
function produces point-in-time training snapshots (e.g. at a past campaign's send time) and the
current scoring snapshot without leaking the future.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine

PROFILE_COLUMNS = [
    "customer_id",
    "signup_date",
    "orders_before_window",
    "last_order_before_window",
    "city_tier",
    "device",
    "language_pref",
    "prime_member",
    "preferred_payment",
    "points_balance",
    "points_expiry_date",
    "age_band",
    "acquisition_channel",
    "opted_out",
]
AGE_ORDER = {"18-24": 0, "25-34": 1, "35-44": 2, "45-54": 3, "55+": 4}

# Columns that are identifiers or descriptive, never model inputs. `gender` is deliberately never loaded.
NON_MODEL_COLUMNS = {"customer_id", "top3_categories", "opted_out"}


@dataclass
class Tables:
    customers: pd.DataFrame
    events: pd.DataFrame
    orders: pd.DataFrame
    items: pd.DataFrame
    assignments: pd.DataFrame
    margins: dict[str, float]
    categories: list[str]
    festive: list[tuple[datetime, datetime]]


def load_tables(engine: Engine, festive_calendar: list[dict] | None = None) -> Tables:
    customers = pd.read_sql(f"SELECT {', '.join(PROFILE_COLUMNS)} FROM customers", engine)
    for col in ("signup_date", "last_order_before_window", "points_expiry_date"):
        customers[col] = pd.to_datetime(customers[col])
    events = pd.read_sql(
        "SELECT customer_id, type, ts, session_id, price_inr, points FROM events", engine, parse_dates=["ts"]
    )
    orders = pd.read_sql("SELECT * FROM orders", engine, parse_dates=["ts"])
    items = pd.read_sql(
        "SELECT i.order_id, i.category, i.price_inr, o.customer_id, o.ts FROM order_items i "
        "JOIN orders o USING(order_id)",
        engine,
        parse_dates=["ts"],
    )
    assignments = pd.read_sql(
        "SELECT * FROM campaign_assignments", engine, parse_dates=["sent_at", "valid_until"]
    )
    products = pd.read_sql("SELECT DISTINCT category, margin_pct FROM products", engine)
    festive = [
        (pd.Timestamp(f["start"]), pd.Timestamp(f["end"]) + pd.Timedelta(days=1))
        for f in festive_calendar or []
    ]
    return Tables(
        customers=customers,
        events=events,
        orders=orders,
        items=items,
        assignments=assignments,
        margins=dict(zip(products["category"], products["margin_pct"], strict=True)),
        categories=sorted(products["category"].unique()),
        festive=festive,
    )


def _count(frame: pd.DataFrame, since: datetime, index: pd.Index, col: str = "ts") -> pd.Series:
    return frame[frame[col] >= since].groupby("customer_id").size().reindex(index, fill_value=0)


def build_features(tables: Tables, as_of: datetime) -> pd.DataFrame:
    as_of = pd.Timestamp(as_of)
    c = tables.customers[tables.customers["signup_date"] < as_of].set_index("customer_id")
    idx = c.index
    orders = tables.orders[tables.orders["ts"] < as_of]
    orders = orders.assign(net=orders["gross_inr"] - orders["discount_inr"] - orders["points_redeemed"])
    events = tables.events[
        (tables.events["ts"] < as_of) & (tables.events["ts"] >= as_of - timedelta(days=90))
    ]
    items = tables.items[(tables.items["ts"] < as_of) & (tables.items["ts"] >= as_of - timedelta(days=365))]

    f = pd.DataFrame(index=idx)
    f["tenure_days"] = (as_of - c["signup_date"]).dt.days
    f["city_tier"] = c["city_tier"].str[-1].astype(int)
    f["prime_member"] = c["prime_member"].astype(int)
    f["device_ios"] = (c["device"] == "ios").astype(int)
    f["device_web"] = (c["device"] == "web").astype(int)
    f["prefers_cod"] = (c["preferred_payment"] == "COD").astype(int)
    f["lang_pa"] = (c["language_pref"] == "pa").astype(int)
    f["lang_hi"] = (c["language_pref"] == "hi-Latn").astype(int)
    f["age_band"] = c["age_band"].map(AGE_ORDER)
    f["acq_paid"] = c["acquisition_channel"].isin(["ads", "social"]).astype(int)
    f["opted_out"] = c["opted_out"].astype(int)

    # --- orders
    o365 = orders[orders["ts"] >= as_of - timedelta(days=365)]
    f["orders_30d"] = _count(orders, as_of - timedelta(days=30), idx)
    f["orders_90d"] = _count(orders, as_of - timedelta(days=90), idx)
    f["orders_365d"] = _count(orders, as_of - timedelta(days=365), idx)
    by = o365.groupby("customer_id")
    f["monetary_90d_inr"] = (
        orders[orders["ts"] >= as_of - timedelta(days=90)]
        .groupby("customer_id")["net"]
        .sum()
        .reindex(idx, fill_value=0)
    )
    f["monetary_365d_inr"] = by["net"].sum().reindex(idx, fill_value=0)
    f["aov_inr"] = by["net"].mean().reindex(idx).fillna(0)
    f["orders_lifetime"] = c["orders_before_window"] + orders.groupby("customer_id").size().reindex(
        idx, fill_value=0
    )
    last_db = orders.groupby("customer_id")["ts"].max().reindex(idx)
    last = last_db.fillna(c["last_order_before_window"])
    f["never_bought"] = last.isna().astype(int)
    f["recency_days"] = (as_of - last).dt.days.fillna(f["tenure_days"])
    f["return_rate"] = by["status"].apply(lambda s: (s == "returned").mean()).reindex(idx).fillna(0)
    f["rto_count_365d"] = by["status"].apply(lambda s: (s == "rto").sum()).reindex(idx).fillna(0)
    f["cod_share"] = (
        by["payment_mode"].apply(lambda s: (s == "COD").mean()).reindex(idx).fillna(f["prefers_cod"])
    )
    f["coupon_share"] = by["coupon_code"].apply(lambda s: s.notna().mean()).reindex(idx).fillna(0)
    discount = by.apply(
        lambda g: (g["discount_inr"].sum() + g["points_redeemed"].sum()) / max(g["gross_inr"].sum(), 1),
        include_groups=False,
    )
    f["discount_dependency"] = discount.reindex(idx).fillna(0)
    if tables.festive:
        festive = np.zeros(len(o365), dtype=bool)
        for start, end in tables.festive:
            festive |= ((o365["ts"] >= start) & (o365["ts"] < end)).to_numpy()
        f["festive_order_share"] = (
            o365.assign(festive=festive).groupby("customer_id")["festive"].mean().reindex(idx).fillna(0)
        )

    # --- engagement (last 90 days of events)
    sessions = events[events["type"] == "session_start"]
    f["sessions_7d"] = _count(sessions, as_of - timedelta(days=7), idx)
    f["sessions_30d"] = _count(sessions, as_of - timedelta(days=30), idx)
    f["sessions_90d"] = _count(sessions, as_of - timedelta(days=90), idx)
    f["views_30d"] = _count(events[events["type"] == "view"], as_of - timedelta(days=30), idx)
    f["searches_30d"] = _count(events[events["type"] == "search"], as_of - timedelta(days=30), idx)
    last_session = sessions.groupby("customer_id")["ts"].max().reindex(idx)
    f["days_since_last_session"] = (as_of - last_session).dt.days.fillna(90)

    carts = events[events["type"].isin(["add_to_cart", "remove_from_cart", "order"])]
    per_session = carts.groupby(["customer_id", "session_id"]).agg(
        ts=("ts", "max"),
        added=("type", lambda s: (s == "add_to_cart").any()),
        ordered=("type", lambda s: (s == "order").any()),
    )
    cart_value = (
        carts.assign(
            v=np.where(
                carts["type"] == "add_to_cart", 1, np.where(carts["type"] == "remove_from_cart", -1, 0)
            )
            * carts["price_inr"].fillna(0)
        )
        .groupby(["customer_id", "session_id"])["v"]
        .sum()
    )
    per_session["value"] = cart_value.reindex(per_session.index).fillna(0)
    per_session = per_session.reset_index()
    recent = per_session[(per_session["ts"] >= as_of - timedelta(days=30)) & per_session["added"]]
    abandoned = recent[~recent["ordered"]]
    f["cart_sessions_30d"] = recent.groupby("customer_id").size().reindex(idx, fill_value=0)
    f["cart_abandon_rate_30d"] = (
        abandoned.groupby("customer_id").size().reindex(idx, fill_value=0)
        / f["cart_sessions_30d"].replace(0, np.nan)
    ).fillna(0)
    open_carts = abandoned[abandoned["ts"] >= as_of - timedelta(days=7)]
    f["open_cart_value_7d_inr"] = (
        open_carts.groupby("customer_id")["value"].sum().clip(lower=0).reindex(idx).fillna(0)
    )
    last_abandon = abandoned.groupby("customer_id")["ts"].max().reindex(idx)
    f["hours_since_cart_abandon"] = (
        ((as_of - last_abandon).dt.total_seconds() / 3600).fillna(24 * 30).clip(upper=720)
    )

    # --- points (balance reconstructed at as_of from the final balance and later activity)
    later = tables.events[tables.events["ts"] >= as_of]
    earned_after = (
        later[later["type"] == "points_earned"].groupby("customer_id")["points"].sum()
        if "points" in later
        else pd.Series(dtype=float)
    )
    f["points_balance"] = c["points_balance"]
    if not earned_after.empty:
        f["points_balance"] = (f["points_balance"] - earned_after.reindex(idx).fillna(0)).clip(lower=0)
    expiry = c["points_expiry_date"]
    f["points_expiring_30d"] = np.where(
        expiry.notna() & (expiry <= as_of + timedelta(days=30)), f["points_balance"], 0
    )

    # --- category spend mix (365d)
    spend = items.pivot_table(index="customer_id", columns="category", values="price_inr", aggfunc="sum")
    spend = spend.reindex(index=idx, columns=tables.categories).fillna(0)
    share = spend.div(spend.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    for cat in tables.categories:
        f[f"cat_{cat}"] = share[cat]
    margins = pd.Series(tables.margins).reindex(tables.categories)
    f["weighted_margin_pct"] = (share * margins).sum(axis=1).where(share.sum(axis=1) > 0, margins.mean())
    f["top3_categories"] = [
        json.dumps([k for k, v in row.nlargest(3).items() if v > 0]) for _, row in share.iterrows()
    ]

    # --- past campaign exposure
    a = tables.assignments[(tables.assignments["sent_at"] < as_of) & tables.assignments["offer_type"].notna()]
    f["offers_received_180d"] = _count(a, as_of - timedelta(days=180), idx, col="sent_at")
    last_offer = a.groupby("customer_id")["sent_at"].max().reindex(idx)
    f["days_since_last_offer"] = (as_of - last_offer).dt.days.fillna(365)
    redeemed = orders["coupon_code"].fillna("").str.startswith("CMP-")
    f["campaign_redemptions"] = orders[redeemed].groupby("customer_id").size().reindex(idx, fill_value=0)

    return f.reset_index()


def model_columns(features: pd.DataFrame) -> list[str]:
    return [c for c in features.columns if c not in NON_MODEL_COLUMNS]
