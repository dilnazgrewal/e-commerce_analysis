"""Copy the local (SQLite) database to the cloud Postgres, slimmed to fit Neon's free 0.5 GB.

Usage (from backend/): uv run python ../scripts/export_cloud_db.py [--events-days 14]

Training happens locally; the cloud only needs what serving reads: profiles, products, scores, orders, past
campaigns, the decision log, bot tables, the simulator traits (Policy Lab) and recent events (Claude's
recent-activity tool). Older browsing events are left out. Target: CLOUD_DATABASE_URL in the repo-root .env.
"""

import argparse
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

import pandas as pd  # noqa: E402
from sqlalchemy import Boolean, Date, DateTime, text  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import get_engine, init_db, make_engine  # noqa: E402

MODEL_TABLES = [
    "customers",
    "products",
    "orders",
    "order_items",
    "campaign_assignments",
    "sim_hidden_traits",
    "offer_decisions",
    "bot_users",
    "feedback",
    "events",
]
PANDAS_TABLES = ["customer_scores", "model_runs"]  # created by pandas, not SQLModel


def coerce(frame: pd.DataFrame, table: str) -> pd.DataFrame:
    """SQLite returns booleans as 0/1 and datetimes as text; Postgres needs the real types."""
    for column in SQLModel.metadata.tables[table].columns:
        if column.name not in frame:
            continue
        if isinstance(column.type, Boolean):
            frame[column.name] = frame[column.name].astype("boolean")
        elif isinstance(column.type, DateTime):
            frame[column.name] = pd.to_datetime(frame[column.name], format="mixed")
        elif isinstance(column.type, Date):
            frame[column.name] = pd.to_datetime(frame[column.name], format="mixed").dt.date
    return frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-days", type=int, default=14)
    args = parser.parse_args()
    cloud_url = get_settings().cloud_database_url
    if cloud_url is None:
        raise SystemExit("CLOUD_DATABASE_URL is not set in the repo-root .env")
    source, target = get_engine(), make_engine(cloud_url.get_secret_value())
    started = time.perf_counter()

    init_db(target, drop=True)
    with target.begin() as conn:
        for table in PANDAS_TABLES:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))

    as_of = pd.read_sql("SELECT MAX(scored_at) AS t FROM customer_scores", source)["t"].iloc[0]
    since = pd.Timestamp(as_of) - timedelta(days=args.events_days)
    for table in MODEL_TABLES + PANDAS_TABLES:
        query = f"SELECT * FROM {table}"
        if table == "events":
            query += f" WHERE ts >= '{since:%Y-%m-%d %H:%M:%S}'"
        frame = pd.read_sql(query, source)
        if table in MODEL_TABLES:
            frame = coerce(frame, table)
            mode = "append"  # keep the SQLModel schema (types, keys, indexes)
        else:
            if "scored_at" in frame:
                frame["scored_at"] = pd.to_datetime(frame["scored_at"], format="mixed")
            mode = "replace"
        if table == "order_items" and "id" in frame:
            frame = frame.drop(columns=["id"])  # let Postgres assign the serial ids
        # Postgres allows at most 65,535 bound values per statement: size batches by column count.
        rows_per_batch = max(1, 60_000 // max(len(frame.columns), 1))
        frame.to_sql(table, target, if_exists=mode, index=False, chunksize=rows_per_batch, method="multi")
        print(f"  {table:22} {len(frame):>9,} rows")

    with target.connect() as conn:
        size = conn.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar()
    print(f"Done in {time.perf_counter() - started:.0f}s. Cloud database size: {size}")


if __name__ == "__main__":
    main()
