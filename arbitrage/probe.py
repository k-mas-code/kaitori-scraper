"""取りこぼしの調査: JAN 1 件について、API が返した店ごとに「候補になるか / ならない理由」を表示する。

python -m arbitrage.probe JAN [JAN ...] [--date YYYY-MM-DD] [--pages]
--pages を付けると、店舗リストの店については商品ページも取得して本来の式で計算する (1 店 2 秒以上かかる)。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

from .buyback import load_buyback_prices
from .config import ROOT, ConfigError, active_config
from .coupons import best_manual_discount
from .points import optimistic_effective_price
from .prefilter import top_genre
from .prefilter import max_upsell, optimistic_components, realistic_gap
from .run import check_candidate, load_settings, search_variants
from .stores import StoreListError, load_stores
from .yahoo_api import MAX_START_PLUS_RESULTS, RESULTS_PER_PAGE, ItemSearchClient, YahooApiError
from .yahoo_page import PageClient

logger = logging.getLogger(__name__)


def all_hits(client: ItemSearchClient, jan_code: str) -> list[dict]:
    """JAN の検索結果を全ページ集める (絞り込みはしない)"""
    hits: list[dict] = []
    for jan in search_variants(jan_code):
        start = 1
        while start + RESULTS_PER_PAGE <= MAX_START_PLUS_RESULTS + 1:
            data = client.search_jan(jan, start=start)
            page = data["hits"]
            hits += page
            available = data.get("totalResultsAvailable") or 0
            if not page or start - 1 + len(page) >= available:
                break
            start += RESULTS_PER_PAGE
        if hits:
            break
    return hits


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m arbitrage.probe", description=__doc__)
    p.add_argument("jan", nargs="+")
    p.add_argument("--date", type=date.fromisoformat, default=None, help="購入予定日 (既定は設定の値)")
    p.add_argument("--coupon-margin", type=float, default=0.2)
    p.add_argument("--pages", action="store_true", help="店舗リストの店は商品ページも取得して本来の式で計算する")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")
    try:
        settings, _ = load_settings(None)
        run_date = args.date or settings.purchase_date
        cfg = active_config(settings, run_date)
        stores = load_stores(run_date)
        client = ItemSearchClient(os.environ.get("YAHOO_CLIENT_ID", ""))
    except (ConfigError, StoreListError, YahooApiError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    buyback = load_buyback_prices(min(run_date, date.today()))
    pages = PageClient() if args.pages else None

    for jan in args.jan:
        bb = buyback.get(jan)
        print(f"\n=== JAN {jan}: 買取 {bb.price if bb else '無し'}"
              f"{f' ({bb.source} {bb.scraped_date})' if bb else ''} / 実行日 {run_date}")
        if bb is None:
            print("  直近 7 日の新品の買取価格が無いので対象外")
            continue
        hits = all_hits(client, jan)
        print(f"  API の返答 {len(hits)} 件 (価格の安い順)")
        for h in hits:
            seller = h.get("seller") or {}
            sid, price = seller.get("sellerId"), h.get("price")
            store = stores.stores.get(sid)
            head = f"  {str(price):>7} {sid:<16} {(seller.get('name') or '')[:20]:<20}"
            if store is None:
                print(f"{head} × 店舗リストに無い (または除外リスト)")
                continue
            if not h.get("inStock"):
                print(f"{head} × 在庫なし")
                continue
            if h.get("condition") not in (None, "new"):
                print(f"{head} × 中古 ({h.get('condition')})")
                continue
            comps = optimistic_components(store, cfg)
            manual = best_manual_discount(cfg.coupons, sid, price)
            optimistic = optimistic_effective_price(price, comps, args.coupon_margin, manual)
            cand = {"sale_price": price, "buyback_price": bb.price, "store_id": sid}
            gap = realistic_gap(cand, store, cfg)
            mark = "○ 候補" if optimistic < bb.price else "× 甘い実質価格でも届かない"
            configured = cfg.upsell_rate(sid)
            upsell_note = f" (設定 {float(configured) * 100:g}%)" if configured else ""
            print(f"{head} {mark}: 甘い実質 {optimistic:,.0f} / 現実的な見積もり {gap:+,.0f} 円 "
                  f"(BS+ {float(store.bsplus_rate) * 100:g}%, 上乗せ最大 {float(max_upsell(store, cfg)) * 100:g}%"
                  f"{upsell_note})")
            if pages is not None and h.get("url"):
                candidate = {"jan_code": jan, "buyback_price": bb.price, "buyback_source": bb.source,
                             "buyback_date": bb.scraped_date, "store_id": sid, "store_name": seller.get("name"),
                             "item_code": h.get("code"), "item_name": h.get("name"), "item_url": h["url"],
                             "sale_price": price, "category": top_genre(h)}
                record = check_candidate(pages, candidate, cfg, stores)
                row = record.get("row")
                if row is None:
                    print(f"{'':>45} ページ: {record['status']}"
                          + (f" (見つけたクーポン {len(record.get('coupons_seen') or [])} 件)" if "coupons_seen" in record else ""))
                    continue
                lines = ", ".join(f"{c['name'][:10]} {float(c['rate']) * 100:g}%={c['points']}" for c in row["points"])
                coupon = row["coupon"]
                print(f"{'':>45} ページ: 価格 {row['sale_price']:,} クーポン -{row['coupon_discount']:,}"
                      f"{' (' + str(coupon.get('name', ''))[:30] + ')' if coupon else ''} → 実質 {row['effective_price']:,}"
                      f" 利益 {row['profit']:+,} [{lines}]")
                for c in record.get("coupons_seen") or []:
                    print(f"{'':>47} クーポン一覧: {str(c)[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
