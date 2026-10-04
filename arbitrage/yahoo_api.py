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
NETWORK_BACKOFF = 10         # 通信失敗・5xx のときの待機秒数 (回数に比例して延ばす)


class YahooApiError(RuntimeError):
    """実行全体を止めるべきエラー (Client ID の誤り、失敗の連続)"""


class JanSearchError(RuntimeError):
    """その JAN だけの失敗 (不正な JAN で 400 など)。記録して次の JAN に進める"""


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

    def _mask(self, text: object) -> str:
        """例外文には URL (appid=...) が入るので、Client ID をログに出さない"""
        return str(text).replace(self._client_id, "***")

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
                logger.warning("itemSearch failed (%d/%d): %s %s", attempt, MAX_RETRIES, jan_code, self._mask(e))
                time.sleep(NETWORK_BACKOFF * attempt)
                continue
            if resp.status_code == 429:
                wait = RATE_LIMIT_BACKOFF * attempt
                logger.warning("itemSearch 429, sleep %ds (%d/%d)", wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                # Client ID の誤りなど。リトライしても直らないので即停止
                raise YahooApiError(f"itemSearch {resp.status_code}: {self._mask(resp.text[:200])}")
            if resp.status_code >= 500:
                logger.warning("itemSearch %d (%d/%d): %s", resp.status_code, attempt, MAX_RETRIES, jan_code)
                time.sleep(NETWORK_BACKOFF * attempt)
                continue
            if resp.status_code != 200:
                raise JanSearchError(f"itemSearch {resp.status_code}: {self._mask(resp.text[:200])}")
            try:
                data = resp.json()
            except ValueError:
                data = None
            if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
                # 200 でも HTML のエラーページ等が返ることがある → 再試行
                logger.warning("itemSearch: unexpected body (%d/%d): %s", attempt, MAX_RETRIES, jan_code)
                time.sleep(NETWORK_BACKOFF * attempt)
                continue
            return data
        raise JanSearchError(f"itemSearch: {MAX_RETRIES} 回失敗 (jan={jan_code})")
