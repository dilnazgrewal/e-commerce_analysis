"""Build engine inputs from the database: customer contexts (scores + profile) and offer history."""

import json
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import Engine, inspect, text

from app.rules.engine import CustomerContext, History, PastOffer

UPLIFT_PREFIX = "uplift_"


def load_contexts(engine: Engine, customer_ids: list[str] | None = None) -> dict[str, CustomerContext]:
    where, params = "", {}
    if customer_ids:
        keys = [f":c{i}" for i in range(len(customer_ids))]
        where = f"WHERE s.customer_id IN ({', '.join(keys)})"
        params = {f"c{i}": cid for i, cid in enumerate(customer_ids)}
    frame = pd.read_sql(
        text(
            "SELECT s.*, c.name, c.language_pref, c.opted_out AS profile_opted_out, c.points_expiry_date "
            f"FROM customer_scores s JOIN customers c USING(customer_id) {where}"
        ),
        engine,
        params=params,
    )
    uplift_cols = [
        c for c in frame.columns if c.startswith(UPLIFT_PREFIX) and c[len(UPLIFT_PREFIX) :].isupper()
    ]
    contexts = {}
    for row in frame.itertuples(index=False):
        r = row._asdict()
        scored_at = pd.Timestamp(r["scored_at"]).to_pydatetime()
        last_order = None if r["never_bought"] else scored_at - timedelta(days=float(r["recency_days"]))
        top = json.loads(r["top3_categories"] or "[]")
        contexts[r["customer_id"]] = CustomerContext(
            customer_id=r["customer_id"],
            name=r["name"],
            language=r["language_pref"],
            lifecycle=r["lifecycle"],
            uplift_group=r["uplift_group"],
            churn_60d=float(r["churn_60d"]),
            p_buy=float(r["p_buy_14d_no_offer"]),
            uplift_any=float(r.get("uplift_any", r["uplift_best"])),
            uplift_by_type={c[len(UPLIFT_PREFIX) :]: float(r[c]) for c in uplift_cols},
            clv_margin_90d_inr=float(r["clv_margin_90d_inr"] or 0),
            quarterly_margin_inr=float(r["monetary_365d_inr"]) / 4 * float(r["weighted_margin_pct"]) / 100,
            aov_inr=float(r["aov_inr"] or 0),
            weighted_margin_pct=float(r["weighted_margin_pct"]),
            top_category=top[0] if top else None,
            tenure_days=int(r["tenure_days"]),
            opted_out=bool(r["profile_opted_out"]),
            prefers_cod=bool(r["prefers_cod"]),
            points_expiring_30d=int(r["points_expiring_30d"] or 0),
            last_order_at=last_order,
        )
    return contexts


def load_history(engine: Engine, customer_id: str, now: datetime) -> History:
    """This customer's offers in the last 30 days, plus expected cost committed today (all customers)."""
    if not inspect(engine).has_table("offer_decisions"):
        return History()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT created_at, valid_until, offer_type, status FROM offer_decisions "
                "WHERE customer_id = :c AND created_at >= :since"
            ),
            {"c": customer_id, "since": now - timedelta(days=30)},
        ).all()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = conn.execute(
            text(
                "SELECT COALESCE(SUM(expected_cost_inr), 0) FROM offer_decisions "
                "WHERE status IN ('sent', 'claimed') AND created_at >= :d"
            ),
            {"d": day_start},
        ).scalar()
    offers = [
        PastOffer(
            pd.Timestamp(r[0]).to_pydatetime(),
            pd.Timestamp(r[1]).to_pydatetime() if r[1] else pd.Timestamp(r[0]).to_pydatetime(),
            r[2] or "",
            r[3],
        )
        for r in rows
    ]
    return History(offers, float(spent or 0))
