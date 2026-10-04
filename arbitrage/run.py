"""実行コマンド: python -m arbitrage.run --coupon-margin 0.2

現在の実装範囲: 買取価格の読み込み → 商品検索API → 絞り込み (候補をローカルに保存)。
途中で止めても、同じ --run-name (既定は実行日) で再実行すれば続きから再開する。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from .buyback import load_buyback_prices
from .config import ROOT, ConfigError, load_campaigns
from .prefilter import max_total_rate, price_can_never_pass, select_candidates
from .state import RunState
from .stores import StoreListError, load_stores
from .yahoo_api import MAX_START_PLUS_RESULTS, RESULTS_PER_PAGE, ItemSearchClient, YahooApiError

logger = logging.getLogger("arbitrage")

PROGRESS_EVERY = 25


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m arbitrage.run", description=__doc__)
    p.add_argument("--coupon-margin", type=float, default=0.2,
                   help="絞り込みで見込むクーポン値引きの余地 (0.2 = 20%%)")
    p.add_argument("--limit", type=int, default=None,
                   help="対象 JAN を買取価格の高い順に先頭 N 件に絞る (試行用)")
    p.add_argument("--date", type=date.fromisoformat, default=date.today(),
                   help="実行日 YYYY-MM-DD (ボーナスストアPlus枠の日付)。既定は今日")
    p.add_argument("--run-name", default=None, help="状態を保存するフォルダ名。既定は実行日 (YYYYMMDD)")
    args = p.parse_args(argv)
    if not 0 <= args.coupon_margin < 1:
        p.error("--coupon-margin は 0 以上 1 未満")
    return args


def search_one_jan(client: ItemSearchClient, buyback, cfg, stores, coupon_margin: float,
                   total_rate) -> dict:
    """JAN 1 件を価格の安い順に検索し、候補を集める。これ以上は通らない価格に達したら打ち切る"""
    hits_seen, total, candidates = 0, 0, []
    start = 1
    while start + RESULTS_PER_PAGE <= MAX_START_PLUS_RESULTS:
        data = client.search_jan(buyback.jan_code, start=start)
        hits = data["hits"]
        total = data.get("totalResultsAvailable") or 0
        hits_seen += len(hits)
        candidates += select_candidates(hits, buyback, cfg, stores, coupon_margin)
        if not hits or hits_seen >= total:
            break
        last_price = hits[-1].get("price")
        if isinstance(last_price, int) and price_can_never_pass(
                last_price, buyback.price, total_rate, coupon_margin):
            break
        start += RESULTS_PER_PAGE
    return {"jan_code": buyback.jan_code, "hits_total": total, "hits_seen": hits_seen,
            "candidates": candidates}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")

    try:
        cfg = load_campaigns()
        stores = load_stores(args.date)
    except (ConfigError, StoreListError) as e:
        logger.error("%s", e)
        return 2
    bsplus_stores = sum(1 for s in stores.stores.values() if s.bsplus_rate > 0)
    logger.info("stores: %d (うち %s のボーナスストアPlus枠あり %d, 全体加算 %s)",
                len(stores.stores), args.date, bsplus_stores, stores.global_bonus_labels or "なし")

    try:
        client = ItemSearchClient(os.environ.get("YAHOO_CLIENT_ID", ""))
    except YahooApiError as e:
        logger.error("%s", e)
        return 2

    run_name = args.run_name or f"{args.date:%Y%m%d}"
    state = RunState(run_name)
    # 再開時に一致を求める条件 (--limit は対象 JAN を絞るだけなので含めない)
    meta = {"run_date": args.date.isoformat(), "coupon_margin": args.coupon_margin,
            "config": cfg.raw, "store_list": stores.source_file}
    previous = state.load_meta()
    if previous is None:
        state.save_meta({**meta, "started_at": datetime.now().isoformat(timespec="seconds")})
    elif any(previous.get(k) != v for k, v in meta.items()):
        changed = [k for k, v in meta.items() if previous.get(k) != v]
        logger.error("run %s は別の条件 (%s が違う) で開始されている。続きから再開するなら条件を戻す。"
                     "新しい条件でやり直すなら --run-name で別名を付ける", run_name, ", ".join(changed))
        return 2

    buyback = load_buyback_prices(args.date)
    targets = sorted(buyback.values(), key=lambda b: (-b.price, b.jan_code))
    if args.limit:
        targets = targets[:args.limit]

    done = {r["jan_code"]: r for r in state.load_api_records()}
    target_jans = {b.jan_code for b in targets}
    candidate_count = sum(len(r["candidates"]) for jan, r in done.items() if jan in target_jans)
    pending = [b for b in targets if b.jan_code not in done]
    logger.info("target JANs: %d (済み %d, 残り %d) run=%s", len(targets), len(targets) - len(pending),
                len(pending), run_name)

    if pending:
        total_rate = max_total_rate(cfg, stores)
        finished = len(targets) - len(pending)
        try:
            for b in pending:
                record = search_one_jan(client, b, cfg, stores, args.coupon_margin, total_rate)
                state.append_api_record(record)
                finished += 1
                candidate_count += len(record["candidates"])
                if finished % PROGRESS_EVERY == 0 or finished == len(targets):
                    logger.info("API %d/%d JAN 完了, 候補 %d 件 (API %d 回)",
                                finished, len(targets), candidate_count, client.request_count)
        except YahooApiError as e:
            logger.error("中断: %s (同じコマンドで再開できる)", e)
            return 1
        except KeyboardInterrupt:
            logger.warning("中断 (%d/%d JAN 完了)。同じコマンドで再開できる", finished, len(targets))
            return 130

    logger.info("絞り込み完了: JAN %d 件, 候補 %d 件 → %s", len(targets), candidate_count, state.api_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
