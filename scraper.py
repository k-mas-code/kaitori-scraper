#!/usr/bin/env python3
"""買取商店 (kaitorishouten-co.jp) 商品・買取価格スクレイパー

大カテゴリ (keitai/kaden/nitiyouhin) ごとに:
  1. 「すべて」ページのAjax API (list_{cat}_new/N) を全ページ取得
  2. 各サブカテゴリAPI (products/{N}/list_category/{ID}) を全ページ取得
  3. JANコード（または商品名）で重複排除して合算
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString

BASE_URL = "https://www.kaitorishouten-co.jp"
CATEGORIES = ["keitai", "kaden", "nitiyouhin"]

OUTPUT_DIR = Path(__file__).parent / "output"
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
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


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch(session: requests.Session, url: str, referer: str | None = None) -> str | None:
    headers = {"Referer": referer} if referer else {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers, timeout=30)
            if resp.status_code == 500:
                # ページ範囲外と判断
                return None
            if resp.status_code == 503:
                # レート制限 → 長めに待ってリトライ
                logger.warning("503 rate-limited, sleep %ds: %s", RATE_LIMIT_BACKOFF, url)
                time.sleep(RATE_LIMIT_BACKOFF)
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logger.warning("fetch failed (%d/%d): %s %s", attempt, MAX_RETRIES, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)
    return None


def parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def extract_deduction_options(scope) -> list[dict]:
    options = []
    for label in scope.select("label.form-check-label"):
        text = label.get_text(strip=True)
        m = re.search(r"(.+?)\s*([+-]\d[\d,]*)\s*円", text)
        if m:
            options.append({"label": m.group(1).strip(), "amount": int(m.group(2).replace(",", ""))})
    return options


def extract_card_products(soup: BeautifulSoup) -> list[dict]:
    """「すべて」ページのカード形式から商品を抽出"""
    products = []
    for item in soup.select("div.item.item-thumbnail.item-product-list"):
        title_el = item.select_one("h4.item-title")
        if not title_el:
            continue
        name = title_el.get_text(separator=" ", strip=True)

        code_els = item.select("span.product-code-default")
        jan_code = next((c.get_text(strip=True) for c in code_els if c.get_text(strip=True).isdigit()), None)

        img = item.select_one("div.item-image img")
        image_url = img["src"] if img else None

        price_els = item.select("div.item-price.encrypt-price.plain-price")
        prices: dict = {}
        if len(price_els) >= 1:
            prices["new"] = parse_price(price_els[0].get_text(strip=True))
        if len(price_els) >= 2:
            prices["used"] = parse_price(price_els[1].get_text(strip=True))

        deductions = extract_deduction_options(item)

        product: dict = {"name": name, "jan_code": jan_code, "image_url": image_url, "prices": prices}
        if deductions:
            product["deduction_options"] = deductions
        products.append(product)
    return products


def extract_table_products(soup: BeautifulSoup) -> list[dict]:
    """サブカテゴリAPIのテーブル形式から商品を抽出"""
    products = []
    for tr in soup.select('tr[id^="ex-product-"]'):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue

        # 商品名: 2番目のtdの直接の子テキスト
        name_td = tds[1]
        name_parts = []
        for child in name_td.children:
            if isinstance(child, NavigableString):
                t = str(child).strip()
                if t:
                    name_parts.append(t)
            else:
                break
        name = " ".join(name_parts).strip()
        if not name:
            continue

        # JAN
        jan_code = next(
            (sp.get_text(strip=True) for sp in name_td.select("span.product-code-default")
             if sp.get_text(strip=True).isdigit()),
            None,
        )

        # 画像
        img = tds[0].select_one("img") if tds else None
        image_url = img["src"] if img else None

        # 価格（trの中の item-price 要素）
        price_els = tr.select("div.item-price.encrypt-price.plain-price")
        prices: dict = {}
        if len(price_els) >= 1:
            prices["new"] = parse_price(price_els[0].get_text(strip=True))
        if len(price_els) >= 2:
            prices["used"] = parse_price(price_els[1].get_text(strip=True))

        deductions = extract_deduction_options(tr)

        product: dict = {"name": name, "jan_code": jan_code, "image_url": image_url, "prices": prices}
        if deductions:
            product["deduction_options"] = deductions
        products.append(product)
    return products


def discover_endpoints(html: str) -> tuple[str | None, list[str]]:
    """ページHTMLから:
    - 「すべて」のAjaxエンドポイント (e.g. /products/list_keitai_new/9)
    - 全サブカテゴリエンドポイント (e.g. /products/1/list_category/17)
    を抽出して返す
    """
    all_endpoint = None
    m = re.search(r'var topUrl\s*=\s*"([^"]+)"', html)
    if m:
        all_endpoint = m.group(1)

    sub_endpoints = sorted(set(
        re.findall(r"/products/\d+/list_category/\d+(?:/list_tag/\d+)?", html)
    ))
    # /list_tag/ を含むものはタグフィルタなので除外（親カテゴリで取得済み）
    sub_endpoints = [e for e in sub_endpoints if "/list_tag/" not in e]
    return all_endpoint, sub_endpoints


def crawl_paginated(session: requests.Session, base_url: str, endpoint: str,
                    referer: str, extractor) -> list[dict]:
    """endpoint を pageno=1,2,... と回して全商品を取得"""
    all_products: list[dict] = []
    pageno = 1
    while True:
        url = f"{base_url}{endpoint}?pageno={pageno}"
        html = fetch(session, url, referer=referer)
        if html is None:
            break
        soup = BeautifulSoup(html, "lxml")
        products = extractor(soup)
        if not products:
            break
        all_products.extend(products)
        pageno += 1
        time.sleep(REQUEST_INTERVAL)
    return all_products


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


def scrape_category(session: requests.Session, category: str) -> list[dict]:
    top_url = f"{BASE_URL}/{category}"
    html = fetch(session, top_url)
    if html is None:
        logger.error("failed to fetch top page: %s", category)
        return []

    all_endpoint, sub_endpoints = discover_endpoints(html)
    logger.info("  all-endpoint: %s", all_endpoint)
    logger.info("  sub-endpoints: %d", len(sub_endpoints))

    bucket: dict[str, dict] = {}

    # 1. 「すべて」
    if all_endpoint:
        items = crawl_paginated(session, BASE_URL, all_endpoint, top_url, extract_card_products)
        added = merge_unique(bucket, items)
        logger.info("  [all] %d items, +%d new (total %d)", len(items), added, len(bucket))

    # 2. 各サブカテゴリ
    for ep in sub_endpoints:
        items = crawl_paginated(session, BASE_URL, ep, top_url, extract_table_products)
        added = merge_unique(bucket, items)
        logger.info("  [%s] %d items, +%d new (total %d)", ep, len(items), added, len(bucket))

    return list(bucket.values())


def scrape_all() -> dict:
    result: dict = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "categories": {},
    }
    for category in CATEGORIES:
        logger.info("=== category: %s ===", category)
        session = make_session()
        products = scrape_category(session, category)
        result["categories"][category] = products
        logger.info("category %s done: %d products", category, len(products))
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
