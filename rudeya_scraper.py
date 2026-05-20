#!/usr/bin/env python3
"""買取ルデヤ (kaitori-rudeya.com) 商品・買取価格スクレイパー"""

import json
import logging
import re
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


def collect_categories(soup: BeautifulSoup) -> dict[int, str]:
    """ナビゲーションから全カテゴリID→名前のマップを返す"""
    categories: dict[int, str] = {}
    for a in soup.select('a[href*="/category/detail/"]'):
        href = a.get("href", "")
        name = a.get_text(strip=True)
        part = href.split("/category/detail/")[-1].split("/")[0].split("#")[0]
        if name and part.isdigit():
            cat_id = int(part)
            if cat_id not in categories:
                categories[cat_id] = name
    return categories


def extract_products(soup: BeautifulSoup, category_name: str) -> list[dict]:
    products = []
    tds = soup.select("div.td")

    # td1 → td2 → td3 → td4 の4要素が1セット
    i = 0
    while i < len(tds):
        td = tds[i]
        if "td1" not in td.get("class", []):
            i += 1
            continue

        # 商品名
        h2 = td.select_one("div.ttl h2")
        if not h2:
            i += 1
            continue
        name = h2.get_text(strip=True)

        # 商品詳細ページURL
        detail_link = td.select_one("div.ttl a")
        detail_url = detail_link["href"] if detail_link else None

        # JANコード
        jan_el = td.select_one("p.janc")
        jan_code = jan_el.get_text(strip=True) if jan_el else None

        # 新品/中古
        cond_el = td.select_one("div.cond span")
        condition = cond_el.get_text(strip=True) if cond_el else None

        # 備考（赤文字）
        note_el = td.select_one("div.ttl p[style*='red']")
        note = note_el.get_text(strip=True) if note_el else None

        # 画像URL
        img = td.select_one("div.tham img")
        image_url = img["src"] if img else None

        # 買取価格（次のtd2を探す）
        price = None
        if i + 1 < len(tds) and "td2" in tds[i + 1].get("class", []):
            price_text = tds[i + 1].select_one("div.td2wrap")
            if price_text:
                price = parse_price(price_text.get_text())

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
        i += 4  # td1/td2/td3/td4 をスキップ

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

        soup = fetch_html(url)
        if soup is None:
            logger.error("  skipped: fetch failed")
            result["categories"][cat_name] = []
            continue

        products = extract_products(soup, cat_name)
        result["categories"][cat_name] = products
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
