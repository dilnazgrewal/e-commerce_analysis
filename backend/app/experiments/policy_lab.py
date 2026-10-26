"""Policy Lab: try offer strategies on the simulated customers before trying them on real ones.

Because the customers are synthetic, we know how each one *truly* behaves: purchase rate, whether they are
still active, price sensitivity and how strongly they react to each offer type (the hidden traits).
So the Lab answers what a real platform needs weeks of A/B tests for: what does a strategy do to orders,
revenue and profit compared with sending nothing, and how much discount goes to people who would have bought
anyway?

Expected values, not dice: the Lab computes each customer's *expected* orders, margin and offer cost from the
same equations the simulator samples from (Poisson purchases at rate λ, festive and weekday demand, an offer's
true lift while valid, win-backs of lapsed customers, coupon redemption on the first order). One random run is
dominated by luck (a single ₹1.6 lakh electronics order swings profit), whereas expected values are exact and
take about a second for all 5,000 customers. `simulate_check` runs the dice version for the tests.

This module reads the simulator ground truth on purpose: scoring policies is the one job it exists for
(CLAUDE.md rule 4). Strategies themselves only see what a platform sees (scores, profiles, the rules).

Sensitivity: offer-type strengths are a simulator assumption calibrated on Hillstrom's overall effect, so the
Lab can scale the true effect of discounts and of non-discount rewards, to see whether the conclusion holds.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from app.data.calibrate import load_india
from app.data.generate import Population, festive_multiplier
from app.data.simulator import DISCOUNT_TYPES, apply_offer, new_state, offer_effect, simulate
from app.rules.context import load_contexts
from app.rules.decide import decide_rules_only
from app.rules.effects import OfferEffects
from app.rules.engine import CustomerContext, History
from app.rules.schema import Policy

STRATEGIES = {
    "none": "No offers",
    "blanket20": "20% off for everyone",
    "at_risk15": "15% off for at-risk customers",
    "offeriq": "OfferIQ: rules + uplift + profit",
}
MESSAGE_COST_INR = 0.5
VALIDITY_DAYS = 7
SHIPPING_FEE_INR, FREE_SHIPPING_THRESHOLD_INR = 40, 499


@dataclass
class LabWorld:
    population: Population
    catalogue: pd.DataFrame
    india: dict
    contexts: dict[str, CustomerContext]
    profiles: pd.DataFrame  # city tier, gender, age band (fairness audit only, never a targeting input)
    as_of: date
    basket_inr: np.ndarray  # expected order value per customer (from their category tastes and real prices)
    basket_margin_inr: np.ndarray  # expected gross margin per order


def load_world(engine: Engine, sample: int | None = None, seed: int = 7) -> LabWorld:
    customers = pd.read_sql(text("SELECT * FROM customers"), engine)
    if sample and sample < len(customers):
        customers = customers.sample(n=sample, random_state=seed)
    customers = customers.sort_values("customer_id").reset_index(drop=True)
    ids = customers["customer_id"].tolist()
    traits = pd.read_sql(text("SELECT * FROM sim_hidden_traits"), engine).set_index("customer_id").loc[ids]
    traits = traits.reset_index()
    traits["alive"] = traits["alive"].astype(bool)
    affinity_dicts = [json.loads(a) for a in traits["category_affinity"]]
    categories = list(affinity_dicts[0])
    affinity = np.array([[d[c] for c in categories] for d in affinity_dicts])
    customers["opted_out"] = customers["opted_out"].astype(bool)
    customers["prime_member"] = customers["prime_member"].astype(bool)
    catalogue = pd.read_sql(text("SELECT * FROM products"), engine)
    india = load_india()

    # Expected price and margin of one item in each category, weighted by popularity as the simulator shops.
    item_price, item_margin = [], []
    for category in categories:
        frame = catalogue[catalogue["category"] == category]
        weights = frame["rating_count"].to_numpy(dtype=float) ** 0.7
        price = float(np.average(frame["price_inr"], weights=weights))
        item_price.append(price)
        item_margin.append(price * india["category_margin_pct"][category] / 100)
    items_per_order = 1 + india["items_per_order_extra_mean"]
    scored = pd.read_sql(text("SELECT MAX(scored_at) AS t FROM customer_scores"), engine)["t"].iloc[0]
    return LabWorld(
        population=Population(customers, traits, affinity, categories),
        catalogue=catalogue,
        india=india,
        contexts=load_contexts(engine, ids),
        profiles=customers[
            ["customer_id", "city_tier", "gender", "age_band", "preferred_payment", "prime_member"]
        ],
        as_of=pd.Timestamp(scored).date(),
        basket_inr=items_per_order * affinity @ np.array(item_price),
        basket_margin_inr=items_per_order * affinity @ np.array(item_margin),
    )


_WORLDS: dict[tuple, LabWorld] = {}


def world(engine: Engine, sample: int | None = None, seed: int = 7) -> LabWorld:
    key = (str(engine.url), sample, seed)
    if key not in _WORLDS:
        _WORLDS[key] = load_world(engine, sample, seed)
    return _WORLDS[key]


_CHOICES: dict[tuple, dict[str, dict]] = {}


def cached_choices(strategy: str, w: LabWorld, policy: Policy, effects: OfferEffects) -> dict[str, dict]:
    """Offer choices depend only on the world and the policy, not on the sensitivity sliders: reuse them."""
    key = (id(w), strategy, policy.model_dump_json() if strategy == "offeriq" else "")
    if key not in _CHOICES:
        if len(_CHOICES) > 32:
            _CHOICES.clear()
        _CHOICES[key] = choose_offers(strategy, w, policy, effects)
    return _CHOICES[key]


def choose_offers(strategy: str, w: LabWorld, policy: Policy, effects: OfferEffects) -> dict[str, dict]:
    """Which offer each customer gets under a strategy (decided on day 0, from what a platform knows)."""
    now = datetime.combine(w.as_of, datetime.min.time()).replace(hour=11)
    offers: dict[str, dict] = {}
    for cid, ctx in w.contexts.items():
        if strategy == "blanket20":
            offers[cid] = {"type": "PCT_OFF", "value": 20.0, "days": VALIDITY_DAYS, "expected_cost": None}
        elif strategy == "at_risk15" and ctx.churn_60d >= 0.6:
            offers[cid] = {"type": "PCT_OFF", "value": 15.0, "days": VALIDITY_DAYS, "expected_cost": None}
        elif strategy == "offeriq":
            d = decide_rules_only(ctx, policy, effects, now, History(), trigger="policy_lab")
            if d.status == "sent":
                template = policy.offer(d.offer["offer_id"])
                offers[cid] = {
                    "type": d.offer["type"],
                    "value": float(d.offer["value"]),
                    "days": template.validity_days,
                    "expected_cost": d.economics["expected_cost_inr"],
                }
    return offers


def _demand(w: LabWorld, days: int) -> np.ndarray:
    """Relative demand on each day of the window (festive calendar × day of week), as the simulator uses."""
    return np.array(
        [
            festive_multiplier(d, w.india) * w.india["weekday_weights"][d.weekday()]
            for d in (w.as_of + timedelta(days=k) for k in range(days))
        ]
    )


def cost_if_used(kind: str, value: float, basket: float, cod: bool, prime: bool) -> float:
    """What redeeming the offer costs on an order of this size (mirrors the simulator's redemption)."""
    if kind in ("PCT_OFF", "WIN_BACK"):
        return basket * value / 100
    if kind in ("FLAT_OFF", "SECOND_ORDER"):
        return value if basket >= 2 * value else 0.0
    if kind == "FREE_SHIPPING":
        return SHIPPING_FEE_INR if basket < FREE_SHIPPING_THRESHOLD_INR else 0.0
    if kind == "UPI_CASHBACK":
        return 0.0 if cod else min(value, basket * 0.10)
    if kind == "BONUS_POINTS":
        return basket / 100 * (value - 1) * (2 if prime else 1)
    return 0.0


def expected_outcomes(w: LabWorld, offers: dict[str, dict], days: int, scale: dict) -> dict:
    """Expected orders, margin and offer cost for every customer under one set of offers (no sampling)."""
    t = w.population.traits
    lam, alive = t["purchase_rate_per_day"].to_numpy(), t["alive"].to_numpy()
    price_sens, resp = t["price_sensitivity"].to_numpy(), t["offer_responsiveness"].to_numpy()
    demand = _demand(w, days)
    total_demand = demand.sum()
    ids = w.population.customers["customer_id"].to_numpy()
    cod = (w.profiles["preferred_payment"] == "COD").to_numpy()
    prime = w.profiles["prime_member"].to_numpy()

    orders = np.where(alive, lam * total_demand, 0.0)
    cost = np.zeros(len(ids))
    wasted = np.zeros(len(ids))
    used = np.zeros(len(ids))
    for i, cid in enumerate(ids):
        offer = offers.get(cid)
        if offer is None:
            continue
        valid = demand[: offer["days"]].sum()
        effect = offer_effect(offer["type"], offer["value"], price_sens[i], resp[i])
        if effect > 0:
            effect *= scale.get("discount" if offer["type"] in DISCOUNT_TYPES else "reward", 1.0)
        boost = max(1 + effect, 0.05)
        # A lapsed customer may be won back with probability min(0.25, 0.1 × effect). While the offer is
        # valid the purchase rate is λ·boost until the first order (the offer is used up), then the usual λ:
        #   E[orders in window] = P(first order) + λ·E[time left after it]
        #                       = (1 − e^(−λbV)) + λ·(V − (1 − e^(−λbV)) / (λb))      (V = demand while valid)
        p_active = 1.0 if alive[i] else (min(0.25, 0.1 * effect) if effect > 0 else 0.0)
        rate_b = lam[i] * boost
        p_first = 1 - np.exp(-rate_b * valid)
        in_window = p_first + lam[i] * (valid - p_first / rate_b) if rate_b > 0 else 0.0
        orders[i] = p_active * (in_window + lam[i] * (total_demand - valid))
        p_use = p_active * p_first  # the coupon is used on the first order
        p_anyway = (1 - np.exp(-lam[i] * valid)) if alive[i] else 0.0  # ...who would have bought regardless
        c = cost_if_used(offer["type"], offer["value"], w.basket_inr[i], cod[i], prime[i])
        used[i], cost[i] = p_use, p_use * c + MESSAGE_COST_INR
        wasted[i] = min(p_use, p_anyway) * c
    margin = orders * w.basket_margin_inr
    return {
        "orders": orders,
        "revenue": orders * w.basket_inr,
        "margin": margin,
        "cost": cost,
        "wasted": wasted,
        "used": used,
        "profit": margin - cost,
    }


def compare(
    engine: Engine,
    policy: Policy,
    effects: OfferEffects,
    strategies: list[str],
    days: int = 14,
    discount_strength: float = 1.0,
    reward_strength: float = 1.0,
    sample: int | None = None,
) -> dict:
    """Every strategy on the same simulated customers, compared with sending no offers."""
    w = world(engine, sample)
    scale = {"discount": discount_strength, "reward": reward_strength}
    strategies = ["none"] + [s for s in strategies if s != "none"]
    chosen = {s: cached_choices(s, w, policy, effects) for s in strategies}
    out = {s: expected_outcomes(w, chosen[s], days, scale) for s in strategies}
    base = out["none"]
    results = []
    for s in strategies:
        o = out[s]
        cost = float(o["cost"].sum())
        extra_profit = float(o["profit"].sum() - base["profit"].sum())
        results.append(
            {
                "strategy": s,
                "label": STRATEGIES[s],
                "offers_sent": len(chosen[s]),
                "offers_used": round(float(o["used"].sum())),
                "orders": round(float(o["orders"].sum()), 1),
                "revenue_inr": round(float(o["revenue"].sum())),
                "gross_margin_inr": round(float(o["margin"].sum())),
                "offer_cost_inr": round(cost),
                "profit_inr": round(float(o["profit"].sum())),
                "wasted_discount_inr": round(float(o["wasted"].sum())),
                "extra_orders": round(float(o["orders"].sum() - base["orders"].sum()), 1),
                "extra_revenue_inr": round(float(o["revenue"].sum() - base["revenue"].sum())),
                "extra_profit_inr": round(extra_profit),
                "roi": round(extra_profit / cost, 2) if cost else None,
            }
        )
    mix: dict[str, int] = {}
    for offer in chosen.get("offeriq", {}).values():
        mix[offer["type"]] = mix.get(offer["type"], 0) + 1
    return {
        "as_of": str(w.as_of),
        "days": days,
        "customers": len(w.contexts),
        "assumptions": {"discount_strength": discount_strength, "reward_strength": reward_strength},
        "results": results,
        "offeriq_offer_mix": mix,
        "fairness": fairness(w, chosen.get("offeriq", {})),
    }


def fairness(w: LabWorld, offers: dict[str, dict]) -> dict:
    """Does any group get fewer or cheaper offers? (gender and age are audited, never used to target)"""
    frame = w.profiles[["customer_id", "city_tier", "gender", "age_band"]].copy()
    frame["offered"] = frame["customer_id"].isin(offers)
    frame["expected_cost"] = frame["customer_id"].map(
        lambda c: (offers.get(c) or {}).get("expected_cost") or 0
    )
    out = {}
    for column in ("city_tier", "gender", "age_band"):
        groups = frame.groupby(column).agg(
            customers=("customer_id", "size"),
            offer_rate=("offered", "mean"),
            avg_expected_cost=("expected_cost", "mean"),
        )
        rates = groups["offer_rate"]
        out[column] = {
            "groups": groups.round(3).reset_index().to_dict("records"),
            "max_gap_pp": round(float(rates.max() - rates.min()) * 100, 1),
        }
    return out


def simulate_check(w: LabWorld, offers: dict[str, dict], days: int, seeds: tuple[int, ...]) -> float:
    """Average order count from the dice-rolling simulator, to check the expected values against."""
    counts = []
    for seed in seeds:
        state = new_state(w.population, w.catalogue, w.india, seed)
        index = {cid: i for i, cid in enumerate(state.ids)}
        for cid, offer in offers.items():
            apply_offer(state, index[cid], offer["type"], offer["value"], w.as_of, offer["days"], "LAB")
        _, orders, _ = simulate(state, w.as_of, w.as_of + timedelta(days=days))
        counts.append(len(orders))
    return float(np.mean(counts))
