"""Day-by-day behaviour simulator: turns hidden traits into a realistic platform event log.

The same `step_day` drives both the 12-month history and the live clock ("tick"), so new events keep
arriving after seeding and real-time triggers (abandoned carts, drifting customers) fire naturally.

Per customer per day:
  orders   ~ Poisson(λ · festive · weekday)            (only while alive; BG/NBD dropout after each order)
  sessions = orders + Poisson(λ · festive · (sessions_per_order − 1))  +  window-shopping if churned
Each session emits session_start → [search] → views → [add_to_cart] → [checkout_start → order].
Carts in non-buying sessions are abandoned; the add-to-cart rate is derived so that the overall
cart-abandonment rate matches the configured target.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from app.data.generate import Population, festive_multiplier

EVENT_COLUMNS = [
    "event_id",
    "customer_id",
    "type",
    "ts",
    "product_id",
    "price_inr",
    "session_id",
    "device",
    "order_id",
    "points",
    "query",
]
ORDER_COLUMNS = [
    "order_id",
    "customer_id",
    "ts",
    "gross_inr",
    "discount_inr",
    "coupon_code",
    "payment_mode",
    "status",
    "return_reason",
    "points_earned",
    "points_redeemed",
]
ITEM_COLUMNS = ["order_id", "product_id", "category", "qty", "price_inr"]
RETURN_REASONS = [
    "size/fit issue",
    "not as described",
    "damaged in transit",
    "changed mind",
    "better price elsewhere",
]


@dataclass
class SimState:
    population: Population
    catalogue: pd.DataFrame
    india: dict
    rng: np.random.Generator
    alive: np.ndarray
    points: np.ndarray
    counters: dict = field(default_factory=lambda: defaultdict(int))
    pending: dict = field(default_factory=lambda: defaultdict(list))  # day -> [event rows]
    events: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    items: list = field(default_factory=list)

    def __post_init__(self) -> None:
        c = self.population.customers
        self.ids = c["customer_id"].to_numpy()
        self.signup = pd.to_datetime(c["signup_date"]).dt.date.to_numpy()
        self.device = c["device"].to_numpy()
        self.payment = c["preferred_payment"].to_numpy()
        self.tier = c["city_tier"].to_numpy()
        self.prime = c["prime_member"].to_numpy()
        t = self.population.traits
        self.lam = t["purchase_rate_per_day"].to_numpy()
        self.p_drop = t["dropout_prob_per_order"].to_numpy()
        self.price_sens = t["price_sensitivity"].to_numpy()
        self.cum_affinity = np.cumsum(self.population.affinity, axis=1)

        funnel = self.india["funnel"]
        self.spo = funnel["sessions_per_order"]
        self.views_mean = funnel["views_per_session_mean"]
        # P(at least one add-to-cart in a browse session) solves abandoned/(abandoned+1) = target.
        target = funnel["cart_abandonment_rate"]
        abandoned_per_order = target / (1 - target)
        p_cart_session = min(0.95, abandoned_per_order / (self.spo - 1))
        self.atc_per_view = 1 - (1 - p_cart_session) ** (1 / self.views_mean)

        self.categories = self.population.categories
        self.cat_products, self.cat_cum_pop = [], []
        cat_frames = {k: g for k, g in self.catalogue.groupby("category")}
        for category in self.categories:
            frame = cat_frames[category]
            weights = frame["rating_count"].to_numpy(dtype=float) ** 0.7
            self.cat_products.append(frame[["product_id", "price_inr", "brand", "subcategory"]].to_numpy())
            self.cat_cum_pop.append(np.cumsum(weights) / weights.sum())
        self.margin = self.india["category_margin_pct"]
        self.return_rate = self.india["category_return_rate"]
        hours = np.array(self.india["hour_weights"], dtype=float)
        self.cum_hours = np.cumsum(hours) / hours.sum()
        self.weekday = self.india["weekday_weights"]
        self.browse_per_day = funnel["browse_only_sessions_per_month"] / 30

    def next_id(self, prefix: str) -> str:
        self.counters[prefix] += 1
        return f"{prefix}{self.counters[prefix]:08d}"


def new_state(population: Population, catalogue: pd.DataFrame, india: dict, seed: int) -> SimState:
    traits = population.traits
    return SimState(
        population=population,
        catalogue=catalogue,
        india=india,
        rng=np.random.default_rng(seed + 1),
        alive=traits["alive"].to_numpy().copy(),
        points=population.customers["points_balance"].to_numpy().copy(),
    )


def _product(state: SimState, i: int) -> tuple[int, np.ndarray]:
    cat = int(np.searchsorted(state.cum_affinity[i], state.rng.random() * state.cum_affinity[i, -1]))
    cat = min(cat, len(state.categories) - 1)
    idx = int(np.searchsorted(state.cat_cum_pop[cat], state.rng.random()))
    return cat, state.cat_products[cat][min(idx, len(state.cat_products[cat]) - 1)]


def _event(state: SimState, i: int, kind: str, ts: datetime, session: str | None = None, **extra) -> list:
    row = dict.fromkeys(EVENT_COLUMNS)
    row.update(
        event_id=state.next_id("E"),
        customer_id=state.ids[i],
        type=kind,
        ts=ts,
        session_id=session,
        device=state.device[i],
        **extra,
    )
    return [row[c] for c in EVENT_COLUMNS]


def _session(state: SimState, i: int, day: date, buys: bool) -> None:
    rng = state.rng
    hour = int(np.searchsorted(state.cum_hours, rng.random()))
    ts = datetime(
        day.year, day.month, day.day, min(hour, 23), int(rng.integers(0, 60)), int(rng.integers(0, 60))
    )
    sid = state.next_id("S")
    emit = state.events.append
    emit(_event(state, i, "session_start", ts, sid))

    n_views = 1 + rng.poisson(state.views_mean - 1)
    viewed = [_product(state, i) for _ in range(n_views)]
    if rng.random() < state.india["funnel"]["search_share_of_sessions"]:
        _, first = viewed[0]
        ts += timedelta(seconds=int(rng.integers(5, 40)))
        emit(_event(state, i, "search", ts, sid, query=f"{first[2]} {first[3]}".lower()[:80]))
    for _, product in viewed:
        ts += timedelta(seconds=int(rng.integers(10, 120)))
        emit(_event(state, i, "view", ts, sid, product_id=product[0], price_inr=int(product[1])))

    if not buys:
        for _, product in viewed:
            if rng.random() < state.atc_per_view:
                ts += timedelta(seconds=int(rng.integers(5, 60)))
                emit(
                    _event(state, i, "add_to_cart", ts, sid, product_id=product[0], price_inr=int(product[1]))
                )
                if rng.random() < 0.15:
                    ts += timedelta(seconds=int(rng.integers(20, 300)))
                    emit(
                        _event(
                            state,
                            i,
                            "remove_from_cart",
                            ts,
                            sid,
                            product_id=product[0],
                            price_inr=int(product[1]),
                        )
                    )
        return

    basket = [viewed[-1]] + [
        _product(state, i) for _ in range(rng.poisson(state.india["items_per_order_extra_mean"]))
    ]
    for _, product in basket:
        ts += timedelta(seconds=int(rng.integers(5, 60)))
        emit(_event(state, i, "add_to_cart", ts, sid, product_id=product[0], price_inr=int(product[1])))
    ts += timedelta(seconds=int(rng.integers(30, 240)))
    emit(_event(state, i, "checkout_start", ts, sid))
    _order(state, i, ts + timedelta(seconds=int(rng.integers(30, 180))), sid, basket)


def _order(state: SimState, i: int, ts: datetime, sid: str, basket: list) -> None:
    rng = state.rng
    order_id = state.next_id("O")
    gross = int(sum(int(p[1]) for _, p in basket))
    main_category = state.categories[basket[0][0]]

    discount, coupon = 0, None
    if rng.random() < 0.08 + 0.3 * state.price_sens[i]:
        pct = min(rng.uniform(0.05, 0.15), state.margin[main_category] / 100 * 0.6)
        discount = int(round(gross * pct))
        coupon = f"SAVE{int(round(pct * 100))}"
    redeemed = 0
    if state.points[i] >= 200 and rng.random() < 0.2:
        redeemed = int(min(state.points[i], gross * 0.1))
        state.points[i] -= redeemed

    if rng.random() < 0.8:
        payment = state.payment[i]
    else:
        mix = state.india["payment_mix"][state.tier[i]]
        payment = list(mix)[int(rng.choice(len(mix), p=np.array(list(mix.values())) / sum(mix.values())))]

    status, reason = "delivered", None
    if payment == "COD" and rng.random() < state.india["cod_rto_rate"]:
        status = "rto"
    elif rng.random() < state.return_rate[main_category]:
        status, reason = "returned", str(rng.choice(RETURN_REASONS))

    net = gross - discount - redeemed
    earned = (
        (net // 100) * state.india["points"]["earn_per_100_inr"] * (2 if state.prime[i] else 1)
        if status == "delivered"
        else 0
    )

    state.orders.append(
        [order_id, state.ids[i], ts, gross, discount, coupon, payment, status, reason, earned, redeemed]
    )
    for cat, product in basket:
        state.items.append([order_id, product[0], state.categories[cat], 1, int(product[1])])
    state.events.append(_event(state, i, "order", ts, sid, order_id=order_id, price_inr=gross))
    if coupon:
        state.events.append(
            _event(state, i, "coupon_redeemed", ts, sid, order_id=order_id, price_inr=discount)
        )
    if redeemed:
        state.events.append(_event(state, i, "points_redeemed", ts, sid, order_id=order_id, points=redeemed))

    due = ts.date() + timedelta(days=int(rng.integers(3, 11)))
    if status == "returned":
        state.pending[due].append((i, "return", order_id, None))
    elif earned:
        state.pending[ts.date() + timedelta(days=2)].append((i, "points_earned", order_id, earned))


def step_day(state: SimState, day: date) -> None:
    """Simulate one calendar day for the whole population (appends to state.events/orders/items)."""
    rng = state.rng
    for i, kind, order_id, points in state.pending.pop(day, []):
        ts = datetime(day.year, day.month, day.day, int(rng.integers(9, 21)), int(rng.integers(0, 60)))
        if kind == "points_earned":
            state.points[i] += points
        state.events.append(_event(state, i, kind, ts, order_id=order_id, points=points))

    active = state.signup <= day
    season = festive_multiplier(day, state.india) * state.weekday[day.weekday()]
    rate = np.where(active & state.alive, state.lam * season, 0.0)
    n_orders = rng.poisson(rate)
    n_browse = rng.poisson(rate * (state.spo - 1))
    churned = active & ~state.alive
    n_browse += rng.poisson(np.where(churned, state.browse_per_day * season, 0.0))

    for i in np.flatnonzero(n_orders + n_browse):
        for _ in range(n_browse[i]):
            _session(state, i, day, buys=False)
        for _ in range(n_orders[i]):
            if not state.alive[i]:
                break
            _session(state, i, day, buys=True)
            if rng.random() < state.p_drop[i]:
                state.alive[i] = False


def simulate(state: SimState, start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Simulate [start, end) and return (events, orders, order_items) frames for the new rows."""
    n_events, n_orders, n_items = len(state.events), len(state.orders), len(state.items)
    day = start
    while day < end:
        step_day(state, day)
        day += timedelta(days=1)
    events = pd.DataFrame(state.events[n_events:], columns=EVENT_COLUMNS)
    orders = pd.DataFrame(state.orders[n_orders:], columns=ORDER_COLUMNS)
    items = pd.DataFrame(state.items[n_items:], columns=ITEM_COLUMNS)
    return events, orders, items
