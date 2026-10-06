"""待ち受け: python -m arbitrage.worker

結果ページの「リサーチ実行」で作られた依頼 (Supabase の arbitrage_research_requests) を 10 秒ごとに見に行き、
python -m arbitrage.run を子プロセスで実行する。この PC で起動したままにしておく (止めるのは Ctrl+C)。
依頼の出力は data/arbitrage/requests/{依頼ID}.log に残る。スキーマは db/arbitrage_research.sql。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from . import db
from .buyback import load_buyback_prices
from .config import ROOT
from .progress import EXIT_CANCELLED
from .research import (DEFAULT_RATES, HISTOGRAM_BUCKET, ParamsError, build_run_args, buyback_histogram,
                       parse_params, updated_rates)
from .state import RUNS_DIR, lock_file

logger = logging.getLogger("arbitrage.worker")

POLL_SECONDS = 10                           # 依頼の確認と heartbeat の間隔
HISTOGRAM_REFRESH = timedelta(minutes=30)   # 買取価格の分布を作り直す間隔
HISTOGRAM_RETRY = timedelta(minutes=5)      # 分布の更新に失敗したときにやり直すまでの間隔
MESSAGE_LIMIT = 500
FINISH_ATTEMPTS = 3                         # 終了状態の書き込みは、通信失敗でも数回やり直す (だめなら次の周期以降に再送)
HEARTBEAT_STALE = timedelta(seconds=60)     # 他の待ち受けの heartbeat がこれより新しい間は、依頼に手を出さない
ORPHAN_STOP_WAIT = 60                       # 残っていた run に SIGINT を送ってから終了を待つ秒数 (1 回の回収あたり)
FOREIGN_KEY_VIOLATION = "23503"             # run_id の指す arbitrage_runs 行が無い

MSG_ORPHANED = "待ち受けが止まったため中断された"
MSG_INTERRUPTED = "待ち受けを止めたため中断された。同じ条件で実行し直すと続きから再開する"
MSG_CANCELLED_BEFORE_START = "開始前に中止された"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def spawn_run(argv: list[str], log_path: Path) -> subprocess.Popen:
    """run を子プロセスで起動する (シェルは通さない)。出力は log_path に追記する"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        # 端末の Ctrl+C が子に直接届かないよう別セッションにする (待ち受けが SIGINT を送って終了を待つ)
        return subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, shell=False,
                                start_new_session=True)


def load_histogram_prices() -> list[int]:
    """いまの買取価格 (run の対象と同じ: 直近 7 日の新品最高値)"""
    return [b.price for b in load_buyback_prices(date.today()).values()]


def run_pid_alive(pid: int, request_id: int) -> bool:
    """pid が「この依頼の run」としてまだ動いているか (/proc のコマンドラインで確かめる。PID の使い回しを誤認しない)"""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    expected = [b"--request-id", str(request_id).encode()]
    return b"arbitrage.run" in args and any(args[i:i + 2] == expected for i in range(len(args)))


def interrupt_pid(pid: int) -> None:
    """run に Ctrl+C と同じ合図を送る (すでに終わっていれば何もしない)"""
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass


def parse_time(value) -> datetime | None:
    """DB の timestamptz (ISO 文字列)。読めなければ None"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def last_error_line(log_path: Path) -> str | None:
    """ログ末尾の ERROR 行のメッセージ (日時とレベルを除いた部分)。

    run は原因の次の行に「再開: (コマンド)」を ERROR で出すので、その行は飛ばして原因の方を採る。
    """
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        _, sep, message = line.partition(" ERROR ")
        if sep and not message.startswith("再開:"):
            return message.strip()[:MESSAGE_LIMIT]
    return None


def read_run_meta(runs_dir: Path, run_name) -> dict:
    """progress.run_name の meta.json (無ければ空)。run_name はフォルダ名 1 つだけを受け付ける"""
    if not isinstance(run_name, str) or not run_name or Path(run_name).name != run_name:
        return {}
    try:
        meta = json.loads((runs_dir / run_name / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


class Worker:
    """依頼を 1 件ずつ実行する。DB 呼び出し・子プロセスの起動・時刻・待ちは差し替えられる (テスト用)"""

    def __init__(self, client, *, db_api=db, spawn: Callable = spawn_run, clock: Callable[[], datetime] = utc_now,
                 sleep: Callable[[float], None] = time.sleep,
                 load_prices: Callable[[], list[int]] = load_histogram_prices,
                 runs_dir: Path = RUNS_DIR, python: str = sys.executable,
                 pid_alive: Callable[[int, int], bool] = run_pid_alive,
                 interrupt: Callable[[int], None] = interrupt_pid):
        self.client, self.db, self.spawn, self.clock, self.sleep = client, db_api, spawn, clock, sleep
        self.load_prices, self.runs_dir, self.python = load_prices, runs_dir, python
        self.pid_alive, self.interrupt = pid_alive, interrupt
        self.log_dir = runs_dir / "requests"
        self.owner = False                            # 他の待ち受けが動いていないことを確かめたか
        self.pending_finish: tuple[int, dict, str] | None = None   # 書けなかった終了状態 (依頼ID, 列, 元の状態)
        self.histogram_at: datetime | None = None     # 分布を最後に保存した時刻

    # ---- 起動時・定期の処理 ----

    def take_ownership(self) -> bool:
        """他の待ち受け (別の PC など) が動いていないことを heartbeat で確かめる。確かめられるまで依頼に触らない。

        自分の heartbeat を書く前に見る。新しい heartbeat があれば、それが 60 秒古くなるまで待つ
        (待ち受けを起動し直した直後も、前回の heartbeat が古くなるまで最大 60 秒待つ)。
        """
        if self.owner:
            return True
        state = self.db.fetch_worker_state(self.client) or {}
        beat = parse_time(state.get("heartbeat_at"))
        if beat is not None and self.clock() - beat < HEARTBEAT_STALE:
            logger.warning("別の待ち受けが動いているか、止まった直後 (最後の heartbeat %s)。"
                           "heartbeat が %d 秒古くなるまで依頼を処理しない", beat.isoformat(),
                           HEARTBEAT_STALE.total_seconds())
            return False
        self.owner = True
        return True

    def pid_path(self, request_id: int) -> Path:
        return self.log_dir / f"{request_id}.pid"

    def read_pid(self, request_id: int) -> int | None:
        try:
            return int(self.pid_path(request_id).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def stop_leftover_run(self, request_id: int) -> bool:
        """前回の待ち受けが起動した run がまだ動いていれば止める。止まった (動いていない) なら True。

        run は別セッションで起動するので、待ち受けだけが kill -9 などで落ちると動き続ける。
        """
        pid = self.read_pid(request_id)
        if pid is None or not self.pid_alive(pid, request_id):
            return True
        logger.warning("依頼 %s の run (PID %d) がまだ動いているので止める", request_id, pid)
        self.interrupt(pid)
        for _ in range(ORPHAN_STOP_WAIT // POLL_SECONDS):
            self.sleep(POLL_SECONDS)
            self.safe_heartbeat()
            if not self.pid_alive(pid, request_id):
                return True
        return False

    def recover_orphans(self) -> None:
        """子プロセスを持っていないのに running のまま残っている依頼を failed にする (毎周期)。

        前回の待ち受けが止まった場合のほか、開始の更新が「サーバでは適用済みだが応答が失敗」した場合もここで回収する。
        """
        for row in self.db.fetch_requests_by_status(self.client, "running"):
            request_id = row["id"]
            if not self.stop_leftover_run(request_id):
                logger.warning("依頼 %s の run がまだ終わらない。次の周期でもう一度確かめる", request_id)
                continue
            logger.warning("依頼 %s は running のまま残っていたので failed にする", request_id)
            self.db.update_request(self.client, request_id, {
                "status": "failed", "message": MSG_ORPHANED, "finished_at": self.clock().isoformat()},
                only_status="running")
            self.pid_path(request_id).unlink(missing_ok=True)

    def refresh_state(self) -> None:
        """買取価格の分布を作り直して保存する (30 分ごと)。rates が未保存なら既定値を入れる"""
        now = self.clock()
        if self.histogram_at is not None and now - self.histogram_at < HISTOGRAM_REFRESH:
            return
        counts = buyback_histogram(self.load_prices(), HISTOGRAM_BUCKET)
        fields = {"buyback_histogram": {"bucket": HISTOGRAM_BUCKET, "as_of": now.isoformat(), "counts": counts}}
        current = self.db.fetch_worker_state(self.client)
        if not current or not current.get("rates"):
            fields["rates"] = dict(DEFAULT_RATES)
        self.db.upsert_worker_state(self.client, fields)
        self.histogram_at = now
        logger.info("買取価格の分布を保存: %d JAN", sum(counts.values()))

    def safe_refresh_state(self) -> None:
        """分布の更新に失敗しても依頼の処理は続ける (5 分後にやり直す)"""
        try:
            self.refresh_state()
        except Exception as e:
            logger.warning("買取価格の分布を更新できなかった (5 分後にやり直す): %s", e)
            self.histogram_at = self.clock() - HISTOGRAM_REFRESH + HISTOGRAM_RETRY

    def heartbeat(self) -> None:
        self.db.upsert_worker_state(self.client, {"heartbeat_at": self.clock().isoformat()})

    def safe_heartbeat(self) -> None:
        try:
            self.heartbeat()
        except Exception as e:
            logger.warning("heartbeat を書けなかった (次の周期でやり直す): %s", e)

    # ---- 依頼の処理 ----

    def write_finish(self, request_id: int, fields: dict, from_status: str) -> None:
        """終了状態を 1 回書く。from_status の行だけを更新する (他で終了済みの行を書き戻さない)。

        run_id の指す実行が削除済み (外部キー違反) なら、run_id を外して書く。
        """
        try:
            row = self.db.update_request(self.client, request_id, fields, only_status=from_status)
        except Exception as e:
            if "run_id" not in fields or getattr(e, "code", None) != FOREIGN_KEY_VIOLATION:
                raise
            logger.warning("依頼 %s: run_id %s の実行が見つからないので、run_id なしで書く", request_id, fields["run_id"])
            fields = {k: v for k, v in fields.items() if k != "run_id"}
            row = self.db.update_request(self.client, request_id, fields, only_status=from_status)
        if row is None:
            # 前の試行がサーバでは適用済みだった / 他で終了済み。どちらでも、もう書くものは無い
            logger.warning("依頼 %s はすでに %s ではなかった (状態は書き換えない)", request_id, from_status)
        else:
            logger.info("依頼 %s: %s%s", request_id, fields["status"],
                        f" ({fields['message']})" if fields.get("message") else "")

    def finish(self, request_id: int, status: str, message: str | None = None, *, from_status: str = "running",
               **extra) -> bool:
        """依頼を終了状態にする。書けたら True。

        数回やり直してもだめなら pending_finish に残し、次の周期以降に書けるまで再送する
        (書けないまま忘れると、依頼が running のまま残って新しい依頼を受け付けられなくなる)。
        """
        fields = {"status": status, "message": message, "finished_at": self.clock().isoformat(), **extra}
        for attempt in range(1, FINISH_ATTEMPTS + 1):
            try:
                self.write_finish(request_id, fields, from_status)
                return True
            except Exception as e:
                logger.warning("依頼 %s の状態 (%s) を書けなかった (%d/%d 回目): %s", request_id, status,
                               attempt, FINISH_ATTEMPTS, e)
                if attempt < FINISH_ATTEMPTS:
                    self.sleep(POLL_SECONDS)
        logger.error("依頼 %s の状態 (%s) を書けなかった。書けるまで周期ごとに再送する", request_id, status)
        self.pending_finish = (request_id, fields, from_status)
        return False

    def flush_pending_finish(self) -> None:
        """書けなかった終了状態を再送する。まだ書けなければ例外 (この周期は新しい依頼に進まない)"""
        if self.pending_finish is None:
            return
        request_id, fields, from_status = self.pending_finish
        try:
            self.write_finish(request_id, fields, from_status)
        except Exception:
            if "run_id" not in fields:
                raise
            # 原因が run_id かもしれないので、次の周期は run_id なしで試す (終了状態を届ける方を優先する)
            self.pending_finish = (request_id, {k: v for k, v in fields.items() if k != "run_id"}, from_status)
            raise
        self.pending_finish = None

    def wait_for(self, proc) -> int:
        """子プロセスの終了を待つ。待っている間も 10 秒ごとに heartbeat を更新する"""
        while proc.poll() is None:
            self.sleep(POLL_SECONDS)
            self.safe_heartbeat()
        return proc.returncode

    def update_rates(self, request_id: int, meta: dict) -> None:
        """完了した run の実測で rates を更新する (JAN 200 件以上を処理した場合のみ)"""
        stats = meta.get("stats")
        if not isinstance(stats, dict) or stats.get("request_id") != request_id:
            return   # この依頼の実測ではない (途中で失敗した・古い run の値が残っている)
        try:
            current = self.db.fetch_worker_state(self.client) or {}
            rates = updated_rates(current.get("rates"), stats)
            if rates is not None:
                self.db.upsert_worker_state(self.client, {"rates": rates})
                logger.info("実測で速度を更新: %s", rates)
        except Exception as e:
            logger.warning("速度 (rates) を更新できなかった: %s", e)

    def process(self, request: dict) -> None:
        """queued の依頼 1 件を最後まで処理する"""
        request_id = request["id"]
        if request.get("cancel_requested"):
            self.finish(request_id, "cancelled", MSG_CANCELLED_BEFORE_START, from_status="queued")
            return
        try:
            params = parse_params(request.get("params"))
            run_args = build_run_args(params, request_id)
        except Exception as e:
            # ParamsError 以外 (想定外の値で検証そのものが落ちた) も failed にする。
            # queued のまま残すと毎周期失敗し続け、新しい依頼も受け付けられなくなる
            if not isinstance(e, ParamsError):
                logger.exception("依頼 %s の条件の検証で想定外のエラー", request_id)
            detail = str(e) if isinstance(e, ParamsError) else f"検証できない値 ({type(e).__name__})"
            self.finish(request_id, "failed", f"依頼の条件が不正: {detail}"[:MESSAGE_LIMIT], from_status="queued")
            return
        claimed = self.db.update_request(
            self.client, request_id, {"status": "running", "started_at": self.clock().isoformat()},
            only_status="queued")
        if claimed is None:
            logger.warning("依頼 %s は他で処理が始まっていたので飛ばす", request_id)
            return
        argv = [self.python, "-m", "arbitrage.run", *run_args]
        log_path = self.log_dir / f"{request_id}.log"
        logger.info("依頼 %s を開始: %s (ログ %s)", request_id, " ".join(argv[1:]), log_path)
        try:
            proc = self.spawn(argv, log_path)
        except OSError as e:
            self.finish(request_id, "failed", f"リサーチを起動できなかった: {e}"[:MESSAGE_LIMIT])
            return
        self.write_pid(request_id, proc)
        try:
            code = self.wait_for(proc)
        except KeyboardInterrupt:
            logger.warning("停止: 実行中のリサーチ (依頼 %s) を止める", request_id)
            proc.send_signal(signal.SIGINT)
            proc.wait()
            self.pid_path(request_id).unlink(missing_ok=True)
            self.finish(request_id, "failed", MSG_INTERRUPTED)
            raise
        self.pid_path(request_id).unlink(missing_ok=True)
        self.complete(request_id, code, log_path)

    def write_pid(self, request_id: int, proc) -> None:
        """子の PID を残す。待ち受けだけが落ちたとき、次の待ち受けが動き続けている run を見つけて止めるため"""
        try:
            self.pid_path(request_id).write_text(str(proc.pid), encoding="utf-8")
        except OSError as e:
            logger.warning("依頼 %s の PID を記録できなかった: %s", request_id, e)

    def complete(self, request_id: int, code: int, log_path: Path) -> None:
        """終了コードから依頼の終了状態を決める: 0 = completed / 3 = cancelled / それ以外 = failed"""
        meta: dict = {}
        try:
            row = self.db.fetch_request(self.client, request_id) or {}
            meta = read_run_meta(self.runs_dir, (row.get("progress") or {}).get("run_name"))
        except Exception as e:
            logger.warning("依頼 %s の進捗を読めなかった (run_id は入れない): %s", request_id, e)
        extra = {"run_id": meta["run_id"]} if isinstance(meta.get("run_id"), int) else {}
        if code == 0:
            self.finish(request_id, "completed", **extra)
            self.update_rates(request_id, meta)   # 終了状態が再送待ちでも、実測は今のうちに反映する
        elif code == EXIT_CANCELLED:
            self.finish(request_id, "cancelled", "中止の依頼を受けて打ち切った (確認済みの分は保存済み)", **extra)
        else:
            message = last_error_line(log_path) or f"終了コード {code} で終了した (ログ: {log_path.name})"
            self.finish(request_id, "failed", message, **extra)

    # ---- ループ ----

    def tick(self) -> None:
        """1 周期: 他の待ち受けの確認 (初回) → 書けなかった終了状態の再送 → running の回収 → heartbeat
        → 分布の更新 → いちばん古い queued の依頼を 1 件処理"""
        try:
            if not self.take_ownership():
                return
            self.safe_heartbeat()          # 再送・回収が進まない間も「待ち受けは動いている」ことは伝える
            self.flush_pending_finish()
            # ここでは子プロセスを持っていない (process は終了まで戻らない) ので、running の行はすべて回収の対象
            self.recover_orphans()
            self.safe_refresh_state()
            queued = self.db.fetch_requests_by_status(self.client, "queued")
            if queued:
                self.process(queued[0])
        except Exception as e:   # 一時的な通信失敗などでループを落とさない
            logger.warning("周期の処理に失敗 (次の周期でやり直す): %s", e)

    def run_forever(self) -> int:
        logger.info("待ち受けを開始 (%d 秒ごとに依頼を確認。止めるには Ctrl+C)", POLL_SECONDS)
        try:
            while True:
                self.tick()
                self.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            logger.info("待ち受けを終了")
            return 0


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m arbitrage.worker", description=__doc__).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # 10 秒ごとの通信ログで埋もれないように
    load_dotenv(ROOT / ".env")
    # 端末を閉じた・kill されたときも Ctrl+C と同じ後始末 (子プロセスを止めて依頼を failed にする) をする
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.default_int_handler)
    try:
        client = db.get_client()
    except db.DbConfigError as e:
        logger.error("%s", e)
        return 2
    # この PC で待ち受けを二重に起動しない (2 つ目が実行中の依頼を「残り物」として failed にしてしまう)
    lock = lock_file(RUNS_DIR / "worker.lock")
    if lock is None:
        logger.error("待ち受けはすでに起動している (%s がロックされている)。二重には起動しない", RUNS_DIR / "worker.lock")
        return 2
    with lock:
        return Worker(client).run_forever()


if __name__ == "__main__":
    sys.exit(main())
