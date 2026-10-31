"""店舗リスト (data/yahoo_pp_stores_5genres.xlsx) を取り直して差し替え、スナップショットを Supabase に載せる。

実行: .venv/bin/python -m tools.yahoo_pp.refresh [--request-id N] [--upload-only] [--print-snapshot]
  通常        取得 (bsplus_fetch) → enrich (全段階) → build_xlsx → 検証 (load_stores で店数 >= 500)
              → 旧リストを .bak.xlsx に退避して差し替え → arbitrage_store_lists に 1 行 insert (約 4 時間)
  --upload-only    取得・差し替えをせず、data/yahoo_pp/*.json の現在の内容からスナップショットを insert するだけ
  --print-snapshot DB に書かず、スナップショットの要約を表示する (--upload-only と同じ入力)
  --request-id N   待ち受け (arbitrage.worker) からの実行。依頼行に進捗を書き、中止の依頼 (cancel_requested) を見る

再取得の前に前回の json を data/yahoo_pp/prev/ に退避し cache/ を消す。失敗・中止したら prev/ を戻す。
終了コード: 0 完了 / 1 失敗 (ログ末尾の ERROR 行が理由) / 3 中止 (arbitrage.progress.EXIT_CANCELLED)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from arbitrage import db
from arbitrage.progress import EXIT_CANCELLED, MIN_INTERVAL, Sink, Throttle, local_now, supabase_sink
from arbitrage.stores import DEFAULT_STORES_PATH, StoreListError, load_stores

from . import bsplus_fetch, build_xlsx, enrich
from .common import ROOT, Cancelled, Fetcher, RateLimited
from .snapshot import MIN_STORES, build_snapshot, describe, file_sha

DATA_DIR = ROOT / "data" / "yahoo_pp"
PREV_NAME = "prev"
NEW_XLSX_NAME = "new_stores.xlsx"
# 再取得で作り直すファイル。前回の分は prev/ に退避し、失敗したら戻す
STATE_FILES = ("sched.json", "invoice.json", "points.json", "gold.json", "reviews.json", "dates.json",
               "names.json", "counts.json")
KIND = "store_list"
STAGE_FETCH, STAGE_BUILD, STAGE_DONE = "fetch", "build", "done"
MSG_RATE_LIMITED = "アクセス制限。時間をおいて更新し直す"
MSG_CANCELLED = "中止の依頼を受けて止めた (前回のリストはそのまま)"
log = logging.getLogger(__name__)


class Progress:
    """依頼行の progress ({kind, stage, done, total, eta, note, updated_at}) を 20 秒以上の間隔で書き、
    返ってきた cancel_requested を覚える。sink が無ければ何も書かない"""

    def __init__(self, sink: Sink | None, clock: Callable[[], datetime] = local_now, interval: float = MIN_INTERVAL):
        self.sink, self.clock = sink, clock
        self.throttle = Throttle(interval)
        self.cancelled = False
        self.state: dict = {"kind": KIND, "stage": STAGE_FETCH, "done": 0, "total": 0, "note": None}
        self.stage_started: datetime | None = None

    def _eta(self, now: datetime) -> str | None:
        done, total = self.state["done"], self.state["total"]
        if not done or not total or self.stage_started is None or self.state["stage"] == STAGE_DONE:
            return None
        per_item = (now - self.stage_started) / done
        return (now + per_item * (total - done)).isoformat()

    def _write(self, now: datetime) -> None:
        self.throttle.mark(now)
        if self.sink is None:
            return
        snapshot = {**self.state, "eta": self._eta(now), "updated_at": now.isoformat()}
        try:
            self.cancelled = bool(self.sink(snapshot)) or self.cancelled
        except Exception as e:   # 進捗が書けなくても更新そのものは止めない
            log.warning("進捗を書けなかった (更新は続ける): %s", e)

    def update(self, stage: str, done: int, total: int) -> None:
        """各段階の関数に on_progress として渡す。段階の切り替わりは間隔に関係なく書く"""
        now = self.clock()
        changed = stage != self.state["stage"] or self.stage_started is None
        if changed:
            self.stage_started = now
        self.state.update(stage=stage, done=done, total=total)
        if changed or self.throttle.due(now):
            self._write(now)

    def should_stop(self) -> bool:
        return self.cancelled

    def finish(self, stage: str = STAGE_DONE, note: str | None = None) -> None:
        self.state.update(stage=stage, note=note)
        self._write(self.clock())


def stash_previous(data_dir: Path) -> None:
    """前回の json を prev/ に移し、cache/ を消す (古い本文を使い回さない)"""
    prev = data_dir / PREV_NAME
    shutil.rmtree(prev, ignore_errors=True)
    prev.mkdir(parents=True, exist_ok=True)
    for name in STATE_FILES:
        if (data_dir / name).exists():
            shutil.move(str(data_dir / name), str(prev / name))
    shutil.rmtree(data_dir / "cache", ignore_errors=True)


def restore_previous(data_dir: Path) -> None:
    """失敗・中止: 取りかけの json を捨てて prev/ を戻す (prev/ が無ければ何もしない)"""
    prev = data_dir / PREV_NAME
    if not prev.is_dir():
        return
    for name in STATE_FILES:
        (data_dir / name).unlink(missing_ok=True)
        if (prev / name).exists():
            shutil.move(str(prev / name), str(data_dir / name))
    shutil.rmtree(prev, ignore_errors=True)


def old_list_label(path: Path) -> str:
    """退避ファイル名に使う旧リストの取得日 (概要シート A2 の「取得日：YYYY/MM/DD」)。読めなければ更新日時"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        text = str(wb["概要"]["A2"].value or "") if "概要" in wb.sheetnames else ""
        m = re.search(r"取得日：(\d{4})/(\d{1,2})/(\d{1,2})", text)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except Exception as e:   # 壊れた xlsx でも差し替えは止めない
        log.warning("旧リストの取得日を読めなかった (更新日時で退避する): %s", e)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
    except OSError:
        return datetime.now().strftime("%Y%m%d-%H%M%S")


def swap_in(new_path: Path, target: Path) -> Path | None:
    """target を {stem}.{旧取得日}.bak.xlsx に退避して new_path を target にする。退避先を返す (旧が無ければ None)"""
    backup = None
    if target.exists():
        backup = target.with_name(f"{target.stem}.{old_list_label(target)}.bak{target.suffix}")
        if backup.exists():
            backup = target.with_name(f"{target.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak{target.suffix}")
        shutil.move(str(target), str(backup))
    shutil.move(str(new_path), str(target))
    return backup


def validate_new_list(path: Path, today: dt.date) -> int:
    """新しい xlsx を arbitrage.stores で読めるか。店数 (除外後) が MIN_STORES 未満なら StoreListError"""
    stores = load_stores(today, path)
    if len(stores.stores) < MIN_STORES:
        raise StoreListError(f"新しいリストの店数が少なすぎる ({len(stores.stores)} < {MIN_STORES})。差し替えない")
    return len(stores.stores)


def fetched_date(data_dir: Path, fallback: dt.date) -> dt.date:
    """--upload-only の取得日: sched.json の更新日 (無ければ fallback)"""
    try:
        return datetime.fromtimestamp((data_dir / "sched.json").stat().st_mtime).date()
    except OSError:
        return fallback


def upload_snapshot(client, data_dir: Path, fetched_on: dt.date, stores_path: Path, print_only: bool) -> int:
    snapshot = build_snapshot(build_xlsx.load_inputs(data_dir), fetched_on, file_sha(stores_path))
    if print_only:
        print(describe(snapshot))
        return 0
    row_id = db.insert_store_list(client, snapshot)
    log.info("arbitrage_store_lists に保存 (id %s): 全 %d 店 / 適格 %d", row_id, snapshot["summary"]["total"],
             snapshot["summary"]["qualified"])
    return 0


def refresh(client, progress: Progress, today: dt.date, data_dir: Path = DATA_DIR,
            stores_path: Path = DEFAULT_STORES_PATH, fetcher: Fetcher | None = None) -> int:
    """取得 → enrich → build → 検証 → 差し替え → insert。例外は呼び出し側 (main) で終了コードにする"""
    enrich.warn_if_bad_day(today)
    stash_previous(data_dir)
    fetcher = fetcher or Fetcher(data_dir / "cache")
    result = bsplus_fetch.fetch_schedule(fetcher, today, progress.update, progress.should_stop)
    bsplus_fetch.write_schedule(result, data_dir)
    enrich.run_enrich("all", 0, data_dir, fetcher, progress.update, progress.should_stop, today)
    progress.update(STAGE_BUILD, 0, 1)
    new_path = data_dir / NEW_XLSX_NAME
    inputs = build_xlsx.load_inputs(data_dir)
    build_xlsx.build(new_path, today, inputs)
    count = validate_new_list(new_path, today)
    # 先に DB へ保存し、成功してから差し替える (保存に失敗したら xlsx も json も前のまま戻せる)
    snapshot = build_snapshot(inputs, today, file_sha(new_path))
    row_id = db.insert_store_list(client, snapshot)
    log.info("arbitrage_store_lists に保存 (id %s)", row_id)
    backup = swap_in(new_path, stores_path)
    log.info("店舗リストを差し替えた: %s (%d 店%s)", stores_path, count, f"、旧リストは {backup.name}" if backup else "")
    shutil.rmtree(data_dir / PREV_NAME, ignore_errors=True)
    progress.update(STAGE_BUILD, 1, 1)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m tools.yahoo_pp.refresh",
                                 description="店舗リスト (data/yahoo_pp_stores_5genres.xlsx) を取り直して差し替え、"
                                             "スナップショットを arbitrage_store_lists に保存する (約 4 時間)")
    ap.add_argument("--request-id", type=int, help="待ち受けからの実行 (依頼行に進捗を書き、中止の依頼を見る)")
    ap.add_argument("--upload-only", action="store_true",
                    help="取得・差し替えをせず、data/yahoo_pp/*.json の現在の内容からスナップショットを保存するだけ")
    ap.add_argument("--print-snapshot", action="store_true",
                    help="DB に書かず、スナップショットの要約を表示する (--upload-only と同じ入力)")
    ap.add_argument("--today", type=dt.date.fromisoformat,
                    help="取得日 (YYYY-MM-DD。既定は今日。--upload-only では sched.json の更新日)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(ROOT / ".env")
    today = args.today or dt.date.today()
    client = None
    if not args.print_snapshot:
        try:
            client = db.get_client()
        except db.DbConfigError as e:
            log.error("%s", e)
            return 1
    sink = supabase_sink(client, args.request_id) if client is not None and args.request_id is not None else None
    progress = Progress(sink)
    if args.upload_only or args.print_snapshot:
        try:
            fetched_on = args.today or fetched_date(DATA_DIR, today)
            code = upload_snapshot(client, DATA_DIR, fetched_on, DEFAULT_STORES_PATH, args.print_snapshot)
        except (ValueError, OSError) as e:
            log.error("スナップショットを作れない: %s", e)
            return 1
        progress.finish()
        return code
    try:
        code = refresh(client, progress, today)
    except Cancelled:
        restore_previous(DATA_DIR)
        log.info(MSG_CANCELLED)
        progress.finish(note=MSG_CANCELLED)
        return EXIT_CANCELLED
    except RateLimited as e:
        restore_previous(DATA_DIR)
        log.error("%s (HTTP 489: %s)", MSG_RATE_LIMITED, e)
        progress.finish(note=MSG_RATE_LIMITED)
        return 1
    except (StoreListError, ValueError, OSError, RuntimeError) as e:
        restore_previous(DATA_DIR)
        log.error("店舗リストの更新に失敗: %s", e)
        progress.finish(note=str(e))
        return 1
    except KeyboardInterrupt:
        restore_previous(DATA_DIR)
        log.error("中断された (前回のリストはそのまま)")
        return 130
    except Exception as e:
        restore_previous(DATA_DIR)
        log.exception("想定外のエラー")
        log.error("店舗リストの更新に失敗: %s (%s)", e, type(e).__name__)
        return 1
    progress.finish()
    return code


if __name__ == "__main__":
    sys.exit(main())
