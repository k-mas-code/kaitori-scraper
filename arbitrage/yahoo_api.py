"""Yahoo!ショッピング 商品検索API v3 (itemSearch) クライアント"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

ENDPOINT = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
MIN_INTERVAL = 1.05          # 1 クエリー/秒 の制限
RESULTS_PER_PAGE = 50
MAX_START_PLUS_RESULTS = 1000  # API の上限 (start + results)
MAX_RETRIES = 5
RATE_LIMIT_BACKOFF = 30      # 429 のときの待機秒数 (回数に比例して延ばす)


class YahooApiError(RuntimeError):
    pass


class ItemSearchClient:
    def __init__(self, client_id: str):
        if not client_id:
            raise YahooApiError("YAHOO_CLIENT_ID が未設定 (.env に置く)")
        self._client_id = client_id
        self._session = requests.Session()
        self._last_request = 0.0
        self.request_count = 0

    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def search_jan(self, jan_code: str, start: int = 1) -> dict:
        """JAN で新品・在庫ありを価格の安い順に取得。応答 JSON (totalResultsAvailable, hits) を返す"""
        params = {
            "appid": self._client_id,
            "jan_code": jan_code,
            "condition": "new",
            "in_stock": "true",
            "sort": "+price",
            "results": RESULTS_PER_PAGE,
            "start": start,
        }
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            self.request_count += 1
            try:
                resp = self._session.get(ENDPOINT, params=params, timeout=30)
            except requests.RequestException as e:
                logger.warning("itemSearch failed (%d/%d): %s %s", attempt, MAX_RETRIES, jan_code, e)
                time.sleep(attempt * 2)
                continue
            if resp.status_code == 429:
                wait = RATE_LIMIT_BACKOFF * attempt
                logger.warning("itemSearch 429, sleep %ds (%d/%d)", wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                # Client ID の誤りなど。リトライしても直らないので即停止
                raise YahooApiError(f"itemSearch {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 500:
                logger.warning("itemSearch %d (%d/%d): %s", resp.status_code, attempt, MAX_RETRIES, jan_code)
                time.sleep(attempt * 2)
                continue
            if resp.status_code != 200:
                raise YahooApiError(f"itemSearch {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            if not isinstance(data.get("hits"), list):
                raise YahooApiError(f"itemSearch: hits が無い応答 {str(data)[:200]}")
            return data
        raise YahooApiError(f"itemSearch: {MAX_RETRIES} 回失敗 (jan={jan_code})")
