"""Filesystem locations shared by the data pipeline."""

from app.config import REPO_ROOT

RAW_DIR = REPO_ROOT / "data" / "raw"
CALIBRATION_DIR = REPO_ROOT / "calibration"

AMAZON_RAW_DIR = RAW_DIR / "amazon"
RETAIL_RAW = RAW_DIR / "online_retail_ii.parquet"
HILLSTROM_RAW = RAW_DIR / "hillstrom.csv"

CATALOGUE_FILE = CALIBRATION_DIR / "catalogue.parquet"
CALIBRATION_FILE = CALIBRATION_DIR / "calibration.json"
INDIA_FILE = CALIBRATION_DIR / "india.yaml"
REALISM_FILE = CALIBRATION_DIR / "realism_report.json"
