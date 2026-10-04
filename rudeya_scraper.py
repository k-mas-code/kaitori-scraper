#!/usr/bin/env python3
"""買取ルデヤ (kaitori-rudeya.com) 商品・買取価格スクレイパー"""

import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://kaitori-rudeya.com"
TOP_URL = f"{BASE_URL}/"
OUTPUT_DIR = Path(__file__).parent / "output"
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
MIN_EXPECTED_PRODUCTS = 1500  # 通常件数の約半分。下回ったら異常とみなし exit 1

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


def fetch_html(url: str) -> BeautifulSoup | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            logger.warning("fetch failed (attempt %d/%d): %s %s", attempt, MAX_RETRIES, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)
    return None


def parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


CATEGORY_HREF_RE = re.compile(r"(?:https?://kaitori-rudeya\.com)?/category/detail/(\d+)/?")
ITEM_ID_RE = re.compile(r"/product/item/(\d+)")
JAN_RE = re.compile(r"\d{8,14}")


def collect_categories(soup: BeautifulSoup) -> dict[int, str]:
    """ナビゲーションから全カテゴリID→名前のマップを返す"""
    categories: dict[int, str] = {}
    for a in soup.select('a[href*="/category/detail/"]'):
        # /category/detail/{id}/product{n} 等の商品リンクは除外 (カテゴリ名が商品名になってしまう)
        m = CATEGORY_HREF_RE.fullmatch(a.get("href", "").strip())
        name = a.get_text(strip=True)
        if not m or not name:
            continue
        categories.setdefault(int(m.group(1)), name)
    return categories


def extract_products(soup: BeautifulSoup, category_name: str) -> list[dict]:
    """article.pgrid-card 1つ = 1商品"""
    products = []
    seen_ids: set[str] = set()

    for card in soup.select("article.pgrid-card"):
        # 商品名・商品詳細ページURL
        link = card.select_one("a.product-card-name-link")
        if not link:
            continue
        name = link.get_text(strip=True)
        detail_url = link.get("href")

        # ページ先頭の「買取強化中」セクションは通常セクションの再掲なので商品IDで除外
        m = ITEM_ID_RE.search(detail_url or "")
        item_key = m.group(1) if m else card.get("id") or name
        if item_key in seen_ids:
            continue
        seen_ids.add(item_key)

        # JANコード ("JAN: 4902370548501" 形式)
        jan_el = card.select_one("span.product-card-jan-text")
        jan_match = JAN_RE.search(jan_el.get_text()) if jan_el else None
        jan_code = jan_match.group(0) if jan_match else None

        # 新品/中古
        cond_el = card.select_one("span.product-card-cond-badge")
        condition = cond_el.get_text(strip=True) if cond_el else None

        # 備考
        note_el = card.select_one("span.product-card-memo")
        note = " ".join(note_el.get_text(" ", strip=True).split()) if note_el else None

        # 画像URL
        img = card.select_one("a.product-card-thumb img")
        image_url = img.get("src") if img else None

        # 買取価格 (「価格未掲載」のカードは要素が無く None)
        price_el = card.select_one("span.product-card-price-value")
        price = parse_price(price_el.get_text()) if price_el else None

        product: dict = {
            "name": name,
            "condition": condition,
            "jan_code": jan_code,
            "price": price,
            "image_url": image_url,
            "detail_url": detail_url,
        }
        if note:
            product["note"] = note

        products.append(product)

    return products


def scrape_all() -> dict:
    result: dict = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "categories": {},
    }

    # トップページからカテゴリ一覧を収集
    logger.info("fetching category list from top page")
    top_soup = fetch_html(TOP_URL)
    if top_soup is None:
        logger.error("failed to fetch top page")
        return result

    categories = collect_categories(top_soup)
    logger.info("found %d categories", len(categories))
    time.sleep(REQUEST_INTERVAL)

    for cat_id, cat_name in sorted(categories.items()):
        url = f"{BASE_URL}/category/detail/{cat_id}"
        logger.info("scraping [%d] %s", cat_id, cat_name)
        # 同名カテゴリ (例: EPSON が2つ) があるので上書きせず結合する
        bucket = result["categories"].setdefault(cat_name, [])

        soup = fetch_html(url)
        if soup is None:
            logger.error("  skipped: fetch failed")
            continue

        products = extract_products(soup, cat_name)
        if not products:
            logger.warning("  no product cards found (layout change?): %s", url)
        bucket.extend(products)
        logger.info("  → %d products", len(products))
        time.sleep(REQUEST_INTERVAL)

    return result


def save_output(data: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    output_path = OUTPUT_DIR / f"rudeya_{date_str}.json"
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


if __name__ == "__main__":
    import os
    data = scrape_all()
    path = save_output(data)
    total = sum(len(v) for v in data["categories"].values())
    logger.info("saved %d products → %s", total, path)

    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        from db_writer import save_to_db
        counts = save_to_db(data["categories"], source="rudeya")
        logger.info("DB saved: %s", counts)
    else:
        logger.info("DB skipped (SUPABASE_URL/SUPABASE_KEY not set)")

    # サイト構造変更やブロックで件数が激減しても「成功」で終わらないようにする
    if total < MIN_EXPECTED_PRODUCTS:
        logger.error("too few products: %d < %d (site layout change or block?)",
                     total, MIN_EXPECTED_PRODUCTS)
        sys.exit(1)
