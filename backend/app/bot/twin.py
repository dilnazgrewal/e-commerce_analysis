"""Digital twin matching: a classmate's 4-tap quiz is matched to the most similar synthetic customer.

The twin's 12-month history (orders, visits, points, past campaigns) becomes the classmate's, so their
very first offer is decided from realistic behaviour instead of an empty account.
"""

import json
import random

import pandas as pd
from sqlalchemy import Engine, text

from app.rules.engine import in_holdout

TIERS = {"metro": "tier1", "city": "tier2", "town": "tier3"}
CATEGORY_GROUPS = {
    "fashion": ["Clothing_Shoes_and_Jewelry"],
    "electronics": ["Electronics", "Cell_Phones_and_Accessories"],
    "beauty": ["Beauty_and_Personal_Care", "Health_and_Household"],
    "home": ["Home_and_Kitchen", "Grocery_and_Gourmet_Food"],
    "books": ["Books", "Toys_and_Games"],
    "sports": ["Sports_and_Outdoors"],
}
FREQUENCY = {"weekly": (25, 400), "monthly": (8, 24), "quarterly": (3, 7), "rarely": (0, 2)}  # orders / year
BUDGET = {
    "low": (0, 500),
    "mid": (500, 1500),
    "high": (1500, 5000),
    "premium": (5000, 10**9),
}  # usual order ₹
HOLDOUT_SHARE = 0.10


def chat_in_holdout(chat_id: int) -> bool:
    """Live control group for real people: 10% of chats never get offers, so true lift can be measured."""
    return in_holdout(f"telegram:{chat_id}", HOLDOUT_SHARE, salt="offeriq-telegram-holdout-v1")


def candidates(engine: Engine) -> pd.DataFrame:
    return pd.read_sql(
        text(
            "SELECT s.customer_id, c.city_tier, c.state, s.top3_categories, s.orders_365d, s.aov_inr, "
            "s.uplift_group, s.uplift_any, s.lifecycle "
            "FROM customer_scores s JOIN customers c USING(customer_id) "
            "WHERE c.telegram_chat_id IS NULL AND NOT c.opted_out"
        ),
        engine,
    )


def score(frame: pd.DataFrame, answers: dict, seed: int = 0) -> pd.Series:
    tier = TIERS.get(answers.get("tier"), "tier2")
    wanted = set(CATEGORY_GROUPS.get(answers.get("category"), []))
    lo_f, hi_f = FREQUENCY.get(answers.get("frequency"), (0, 400))
    lo_b, hi_b = BUDGET.get(answers.get("budget"), (0, 10**9))
    top = frame["top3_categories"].map(lambda s: json.loads(s or "[]"))
    rng = random.Random(seed)
    return (
        3 * (frame["city_tier"] == tier)
        + 3 * top.map(lambda cats: bool(cats) and cats[0] in wanted)
        + 1 * top.map(lambda cats: bool(wanted & set(cats)))
        + 2 * frame["orders_365d"].between(lo_f, hi_f)
        + 2 * frame["aov_inr"].between(lo_b, hi_b)
        # Among similar matches, prefer twins an offer can actually help (persuadable, positive uplift), so a
        # classmate isn't silently matched to someone who should never get offers. This biases *who* the bot
        # users are, not the live experiment: holdout is still random by chat.
        + 2 * (frame["uplift_group"] == "persuadable")
        + 1 * (frame["uplift_any"] > 0)
        - 3 * (frame["uplift_group"] == "sleeping_dog")
        + 1 * ((answers.get("language") == "pa") & frame["state"].isin(["Punjab", "Chandigarh"]))
        # twins must be reachable by offers: the rules' own holdout would silence them for good
        - 100 * frame["customer_id"].map(lambda c: in_holdout(c, HOLDOUT_SHARE))
        + pd.Series([rng.random() * 0.5 for _ in range(len(frame))], index=frame.index)
    )


def match(engine: Engine, answers: dict, seed: int) -> str:
    frame = candidates(engine)
    return frame.loc[score(frame, answers, seed).idxmax(), "customer_id"]
