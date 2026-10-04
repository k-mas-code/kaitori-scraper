"""買取価格の読み込み: Supabase の price_history から JAN ごとの新品最高値を作る"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .config import ROOT

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
LOOKBACK_DAYS = 7
SOURCES = ("kaitorishouten", "rudeya", "kaitoriwiki")
# kaitoriwiki は新品の買取価格だけを扱う。過去行は condition='used' で保存されていたので、
# DB の修正前でも拾えるよう source で新品扱いにする
ALWAYS_NEW_SOURCES = {"kaitoriwiki"}


@dataclass(frozen=True)
class Buyback:
    jan_code: str
    price: int
    source: str
    scraped_date: str


def read_credentials() -> tuple[str, str]:
    """読み取り用の URL とキー。.env の service_role があればそれ、無ければ公開 anon キー"""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if url and key:
        return url.rstrip("/"), key
    config_js = (ROOT / "docs" / "config.js").read_text(encoding="utf-8")
    url_m = re.search(r"https://[a-z0-9]+\.supabase\.co", config_js)
    key_m = re.search(r"eyJ[\w\-.]+", config_js)
    if not (url_m and key_m):
        raise RuntimeError("docs/config.js から Supabase の URL / anon キーを読めない")
    return url_m.group(0), key_m.group(0)


def _is_new(row: dict) -> bool:
    return row["condition"] == "new" or row["source"] in ALWAYS_NEW_SOURCES


def load_buyback_prices(today: date, lookback_days: int = LOOKBACK_DAYS) -> dict[str, Buyback]:
    """直近 lookback_days 日の新品価格から、JAN ごとに「各買取店の最新価格」の最高値を返す"""
    url, key = read_credentials()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    since = (today - timedelta(days=lookback_days)).isoformat()
    params = {
        "select": "jan_code,source,condition,scraped_date,price",
        "scraped_date": f"gte.{since}",
        "price": "gt.0",
        # ページ送りが安定するよう主キー順
        "order": "jan_code,source,condition,scraped_date",
    }

    latest: dict[tuple[str, str], dict] = {}   # (jan, source) → 最新の新品行
    offset = 0
    while True:
        resp = requests.get(
            f"{url}/rest/v1/price_history", params=params, timeout=60,
            headers={**headers, "Range": f"{offset}-{offset + PAGE_SIZE - 1}"},
        )
        resp.raise_for_status()
        rows = resp.json()
        for row in rows:
            if row["source"] not in SOURCES or not _is_new(row):
                continue
            k = (row["jan_code"], row["source"])
            if k not in latest or row["scraped_date"] > latest[k]["scraped_date"]:
                latest[k] = row
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    best: dict[str, Buyback] = {}
    for (jan, source), row in latest.items():
        if jan not in best or row["price"] > best[jan].price:
            best[jan] = Buyback(jan, row["price"], source, row["scraped_date"])
    logger.info("buyback prices: %d JANs (since %s, %d source rows)", len(best), since, len(latest))
    return best
