"""Telegram flow on a tiny temporary DB: twin matching, live holdout, L5 send-time checks, claims, cards."""

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlmodel import SQLModel, create_engine

from app import models  # noqa: F401  (registers tables)
from app.agent.harness import Harness
from app.bot import cards, store, twin
from app.bot.service import Services, next_morning, offer_for
from app.models import BotUser
from app.rules.engine import in_holdout
from tests.agent_fakes import FakeLLM, best_offer_submission
from tests.rules_helpers import NOW, POLICY, effects

CATS = ["Clothing_Shoes_and_Jewelry", "Electronics", "Books"]


def contactable_ids(n: int) -> list[str]:
    return [cid for cid in (f"C{i:05d}" for i in range(1, 500)) if not in_holdout(cid, 0.10)][:n]


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'bot.db'}")
    SQLModel.metadata.create_all(eng)
    ids = contactable_ids(3) + [next(f"C{i:05d}" for i in range(1, 500) if in_holdout(f"C{i:05d}", 0.10))]
    customers = pd.DataFrame(
        {
            "customer_id": ids,
            "name": ["Priya Sharma", "Gurpreet Gill", "Aman Verma", "Holdout Person"],
            "age_band": "25-34",
            "gender": "female",
            "city": ["Ludhiana", "Delhi", "Bathinda", "Ludhiana"],
            "state": "Punjab",
            "city_tier": ["tier2", "tier1", "tier3", "tier2"],
            "signup_date": date(2024, 1, 1),
            "orders_before_window": 5,
            "last_order_before_window": None,
            "acquisition_channel": "organic",
            "device": "android",
            "language_pref": "en",
            "prime_member": False,
            "prime_since": None,
            "preferred_payment": "UPI",
            "points_balance": 1240,
            "points_expiry_date": None,
            "opted_out": False,
            "telegram_chat_id": None,
            "twin_of": None,
        }
    )
    customers.to_sql("customers", eng, if_exists="append", index=False)
    scores = pd.DataFrame(
        {
            "customer_id": ids,
            "lifecycle": "active",
            "uplift_group": "persuadable",
            "churn_60d": 0.4,
            "p_buy_14d_no_offer": 0.15,
            "uplift_any": 0.10,
            "uplift_best": 0.10,
            "uplift_BONUS_POINTS": 0.09,
            "uplift_FREE_SHIPPING": 0.08,
            "clv_margin_90d_inr": 900.0,
            "monetary_365d_inr": 6000.0,
            "weighted_margin_pct": 40.0,
            "aov_inr": [1200.0, 3000.0, 400.0, 1200.0],
            "top3_categories": [json.dumps([c]) for c in CATS] + [json.dumps(CATS[:1])],
            "tenure_days": 600,
            "prefers_cod": 0,
            "points_expiring_30d": 0,
            "never_bought": 0,
            "recency_days": 20.0,
            "scored_at": datetime(2026, 10, 22),
            "orders_365d": [10, 30, 1, 10],
        }
    )
    scores.to_sql("customer_scores", eng, index=False)
    products = pd.DataFrame(
        {
            "product_id": [f"P{i}" for i in range(6)],
            "title": [f"Kurta {i}" for i in range(6)],
            "brand": "Brand",
            "category": CATS[0],
            "subcategory": "Women",
            "image_url": "https://example.com/x.jpg",
            "mrp_inr": 999,
            "price_inr": 699,
            "margin_pct": 45.0,
            "rating": 4.4,
            "rating_count": [600, 500, 400, 300, 200, 100],
        }
    )
    products.to_sql("products", eng, if_exists="append", index=False)
    store.ensure_schema(eng)
    return eng


def services(engine, sonnet=None) -> Services:
    return Services(
        engine, POLICY, effects(), Harness(sonnet=sonnet or FakeLLM(best_offer_submission), haiku=FakeLLM())
    )


def bot_user(chat_id=111, twin_id=None, **kw) -> BotUser:
    return BotUser(
        chat_id=chat_id,
        first_name="Simran",
        language="en",  # the fake Claude writes English
        created_at=NOW,
        consent_at=NOW,
        twin_customer_id=twin_id,
        **kw,
    )


def test_twin_matches_quiz_answers_and_skips_rules_holdout(engine):
    frame = twin.candidates(engine)
    answers = {"tier": "metro", "category": "electronics", "frequency": "weekly", "budget": "high"}
    best = frame.loc[twin.score(frame, answers).idxmax()]
    assert best["city_tier"] == "tier1" and "Electronics" in best["top3_categories"]
    small_town = {"tier": "town", "category": "books", "frequency": "rarely", "budget": "low"}
    assert frame.loc[twin.score(frame, small_town).idxmax(), "city_tier"] == "tier3"
    # the rules-holdout customer never wins, even when it matches perfectly
    holdout_id = frame.loc[frame["customer_id"].map(lambda c: in_holdout(c, 0.10)), "customer_id"].iloc[0]
    fashion = {"tier": "city", "category": "fashion", "frequency": "monthly", "budget": "mid"}
    assert twin.match(engine, fashion, seed=1) != holdout_id


def test_bind_twin_and_summary(engine):
    cid = contactable_ids(1)[0]
    store.bind_twin(engine, 111, cid, "pa")
    summary = store.twin_summary(engine, cid, NOW)
    assert summary["name"] == "Priya" and summary["city"] == "Ludhiana" and summary["points"] == 1240
    assert cid not in set(twin.candidates(engine)["customer_id"])  # taken twins aren't offered again
    text = cards.twin_text("Simran", summary)
    assert "Priya from Ludhiana" in text and "12-month" in text


def test_live_holdout_is_about_ten_percent_and_logged(engine):
    share = sum(twin.chat_in_holdout(c) for c in range(100_000, 105_000)) / 5000
    assert 0.08 < share < 0.12
    s = services(engine, sonnet=FakeLLM())  # no API call may happen
    cid = contactable_ids(1)[0]
    d = offer_for(s, bot_user(twin_id=cid, holdout=True), "welcome", NOW)
    assert d.status == "holdout" and d.ai["skipped"] == "live holdout group"
    assert store.get_decision(engine, d.decision_id)["channel"] == "telegram"


def test_ai_offer_is_logged_and_send_time_check_blocks_a_second_one(engine):
    cid = contactable_ids(1)[0]
    store.bind_twin(engine, 111, cid, "pa")
    s = services(engine)
    first = offer_for(s, bot_user(twin_id=cid), "welcome", NOW, idempotency_key="welcome:111")
    assert first.status == "sent" and first.tier == "T1"
    assert any(c["rule"] == "send_time_frequency_cap" and c["passed"] for c in first.rule_checks)
    # same trigger again → duplicate, not re-sent
    dup = offer_for(services(engine), bot_user(twin_id=cid), "welcome", NOW, idempotency_key="welcome:111")
    assert dup.status == "duplicate"
    # a different trigger minutes later → the engine sees the active offer and doesn't stack
    later = offer_for(services(engine), bot_user(twin_id=cid), "requested", NOW + timedelta(minutes=5))
    assert later.status != "sent"


def test_l5_downgrades_a_stale_sent_decision(engine):
    """Simulate a race: an offer was decided, then another one was logged before it was sent."""
    cid = contactable_ids(1)[0]
    s = services(engine)
    a = offer_for(s, bot_user(twin_id=cid), "a", NOW, idempotency_key="a")
    assert a.status == "sent"
    from app.agent.pipeline import decide
    from app.rules.context import load_activity, load_contexts
    from app.rules.engine import History

    ctx = load_contexts(engine, [cid])[cid]
    stale = decide(
        ctx, POLICY, effects(), NOW, History(), services(engine).harness, load_activity(engine, cid, NOW)
    )
    assert stale.status == "sent"  # decided on an outdated (empty) history
    final = store.persist(engine, stale, POLICY, "telegram", "b")
    assert final.status == "suppressed" and "Stopped at send time" in final.explanation["why"]


def test_claim_records_a_simulated_order(engine):
    cid = contactable_ids(1)[0]
    d = offer_for(services(engine), bot_user(twin_id=cid), "welcome", NOW)
    row = store.get_decision(engine, d.decision_id)
    item = store.product(engine, "P0")
    order = store.record_claim_order(engine, row, item, NOW)
    assert order["price_inr"] == 699
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM orders").scalar() == 1
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM events WHERE type='order'").scalar() == 1


def test_opt_out_reaches_the_rules(engine):
    cid = contactable_ids(1)[0]
    store.save_user(engine, bot_user(twin_id=cid))
    store.bind_twin(engine, 111, cid, "en")
    store.set_opt_out(engine, 111, True)
    user = store.get_user(engine, 111)
    d = offer_for(services(engine, sonnet=FakeLLM()), user, "requested", NOW)
    assert d.status == "suppressed" and any(
        c["rule"] == "not_opted_out" and not c["passed"] for c in d.rule_checks
    )


def test_browse_products_and_cart_tracking(engine):
    cid = contactable_ids(1)[0]
    page1 = store.products(engine, [CATS[0]], 800, 0)
    page2 = store.products(engine, [CATS[0]], 800, 3)
    assert len(page1) == 3 and {p["product_id"] for p in page1}.isdisjoint({p["product_id"] for p in page2})
    store.add_events(engine, cid, [{"type": "add_to_cart", "product_id": "P2", "price_inr": 699}], NOW)
    assert store.last_cart_item(engine, cid, NOW - timedelta(hours=1))["product_id"] == "P2"
    store.add_events(engine, cid, [{"type": "order", "order_id": "X"}], NOW + timedelta(minutes=1))
    assert store.last_cart_item(engine, cid, NOW - timedelta(hours=1)) is None


def test_cards_fit_telegram_limits(engine):
    assert len(cards.QUIZ) == 5
    datas = [
        btn.callback_data
        for step in range(len(cards.QUIZ))
        for row in cards.quiz_question(step)[1].inline_keyboard
        for btn in row
    ]
    datas += [
        btn.callback_data for row in cards.offer_buttons("D-0123456789ab").inline_keyboard for btn in row
    ]
    datas += [
        btn.callback_data for row in cards.reason_buttons("D-0123456789ab").inline_keyboard for btn in row
    ]
    datas += [btn.callback_data for row in cards.product_buttons("B0C1XYZABC").inline_keyboard for btn in row]
    assert all(len(d.encode()) <= 64 for d in datas)  # Telegram's callback_data limit
    cid = contactable_ids(1)[0]
    d = offer_for(services(engine), bot_user(twin_id=cid), "welcome", NOW)
    text = cards.offer_text(d.to_dict())
    assert d.offer["code"] in text and len(text) <= 1024  # photo caption limit
    why = cards.why_text(store.get_decision(engine, d.decision_id))
    assert "Passed" in why and "Claude" in why


def test_quiet_hours_end_is_next_morning():
    assert next_morning(datetime(2026, 10, 22, 22, 0), POLICY) == datetime(2026, 10, 23, 9, 0)
    assert next_morning(datetime(2026, 10, 23, 2, 0), POLICY) == datetime(2026, 10, 23, 9, 0)


@pytest.mark.asyncio
async def test_queued_offers_survive_a_restart(engine, monkeypatch):
    from types import SimpleNamespace

    from app.bot import telegram_bot

    cid = contactable_ids(1)[0]
    store.save_user(engine, bot_user(twin_id=cid))
    store.bind_twin(engine, 111, cid, "en")
    night = datetime(2026, 10, 23, 22, 30)
    d = offer_for(services(engine, sonnet=FakeLLM()), store.get_user(engine, 111), "requested", night)
    assert d.status == "queued"  # quiet hours: logged, no Claude call

    # "restart": a fresh application finds the queued decision in the log and schedules it for 09:00
    scheduled = []
    jobs = SimpleNamespace(run_once=lambda cb, when, data, name: scheduled.append((cb, when, data)))
    app = SimpleNamespace(bot_data={"svc": services(engine)}, job_queue=jobs)
    monkeypatch.setattr(telegram_bot, "ist_now", lambda: night)
    await telegram_bot.resume_queued(app)
    assert len(scheduled) == 1
    callback, delay, data = scheduled[0]
    assert (
        callback is telegram_bot.queued_job
        and data["chat_id"] == 111
        and data["decision_ids"] == [d.decision_id]
    )
    assert delay == pytest.approx((datetime(2026, 10, 24, 9, 0) - night).total_seconds())

    # once the job has released it, another restart doesn't schedule it again
    store.set_status(engine, d.decision_id, "released")
    scheduled.clear()
    await telegram_bot.resume_queued(app)
    assert scheduled == []
