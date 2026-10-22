"""Build a synthetic Indian customer population whose behaviour is calibrated on real data.

Each customer gets
* a public profile (what a platform knows: city, tier, device, payment preference, Prime, points …), and
* hidden traits (simulator ground truth, never visible to models):
    - purchase rate λ and per-order dropout probability p, drawn from the BG/NBD distributions fitted on
      real UCI Online Retail II transactions (mean rate rescaled to Indian consumer e-commerce),
    - category affinity, a Dirichlet draw centred on real Amazon category volumes,
    - price sensitivity (higher in smaller cities and for COD shoppers),
    - offer responsiveness, centred on the real randomised effect measured in the Hillstrom experiment
      for the customer's recency × spend-history cell, plus a share of "sleeping dogs" who react badly.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
from faker import Faker

from app.data.calibrate import load_india
from app.data.paths import CALIBRATION_FILE, CATALOGUE_FILE

PUNJABI_FIRST = {
    "male": [
        "Gurpreet",
        "Harpreet",
        "Manpreet",
        "Jaspreet",
        "Amandeep",
        "Navdeep",
        "Simranjit",
        "Harjot",
        "Arshdeep",
        "Karan",
        "Rohit",
        "Jaskaran",
        "Gurvinder",
        "Sukhdeep",
        "Ravinder",
    ],
    "female": [
        "Simran",
        "Harleen",
        "Navneet",
        "Jasleen",
        "Gurleen",
        "Mehak",
        "Kiranjot",
        "Ravneet",
        "Amrit",
        "Prabhjot",
        "Dilnoor",
        "Sukhmani",
        "Manjot",
        "Japneet",
        "Rupinder",
    ],
}
PUNJABI_LAST = [
    "Gill",
    "Sandhu",
    "Dhillon",
    "Grewal",
    "Sidhu",
    "Brar",
    "Bajwa",
    "Randhawa",
    "Sharma",
    "Arora",
    "Bains",
    "Cheema",
    "Virk",
    "Mann",
    "Chahal",
]


@dataclass
class Population:
    customers: pd.DataFrame
    traits: pd.DataFrame
    affinity: np.ndarray  # n_customers × n_categories
    categories: list[str]


def load_calibration() -> dict:
    return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))


def load_catalogue() -> pd.DataFrame:
    return pd.read_parquet(CATALOGUE_FILE)


def _pick(rng: np.random.Generator, mix: dict, size: int) -> np.ndarray:
    keys = list(mix)
    probs = np.array([mix[k] for k in keys], dtype=float)
    return np.array(keys)[rng.choice(len(keys), size=size, p=probs / probs.sum())]


def _names(rng: np.random.Generator, genders: np.ndarray, states: np.ndarray, seed: int) -> list[str]:
    fake = Faker("en_IN")
    fake.seed_instance(seed)
    names = []
    for gender, state in zip(genders, states, strict=True):
        g = "female" if gender == "female" else "male"
        if state in ("Punjab", "Chandigarh") and rng.random() < 0.8:
            first = rng.choice(PUNJABI_FIRST[g])
            last = "Kaur" if g == "female" and rng.random() < 0.4 else rng.choice(PUNJABI_LAST)
            names.append(f"{first} {last}")
        else:
            first = fake.first_name_female() if g == "female" else fake.first_name_male()
            names.append(f"{first} {fake.last_name()}")
    return names


def _languages(rng: np.random.Generator, states: np.ndarray, india: dict) -> np.ndarray:
    by_state = india["language_by_state"]
    out = np.empty(len(states), dtype=object)
    for i, state in enumerate(states):
        if state in by_state:
            mix = by_state[state]
        elif state in india["hindi_belt_states"]:
            mix = by_state["hindi_belt"]
        else:
            mix = by_state["default"]
        out[i] = _pick(rng, mix, 1)[0]
    return out


def festive_multiplier(day: date, india: dict) -> float:
    for event in india["festive_calendar"]:
        if event["start"] <= day <= event["end"]:
            return float(event["multiplier"])
    return 1.0


def hillstrom_relative_lift(
    calibration: dict, recency_months: np.ndarray, history_rank: np.ndarray
) -> np.ndarray:
    """Relative conversion lift from the real RCT, looked up by recency × spend-history cell."""
    hill = calibration["hillstrom"]
    cells = hill["recency_history_cells"]
    hist_counts = np.array(
        [hill["by_segment"]["history_bucket"][b]["n"] for b in ["<100", "100-300", "300-700", "700+"]]
    )
    hist_cut = np.cumsum(hist_counts) / hist_counts.sum()
    hist_labels = np.array(["<100", "100-300", "300-700", "700+"])[np.searchsorted(hist_cut, history_rank)]
    rec_labels = np.select(
        [recency_months <= 3, recency_months <= 6, recency_months <= 9], ["1-3", "4-6", "7-9"], "10-12"
    )
    overall = hill["relative_lift_overall"]
    return np.array(
        [
            cells.get(f"{r}|{h}", {}).get("relative_lift", overall)
            for r, h in zip(rec_labels, hist_labels, strict=True)
        ]
    )


def build_population(
    n: int, as_of: date, seed: int = 42, calibration: dict | None = None, india: dict | None = None
) -> Population:
    calibration = calibration or load_calibration()
    india = india or load_india()
    rng = np.random.default_rng(seed)
    window_start = as_of - timedelta(days=365)

    # --- location, demographics, channel
    tiers = _pick(rng, {t: v["share"] for t, v in india["city_tiers"].items()}, n)
    cities, states = np.empty(n, dtype=object), np.empty(n, dtype=object)
    for tier, spec in india["city_tiers"].items():
        idx = np.flatnonzero(tiers == tier)
        picks = rng.integers(0, len(spec["cities"]), size=len(idx))
        cities[idx] = [spec["cities"][p][0] for p in picks]
        states[idx] = [spec["cities"][p][1] for p in picks]
    genders = _pick(rng, india["gender_mix"], n)
    payment = np.array([_pick(rng, india["payment_mix"][t], 1)[0] for t in tiers])
    prime = rng.random(n) < np.array([india["prime_share"][t] for t in tiers])

    # --- signup: 65% joined before the window (tenure ~ exp, mean 2.5y); 35% during it, more in sale periods
    pre = rng.random(n) < 0.65
    tenure_pre = np.minimum(rng.exponential(2.5 * 365, n), 8 * 365).astype(int) + 1
    window_days = [window_start + timedelta(days=d) for d in range(365)]
    day_w = np.array([festive_multiplier(d, india) for d in window_days])
    in_window = rng.choice(365, size=n, p=day_w / day_w.sum())
    signup = np.array(
        [
            window_start - timedelta(days=int(t)) if p else window_days[int(d)]
            for p, t, d in zip(pre, tenure_pre, in_window, strict=True)
        ]
    )

    # --- BG/NBD purchase process (real heterogeneity shape, Indian mean)
    fit = calibration["retail"]["bgnbd_days"]
    target_rate = india["orders_per_year_active"] / 365
    scale = target_rate / (fit["r"] / fit["alpha"])
    lam = rng.gamma(fit["r"], 1 / fit["alpha"], n) * scale
    p_drop = rng.beta(fit["a"], fit["b"], n)

    # pre-window history: n_pre purchases at rate λ; survive each with prob (1-p)
    n_pre = np.where(pre, rng.poisson(lam * tenure_pre), 0)
    alive = rng.random(n) < (1 - p_drop) ** n_pre
    gap_last = np.minimum(rng.exponential(1 / np.maximum(lam, 1e-6)), tenure_pre)
    last_pre_days = np.where(alive, gap_last, rng.uniform(0, 1, n) * tenure_pre)
    last_order_pre = [
        window_start - timedelta(days=int(d)) if k > 0 else None
        for d, k in zip(last_pre_days, n_pre, strict=True)
    ]

    # --- price sensitivity: tier + COD effects
    tier_shift = np.select([tiers == "tier1", tiers == "tier3"], [-0.08, 0.10], 0.0)
    price_sens = np.clip(rng.beta(2.0, 2.5, n) + tier_shift + 0.05 * (payment == "COD"), 0.02, 0.98)

    # --- category affinity centred on real Amazon category volumes
    categories = list(calibration["category_share"])
    share = np.array([calibration["category_share"][c] for c in categories])
    affinity = rng.dirichlet(share * 4.0, n)

    # --- offer responsiveness from the Hillstrom RCT cell + individual spread + sleeping dogs
    recency_months = np.where(n_pre > 0, np.clip(last_pre_days / 30, 1, 12), 1)
    history_rank = pd.Series(lam * (n_pre + 1)).rank(pct=True).to_numpy() - 1e-9
    base_lift = hillstrom_relative_lift(calibration, recency_months, history_rank)
    sigma = india["responsiveness_sigma"]
    responsiveness = base_lift * rng.lognormal(-(sigma**2) / 2, sigma, n) * (0.6 + 0.8 * price_sens)
    sleeping = rng.random(n) < india["sleeping_dog_share"]
    responsiveness = np.where(sleeping, -rng.uniform(0.2, 0.6, n), responsiveness)

    points = np.where(n_pre > 0, (n_pre * rng.uniform(3, 15, n) * (1 + prime)).astype(int), 0)
    ids = [f"C{i:05d}" for i in range(1, n + 1)]
    customers = pd.DataFrame(
        {
            "customer_id": ids,
            "name": _names(rng, genders, states, seed),
            "age_band": _pick(rng, india["age_band_mix"], n),
            "gender": genders,
            "city": cities,
            "state": states,
            "city_tier": tiers,
            "signup_date": signup,
            "orders_before_window": n_pre.astype(int),
            "last_order_before_window": last_order_pre,
            "acquisition_channel": _pick(rng, india["acquisition_mix"], n),
            "device": _pick(rng, india["device_mix"], n),
            "language_pref": _languages(rng, states, india),
            "prime_member": prime,
            "prime_since": [
                s + timedelta(days=int(rng.integers(0, 200))) if p else None
                for s, p in zip(signup, prime, strict=True)
            ],
            "preferred_payment": payment,
            "points_balance": points,
            "points_expiry_date": [
                as_of + timedelta(days=int(rng.integers(20, 365))) if pt > 0 else None for pt in points
            ],
            "opted_out": rng.random(n) < 0.02,
            "telegram_chat_id": None,
            "twin_of": None,
        }
    )
    traits = pd.DataFrame(
        {
            "customer_id": ids,
            "purchase_rate_per_day": lam,
            "dropout_prob_per_order": p_drop,
            "alive": alive,
            "price_sensitivity": price_sens,
            "offer_responsiveness": responsiveness,
            "sleeping_dog": sleeping,
            "category_affinity": [
                json.dumps(dict(zip(categories, a.round(4).tolist(), strict=True))) for a in affinity
            ],
        }
    )
    return Population(customers, traits, affinity, categories)
