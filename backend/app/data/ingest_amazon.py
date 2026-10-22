"""Sample the Amazon Reviews 2023 dataset (McAuley Lab) without downloading the multi-GB files.

The files on HuggingFace support HTTP range requests, so we read N random slices of each file,
drop the partial records at the slice edges, and keep the complete ones:

* product metadata (raw/meta_categories/meta_<Cat>.jsonl) -> titles, brands, USD prices, ratings, images
* user rating histories (benchmark/5core/rating_only/<Cat>.csv) -> real per-user purchase-proxy timelines.
  These CSVs are grouped by user and time-ordered, so a slice holds complete histories for every user
  except the first and last one, which we discard.
"""

import io
import json
import random
from concurrent.futures import ThreadPoolExecutor

import httpx
import pandas as pd

from app.data.paths import AMAZON_RAW_DIR

REPO = "McAuley-Lab/Amazon-Reviews-2023"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"
TREE = f"https://huggingface.co/api/datasets/{REPO}/tree/main"

CATEGORIES = [
    "Electronics",
    "Cell_Phones_and_Accessories",
    "Clothing_Shoes_and_Jewelry",
    "Beauty_and_Personal_Care",
    "Home_and_Kitchen",
    "Grocery_and_Gourmet_Food",
    "Health_and_Household",
    "Sports_and_Outdoors",
    "Toys_and_Games",
    "Books",
]

CHUNK_BYTES = 600_000
META_CHUNKS = 25
RATING_CHUNKS = 16


def file_sizes(folder: str) -> dict[str, int]:
    """Byte size of every file in a dataset folder, from the HuggingFace tree API."""
    entries = httpx.get(f"{TREE}/{folder}", timeout=30).raise_for_status().json()
    return {e["path"].split("/")[-1]: (e.get("lfs") or {}).get("size") or e["size"] for e in entries}


def _fetch_range(client: httpx.Client, url: str, start: int, length: int) -> bytes:
    headers = {"Range": f"bytes={start}-{start + length - 1}"}
    for attempt in range(4):
        try:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError:
            if attempt == 3:
                raise
    raise AssertionError("unreachable")


def _sample_slices(url: str, size: int, n_chunks: int, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    starts = sorted(rng.randrange(0, max(1, size - CHUNK_BYTES)) for _ in range(n_chunks))
    with (
        httpx.Client(follow_redirects=True, timeout=60) as client,
        ThreadPoolExecutor(max_workers=8) as pool,
    ):
        return list(pool.map(lambda s: _fetch_range(client, url, s, CHUNK_BYTES), starts))


def _complete_lines(blob: bytes) -> list[str]:
    """Lines of a slice, minus the partial first and last line."""
    lines = blob.decode("utf-8", errors="replace").split("\n")
    return [line for line in lines[1:-1] if line.strip()]


def _first_image(images: list[dict] | None) -> str | None:
    for image in images or []:
        url = image.get("large") or image.get("hi_res") or image.get("thumb")
        if url:
            return url
    return None


def _parse_price(value) -> float | None:
    if value is None:
        return None
    try:
        price = float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return price if price > 0 else None


def sample_meta(category: str, size: int, seed: int) -> pd.DataFrame:
    rows = []
    for blob in _sample_slices(f"{BASE}/raw/meta_categories/meta_{category}.jsonl", size, META_CHUNKS, seed):
        for line in _complete_lines(blob):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(
                {
                    "parent_asin": item.get("parent_asin"),
                    "title": item.get("title"),
                    "brand": item.get("store"),
                    "main_category": item.get("main_category"),
                    "subcategory": " > ".join(item.get("categories") or []) or None,
                    "price_usd": _parse_price(item.get("price")),
                    "average_rating": item.get("average_rating"),
                    "rating_number": item.get("rating_number"),
                    "image_url": _first_image(item.get("images")),
                    "category": category,
                }
            )
    return pd.DataFrame(rows).drop_duplicates("parent_asin")


def sample_ratings(category: str, size: int, seed: int) -> pd.DataFrame:
    frames = []
    names = ["user_id", "parent_asin", "rating", "timestamp"]
    for blob in _sample_slices(
        f"{BASE}/benchmark/5core/rating_only/{category}.csv", size, RATING_CHUNKS, seed
    ):
        lines = [line for line in _complete_lines(blob) if not line.startswith("user_id,")]
        if not lines:
            continue
        frame = pd.read_csv(io.StringIO("\n".join(lines)), names=names, on_bad_lines="skip")
        users = frame["user_id"].unique()
        # first/last user in a slice may have a truncated history
        frames.append(frame[~frame["user_id"].isin([users[0], users[-1]])])
    ratings = pd.concat(frames, ignore_index=True)
    ratings["category"] = category
    return ratings


def run(seed: int = 7, force: bool = False) -> None:
    AMAZON_RAW_DIR.mkdir(parents=True, exist_ok=True)
    meta_sizes = file_sizes("raw/meta_categories")
    rating_sizes = file_sizes("benchmark/5core/rating_only")
    volume_sizes = file_sizes("benchmark/0core/rating_only")

    # Category volume proxy: size of the full (0-core) rating file ≈ number of real ratings.
    volumes = {c: volume_sizes[f"{c}.csv"] for c in CATEGORIES}
    (AMAZON_RAW_DIR / "category_volume_bytes.json").write_text(json.dumps(volumes, indent=2))

    for i, category in enumerate(CATEGORIES):
        meta_out = AMAZON_RAW_DIR / f"meta_{category}.parquet"
        if force or not meta_out.exists():
            meta = sample_meta(category, meta_sizes[f"meta_{category}.jsonl"], seed + i)
            meta.to_parquet(meta_out, index=False)
            print(f"  meta    {category:28} {len(meta):6} items ({meta['price_usd'].notna().sum()} priced)")
        ratings_out = AMAZON_RAW_DIR / f"ratings_{category}.parquet"
        if force or not ratings_out.exists():
            ratings = sample_ratings(category, rating_sizes[f"{category}.csv"], seed + 100 + i)
            ratings.to_parquet(ratings_out, index=False)
            print(f"  ratings {category:28} {len(ratings):6} rows, {ratings['user_id'].nunique()} users")
