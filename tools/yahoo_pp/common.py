"""Yahoo!ショッピング 店舗リスト再取得の共通部品 (HTTP・キャッシュ・間隔制御)

HANDOFF_yahoo_pp_store_list.md §2 / §4 に従う:
- 1 並列・リクエスト間隔 2.5 秒
- HTTP 489 (アクセス制限) を受けたら即座に止める (RateLimited)
- 取得した本文は URL 単位でキャッシュし、同じ URL を二度叩かない
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "yahoo_pp" / "cache"
INTERVAL = 2.5
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
}
log = logging.getLogger(__name__)


class RateLimited(Exception):
    """HTTP 489: アクセス制限。時間をおいて再開する"""


class Cancelled(Exception):
    """中止の依頼 (refresh --request-id の cancel_requested) を受けて、店 1 件の区切りで止めた"""


# 各段階の関数は on_progress(段階名, 済み件数, 全件数) と should_stop() -> bool を受け取る (どちらも省略可)
def check_stop(should_stop) -> None:
    """should_stop() が True なら Cancelled (店 1 件の区切りで呼ぶ)"""
    if should_stop is not None and should_stop():
        raise Cancelled()


class Fetcher:
    def __init__(self, cache_dir: Path = CACHE_DIR, interval: float = INTERVAL):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._last = 0.0
        self.requests_made = 0

    def _key(self, method: str, url: str, body: dict | None) -> Path:
        raw = method + " " + url + (" " + json.dumps(body, sort_keys=True) if body else "")
        return self.cache_dir / (hashlib.sha1(raw.encode()).hexdigest() + ".json")

    def _wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.interval:
            time.sleep(self.interval - delta)
        self._last = time.monotonic()

    def fetch(self, url: str, *, method: str = "GET", json_body: dict | None = None,
              use_cache: bool = True, timeout: int = 30) -> tuple[int, str, str]:
        """(status, final_url, text)。キャッシュがあればネットワークに出ない"""
        path = self._key(method, url, json_body)
        if use_cache and path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return d["status"], d["url"], d["text"]
        self._wait()
        self.requests_made += 1
        resp = self.session.request(method, url, json=json_body, timeout=timeout,
                                    allow_redirects=True)
        if resp.status_code == 489:
            raise RateLimited(url)
        resp.encoding = resp.encoding or "utf-8"
        text = resp.text
        if resp.ok:
            path.write_text(json.dumps({"status": resp.status_code, "url": resp.url,
                                        "text": text}, ensure_ascii=False), encoding="utf-8")
        return resp.status_code, resp.url, text
