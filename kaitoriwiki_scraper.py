#!/usr/bin/env python3
"""買取wiki (kaitori.wiki) 商品・買取価格スクレイパー

検索ページ `/search/{page}/price/{range}/name/all` を巡回:
  - page: 1から商品が無くなるまで
  - range: 1〜5 (5,000円以下 / 10,000 / 20,000 / 30,000 / 50,000円以下)

1ページあたり50〜60件。商品名末尾にJANコード(13桁)が付与されており、
商品URLは外部サブドメイン(iphonekaitori.tokyo, gamekaitori.jp 等)を指す。
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://kaitori.wiki"
# price/N は「N円以下」のフィルター (N=1:5000, 2:10000, 3:20000, 4:30000, 5:50000)
# 5 = 50,000円以下が全件を含むので、5のみ巡回すれば十分
PRICE_RANGES = [5]
PRICE_RANGE_LABELS = {
    1: "5000円以下",
    2: "10000円以下",
    3: "20000円以下",
    4: "30000円以下",
    5: "50000円以下",
}
MAX_PAGES_PER_RANGE = 300

OUTPUT_DIR = Path(__file__).parent / "output"
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
RATE_LIMIT_BACKOFF = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

DOMAIN_TO_CATEGORY = {
    "iphonekaitori.tokyo": "スマートフォン",
    "ipadkaitori.jp": "タブレット",
    "gamekaitori.jp": "ゲーム",
    "kadenkaitori.tokyo": "家電",
    "pckaitori.tokyo": "パソコン・周辺機器",
    "camerakaitori.tokyo": "カメラ",
    "cosmekaitori.jp": "化粧品",
    "kaitori.wiki": "その他",
}

JAN_RE = re.compile(r"(\d{13})\s*$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def fetch_html(url: str) -> BeautifulSoup | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 503:
                logger.warning("503 rate-limited, sleep %ds: %s", RATE_LIMIT_BACKOFF, url)
                time.sleep(RATE_LIMIT_BACKOFF)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            logger.warning("fetch failed (%d/%d): %s %s", attempt, MAX_RETRIES, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)
    return None


def parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def extract_jan(name: str) -> str | None:
    m = JAN_RE.search(name)
    return m.group(1) if m else None


def category_from_url(url: str) -> str:
    if not url:
        return "不明"
    host = urlparse(url).netloc.lower()
    return DOMAIN_TO_CATEGORY.get(host, "その他")


def extract_products(soup: BeautifulSoup, price_range: int) -> list[dict]:
    """tr 配下の td-pic / td-name / td-price から商品データを抽出"""
    products: list[dict] = []
    # 同じテーブルがPC版とモバイル版で重複描画されている可能性があるためJANで後段dedupする
    for tr in soup.select("tr"):
        name_td = tr.select_one("td.td-name")
        price_td = tr.select_one("td.td-price")
        pic_td = tr.select_one("td.td-pic")
        if not (name_td and price_td):
            continue

        a = name_td.select_one("a")
        if not a:
            continue
        name = a.get_text(strip=True)
        detail_url = a.get("href")

        img = pic_td.select_one("img") if pic_td else None
        image_url = img.get("src") if img else None

        price = parse_price(price_td.get_text())
        jan_code = extract_jan(name)
        category = category_from_url(detail_url)

        products.append({
            "name": name,
            "jan_code": jan_code,
            "price": price,
            "image_url": image_url,
            "detail_url": detail_url,
            "category": category,
            "price_range": PRICE_RANGE_LABELS.get(price_range),
        })
    return products


def scrape_range(price_range: int) -> list[dict]:
    """価格帯ごとに全ページを巡回"""
    collected: list[dict] = []
    seen_keys: set[str] = set()

    for page in range(1, MAX_PAGES_PER_RANGE + 1):
        url = f"{BASE_URL}/search/{page}/price/{price_range}/name/all"
        soup = fetch_html(url)
        if soup is None:
            logger.info("  page %d: fetch failed -> stop range %d", page, price_range)
            break

        items = extract_products(soup, price_range)
        # 同一ページ内のPC/モバイル重複を除去
        unique_items: list[dict] = []
        page_keys: set[str] = set()
        for p in items:
            key = p.get("jan_code") or p.get("detail_url") or p.get("name")
            if not key or key in page_keys:
                continue
            page_keys.add(key)
            unique_items.append(p)

        if not unique_items:
            logger.info("  page %d: no items -> stop range %d", page, price_range)
            break

        # 全ページを通じての重複検出 (ループ検出のため)
        new_in_range = 0
        for p in unique_items:
            key = p.get("jan_code") or p.get("detail_url") or p.get("name")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append(p)
            new_in_range += 1

        logger.info("  range=%d page=%d items=%d new=%d (total %d)",
                    price_range, page, len(unique_items), new_in_range, len(collected))

        # 全件既出 = 末尾ループ (最終ページ以降は同じものを返してくる可能性)
        if new_in_range == 0:
            logger.info("  range=%d page=%d all duplicates -> stop", price_range, page)
            break

        time.sleep(REQUEST_INTERVAL)

    return collected


def scrape_all() -> dict:
    result: dict = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "categories": {},
    }

    all_by_key: dict[str, dict] = {}

    for r in PRICE_RANGES:
        logger.info("=== price range %d (%s) ===", r, PRICE_RANGE_LABELS[r])
        items = scrape_range(r)
        added = 0
        for p in items:
            key = p.get("jan_code") or p.get("detail_url") or p.get("name")
            if not key or key in all_by_key:
                continue
            all_by_key[key] = p
            added += 1
        logger.info("range %d done: collected=%d added=%d (grand total %d)",
                    r, len(items), added, len(all_by_key))

    # カテゴリ別に整理
    by_category: dict[str, list[dict]] = {}
    for p in all_by_key.values():
        cat = p.get("category", "不明")
        by_category.setdefault(cat, []).append(p)

    result["categories"] = by_category
    return result


def save_output(data: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    output_path = OUTPUT_DIR / f"kaitoriwiki_{date_str}.json"
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


if __name__ == "__main__":
    data = scrape_all()
    path = save_output(data)
    total = sum(len(v) for v in data["categories"].values())
    logger.info("saved %d products -> %s", total, path)

    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        from db_writer import save_to_db
        counts = save_to_db(data["categories"], source="kaitoriwiki")
        logger.info("DB saved: %s", counts)
    else:
        logger.info("DB skipped (SUPABASE_URL/SUPABASE_KEY not set)")
