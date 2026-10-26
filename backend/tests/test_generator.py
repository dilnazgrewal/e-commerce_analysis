"""Generator tests run on the committed calibration files — no raw downloads needed."""

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlmodel import SQLModel

from app import models  # noqa: F401  (registers tables)
from app.data.calibrate import load_india
from app.data.generate import build_population, load_calibration, load_catalogue
from app.data.history import generate_history
from app.data.paths import CALIBRATION_FILE, CATALOGUE_FILE
from app.data.realism import build_report

pytestmark = pytest.mark.skipif(
    not (CALIBRATION_FILE.exists() and CATALOGUE_FILE.exists()), reason="run scripts/bootstrap_data.py first"
)
AS_OF = date(2026, 10, 22)


@pytest.fixture(scope="module")
def world():
    calibration, india, catalogue = load_calibration(), load_india(), load_catalogue()
    population = build_population(1500, AS_OF, seed=3, calibration=calibration, india=india)
    history = generate_history(population, catalogue, india, AS_OF, seed=3)
    return population, history.events, history.orders, history.items, catalogue, calibration, india, history


def test_population_columns_match_database_tables(world):
    population, events, orders, items, *_ = world
    tables = SQLModel.metadata.tables
    assert set(population.customers.columns) == set(tables["customers"].columns.keys())
    assert set(population.traits.columns) == set(tables["sim_hidden_traits"].columns.keys())
    assert set(events.columns) == set(tables["events"].columns.keys())
    assert set(orders.columns) == set(tables["orders"].columns.keys())
    assert set(items.columns) <= set(tables["order_items"].columns.keys())


def test_same_seed_gives_identical_population():
    a = build_population(200, AS_OF, seed=11)
    b = build_population(200, AS_OF, seed=11)
    pd.testing.assert_frame_equal(a.customers, b.customers)
    pd.testing.assert_frame_equal(a.traits, b.traits)


def test_profiles_are_plausible(world):
    customers = world[0].customers
    traits = world[0].traits
    assert customers["customer_id"].is_unique
    assert set(customers["city_tier"]) == {"tier1", "tier2", "tier3"}
    assert (pd.to_datetime(customers["signup_date"]).dt.date <= AS_OF).all()
    assert traits["price_sensitivity"].between(0, 1).all()
    assert (traits.loc[traits["sleeping_dog"], "offer_responsiveness"] < 0).all()
    affinity = traits["category_affinity"].map(lambda s: sum(json.loads(s).values()))
    assert affinity.between(0.99, 1.01).all()


def test_event_log_is_consistent(world):
    _, events, orders, items, catalogue, *_ = world
    assert events["event_id"].is_unique and orders["order_id"].is_unique
    assert pd.to_datetime(events["ts"]).max() < pd.Timestamp(AS_OF + timedelta(days=11))
    ordered = set(events.loc[events["type"] == "order", "order_id"])
    assert ordered == set(orders["order_id"])
    assert set(items["product_id"]) <= set(catalogue["product_id"])
    assert (orders["gross_inr"] > 0).all()
    assert (orders["discount_inr"] <= orders["gross_inr"]).all()


def test_synthetic_data_passes_realism_checks(world):
    population, events, orders, items, catalogue, calibration, india, _ = world
    report = build_report(population.customers, events, orders, items, catalogue, calibration, india)
    failed = [c for c in report["checks"] if not c["passed"]]
    assert not failed, failed


def test_models_never_read_hidden_traits():
    """Only the simulator/seed code may touch the ground-truth table."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    allowed = {
        "models/tables.py",
        "models/__init__.py",
        "data/generate.py",
        "data/simulator.py",
        "experiments/policy_lab.py",  # scores policies against ground truth (rule 4 allows it)
    }
    # analytics/, agent/, rules/ … must never appear here
    offenders = [
        str(path.relative_to(app_dir)).replace("\\", "/")
        for path in app_dir.rglob("*.py")
        if str(path.relative_to(app_dir)).replace("\\", "/") not in allowed
        and any(
            token in path.read_text(encoding="utf-8") for token in ("sim_hidden_traits", "SimHiddenTrait")
        )
    ]
    assert not offenders, offenders


def test_past_campaigns_are_randomised_and_offers_lift_orders(world):
    assignments, orders = world[-1].assignments, world[2]
    assert set(assignments["campaign_id"]) == {"CMP-MAR", "CMP-MAY", "CMP-AUG"}
    control_share = (assignments["arm"] == "control").mean()
    assert 0.30 < control_share < 0.40
    merged = assignments.merge(orders[["customer_id", "ts"]], on="customer_id", how="left")
    in_window = (merged["ts"] >= merged["sent_at"]) & (merged["ts"] < merged["valid_until"])
    keys = ["campaign_id", "customer_id", "arm"]
    bought = merged.assign(hit=in_window).groupby(keys)["hit"].any().reset_index()
    rate = bought.groupby(bought["arm"] == "control")["hit"].mean()
    assert rate[False] > rate[True]  # offers raise the 14-day purchase rate on average
    redeemed = orders["coupon_code"].fillna("").str.startswith("CMP-")
    assert redeemed.sum() > 0
