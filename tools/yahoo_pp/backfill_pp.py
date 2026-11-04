"""points.json に PP 加入フラグ (pp) を、既存のキャッシュ (data/yahoo_pp/cache) だけから補う。ネットワークには出ない。

実行: .venv/bin/python -m tools.yahoo_pp.backfill_pp [--dry-run]
  上乗せ調査 (survey_points) と同じ順で店内検索 → 商品ページをキャッシュから読み直し、
  seller.isPromotionPackageApplied を points[sid]["pp"] に入れる。キャッシュに無い店は None のまま。
  そのあと `python -m tools.yahoo_pp.refresh --upload-only` で新しいスナップショットを DB に積む。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .common import CACHE_DIR, Fetcher
from .enrich import MAX_PRODUCT_PAGES, OUT, next_data, promotion_package, search_products

log = logging.getLogger("yahoo_pp.backfill_pp")


class CacheOnlyFetcher(Fetcher):
    """キャッシュに無い URL は 0 (取れない) を返し、ネットワークに出ない"""

    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        super().__init__(cache_dir=cache_dir, interval=0)
        self.misses = 0

    def fetch(self, url: str, *, method: str = "GET", json_body: dict | None = None,
              use_cache: bool = True, timeout: int = 30) -> tuple[int, str, str]:
        path = self._key(method, url, json_body)
        if not path.exists():
            self.misses += 1
            return 0, url, ""
        d = json.loads(path.read_text(encoding="utf-8"))
        return d["status"], d["url"], d["text"]


def pp_from_cache(f: Fetcher, sid: str) -> bool | None:
    items = search_products(f, sid)
    if not items:
        return None
    rates = sorted({r for _, r in items}, reverse=True)
    for rate in rates[:MAX_PRODUCT_PAGES]:
        url = next(u for u, r in items if r == rate)
        if url.startswith("/"):
            url = "https://store.shopping.yahoo.co.jp" + url
        st, _, t = f.fetch(url)
        if st != 200:
            continue
        pp = promotion_package(next_data(t))
        if pp is not None:
            return pp
    return None


def backfill(out: Path = OUT, f: Fetcher | None = None, dry_run: bool = False) -> dict[str, int]:
    path = out / "points.json"
    points: dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
    f = f or CacheOnlyFetcher()
    counts = {"true": 0, "false": 0, "none": 0}
    for sid, p in points.items():
        pp = pp_from_cache(f, sid)
        p["pp"] = pp
        counts["true" if pp is True else "false" if pp is False else "none"] += 1
    log.info("PP 加入 %d / 非加入 %d / 不明 %d (全 %d 店)", counts["true"], counts["false"], counts["none"], len(points))
    if not dry_run:
        path.write_text(json.dumps(points, ensure_ascii=False, indent=1), encoding="utf-8")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="points.json を書き換えずに集計だけ表示")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backfill(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
