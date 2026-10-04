#!/usr/bin/env python3
"""買取商店 (kaitorishouten-co.jp) 商品・買取価格スクレイパー

サイトは React SPA で、商品データは公開 JSON API (/api/v1) から取得する:
  1. /api/v1/categories で最上位カテゴリ (スマホ/家電/お酒) の ID を名前から引く
  2. /api/v1/products?per_page=100&page=N&category_id=ID を全ページ取得
     (親カテゴリIDを指定すると配下サブカテゴリの商品もすべて返る)
  3. JANコード（または商品名）で重複排除
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_URL = "https://www.kaitorishouten-co.jp"
API_URL = f"{BASE_URL}/api/v1"
# 出力キー (旧URLパス名。DB の category 値を変えないため維持) → API の最上位カテゴリ名
CATEGORIES = {"keitai": "スマホ", "kaden": "家電", "nitiyouhin": "お酒"}
PER_PAGE = 100  # サーバ側上限 (省略時は20)
RANK_TO_CONDITION = {1: "new", 2: "used"}
# JAN/UPC/GTIN として採用する桁数 (「送料」「ポイント」等のダミー "111" "777" を除外)
JAN_LENGTHS = {8, 12, 13, 14}

OUTPUT_DIR = Path(__file__).parent / "output"
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
MIN_EXPECTED_PRODUCTS = 4000  # 通常件数の約半分。下回ったら異常とみなし exit 1
RATE_LIMIT_BACKOFF = 30  # 503 のときの待機秒数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


class ApiFormatError(RuntimeError):
    """API 応答が想定した形でない (サイト側の仕様変更の疑い)"""


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_json(session: requests.Session, path: str, referer: str | None = None) -> dict | None:
    url = f"{API_URL}{path}"
    headers = {"Referer": referer} if referer else {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers, timeout=30)
            if resp.status_code == 503:
                # レート制限 → 長めに待ってリトライ
                logger.warning("503 rate-limited, sleep %ds: %s", RATE_LIMIT_BACKOFF, url)
                time.sleep(RATE_LIMIT_BACKOFF)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("fetch failed (%d/%d): %s %s", attempt, MAX_RETRIES, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)
    return None


def normalize_jan(raw) -> str | None:
    """数字のみ・妥当な桁数のものだけ JAN として採用。
    キャリア版の "840353956209dc" のような接尾辞つきは None (剥がすと SIMフリー版と衝突する)"""
    jan = str(raw or "").strip()
    return jan if jan.isdigit() and len(jan) in JAN_LENGTHS else None


def to_product(item: dict) -> dict | None:
    """API の商品 1 件を db_writer が読む dict に変換"""
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    jan_code = normalize_jan(item.get("jan"))

    prices: dict = {}
    deductions: list[dict] = []
    if not item.get("price_undecided"):
        for rank in item.get("rank_options") or []:
            cond = RANK_TO_CONDITION.get(rank.get("rank_type_id"))
            base = rank.get("base")
            if cond and isinstance(base, int) and cond not in prices:
                prices[cond] = base
            for opt in rank.get("options") or []:
                amount = opt.get("price_change")
                if not isinstance(amount, int) or amount == 0:
                    continue
                opt_name = str(opt.get("name") or "")
                # "利用制限△ -5000円" → "利用制限△"
                label = re.sub(r"\s*[+-]\s*[\d,]+\s*円\s*$", "", opt_name).strip() or opt_name
                deduction = {"label": label, "amount": amount}
                if deduction not in deductions:
                    deductions.append(deduction)

    if jan_code:
        detail_url = f"{BASE_URL}/products/list?name={jan_code}"
    else:
        detail_url = f"{BASE_URL}/products/detail/{item.get('id')}"

    product: dict = {
        "name": name,
        "jan_code": jan_code,
        "image_url": item.get("image_url"),
        "prices": prices,
        "detail_url": detail_url,
    }
    if deductions:
        product["deduction_options"] = deductions
    return product


def fetch_top_category_ids(session: requests.Session) -> dict[str, int]:
    """最上位カテゴリ名 → ID"""
    data = fetch_json(session, "/categories", referer=f"{BASE_URL}/")
    if data is None:
        raise ApiFormatError("failed to fetch /categories")
    items = data.get("items")
    if not isinstance(items, list):
        raise ApiFormatError(f"/categories: unexpected response keys {list(data)[:10]}")
    return {c["name"]: c["id"] for c in items if c.get("hierarchy") == 1}


def crawl_category(session: requests.Session, category_id: int, referer: str) -> list[dict]:
    """category_id の商品を page=1,2,... と回して全件取得"""
    raw_items: list[dict] = []
    total = 0
    page = 1
    while True:
        path = f"/products?per_page={PER_PAGE}&page={page}&category_id={category_id}"
        data = fetch_json(session, path, referer=referer)
        if data is None:
            logger.error("  page %d: fetch failed -> stop (fetched %d)", page, len(raw_items))
            break
        items, total = data.get("items"), data.get("total")
        if not isinstance(items, list) or not isinstance(total, int):
            raise ApiFormatError(f"{path}: unexpected response keys {list(data)[:10]}")
        if not items:
            if page == 1 and total > 0:
                raise ApiFormatError(f"{path}: total={total} but no items")
            break
        raw_items.extend(items)
        if len(raw_items) >= total:
            break
        page += 1
        time.sleep(REQUEST_INTERVAL)

    if len(raw_items) < total:
        logger.warning("  fetched %d of total %d", len(raw_items), total)
    return [p for p in (to_product(i) for i in raw_items) if p]


def merge_unique(existing: dict[str, dict], new_items: list[dict]) -> int:
    """JANコード（無ければ商品名）をキーに重複排除しながらマージ。追加件数を返す"""
    added = 0
    for p in new_items:
        key = p.get("jan_code") or p.get("name")
        if not key or key in existing:
            continue
        existing[key] = p
        added += 1
    return added


def scrape_all() -> dict:
    result: dict = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "categories": {},
    }
    session = make_session()
    top_ids = fetch_top_category_ids(session)
    time.sleep(REQUEST_INTERVAL)

    for category, api_name in CATEGORIES.items():
        logger.info("=== category: %s (%s) ===", category, api_name)
        if api_name not in top_ids:
            raise ApiFormatError(f"top category {api_name!r} not found in {sorted(top_ids)}")
        items = crawl_category(session, top_ids[api_name], referer=f"{BASE_URL}/{category}")
        bucket: dict[str, dict] = {}
        merge_unique(bucket, items)
        result["categories"][category] = list(bucket.values())
        logger.info("category %s done: %d products (%d before dedup)", category, len(bucket), len(items))
        time.sleep(REQUEST_INTERVAL)
    return result


def save_output(data: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    output_path = OUTPUT_DIR / f"kaitorishouten_{date_str}.json"
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


if __name__ == "__main__":
    data = scrape_all()
    path = save_output(data)
    total = sum(len(v) for v in data["categories"].values())
    logger.info("saved %d products → %s", total, path)

    # SUPABASE_URL / SUPABASE_KEY が設定されていれば DB にも保存
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        from db_writer import save_to_db
        counts = save_to_db(data["categories"], source="kaitorishouten")
        logger.info("DB saved: %s", counts)
    else:
        logger.info("DB skipped (SUPABASE_URL/SUPABASE_KEY not set)")

    # サイト構造変更やブロックで件数が激減しても「成功」で終わらないようにする
    if total < MIN_EXPECTED_PRODUCTS:
        logger.error("too few products: %d < %d (site layout change or block?)",
                     total, MIN_EXPECTED_PRODUCTS)
        sys.exit(1)
