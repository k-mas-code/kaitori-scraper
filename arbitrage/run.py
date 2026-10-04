"""実行コマンド: python -m arbitrage.run --coupon-margin 0.2

流れ: 買取価格の読み込み → 商品検索API → 絞り込み → 商品ページで確定判定 → Supabase に保存。
途中で止めても、ログに出る「再開」のコマンドで続きから再開できる (状態は data/arbitrage/{run名}/)。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from dataclasses import asdict

from .buyback import Buyback, load_buyback_prices
from . import db
from .config import ROOT, ConfigError, load_campaigns
from .finalize import NOT_PROFITABLE, build_result, skip_reason
from .prefilter import max_price_rate, price_can_never_pass, select_candidates
from .state import RunState
from .stores import StoreListError, load_stores
from .yahoo_page import Blocked, PageClient, PageFormatError, coupon_is_valid
from .yahoo_api import (MAX_START_PLUS_RESULTS, RESULTS_PER_PAGE, ItemSearchClient, JanSearchError,
                        YahooApiError)

logger = logging.getLogger("arbitrage")

PROGRESS_EVERY = 25
MAX_CONSECUTIVE_FAILURES = 5   # JAN 単位の失敗がこの回数続いたら全体を止める


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m arbitrage.run", description=__doc__)
    p.add_argument("--coupon-margin", type=float, default=0.2,
                   help="絞り込みで見込むクーポン値引きの余地 (0.2 = 20%%)")
    p.add_argument("--limit", type=int, default=None,
                   help="対象 JAN を買取価格の高い順に先頭 N 件に絞る (試行用)")
    p.add_argument("--date", type=date.fromisoformat, default=date.today(),
                   help="実行日 YYYY-MM-DD (ボーナスストアPlus枠の日付)。既定は今日")
    p.add_argument("--run-name", default=None, help="状態を保存するフォルダ名。既定は実行日 (YYYYMMDD)")
    p.add_argument("--stage", choices=("api", "all"), default="all",
                   help="api = 商品検索APIでの絞り込みまで / all = 商品ページの確定判定と保存まで (既定)")
    p.add_argument("--no-db", action="store_true", help="Supabase に保存しない (結果はローカルの pages.jsonl のみ)")
    p.add_argument("--check-config", action="store_true",
                   help="config/campaigns.yaml と店舗リストを読み、解釈した内容を表示して終了する (通信なし)")
    args = p.parse_args(argv)
    if not 0 <= args.coupon_margin < 1:
        p.error("--coupon-margin は 0 以上 1 未満")
    return args


def search_variants(jan_code: str) -> list[str]:
    """API に渡す JAN。12 桁 (UPC) は Yahoo! 側が先頭 0 付きの 13 桁で登録していることがあるので両方試す"""
    return [jan_code, "0" + jan_code] if len(jan_code) == 12 else [jan_code]


def search_one_jan(client: ItemSearchClient, buyback: Buyback, cfg, stores, coupon_margin: float,
                   price_rate: float) -> dict:
    """JAN 1 件を価格の安い順に検索し、候補を集める。これ以上は通らない価格に達したら打ち切る"""
    hits_seen, total, candidates = 0, 0, []
    for jan in search_variants(buyback.jan_code):
        start = 1
        while start + RESULTS_PER_PAGE <= MAX_START_PLUS_RESULTS + 1:
            data = client.search_jan(jan, start=start)
            hits = data["hits"]
            available = data.get("totalResultsAvailable") or 0
            candidates += select_candidates(hits, buyback, cfg, stores, coupon_margin)
            if not hits or start - 1 + len(hits) >= available:
                break
            last_price = hits[-1].get("price")
            if isinstance(last_price, int) and price_can_never_pass(
                    last_price, buyback.price, price_rate, coupon_margin):
                break
            start += RESULTS_PER_PAGE
        hits_seen += start - 1 + len(hits)
        total += available
        if total:
            break   # 元の桁数で見つかったら 0 付きは試さない
    # 12 桁と 13 桁の両方で同じ商品が返った場合の重複を除く
    unique = {(c["store_id"], c["item_code"]): c for c in candidates}
    return {"jan_code": buyback.jan_code, "hits_total": total, "hits_seen": hits_seen,
            "candidates": list(unique.values())}


def check_candidate(pages: PageClient, candidate: dict, cfg, stores) -> dict:
    """候補 1 件を商品ページで確定判定する。戻り値は pages.jsonl の 1 行"""
    key = {"store_id": candidate["store_id"], "item_code": candidate["item_code"],
           "jan_code": candidate["jan_code"]}
    page = pages.fetch_item(candidate["item_url"])
    reason = skip_reason(candidate, page)
    if reason:
        return {**key, "status": reason}
    store = stores.stores[page.store_id]
    coupon, detail = None, None
    for c in pages.fetch_coupons(page)[:2]:   # 値引きの大きい順に、有効なものが見つかるまで (最大 2 件)
        d = pages.fetch_coupon_detail(c, page.item_code)
        if d and coupon_is_valid(d, page):
            coupon, detail = c, d
            break
    row = build_result(candidate, page, store, cfg, stores, coupon, detail)
    if row is None:
        return {**key, "status": NOT_PROFITABLE, "sale_price": page.price,
                "coupon_discount": coupon.discount if coupon else 0}
    return {**key, "status": "result", "row": {**row, "checked_at": datetime.now().astimezone().isoformat()}}


def print_config(cfg, stores) -> None:
    """設定をどう解釈したかを、人が読める形で表示する"""
    def describe(c) -> str:
        cap = "上限なし" if c.cap is None else f"上限 {c.cap:,}pt"
        base = "税込" if c.base == "tax_included" else "税抜"
        minimum = f"、{c.min_purchase:,}円以上" if c.min_purchase else ""
        return f"{float(c.rate) * 100:g}% ({base}価格に対して、{cap}{minimum})"

    targets = {"all": "すべての店", "bsplus": "その日ボーナスストアPlus枠がある店", "good_store": "優良ストア"}
    print(f"実行日 {stores.run_date} / 店舗リスト {stores.source_file}")
    print("\n[common] 毎回付く分")
    for c in cfg.common:
        print(f"  - {c.name}: {describe(c)}")
    print("\n[campaigns] その日のキャンペーン" + ("" if cfg.campaigns else " (なし)"))
    for c in cfg.campaigns:
        if c.target == "page":
            target = f"商品ページの内訳に「{c.page_title}」を含む行が出ている店"
        elif isinstance(c.target, tuple):
            target = "指定の店: " + ", ".join(c.target)
            unknown = [t for t in c.target if t not in stores.stores]
            if unknown:
                target += f"  ※店舗リストに無い店ID: {', '.join(unknown)}"
        else:
            target = targets[c.target]
        entry = "、要エントリー" if c.entry_required else ""
        print(f"  - {c.component.name}: {describe(c.component)}{entry}\n      対象: {target}")
    slots = sum(1 for s in stores.stores.values() if s.bsplus_rate > 0)
    cap = "上限なし" if cfg.bsplus.cap is None else f"上限 {cfg.bsplus.cap:,}pt"
    print(f"\n[bsplus] ボーナスストアPlus: {cap}。この日の枠あり {slots} 店 / 全 {len(stores.stores)} 店")
    if stores.global_bonus_labels:
        good = "+2% に重ねて +3% (計 +5%)" if cfg.bsplus.good_store_bonus_stacks else "+3% のみ"
        print(f"  全体加算日 ({stores.global_bonus_labels}): 枠のある店 +2%、優良ストアは {good}")
    else:
        print("  全体加算日ではない")
    print("\n[自動で読む分] ストアポイント 1% + 上乗せ (商品ページ)、ボーナスストアPlus の率 (店舗リスト)")
    print("\n設定に問題は見つかりませんでした。")


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

    if args.check_config:
        print_config(cfg, stores)
        return 0

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

    resume_cmd = (f"python -m arbitrage.run --date {args.date} --run-name {run_name} "
                  f"--coupon-margin {args.coupon_margin}" + (f" --limit {args.limit}" if args.limit else ""))

    # 買取価格は開始時点のものを保存し、再開しても同じ価格で判定を続ける
    saved = state.load_buyback()
    if saved is None:
        buyback = load_buyback_prices(args.date)
        state.save_buyback([asdict(b) for b in buyback.values()])
    else:
        buyback = {row["jan_code"]: Buyback(**row) for row in saved}
        logger.info("buyback prices: %d JANs (run %s の開始時点の保存分)", len(buyback), run_name)
    targets = sorted(buyback.values(), key=lambda b: (-b.price, b.jan_code))
    if args.limit:
        targets = targets[:args.limit]

    done = {r["jan_code"]: r for r in state.load_api_records()}
    target_jans = {b.jan_code for b in targets}
    candidate_count = sum(len(r["candidates"]) for jan, r in done.items() if jan in target_jans)
    pending = [b for b in targets if b.jan_code not in done]
    logger.info("target JANs: %d (済み %d, 残り %d) run=%s", len(targets), len(targets) - len(pending),
                len(pending), run_name)

    failed: list[str] = []
    if pending:
        price_rate = max_price_rate(cfg, stores)
        finished = len(targets) - len(pending)
        consecutive_failures = 0
        try:
            for b in pending:
                try:
                    record = search_one_jan(client, b, cfg, stores, args.coupon_margin, price_rate)
                except JanSearchError as e:
                    # その JAN は済みにせず (次回の再開でやり直す)、次へ進む
                    failed.append(b.jan_code)
                    consecutive_failures += 1
                    logger.warning("JAN %s 失敗: %s", b.jan_code, e)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        raise YahooApiError(f"JAN 単位の失敗が {consecutive_failures} 件続いた") from e
                    continue
                consecutive_failures = 0
                state.append_api_record(record)
                finished += 1
                candidate_count += len(record["candidates"])
                if finished % PROGRESS_EVERY == 0 or finished == len(targets):
                    logger.info("API %d/%d JAN 完了, 候補 %d 件 (API %d 回)",
                                finished, len(targets), candidate_count, client.request_count)
        except YahooApiError as e:
            logger.error("中断: %s", e)
            logger.error("再開: %s", resume_cmd)
            return 1
        except KeyboardInterrupt:
            logger.warning("中断 (%d/%d JAN 完了)", finished, len(targets))
            logger.warning("再開: %s", resume_cmd)
            return 130

    if failed:
        logger.warning("失敗した JAN %d 件 (未処理のまま): %s", len(failed), ", ".join(failed[:20]))
        logger.warning("やり直す: %s", resume_cmd)
        return 1
    logger.info("絞り込み完了: JAN %d 件, 候補 %d 件 → %s", len(targets), candidate_count, state.api_path)
    if args.stage == "api":
        return 0

    # ---- 確定判定: 候補の商品ページを、甘い見積もりで利益が大きい順に確認する ----
    done = {r["jan_code"]: r for r in state.load_api_records()}
    candidates = [c for b in targets for c in done[b.jan_code]["candidates"]]
    candidates.sort(key=lambda c: c["optimistic_effective_price"] - c["buyback_price"])
    checked = {(r["store_id"], r["item_code"]): r for r in state.load_page_records()}
    pending_pages = [c for c in candidates if (c["store_id"], c["item_code"]) not in checked]
    logger.info("候補 %d 件 (確認済み %d, 残り %d)", len(candidates), len(candidates) - len(pending_pages),
                len(pending_pages))

    pages = PageClient()
    finished = len(candidates) - len(pending_pages)
    page_errors = 0
    try:
        for c in pending_pages:
            try:
                record = check_candidate(pages, c, cfg, stores)
            except PageFormatError as e:
                # その候補は済みにせず (再開でやり直す)、次へ。続くようならサイト側の仕様変更を疑って止める
                page_errors += 1
                logger.warning("候補 %s/%s 失敗: %s", c["store_id"], c["item_code"], e)
                if page_errors >= MAX_CONSECUTIVE_FAILURES:
                    logger.error("中断: 商品ページの読み取り失敗が %d 件続いた (サイトの構造変更の疑い)", page_errors)
                    logger.error("再開: %s", resume_cmd)
                    return 1
                continue
            page_errors = 0
            state.append_page_record(record)
            checked[(c["store_id"], c["item_code"])] = record
            finished += 1
            if finished % PROGRESS_EVERY == 0 or finished == len(candidates):
                profitable = sum(1 for r in checked.values() if r["status"] == "result")
                logger.info("商品ページ %d/%d 件 完了, 黒字 %d 件 (アクセス %d 回)",
                            finished, len(candidates), profitable, pages.request_count)
    except Blocked as e:
        logger.error("中断: %s。時間を置いてから再開する", e)
        logger.error("再開: %s", resume_cmd)
        return 1
    except KeyboardInterrupt:
        logger.warning("中断 (商品ページ %d/%d 件 完了)", finished, len(candidates))
        logger.warning("再開: %s", resume_cmd)
        return 130

    wanted = {(c["store_id"], c["item_code"]) for c in candidates}
    records = [r for key, r in checked.items() if key in wanted]
    statuses: dict[str, int] = {}
    for r in records:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    rows = sorted((r["row"] for r in records if r["status"] == "result"), key=lambda r: -r["profit_rate"])
    logger.info("確定判定完了: 黒字 %d 件 / 候補 %d 件 (内訳 %s)", len(rows), len(candidates), statuses)
    if len(records) < len(candidates):
        logger.warning("未確認の候補が %d 件残っている。やり直す: %s", len(candidates) - len(records), resume_cmd)
        return 1
    for r in rows[:10]:
        logger.info("  %+.1f%% %+d円 %s [%s] 実質%d → %s %d", r["profit_rate"] * 100, r["profit"],
                    r["item_name"][:30], r["store_id"], r["effective_price"], r["buyback_source"],
                    r["buyback_price"])

    if args.no_db:
        logger.info("DB 保存なし (--no-db)。結果は %s", state.pages_path)
        return 0
    try:
        client_db = db.get_client()
    except db.DbConfigError as e:
        logger.error("%s", e)
        return 2
    meta_now = state.load_meta()
    run_id = meta_now.get("run_id")
    if run_id is None:
        run_id = db.create_run(client_db, cfg.raw, args.coupon_margin, len(targets))
        state.save_meta({**meta_now, "run_id": run_id})
    saved_count = db.upsert_results(client_db, run_id, rows)
    db.finish_run(client_db, run_id, len(candidates), saved_count)
    logger.info("DB 保存: run_id=%d, %d 件", run_id, saved_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
