"""Generate the platform's 12-month history, including two past randomised campaigns.

Real platforms learn offer uplift from their own A/B history. We give the synthetic platform the same:
two campaigns in the window randomly split customers into a control group and five offer arms. The
uplift models train only on these assignments and the resulting orders, never on hidden traits.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from app.data.generate import Population
from app.data.simulator import SimState, assignments_frame, new_state, run_campaign, simulate

CAMPAIGN_ARMS: dict[str, tuple[str, float] | None] = {
    "control": None,
    "nudge": ("CART_NUDGE", 0),
    "points2x": ("BONUS_POINTS", 2),
    "freeship": ("FREE_SHIPPING", 0),
    "pct10": ("PCT_OFF", 10),
    "flat100": ("FLAT_OFF", 100),
}
CAMPAIGN_WEIGHTS = {
    "control": 0.35,
    "nudge": 0.13,
    "points2x": 0.13,
    "freeship": 0.13,
    "pct10": 0.13,
    "flat100": 0.13,
}
# (campaign id, days before as_of)
CAMPAIGNS = [("CMP-MAR", 225), ("CMP-MAY", 150), ("CMP-AUG", 75)]
CAMPAIGN_VALID_DAYS = 14


@dataclass
class History:
    state: SimState
    events: pd.DataFrame
    orders: pd.DataFrame
    items: pd.DataFrame
    assignments: pd.DataFrame


def generate_history(
    population: Population, catalogue: pd.DataFrame, india: dict, as_of: date, seed: int
) -> History:
    state = new_state(population, catalogue, india, seed)
    cursor = as_of - timedelta(days=365)
    chunks = []
    for campaign_id, days_before in CAMPAIGNS:
        launch = as_of - timedelta(days=days_before)
        chunks.append(simulate(state, cursor, launch))
        run_campaign(state, campaign_id, launch, CAMPAIGN_ARMS, CAMPAIGN_WEIGHTS, CAMPAIGN_VALID_DAYS)
        cursor = launch
    chunks.append(simulate(state, cursor, as_of))
    events, orders, items = (pd.concat(parts, ignore_index=True) for parts in zip(*chunks, strict=True))
    return History(state, events, orders, items, assignments_frame(state))
