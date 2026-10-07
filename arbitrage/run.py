"""実行コマンド: python -m arbitrage.run --coupon-margin 0.2

流れ: 買取価格の読み込み → 商品検索API → 絞り込み → 商品ページで確定判定 → Supabase に保存。
途中で止めても、ログに出る「再開」のコマンドで続きから再開できる (状態は data/arbitrage/{run名}/)。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from dataclasses import asdict, replace

from .buyback import Buyback, load_buyback_prices, load_categories, read_credentials
from .categories import GROUP_OTHER, GROUPS
from . import db
from .config import (ROOT, CampaignConfig, ConfigError, Settings, active_config, load_settings_file,
                     parse_settings, TARGET_PAGE)
from .describe import print_config  # noqa: F401  (--check-config の表示。テストからも run 経由で使う)
from .finalize import NOT_PROFITABLE, build_result, skip_reason
from .prefilter import max_price_rate, price_can_never_pass, select_candidates
from .progress import (EXIT_CANCELLED, STAGE_API, STAGE_PAGES, STAGE_SAVING, STOP_CANCELLED, STOP_NOTES,
                       Clock, build_reporter, local_now, parse_deadline)
from .scope import DEFAULT_REACH_SLACK, MAX_REACH_SLACK, PAGE_SCOPES, SCOPE_REACHABLE, plan_page_checks
from .state import RunState, lock_file
from .stores import StoreList, StoreListError, load_stores
from .coupon_report import write_coupon_report
from .yahoo_page import (Blocked, PageClient, PageFormatError, coupon_expired, coupon_is_valid,
                         parse_all_coupons, parse_usable_coupons)
from .yahoo_api import (MAX_START_PLUS_RESULTS, RESULTS_PER_PAGE, ItemSearchClient, JanSearchError,
                        YahooApiError)

logger = logging.getLogger("arbitrage")

PROGRESS_EVERY = 25
# 赤字でもこの金額までは計算結果 (row) を pages.jsonl に残す。--min-profit を変えても商品ページを取り直さずに済む
KEEP_ROW_MIN_PROFIT = -5000
MAX_CONSECUTIVE_FAILURES = 5   # JAN 単位の失敗がこの回数続いたら全体を止める


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m arbitrage.run", description=__doc__)
    p.add_argument("--coupon-margin", type=float, default=0.2,
                   help="絞り込みで見込むクーポン値引きの余地 (0.2 = 20%%)")
    p.add_argument("--min-buyback", type=int, default=None, help="買取価格がこの金額 (円) 以上の JAN だけを対象にする")
    p.add_argument("--max-buyback", type=int, default=None, help="買取価格がこの金額 (円) 以下の JAN だけを対象にする")
    p.add_argument("--category", action="append", choices=GROUPS, default=None, metavar="NAME",
                   help=f"この大分類の JAN だけを対象にする (繰り返し可)。{' / '.join(GROUPS)}")
    p.add_argument("--limit", type=int, default=None,
                   help="対象 JAN を N 件に絞る (試行用)。買取価格の高い順に並べ、全体から均等な間隔で N 件を選ぶ")
    p.add_argument("--date", type=date.fromisoformat, default=None,
                   help="実行日 YYYY-MM-DD (この日のボーナスストアPlus枠・キャンペーン・クーポンで計算する)。"
                        "既定は設定の購入予定日 (purchase_date)")
    p.add_argument("--config-file", type=Path, default=None,
                   help="設定を Supabase (結果ページの設定画面で保存したもの) ではなく、この YAML / JSON ファイルから読む")
    p.add_argument("--run-name", default=None, help="状態を保存するフォルダ名。既定は実行日 (YYYYMMDD)")
    p.add_argument("--stage", choices=("api", "all", "save"), default="all",
                   help="api = 商品検索APIでの絞り込みまで / all = 商品ページの確定判定と保存まで (既定) / "
                        "save = 新しい検索・確認はせず、確認済みの分だけを保存する (途中経過を結果ページに出す)")
    p.add_argument("--min-profit", type=int, default=1,
                   help=f"利益がこの金額 (円) 以上の商品を結果にする。既定は 1 (黒字のみ)。"
                        f"-1000 なら 1,000 円までの赤字も載せる (下限 {KEEP_ROW_MIN_PROFIT})")
    p.add_argument("--page-scope", choices=PAGE_SCOPES, default=SCOPE_REACHABLE,
                   help="確定判定の範囲。reachable = クーポン余地なしの見積もりで届きそうな候補だけ (既定) / "
                        "all = 全候補 (届きそうな候補を先に確認する)")
    p.add_argument("--reach-slack", type=float, default=DEFAULT_REACH_SLACK,
                   help=f"「届きそう」の判定で見込む余裕 (販売価格に対する比率。既定 {DEFAULT_REACH_SLACK}、"
                        f"0 以上 {MAX_REACH_SLACK} 以下)。公開クーポンなどで縮まる分")
    p.add_argument("--deadline", type=parse_deadline, default=None,
                   help="終了時刻の上限 (ISO 8601。例 2026-10-05T23:00。タイムゾーン無しはローカル時刻)。"
                        "過ぎたらそこで打ち切り、確認済みの分だけを保存する")
    p.add_argument("--request-id", type=int, default=None,
                   help="結果ページからの依頼 (arbitrage_research_requests) の ID。進捗を書き戻し、"
                        "中止の依頼があれば打ち切る (待ち受け python -m arbitrage.worker が付ける)")
    p.add_argument("--no-db", action="store_true", help="Supabase に保存しない (結果はローカルの pages.jsonl のみ)")
    p.add_argument("--check-config", action="store_true",
                   help="設定と店舗リストを読み、解釈した内容を表示して終了する "
                        "(通信は Supabase からの設定の読み込みだけ。--config-file なら通信なし)")
    args = p.parse_args(argv)
    if not 0 <= args.coupon_margin < 1:
        p.error("--coupon-margin は 0 以上 1 未満")
    if args.limit is not None and args.limit <= 0:
        p.error("--limit は 1 以上")
    if args.min_profit < KEEP_ROW_MIN_PROFIT:
        p.error(f"--min-profit は {KEEP_ROW_MIN_PROFIT} 以上")
    if not 0 <= args.reach_slack <= MAX_REACH_SLACK:
        p.error(f"--reach-slack は 0 以上 {MAX_REACH_SLACK} 以下")
    return args


def select_targets(buyback, min_price: int | None, max_price: int | None, limit: int | None,
                   categories=None) -> list[Buyback]:
    """対象 JAN を買取価格の高い順に並べる。limit があれば、価格帯が偏らないよう均等な間隔で選ぶ。

    categories (大分類名の列) があればその分類だけ。大分類が無い JAN (古い run の buyback.json) は対象から外す
    """
    wanted = set(categories or ())
    targets = sorted(
        (b for b in buyback
         if (min_price is None or b.price >= min_price) and (max_price is None or b.price <= max_price)
         and (not wanted or b.category in wanted)),
        key=lambda b: (-b.price, b.jan_code))
    if limit and len(targets) > limit:
        targets = [targets[i * len(targets) // limit] for i in range(limit)]
    return targets


def search_variants(jan_code: str) -> list[str]:
    """API に渡す JAN。12 桁 (UPC) をそのまま渡すと 400 になるので、先頭に 0 を付けた 13 桁で検索する"""
    return ["0" + jan_code] if len(jan_code) == 12 else [jan_code]


def search_one_jan(client: ItemSearchClient, buyback: Buyback, cfg: CampaignConfig, stores: StoreList,
                   coupon_margin: float,
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
                    last_price, buyback.price, price_rate, coupon_margin, cfg.coupons):
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


def check_candidate(pages: PageClient, candidate: dict, cfg: CampaignConfig, stores: StoreList) -> dict:
    """候補 1 件を商品ページで確定判定する。戻り値は pages.jsonl の 1 行"""
    key = {"store_id": candidate["store_id"], "item_code": candidate["item_code"],
           "jan_code": candidate["jan_code"]}
    page = pages.fetch_item(candidate["item_url"])
    reason = skip_reason(candidate, page)
    if reason:
        return {**key, "status": reason}
    store = stores.stores[page.store_id]
    coupon_data = pages.fetch_coupon_data(page)
    # 見つけたクーポンはすべて記録する (この商品には使えないものも、あとで一覧にして報告する)
    seen = {"coupons_seen": parse_all_coupons(coupon_data), "store_name": page.store_name,
            "item_name": page.name, "item_url": candidate["item_url"], "sale_price": page.price}
    coupon, detail = None, None
    # 購入予定日より前に切れる公開クーポンは使えないので外す
    usable = [c for c in parse_usable_coupons(coupon_data) if not coupon_expired(c.end_text, stores.run_date)]
    for c in usable[:2]:   # 値引きの大きい順に、有効なものが見つかるまで (最大 2 件)
        d = pages.fetch_coupon_detail(c, page.item_code)
        if d and coupon_is_valid(d, page):
            coupon, detail = c, d
            break
    row = build_result(candidate, page, store, cfg, coupon, detail, min_profit=KEEP_ROW_MIN_PROFIT)
    if row is None:
        return {**key, **seen, "status": NOT_PROFITABLE, "coupon_discount": coupon.discount if coupon else 0}
    row = {**row, "checked_at": datetime.now().astimezone().isoformat()}
    if row["profit"] <= 0:   # 惜しい赤字: 状態は赤字のまま、計算結果だけ残す (--min-profit で結果に含められる)
        return {**key, **seen, "status": NOT_PROFITABLE, "coupon_discount": row["coupon_discount"], "row": row}
    return {**key, **seen, "status": "result", "row": row}


def load_settings(config_file: Path | None) -> tuple[Settings, str]:
    """設定と、その読み込み元の説明。既定は Supabase、--config-file があればそのファイル"""
    if config_file is not None:
        return load_settings_file(config_file), f"ファイル {config_file}"
    try:
        client = db.get_client()
    except db.DbConfigError as e:
        raise ConfigError(f"{e}。設定を Supabase から読めない (ファイルから読むなら --config-file)") from e
    try:
        return parse_settings(db.fetch_settings(client)), "Supabase (arbitrage_settings)"
    except ConfigError as e:
        raise ConfigError(f"Supabase の設定: {e}") from e


def count_results(records, min_profit: int) -> int:
    """利益が min_profit 以上の件数 (惜しい赤字の row も数える)"""
    return sum(1 for r in records if "row" in r and r["row"]["profit"] >= min_profit)


def conditions_suffix(meta: dict) -> str:
    """再開時に一致を求める条件 (meta) から決まる run 名の接尾辞。同じ条件なら同じ名前になる"""
    digest = hashlib.sha256(json.dumps(meta, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"c{digest[:8]}"


def main(argv: list[str] | None = None, clock: Clock = local_now) -> int:
    """clock は現在時刻の取得 (終了時刻の上限・進捗の間隔に使う。テストで差し替える)"""
    with contextlib.ExitStack() as stack:   # run フォルダのロックを、どの経路で終わっても外す
        return _main(argv, clock, stack)


def _main(argv: list[str] | None, clock: Clock, stack: contextlib.ExitStack) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")

    try:
        settings, source = load_settings(args.config_file)
        # 実行日: --date があればそれ、無ければ設定の購入予定日
        run_date = args.date or settings.purchase_date
        cfg = active_config(settings, run_date)
        stores = load_stores(run_date)
    except (ConfigError, StoreListError) as e:
        logger.error("%s", e)
        return 2
    logger.info("設定: %s / 実行日 %s (%s) / キャンペーン %d 件, 手持ちクーポン %d 件, 店ごとの上乗せ %d 件が有効",
                source, run_date, "--date" if args.date else "設定の購入予定日",
                len(cfg.campaigns), len(cfg.coupons), len(cfg.store_upsell))
    bsplus_stores = sum(1 for s in stores.stores.values() if s.bsplus_rate > 0)
    logger.info("stores: %d (うち %s のボーナスストアPlus枠あり %d)", len(stores.stores), run_date, bsplus_stores)

    if args.check_config:
        print_config(settings, cfg, stores, source)
        return 0

    try:
        client = ItemSearchClient(os.environ.get("YAHOO_CLIENT_ID", ""))
    except YahooApiError as e:
        logger.error("%s", e)
        return 2

    # 既定の run 名は「実行した日 + 購入予定日」。同じ購入予定日でも、別の日に実行すれば新しい run になる
    # (古い販売価格・買取価格の結果を黙って再利用しない)
    run_name = args.run_name or f"{date.today():%Y%m%d}-buy{run_date:%m%d}"
    if run_date != date.today() and any(c.target == TARGET_PAGE for c in cfg.campaigns):
        logger.warning("購入予定日 (%s) が今日ではないので、対象が「商品ページに行が出ている店」のキャンペーンは"
                       "今日のページで判定される (その日だけの企画は付かない扱いになる)", run_date)
    state = RunState(run_name)
    # 再開時に一致を求める条件 (--limit は対象 JAN を絞るだけなので含めない)
    meta = {"run_date": run_date.isoformat(), "coupon_margin": args.coupon_margin,
            "config": cfg.raw, "store_list": stores.source_file}
    previous = state.load_meta()
    if (previous is not None and args.request_id is not None and args.run_name is None
            and any(previous.get(k) != v for k, v in meta.items())):
        # 結果ページからの依頼では --run-name を付けられない。同じ日に設定を変えて実行し直したときは、
        # 条件から決まる別名にする (古い条件の結果と混ぜない)。依頼ごとの名前にはしない:
        # 中断・打ち切りの後に同じ条件で依頼し直せば、同じ名前になって続きから再開できる
        run_name = f"{run_name}-{conditions_suffix(meta)}"
        state = RunState(run_name)
        previous = state.load_meta()
        logger.warning("同じ日の run が別の条件で開始されているので、run 名を %s にして%s", run_name,
                       "新しく始める" if previous is None else "続きから再開する")
    # 同じ run フォルダを 2 つのプロセスで同時に進めない (api.jsonl / pages.jsonl への二重追記と API の二重呼び出しを防ぐ)
    lock = lock_file(state.lock_path)
    if lock is None:
        logger.error("run %s は別のプロセスが実行中。終わるのを待つか、そのプロセスを止めてから実行する", run_name)
        return 2
    stack.callback(lock.close)
    if previous is None:
        state.save_meta({**meta, "started_at": datetime.now().isoformat(timespec="seconds")})
    elif any(previous.get(k) != v for k, v in meta.items()):
        changed = [k for k, v in meta.items() if previous.get(k) != v]
        logger.error("run %s は別の条件 (%s が違う) で開始されている。続きから再開するなら条件を戻す。"
                     "新しい条件でやり直すなら --run-name で別名を付ける", run_name, ", ".join(changed))
        return 2

    # 設定の購入予定日をあとで変えても同じ日付で再開できるよう、--date を明示する
    resume_cmd = (f"python -m arbitrage.run --date {run_date} --run-name {run_name} "
                  f"--coupon-margin {args.coupon_margin}"
                  + (f" --config-file {args.config_file}" if args.config_file else "")
                  + "".join(f" {flag} {value}" for flag, value in (
                      ("--min-buyback", args.min_buyback), ("--max-buyback", args.max_buyback),
                      ("--limit", args.limit)) if value is not None)
                  + "".join(f" --category {c}" for c in args.category or ())
                  + (f" --min-profit {args.min_profit}" if args.min_profit != 1 else "")
                  + (f" --page-scope {args.page_scope}" if args.page_scope != SCOPE_REACHABLE else "")
                  + (f" --reach-slack {args.reach_slack}" if args.reach_slack != DEFAULT_REACH_SLACK else "")
                  + (" --no-db" if args.no_db else ""))

    # 進捗の書き戻し (--request-id) と、打ち切りの判定 (--deadline・中止の依頼)。どちらも無ければ何もしない
    reporter = build_reporter(run_name, args.request_id, args.deadline, args.page_scope, clock)
    reporter.set_stage(reporter.state["stage"])   # 開始を知らせる
    save_only = args.stage == "save"   # 新しい検索・確認をせず、確認済みの分だけ保存する (打ち切り後も同じ扱い)
    stop_reason: str | None = None

    # 買取価格は開始時点のものを保存し、再開しても同じ価格で判定を続ける
    saved = state.load_buyback()
    if saved is None:
        # 買取価格は「いま」の相場を使う (購入予定日が先の日付でも、今日までの直近分を読む)
        buyback = load_buyback_prices(min(run_date, date.today()))
        state.save_buyback([asdict(b) for b in buyback.values()])
    else:
        buyback = {row["jan_code"]: Buyback(**row) for row in saved}
        logger.info("buyback prices: %d JANs (run %s の開始時点の保存分)", len(buyback), run_name)
    if args.category and any(b.category is None for b in buyback.values()):
        # 大分類が付く前に始まった run の保存分。分類は価格と違って「開始時点」に固定する必要が無いので、いま読んで補う
        logger.info("run %s の買取価格に大分類が無いので products から読み直す", run_name)
        groups = load_categories(*read_credentials())
        buyback = {jan: replace(b, category=groups.get(jan, GROUP_OTHER)) if b.category is None else b
                   for jan, b in buyback.items()}
        state.save_buyback([asdict(b) for b in buyback.values()])
    targets = select_targets(buyback.values(), args.min_buyback, args.max_buyback, args.limit, args.category)

    done = {r["jan_code"]: r for r in state.load_api_records()}
    target_jans = {b.jan_code for b in targets}
    candidate_count = sum(len(r["candidates"]) for jan, r in done.items() if jan in target_jans)
    pending = [b for b in targets if b.jan_code not in done]
    if save_only:
        # 確認済みの分だけを保存する: 未検索の JAN は対象から外し、新しい検索はしない
        if pending:
            logger.warning("未検索の JAN %d 件は対象から外して保存する", len(pending))
        targets = [b for b in targets if b.jan_code in done]
        pending = []
    logger.info("target JANs: %d (済み %d, 残り %d) run=%s", len(targets), len(targets) - len(pending),
                len(pending), run_name)

    failed: list[str] = []
    api_started, jan_processed = clock(), 0
    if pending:
        price_rate = max_price_rate(cfg, stores)
        finished = len(targets) - len(pending)
        consecutive_failures = 0
        reporter.set_stage(STAGE_API, jan_total=len(targets), jan_done=finished, candidates=candidate_count)
        try:
            for b in pending:
                # 各件の前に、終了時刻の上限と中止の依頼を見る
                stop_reason = reporter.check(jan_done=finished, candidates=candidate_count)
                if stop_reason:
                    break
                try:
                    record = search_one_jan(client, b, cfg, stores, args.coupon_margin, price_rate)
                except JanSearchError as e:
                    consecutive_failures += 1
                    logger.warning("JAN %s 失敗: %s", b.jan_code, e)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        raise YahooApiError(f"JAN 単位の失敗が {consecutive_failures} 件続いた") from e
                    if e.permanent:
                        # 何度やっても同じ (API が受け付けない JAN)。候補なしとして済みにする
                        state.append_api_record({"jan_code": b.jan_code, "hits_total": 0, "hits_seen": 0,
                                                 "candidates": [], "error": str(e)})
                        finished += 1
                        jan_processed += 1
                    else:
                        failed.append(b.jan_code)   # 済みにせず、次回の再開でやり直す
                    continue
                consecutive_failures = 0
                state.append_api_record(record)
                finished += 1
                jan_processed += 1
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

    api_seconds = (clock() - api_started).total_seconds()
    done = {r["jan_code"]: r for r in state.load_api_records()}
    if stop_reason:
        # API 段階の途中で打ち切り: 検索済みの JAN だけを対象に保存まで進む (確定判定は行わない)
        searched = [b for b in targets if b.jan_code in done]
        logger.warning("%s (API %d/%d JAN 完了)。検索済みの JAN だけを対象に、確認済みの分を保存する。続き: %s",
                       STOP_NOTES[stop_reason], len(searched), len(targets), resume_cmd)
        targets, save_only = searched, True
    elif failed:
        logger.warning("失敗した JAN %d 件 (未処理のまま): %s", len(failed), ", ".join(failed[:20]))
        logger.warning("やり直す: %s", resume_cmd)
        return 1
    logger.info("絞り込み完了: JAN %d 件, 候補 %d 件 → %s", len(targets), candidate_count, state.api_path)
    if args.stage == "api":
        reporter.finish(STOP_NOTES.get(stop_reason), jan_done=len(targets), candidates=candidate_count)
        return EXIT_CANCELLED if stop_reason == STOP_CANCELLED else 0

    # ---- 確定判定: 届きそうな候補の商品ページを、届きやすい順に確認する (arbitrage/scope.py) ----
    candidates = [c for b in targets for c in done[b.jan_code]["candidates"]]
    plan = plan_page_checks(candidates, stores.stores, cfg, args.page_scope, args.reach_slack, args.min_profit)
    wanted = {(c["store_id"], c["item_code"]) for c in candidates}
    checked = {(r["store_id"], r["item_code"]): r for r in state.load_page_records()}
    pending_pages = [c for c in plan.ordered if (c["store_id"], c["item_code"]) not in checked]
    finished = len(plan.ordered) - len(pending_pages)
    logger.info("候補 %d 件のうち届きそう %d 件 / 確認の対象 %d 件 (確認済み %d, 残り %d)", len(candidates),
                plan.reachable, len(plan.ordered), finished, len(pending_pages))
    if plan.out_of_scope:
        logger.info("届きそうにない候補 %d 件は確認しない (全候補を確認するなら --page-scope all)",
                    plan.out_of_scope)
    if save_only:
        pending_pages = []

    pages = PageClient()
    page_errors = 0
    result_count = count_results((r for key, r in checked.items() if key in wanted), args.min_profit)
    pages_started, pages_processed = clock(), 0
    if pending_pages:
        reporter.set_stage(STAGE_PAGES, jan_done=len(targets), candidates=len(candidates),
                           pages_total=len(plan.ordered), pages_done=finished, results=result_count)
    try:
        for c in pending_pages:
            stop_reason = reporter.check(pages_done=finished, results=result_count)
            if stop_reason:
                break
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
            pages_processed += 1
            result_count += count_results([record], args.min_profit)
            if finished % PROGRESS_EVERY == 0 or finished == len(plan.ordered):
                profitable = sum(1 for r in checked.values() if r["status"] == "result")
                logger.info("商品ページ %d/%d 件 完了, 黒字 %d 件 (アクセス %d 回)",
                            finished, len(plan.ordered), profitable, pages.request_count)
    except Blocked as e:
        logger.error("中断: %s。時間を置いてから再開する", e)
        logger.error("再開: %s", resume_cmd)
        return 1
    except KeyboardInterrupt:
        logger.warning("中断 (商品ページ %d/%d 件 完了)", finished, len(plan.ordered))
        logger.warning("再開: %s", resume_cmd)
        return 130

    pages_seconds = (clock() - pages_started).total_seconds()
    # 確認の対象のうち、まだ確認できていない候補 (範囲外にした候補は数えない)
    unchecked = sum(1 for c in plan.ordered if (c["store_id"], c["item_code"]) not in checked)
    if stop_reason and not save_only:
        logger.warning("%s (商品ページ %d/%d 件 完了)", STOP_NOTES[stop_reason], finished, len(plan.ordered))
        save_only = True
    reporter.set_stage(STAGE_SAVING, pages_total=len(plan.ordered), pages_done=finished, results=result_count,
                       jan_done=len(targets), candidates=len(candidates))
    records = [r for key, r in checked.items() if key in wanted]
    statuses: dict[str, int] = {}
    for r in records:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    # 惜しい赤字 (row 付きの not_profitable) も --min-profit 以上なら結果に含める
    rows = sorted((r["row"] for r in records if "row" in r and r["row"]["profit"] >= args.min_profit),
                  key=lambda r: -r["profit_rate"])
    logger.info("確定判定完了: 結果 %d 件 (利益 %+d 円以上) / 候補 %d 件 (内訳 %s)", len(rows), args.min_profit,
                len(candidates), statuses)
    if save_only:
        if unchecked:
            logger.warning("未確認の候補 %d 件を残したまま、確認済みの分を保存する。続き: %s", unchecked, resume_cmd)
    elif unchecked:
        logger.warning("未確認の候補が %d 件残っている。やり直す: %s", unchecked, resume_cmd)
        return 1
    for r in rows[:10]:
        logger.info("  %+.1f%% %+d円 %s [%s] 実質%d → %s %d", r["profit_rate"] * 100, r["profit"],
                    r["item_name"][:30], r["store_id"], r["effective_price"], r["buyback_source"],
                    r["buyback_price"])

    coupon_rows = write_coupon_report(records, state.dir / "coupons.csv")
    logger.info("見つけたクーポン %d 種類 (うちログイン不要 %d) → %s", len(coupon_rows),
                sum(1 for c in coupon_rows if c["ログイン"] == "不要"), state.dir / "coupons.csv")

    # 今回の実行の実測。待ち受けが所要時間予測の速度 (arbitrage_worker_state.rates) を更新するのに使う
    state.save_meta({**state.load_meta(), "stats": {
        "request_id": args.request_id, "jan_total": len(targets), "candidates": len(candidates),
        "reachable": plan.reachable, "jan_processed": jan_processed, "api_seconds": round(api_seconds, 1),
        "pages_processed": pages_processed, "pages_seconds": round(pages_seconds, 1)}})
    exit_code = EXIT_CANCELLED if stop_reason == STOP_CANCELLED else 0

    if args.no_db:
        logger.info("DB 保存なし (--no-db)。結果は %s", state.pages_path)
        reporter.finish(STOP_NOTES.get(stop_reason), results=len(rows))
        return exit_code
    try:
        client_db = db.get_client()
    except db.DbConfigError as e:
        logger.error("%s。保存せずに試すなら --no-db", e)
        return 2
    meta_now = state.load_meta()
    run_id = meta_now.get("run_id")
    if run_id is None:
        run_id = db.create_run(client_db, cfg.raw, args.coupon_margin, len(targets))
        state.save_meta({**meta_now, "run_id": run_id})
    saved_count = db.upsert_results(client_db, run_id, rows)
    db.finish_run(client_db, run_id, len(candidates), saved_count)
    logger.info("DB 保存: run_id=%d, %d 件", run_id, saved_count)
    reporter.finish(STOP_NOTES.get(stop_reason), results=saved_count)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
