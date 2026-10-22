"""Download (sampled) real datasets into data/raw/ and rebuild calibration/.

Usage (from backend/):  uv run python ../scripts/bootstrap_data.py [--force] [--skip-download]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

from app.data import calibrate, ingest_amazon, ingest_retail  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-download even if files exist")
    parser.add_argument("--skip-download", action="store_true", help="only rebuild calibration from data/raw")
    args = parser.parse_args()

    if not args.skip_download:
        print("Hillstrom e-mail experiment…")
        ingest_retail.fetch_hillstrom(args.force)
        print("Amazon Reviews 2023 (random-slice sample)…")
        ingest_amazon.run(force=args.force)
        print("UCI Online Retail II…")
        ingest_retail.fetch_online_retail(args.force)

    print("Calibrating…")
    calibrate.run()
    print("Done.")


if __name__ == "__main__":
    main()
