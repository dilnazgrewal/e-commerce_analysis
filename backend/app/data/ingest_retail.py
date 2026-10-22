"""Download real transaction data: UCI Online Retail II and the Hillstrom e-mail experiment."""

import io
import zipfile

import httpx
import pandas as pd

from app.data.paths import HILLSTROM_RAW, RETAIL_RAW

UCI_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
HILLSTROM_URL = "http://www.minethatdata.com/Kevin_Hillstrom_MineThatData_E-MailAnalytics_DataMiningChallenge_2008.03.20.csv"


def fetch_online_retail(force: bool = False) -> None:
    """~1M real UK online-retail transaction lines (2009-12 → 2011-12), stored as parquet."""
    if RETAIL_RAW.exists() and not force:
        return
    RETAIL_RAW.parent.mkdir(parents=True, exist_ok=True)
    zip_path = RETAIL_RAW.with_suffix(".zip")
    if force or not zip_path.exists():
        zip_path.write_bytes(
            httpx.get(UCI_URL, follow_redirects=True, timeout=300).raise_for_status().content
        )
    with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as archive:
        xlsx_name = next(n for n in archive.namelist() if n.endswith(".xlsx"))
        text_cols = {"Invoice": str, "StockCode": str, "Description": str, "Country": str}
        sheets = pd.read_excel(archive.open(xlsx_name), sheet_name=None, dtype=text_cols)
    retail = pd.concat(sheets.values(), ignore_index=True)
    retail = retail.rename(columns={"Customer ID": "CustomerID"})
    retail.to_parquet(RETAIL_RAW, index=False)
    print(f"  retail  {len(retail):,} transaction lines, {retail['CustomerID'].nunique():,} customers")


def fetch_hillstrom(force: bool = False) -> None:
    """64,000 customers randomly assigned to Men's e-mail / Women's e-mail / no e-mail (a real RCT)."""
    if HILLSTROM_RAW.exists() and not force:
        return
    HILLSTROM_RAW.parent.mkdir(parents=True, exist_ok=True)
    HILLSTROM_RAW.write_bytes(httpx.get(HILLSTROM_URL, timeout=120).raise_for_status().content)
    print(f"  hillstrom {len(pd.read_csv(HILLSTROM_RAW)):,} customers")
