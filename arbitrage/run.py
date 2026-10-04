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
from pathlib import Path

from dotenv import load_dotenv

from dataclasses import asdict

from .buyback import Buyback, load_buyback_prices
from . import db
from .config import (ROOT, CampaignConfig, ConfigError, Settings, active_config, load_settings_file,
                     parse_settings, TARGET_PAGE)
from .finalize import NOT_PROFITABLE, build_result, skip_reason
from .prefilter import max_price_rate, price_can_never_pass, select_candidates
from .state import RunState
from .stores import StoreList, StoreListError, load_stores
from .coupon_report import write_coupon_report
from .yahoo_page import (Blocked, PageClient, PageFormatError, coupon_expired, coupon_is_valid,
                         parse_all_coupons, parse_usable_coupons)
from .yahoo_api import (MAX_START_PLUS_RESULTS, RESULTS_PER_PAGE, ItemSearchClient, JanSearchError,
                        YahooApiError)

logger = logging.getLogger("arbitrage")

PROGRESS_EVERY = 25
MAX_CONSECUTIVE_FAILURES = 5   # JAN 単位の失敗がこの回数続いたら全体を止める


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m arbitrage.run", description=__doc__)
    p.add_argument("--coupon-margin", type=float, default=0.2,
                   help="絞り込みで見込むクーポン値引きの余地 (0.2 = 20%%)")
    p.add_argument("--min-buyback", type=int, default=None, help="買取価格がこの金額 (円) 以上の JAN だけを対象にする")
    p.add_argument("--max-buyback", type=int, default=None, help="買取価格がこの金額 (円) 以下の JAN だけを対象にする")
    p.add_argument("--limit", type=int, default=None,
                   help="対象 JAN を N 件に絞る (試行用)。買取価格の高い順に並べ、全体から均等な間隔で N 件を選ぶ")
    p.add_argument("--date", type=date.fromisoformat, default=None,
                   help="実行日 YYYY-MM-DD (この日のボーナスストアPlus枠・キャンペーン・クーポンで計算する)。"
                        "既定は設定の購入予定日 (purchase_date)")
    p.add_argument("--config-file", type=Path, default=None,
                   help="設定を Supabase (結果ページの設定画面で保存したもの) ではなく、この YAML / JSON ファイルから読む")
    p.add_argument("--run-name", default=None, help="状態を保存するフォルダ名。既定は実行日 (YYYYMMDD)")
    p.add_argument("--stage", choices=("api", "all"), default="all",
                   help="api = 商品検索APIでの絞り込みまで / all = 商品ページの確定判定と保存まで (既定)")
    p.add_argument("--no-db", action="store_true", help="Supabase に保存しない (結果はローカルの pages.jsonl のみ)")
    p.add_argument("--check-config", action="store_true",
                   help="設定と店舗リストを読み、解釈した内容を表示して終了する "
                        "(通信は Supabase からの設定の読み込みだけ。--config-file なら通信なし)")
    args = p.parse_args(argv)
    if not 0 <= args.coupon_margin < 1:
        p.error("--coupon-margin は 0 以上 1 未満")
    if args.limit is not None and args.limit <= 0:
        p.error("--limit は 1 以上")
    return args


def select_targets(buyback, min_price: int | None, max_price: int | None, limit: int | None) -> list[Buyback]:
    """対象 JAN を買取価格の高い順に並べる。limit があれば、価格帯が偏らないよう均等な間隔で選ぶ"""
    targets = sorted(
        (b for b in buyback
         if (min_price is None or b.price >= min_price) and (max_price is None or b.price <= max_price)),
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
    row = build_result(candidate, page, store, cfg, coupon, detail)
    if row is None:
        return {**key, **seen, "status": NOT_PROFITABLE, "coupon_discount": coupon.discount if coupon else 0}
    return {**key, **seen, "status": "result",
            "row": {**row, "checked_at": datetime.now().astimezone().isoformat()}}


TARGET_LABELS = {"all": "すべての店", "bsplus": "その日ボーナスストアPlus枠がある店",
                 "bsplus_good": "その日ボーナスストアPlus枠があり、かつ優良ストア", "good_store": "優良ストア"}


def describe_stores(store_ids: tuple[str, ...], stores: StoreList) -> str:
    text = "指定の店: " + ", ".join(store_ids)
    unknown = [s for s in store_ids if s not in stores.stores]
    return text + (f"  ※店舗リストに無い店ID: {', '.join(unknown)}" if unknown else "")


def describe_coupon(c) -> str:
    if c.type == "fixed":
        value = f"{c.value:,}円OFF"
    else:
        limit = "" if c.max_discount is None else f" (値引き上限 {c.max_discount:,}円)"
        value = f"{float(c.value) * 100:g}%OFF{limit}"
    minimum = purchase_range_text(c)
    period = f"、期間 {c.valid_from or '指定なし'} 〜 {c.valid_until or '指定なし'}"
    return f"{value}{minimum}{period}"


def purchase_range_text(c) -> str:
    """最低・最高購入額の表示 (例: 「、5,000〜19,999円」)"""
    if getattr(c, "max_purchase", None) is not None:   # 手持ちクーポンには最高購入額が無い
        return f"、{c.min_purchase:,}〜{c.max_purchase:,}円"
    return f"、{c.min_purchase:,}円以上" if c.min_purchase else ""


def print_config(settings: Settings, cfg: CampaignConfig, stores: StoreList, source: str) -> None:
    """設定をどう解釈したかを、人が読める形で表示する"""
    def describe(c) -> str:
        cap = "上限なし" if c.cap is None else f"上限 {c.cap:,}pt"
        base = "税込" if c.base == "tax_included" else "税抜"
        minimum = purchase_range_text(c)
        return f"{float(c.rate) * 100:g}% ({base}価格に対して、{cap}{minimum})"

    print(f"設定の読み込み元: {source}")
    override = "" if stores.run_date == settings.purchase_date else " ← --date で上書き"
    print(f"購入予定日 (設定): {settings.purchase_date} / 実行日: {stores.run_date}{override}")
    print(f"店舗リスト: {stores.source_file}")
    print("\n[common] 毎日付く分" + ("" if cfg.common else " (なし)"))
    for c in cfg.common:
        print(f"  - {c.name}: {describe(c)}")
    print(f"\n[campaigns] {stores.run_date} に有効なキャンペーン" + ("" if cfg.campaigns else " (なし)"))
    for c in cfg.campaigns:
        if c.target == "page":
            target = f"商品ページの内訳に「{c.page_title}」を含む行が出ている店"
        elif c.target == "stores":
            target = describe_stores(c.store_ids, stores)
        else:
            target = TARGET_LABELS[c.target]
        entry = "、要エントリー" if c.entry_required else ""
        print(f"  - {c.component.name}: {describe(c.component)}{entry}\n      対象: {target}")
    print(f"  日付外で無効: {len(settings.campaigns) - len(cfg.campaigns)} 件")
    print(f"\n[coupons] {stores.run_date} に使える手持ちクーポン (1 注文 1 枚、併用なし)"
          + ("" if cfg.coupons else " (なし)"))
    for c in cfg.coupons:
        target = describe_stores(c.store_ids, stores) if c.target == "stores" else TARGET_LABELS["all"]
        print(f"  - {c.name}: {describe_coupon(c)}\n      対象: {target}")
    print(f"  期間外で無効: {len(settings.coupons) - len(cfg.coupons)} 件")
    slots = sum(1 for s in stores.stores.values() if s.bsplus_rate > 0)
    cap = "上限なし" if cfg.bsplus.cap is None else f"上限 {cfg.bsplus.cap:,}pt"
    print(f"\n[bsplus] ボーナスストアPlus: {cap}。この日の枠あり {slots} 店 / 全 {len(stores.stores)} 店")
    if stores.global_bonus_labels:
        print(f"  店舗リストではこの日は全体加算日 ({stores.global_bonus_labels})。加算分は自動では付けないので、"
              "campaigns に target=bsplus / bsplus_good の行があるか確認する")
    print("\n[自動で読む分] ストアポイント 1% + 上乗せ (商品ページ)、ボーナスストアPlus の率 (店舗リスト)")
    print("\n設定に問題は見つかりませんでした。")


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


def main(argv: list[str] | None = None) -> int:
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
    logger.info("設定: %s / 実行日 %s (%s) / キャンペーン %d 件, 手持ちクーポン %d 件が有効", source, run_date,
                "--date" if args.date else "設定の購入予定日", len(cfg.campaigns), len(cfg.coupons))
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
                  + (" --no-db" if args.no_db else ""))

    # 買取価格は開始時点のものを保存し、再開しても同じ価格で判定を続ける
    saved = state.load_buyback()
    if saved is None:
        # 買取価格は「いま」の相場を使う (購入予定日が先の日付でも、今日までの直近分を読む)
        buyback = load_buyback_prices(min(run_date, date.today()))
        state.save_buyback([asdict(b) for b in buyback.values()])
    else:
        buyback = {row["jan_code"]: Buyback(**row) for row in saved}
        logger.info("buyback prices: %d JANs (run %s の開始時点の保存分)", len(buyback), run_name)
    targets = select_targets(buyback.values(), args.min_buyback, args.max_buyback, args.limit)

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
                    consecutive_failures += 1
                    logger.warning("JAN %s 失敗: %s", b.jan_code, e)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        raise YahooApiError(f"JAN 単位の失敗が {consecutive_failures} 件続いた") from e
                    if e.permanent:
                        # 何度やっても同じ (API が受け付けない JAN)。候補なしとして済みにする
                        state.append_api_record({"jan_code": b.jan_code, "hits_total": 0, "hits_seen": 0,
                                                 "candidates": [], "error": str(e)})
                        finished += 1
                    else:
                        failed.append(b.jan_code)   # 済みにせず、次回の再開でやり直す
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

    coupon_rows = write_coupon_report(records, state.dir / "coupons.csv")
    logger.info("見つけたクーポン %d 種類 (うちログイン不要 %d) → %s", len(coupon_rows),
                sum(1 for c in coupon_rows if c["ログイン"] == "不要"), state.dir / "coupons.csv")

    if args.no_db:
        logger.info("DB 保存なし (--no-db)。結果は %s", state.pages_path)
        return 0
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
