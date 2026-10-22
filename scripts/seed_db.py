"""Generate the synthetic population + 12-month history, load it into the database, write the realism report.

Usage (from backend/): uv run python ../scripts/seed_db.py [--customers 5000] [--seed 42] [--as-of YYYY-MM-DD]
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

from app.data.calibrate import load_india  # noqa: E402
from app.data.generate import build_population, load_calibration, load_catalogue  # noqa: E402
from app.data.history import generate_history  # noqa: E402
from app.data.paths import REALISM_FILE  # noqa: E402
from app.data.realism import build_report  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customers", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    started = time.perf_counter()
    calibration, india, catalogue = load_calibration(), load_india(), load_catalogue()
    population = build_population(args.customers, args.as_of, args.seed, calibration, india)
    history = generate_history(population, catalogue, india, args.as_of, args.seed)
    events, orders, items = history.events, history.orders, history.items
    population.traits["alive"] = history.state.alive
    population.customers["points_balance"] = history.state.points
    print(f"Simulated {len(events):,} events, {len(orders):,} orders in {time.perf_counter() - started:.0f}s")

    report = build_report(population.customers, events, orders, items, catalogue, calibration, india)
    REALISM_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for check in report["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(
            f"  [{mark}] {check['name']:30} stat={check['statistic']:.3f} (≤ {check['threshold']})"
            f"  synthetic={check['synthetic']} real={check['real']}"
        )

    engine = get_engine()
    init_db(engine, drop=True)
    tables = [
        ("customers", population.customers),
        ("sim_hidden_traits", population.traits),
        ("products", catalogue),
        ("events", events),
        ("orders", orders),
        ("order_items", items),
        ("campaign_assignments", history.assignments),
    ]
    for name, frame in tables:
        frame.to_sql(name, engine, if_exists="append", index=False, chunksize=20_000)
        print(f"  loaded {name:18} {len(frame):>9,} rows")
    print(f"Done in {time.perf_counter() - started:.0f}s. Realism report → {REALISM_FILE}")


if __name__ == "__main__":
    main()
