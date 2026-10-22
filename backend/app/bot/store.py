"""Database access for the Telegram bot: bot users, live events, offer delivery (layer L5) and feedback."""

import json
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import Engine, inspect, text
from sqlmodel import Session

from app.db import init_db
from app.models import BotUser, Feedback
from app.rules.decide import Decision
from app.rules.schema import Policy

_SEND_LOCK = threading.Lock()  # one bot process: serialises the send-time re-check + insert (L5)


def ensure_schema(engine: Engine) -> None:
    """Create missing tables and add columns introduced after the first seed (no data is dropped)."""
    init_db(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("offer_decisions")}
    with engine.begin() as conn:
        for name in ("channel", "idempotency_key"):
            if name not in columns:
                conn.execute(text(f"ALTER TABLE offer_decisions ADD COLUMN {name} VARCHAR"))


# --------------------------------------------------------------------------- bot users


def get_user(engine: Engine, chat_id: int) -> BotUser | None:
    with Session(engine) as s:
        return s.get(BotUser, chat_id)


def save_user(engine: Engine, user: BotUser) -> BotUser:
    with Session(engine) as s:
        merged = s.merge(user)
        s.commit()
        s.refresh(merged)
        return merged


def bind_twin(engine: Engine, chat_id: int, customer_id: str, language: str) -> None:
    """The twin's history becomes the bot user's; their chosen language replaces the twin's."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE customers SET telegram_chat_id = NULL WHERE telegram_chat_id = :c"), {"c": chat_id}
        )
        conn.execute(
            text(
                "UPDATE customers SET telegram_chat_id = :c, language_pref = :l, opted_out = 0 "
                "WHERE customer_id = :id"
            ),
            {"c": chat_id, "l": language, "id": customer_id},
        )


def set_opt_out(engine: Engine, chat_id: int, opted_out: bool) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE bot_users SET opted_out = :o WHERE chat_id = :c"), {"o": opted_out, "c": chat_id}
        )
        conn.execute(
            text("UPDATE customers SET opted_out = :o WHERE telegram_chat_id = :c"),
            {"o": opted_out, "c": chat_id},
        )


def twin_summary(engine: Engine, customer_id: str, now: datetime) -> dict:
    row = pd.read_sql(
        text(
            "SELECT c.name, c.city, c.city_tier, c.points_balance, s.top3_categories, s.orders_365d, "
            "s.aov_inr, s.recency_days, s.never_bought, s.lifecycle "
            "FROM customers c JOIN customer_scores s USING(customer_id) "
            "WHERE customer_id = :id"
        ),
        engine,
        params={"id": customer_id},
    ).iloc[0]
    return {
        "name": row["name"].split()[0],
        "city": row["city"],
        "tier": row["city_tier"].replace("tier", "Tier-"),
        "categories": json.loads(row["top3_categories"] or "[]")[:2],
        "orders_365d": int(row["orders_365d"]),
        "aov_inr": int(row["aov_inr"] or 0),
        "points": int(row["points_balance"] or 0),
        "last_order_days": None if row["never_bought"] else int(row["recency_days"]),
        "lifecycle": row["lifecycle"],
    }


# --------------------------------------------------------------------------- products and live events


def products(
    engine: Engine, categories: list[str], max_price: int | None, offset: int, n: int = 3
) -> list[dict]:
    keys = ", ".join(f":c{i}" for i in range(len(categories)))
    params = {f"c{i}": c for i, c in enumerate(categories)} | {"n": n, "o": offset, "p": max_price or 10**9}
    frame = pd.read_sql(
        text(
            f"SELECT product_id, title, brand, category, image_url, price_inr, mrp_inr, rating, rating_count "
            f"FROM products WHERE category IN ({keys}) AND price_inr <= :p AND rating >= 4 "
            f"ORDER BY rating_count DESC LIMIT :n OFFSET :o"
        ),
        engine,
        params=params,
    )
    return frame.to_dict("records")


def product(engine: Engine, product_id: str) -> dict | None:
    frame = pd.read_sql(
        text("SELECT * FROM products WHERE product_id = :p"), engine, params={"p": product_id}
    )
    return frame.iloc[0].to_dict() if len(frame) else None


def add_events(engine: Engine, customer_id: str, rows: list[dict], now: datetime) -> None:
    """Live behaviour from the bot, in the same event format the simulator and a real platform use."""
    frame = pd.DataFrame(
        [
            {
                "event_id": f"TG-{uuid.uuid4().hex[:16]}",
                "customer_id": customer_id,
                "ts": now,
                "device": "telegram",
                "product_id": None,
                "price_inr": None,
                "session_id": None,
                "order_id": None,
                "points": None,
                "query": None,
                **r,
            }
            for r in rows
        ]
    )
    frame.to_sql("events", engine, if_exists="append", index=False)


def last_cart_item(engine: Engine, customer_id: str, since: datetime) -> dict | None:
    """Most recent item added from the bot that hasn't been ordered since."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT e.product_id, e.ts FROM events e WHERE e.customer_id = :c AND e.type = 'add_to_cart' "
                "AND e.device = 'telegram' AND e.ts >= :s ORDER BY e.ts DESC LIMIT 1"
            ),
            {"c": customer_id, "s": since},
        ).first()
        if row is None:
            return None
        ordered = conn.execute(
            text("SELECT 1 FROM events WHERE customer_id = :c AND type = 'order' AND ts >= :t LIMIT 1"),
            {"c": customer_id, "t": row[1]},
        ).first()
    return None if ordered else product(engine, row[0])


# --------------------------------------------------------------------------- delivery (layer L5)


def _check(rule: str, passed: bool, reason: str) -> dict:
    return {"rule": rule, "passed": passed, "reason": reason}


def already_handled(engine: Engine, idempotency_key: str | None) -> str | None:
    if not idempotency_key:
        return None
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT decision_id FROM offer_decisions WHERE idempotency_key = :k"), {"k": idempotency_key}
        ).scalar()


def persist(
    engine: Engine, decision: Decision, policy: Policy, channel: str, idempotency_key: str | None
) -> Decision:
    """Layer L5: re-check send-time limits inside one transaction, then log the decision exactly once.

    Between the decision and the send, another trigger may have sent an offer, the customer may have opted
    out, or the budget may have been spent. A sent decision that fails these checks is downgraded to
    `suppressed` and logged with the reason; nothing is sent for it.
    """
    with _SEND_LOCK, engine.begin() as conn:
        if idempotency_key:
            existing = conn.execute(
                text("SELECT decision_id FROM offer_decisions WHERE idempotency_key = :k"),
                {"k": idempotency_key},
            ).first()
            if existing:
                decision.status, decision.explanation = (
                    "duplicate",
                    {**decision.explanation, "why": f"Already handled as {existing[0]} (idempotency key)."},
                )
                return decision
        if decision.status == "sent":
            now = decision.created_at
            opted = conn.execute(
                text("SELECT opted_out FROM customers WHERE customer_id = :c"), {"c": decision.customer_id}
            ).scalar()
            week = conn.execute(
                text(
                    "SELECT COUNT(*) FROM offer_decisions WHERE customer_id = :c "
                    "AND status IN ('sent','claimed') "
                    "AND created_at > :w"
                ),
                {"c": decision.customer_id, "w": now - timedelta(days=7)},
            ).scalar()
            active = conn.execute(
                text(
                    "SELECT COUNT(*) FROM offer_decisions WHERE customer_id = :c "
                    "AND status IN ('sent','claimed') "
                    "AND valid_until > :n"
                ),
                {"c": decision.customer_id, "n": now},
            ).scalar()
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            spent = conn.execute(
                text(
                    "SELECT COALESCE(SUM(expected_cost_inr), 0) FROM offer_decisions "
                    "WHERE status IN ('sent','claimed') AND created_at >= :d"
                ),
                {"d": day},
            ).scalar()
            cost = (decision.economics or {}).get("expected_cost_inr", 0) or 0
            checks = [
                _check("send_time_not_opted_out", not opted, "opted out" if opted else "ok"),
                _check(
                    "send_time_frequency_cap",
                    week < policy.contact.max_offers_per_7_days,
                    f"{week} sent in the last 7 days",
                ),
                _check("send_time_no_stacking", active == 0, f"{active} active offer(s)"),
                _check(
                    "send_time_daily_budget",
                    spent + cost <= policy.daily_budget(now.date()),
                    f"₹{spent:,.0f} + ₹{cost:,.0f} of ₹{policy.daily_budget(now.date()):,.0f}",
                ),
            ]
            decision.rule_checks = decision.rule_checks + checks
            failed = [c for c in checks if not c["passed"]]
            if failed:
                decision.status = "suppressed"
                decision.explanation = {
                    **decision.explanation,
                    "why": "Stopped at send time: " + "; ".join(c["reason"] for c in failed),
                }
        row = decision.to_row() | {"channel": channel, "idempotency_key": idempotency_key}
        conn.execute(
            text(
                f"INSERT INTO offer_decisions ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})"
            ),
            row,
        )
    return decision


def get_decision(engine: Engine, decision_id: str) -> dict | None:
    frame = pd.read_sql(
        text("SELECT * FROM offer_decisions WHERE decision_id = :d"), engine, params={"d": decision_id}
    )
    return frame.iloc[0].to_dict() if len(frame) else None


def set_status(engine: Engine, decision_id: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE offer_decisions SET status = :s WHERE decision_id = :d"),
            {"s": status, "d": decision_id},
        )


def add_feedback(
    engine: Engine, decision_id: str, chat_id: int, action: str, detail: str | None, now: datetime
) -> None:
    with Session(engine) as s:
        s.add(
            Feedback(decision_id=decision_id, chat_id=chat_id, action=action, detail=detail, created_at=now)
        )
        s.commit()


def recent_decisions(engine: Engine, customer_id: str, limit: int = 5) -> list[dict]:
    frame = pd.read_sql(
        text(
            "SELECT decision_id, created_at, tier, status, offer_type, offer_value, code, valid_until "
            "FROM offer_decisions WHERE customer_id = :c ORDER BY created_at DESC LIMIT :n"
        ),
        engine,
        params={"c": customer_id, "n": limit},
    )
    return frame.to_dict("records")


def record_claim_order(engine: Engine, decision: dict, item: dict | None, now: datetime) -> dict:
    """A simulated order using the claimed coupon, so claims show up as real outcomes in the data."""
    price = int(item["price_inr"]) if item else 999
    kind, value = decision["offer_type"], float(decision["offer_value"] or 0)
    discount = {
        "PCT_OFF": price * value / 100,
        "WIN_BACK": price * value / 100,
        "FLAT_OFF": value,
        "SECOND_ORDER": value,
        "UPI_CASHBACK": min(value, price * 0.1),
        "FREE_SHIPPING": 40 if price < 499 else 0,
    }.get(kind, 0)
    discount = int(round(min(discount, price)))
    order_id = f"TGO-{uuid.uuid4().hex[:10]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO orders (order_id, customer_id, ts, gross_inr, discount_inr, coupon_code, "
                "payment_mode, "
                "status, return_reason, points_earned, points_redeemed) VALUES "
                "(:o, :c, :t, :g, :d, :code, 'UPI', 'delivered', NULL, :pts, 0)"
            ),
            {
                "o": order_id,
                "c": decision["customer_id"],
                "t": now,
                "g": price,
                "d": discount,
                "code": decision["code"],
                "pts": (price - discount) // 100 * (int(value) if kind == "BONUS_POINTS" else 1),
            },
        )
        if item:
            conn.execute(
                text(
                    "INSERT INTO order_items (order_id, product_id, category, qty, price_inr) "
                    "VALUES (:o, :p, :cat, 1, :pr)"
                ),
                {"o": order_id, "p": item["product_id"], "cat": item["category"], "pr": price},
            )
    add_events(
        engine,
        decision["customer_id"],
        [
            {
                "type": "order",
                "order_id": order_id,
                "price_inr": price,
                "product_id": item["product_id"] if item else None,
            },
            {"type": "coupon_redeemed", "order_id": order_id, "price_inr": discount},
        ],
        now,
    )
    return {"order_id": order_id, "price_inr": price, "discount_inr": discount, "item": item}


def with_status(decision: Decision, status: str, why: str) -> Decision:
    return replace(decision, status=status, explanation={**decision.explanation, "why": why})
