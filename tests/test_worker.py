"""待ち受けの状態遷移 (DB・子プロセス・時計・待ちはすべて偽物)"""

import json
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import arbitrage.worker as worker_module
from arbitrage.buyback import Buyback
from arbitrage.categories import GROUPS
from arbitrage.research import DEFAULT_RATES
from arbitrage.state import lock_file
from arbitrage.worker import (MSG_INTERRUPTED, MSG_ORPHANED, Worker, last_error_line, read_run_meta,
                              run_pid_alive)

T0 = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
PARAMS = {"purchase_date": "2026-10-11", "min_buyback": 5000, "max_buyback": 60000}


class FakeDb:
    """arbitrage_research_requests / arbitrage_worker_state を dict で持つ"""

    def __init__(self, requests=()):
        self.requests = {r["id"]: {"status": "queued", "cancel_requested": False, "progress": {}, **r}
                         for r in requests}
        self.state: dict | None = None
        self.fail = 0            # 残り何回、呼び出しを失敗させるか
        self.updates: list[tuple[int, dict]] = []

    def _call(self):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("connection reset")

    def fetch_request(self, client, request_id):
        self._call()
        return self.requests.get(request_id)

    def fetch_requests_by_status(self, client, status):
        self._call()
        return [r for _, r in sorted(self.requests.items()) if r["status"] == status]

    def update_request(self, client, request_id, fields, only_status=None):
        self._call()
        row = self.requests.get(request_id)
        if row is None or (only_status is not None and row["status"] != only_status):
            return None
        row.update(fields)
        self.updates.append((request_id, fields))
        return row

    def fetch_worker_state(self, client):
        self._call()
        return self.state

    def upsert_worker_state(self, client, fields):
        self._call()
        self.state = {**(self.state or {}), **fields}


class FakeProc:
    def __init__(self, code, polls=2, on_poll=None):
        self.code, self.polls, self.on_poll = code, polls, on_poll
        self.pid = 4321
        self.returncode, self.signals, self.waited = None, [], False

    def poll(self):
        if self.on_poll:
            self.on_poll(self)
        if self.polls > 0:
            self.polls -= 1
            return None
        self.returncode = self.code
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self):
        self.waited, self.returncode = True, 130
        return 130


class Harness:
    def __init__(self, tmp_path, requests=(), code=0, log_text="", polls=2, on_poll=None):
        self.db = FakeDb(requests)
        self.now = T0
        self.spawned: list[list[str]] = []
        self.proc = FakeProc(code, polls, on_poll)
        self.runs_dir = tmp_path
        self.price_loads = 0
        self.alive: set[int] = set()            # 動いていることにする PID
        self.interrupted: list[int] = []
        self.dies_after_interrupt = True

        def spawn(argv, log_path):
            self.spawned.append(argv)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(log_text, encoding="utf-8")
            return self.proc

        def load_prices():
            self.price_loads += 1
            # 古い run 由来の None (大分類なし) は「その他」に数える
            return [Buyback("1", 500, "rudeya", "2026-10-04", "家電"), Buyback("2", 5500, "rudeya", "2026-10-04", "家電"),
                    Buyback("3", 5900, "rudeya", "2026-10-04", "ゲーム"), Buyback("4", 61000, "rudeya", "2026-10-04", None)]

        def interrupt(pid):
            self.interrupted.append(pid)
            if self.dies_after_interrupt:
                self.alive.discard(pid)

        self.worker = Worker("client", db_api=self.db, spawn=spawn, clock=lambda: self.now, sleep=self.sleep,
                             load_prices=load_prices, runs_dir=tmp_path, python="python",
                             pid_alive=lambda pid, request_id: pid in self.alive, interrupt=interrupt)

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)

    def write_meta(self, run_name, meta):
        (self.runs_dir / run_name).mkdir(parents=True, exist_ok=True)
        (self.runs_dir / run_name / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_startup_recovers_orphans_and_saves_histogram_and_default_rates(tmp_path):
    h = Harness(tmp_path, [{"id": 1, "status": "running", "params": PARAMS},
                           {"id": 2, "status": "completed", "params": PARAMS}])
    h.worker.tick()
    assert h.db.requests[1]["status"] == "failed" and h.db.requests[1]["message"] == MSG_ORPHANED
    assert h.db.requests[1]["finished_at"] == T0.isoformat() and h.db.requests[2]["status"] == "completed"
    assert h.db.state["heartbeat_at"] == T0.isoformat() and h.db.state["rates"] == DEFAULT_RATES
    histogram = h.db.state["buyback_histogram"]
    assert histogram == {"bucket": 1000, "as_of": T0.isoformat(), "counts": {"0": 1, "5000": 2, "61000": 1},
                         "by_group": {**{g: {} for g in GROUPS}, "家電": {"0": 1, "5000": 1}, "ゲーム": {"5000": 1},
                                      "その他": {"61000": 1}}}
    assert list(histogram["by_group"]) == list(GROUPS)
    assert h.spawned == []


def test_histogram_refreshes_every_30_minutes_and_keeps_saved_rates(tmp_path):
    h = Harness(tmp_path)
    h.db.state = {"rates": {"jan_per_min": 20}}
    h.worker.tick()
    h.sleep(29 * 60)
    h.worker.tick()
    assert h.price_loads == 1 and h.db.state["rates"] == {"jan_per_min": 20}     # 保存済みの rates は上書きしない
    h.sleep(60)
    h.worker.tick()
    assert h.price_loads == 2 and h.db.state["heartbeat_at"] == h.now.isoformat()


def test_queued_request_runs_and_completes(tmp_path):
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}, {"id": 8, "params": PARAMS}], code=0)
    h.write_meta("20261005-buy1011", {"run_id": 31, "stats": {
        "request_id": 7, "jan_processed": 300, "api_seconds": 600, "jan_total": 300, "candidates": 450,
        "reachable": 90, "pages_processed": 90, "pages_seconds": 270}})

    def on_poll(proc):   # run が進捗を書き戻す
        h.db.requests[7]["progress"] = {"stage": "api", "run_name": "20261005-buy1011"}
    h.proc.on_poll = on_poll
    h.worker.tick()
    assert h.spawned == [["python", "-m", "arbitrage.run", "--date", "2026-10-11", "--request-id", "7",
                          "--page-scope", "reachable", "--reach-slack", "0.03", "--min-profit=1",
                          "--min-buyback", "5000", "--max-buyback", "60000"]]
    row = h.db.requests[7]
    assert (row["status"], row["message"], row["run_id"]) == ("completed", None, 31)
    assert row["started_at"] == T0.isoformat() and row["finished_at"] == (T0 + timedelta(seconds=20)).isoformat()
    statuses = [f["status"] for i, f in h.db.updates if i == 7 and "status" in f]
    assert statuses == ["running", "completed"]
    assert h.db.state["heartbeat_at"] == (T0 + timedelta(seconds=20)).isoformat()     # 実行中も更新している
    assert h.db.state["rates"] == {"jan_per_min": 30.0, "candidates_per_jan": 1.5, "reachable_fraction": 0.2,
                                   "seconds_per_check": 3.0}
    assert h.db.requests[8]["status"] == "queued"                                     # 1 周期に 1 件だけ


def test_rates_are_not_updated_from_small_or_foreign_runs(tmp_path):
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS, "progress": {"run_name": "r"}}])
    h.write_meta("r", {"stats": {"request_id": 6, "jan_processed": 900, "api_seconds": 600}})
    h.worker.tick()
    assert h.db.requests[7]["status"] == "completed" and "run_id" not in h.db.requests[7]
    assert h.db.state["rates"] == DEFAULT_RATES
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS, "progress": {"run_name": "r"}}])
    h.write_meta("r", {"stats": {"request_id": 7, "jan_processed": 199, "api_seconds": 600}})
    h.worker.tick()
    assert h.db.state["rates"] == DEFAULT_RATES


def test_exit_code_3_is_cancelled(tmp_path):
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}], code=3)
    h.worker.tick()
    assert h.db.requests[7]["status"] == "cancelled" and "中止" in h.db.requests[7]["message"]


def test_other_exit_codes_fail_with_last_error_line(tmp_path):
    log = ("2026-10-05 12:00:00,000 INFO stores: 100\n"
           "2026-10-05 12:00:01,000 ERROR 最初のエラー\n"
           "2026-10-05 12:00:02,000 ERROR 中断: アクセスが拒否された " + "x" * 600 + "\n"
           "2026-10-05 12:00:02,500 ERROR 再開: python -m arbitrage.run --date 2026-10-11\n"
           "2026-10-05 12:00:03,000 INFO おわり\n")
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}], code=1, log_text=log)
    h.worker.tick()
    row = h.db.requests[7]
    # 最後の ERROR 行。ただし「再開: (コマンド)」の行は原因ではないので飛ばす
    assert row["status"] == "failed" and row["message"].startswith("中断: アクセスが拒否された")
    assert len(row["message"]) == 500
    h = Harness(tmp_path, [{"id": 9, "params": PARAMS}], code=2, log_text="Traceback ...\n")
    h.worker.tick()
    assert h.db.requests[9]["message"] == "終了コード 2 で終了した (ログ: 9.log)"


def test_cancel_before_start_is_not_run(tmp_path):
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS, "cancel_requested": True}])
    h.worker.tick()
    assert h.db.requests[7]["status"] == "cancelled" and h.spawned == []
    assert "started_at" not in h.db.requests[7]


@pytest.mark.parametrize("params, match", [
    ({"purchase_date": "2026-10-11", "min_buyback": -5}, "min_buyback"),
    ({"purchase_date": "2026-10-11", "run_name": "../../x"}, "不明なキー"),
    (None, "params"),
])
def test_invalid_params_fail_without_running(tmp_path, params, match):
    h = Harness(tmp_path, [{"id": 7, "params": params}])
    h.worker.tick()
    row = h.db.requests[7]
    assert row["status"] == "failed" and match in row["message"] and row["message"].startswith("依頼の条件が不正")
    assert h.spawned == []


def test_oldest_queued_request_goes_first(tmp_path):
    h = Harness(tmp_path, [{"id": 9, "params": PARAMS}, {"id": 4, "params": PARAMS}])
    h.worker.tick()
    assert h.db.requests[4]["status"] == "completed" and h.db.requests[9]["status"] == "queued"


def test_request_claimed_elsewhere_is_skipped(tmp_path):
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}])
    original = h.db.update_request

    def racing_update(client, request_id, fields, only_status=None):
        if only_status == "queued":
            h.db.requests[request_id]["status"] = "running"      # 別の待ち受けが先に取った
        return original(client, request_id, fields, only_status)
    h.db.update_request = racing_update
    h.worker.tick()
    assert h.spawned == [] and h.db.requests[7]["status"] == "running"


def test_ctrl_c_stops_child_and_marks_request_failed(tmp_path):
    def on_poll(proc):
        raise KeyboardInterrupt
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}], on_poll=on_poll)
    assert h.worker.run_forever() == 0
    assert h.proc.signals == [signal.SIGINT] and h.proc.waited
    assert h.db.requests[7]["status"] == "failed" and h.db.requests[7]["message"] == MSG_INTERRUPTED


def test_temporary_db_failures_do_not_break_the_loop(tmp_path, caplog):
    h = Harness(tmp_path, [{"id": 1, "status": "running", "params": PARAMS}, {"id": 7, "params": PARAMS}])
    h.db.fail = 1
    h.worker.tick()                                     # 回収の読み込みで失敗 → 落ちずに次の周期へ
    assert "周期の処理に失敗" in caplog.text and h.db.requests[1]["status"] == "running"
    h.worker.tick()                                     # 次の周期で回収からやり直す
    assert h.db.requests[1]["status"] == "failed" and h.db.requests[7]["status"] == "completed"


def test_heartbeat_failure_while_running_and_final_update_retry(tmp_path, caplog):
    def on_poll(proc):
        if proc.polls == 1:
            h.db.fail = 1                               # 実行中の heartbeat が 1 回失敗
        if proc.polls == 0:
            h.db.fail = 2                               # 終了直後の読み込みと、終了状態の書き込み 1 回目が失敗
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}], on_poll=on_poll)
    h.worker.owner, h.worker.histogram_at = True, T0
    h.worker.tick()
    assert "heartbeat を書けなかった" in caplog.text and "1/3 回目" in caplog.text
    assert h.db.requests[7]["status"] == "completed"


def test_histogram_failure_does_not_block_requests(tmp_path, caplog):
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}])

    def broken():
        h.price_loads += 1
        raise RuntimeError("price_history timeout")
    h.worker.load_prices = broken
    h.worker.tick()
    assert "買取価格の分布を更新できなかった" in caplog.text and h.db.requests[7]["status"] == "completed"
    h.sleep(60)
    h.worker.tick()
    assert h.price_loads == 1                           # すぐにはやり直さない (5 分後)
    h.sleep(5 * 60)
    h.worker.tick()
    assert h.price_loads == 2


def test_last_error_line_and_run_meta_helpers(tmp_path):
    assert last_error_line(tmp_path / "none.log") is None
    (tmp_path / "a.log").write_text("x INFO ok\n", encoding="utf-8")
    assert last_error_line(tmp_path / "a.log") is None
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "meta.json").write_text('{"run_id": 5}', encoding="utf-8")
    assert read_run_meta(tmp_path, "run1") == {"run_id": 5}
    for bad in (None, "", "../run1", "run1/../run1", "missing", 5):
        assert read_run_meta(tmp_path, bad) == {}
    (tmp_path / "run1" / "meta.json").write_text("{broken", encoding="utf-8")
    assert read_run_meta(tmp_path, "run1") == {}


class ApiError(Exception):
    """postgrest.APIError の代わり (code を持つ)"""

    def __init__(self, code):
        super().__init__(f"db error {code}")
        self.code = code


def test_finish_that_keeps_failing_is_resent_on_later_ticks(tmp_path, caplog):
    # run が正常終了した瞬間に DB が不通: 3 回やり直してもだめ → 次の周期以降に、書けるまで再送する
    def on_poll(proc):
        if proc.polls == 0 and len(h.spawned) == 1:     # 依頼 7 の run が終わった瞬間から不通
            h.db.fail = 5                               # 進捗の読み込み + 終了状態 3 回 + rates の読み込み
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS, "progress": {"run_name": "r"}}], on_poll=on_poll)
    h.write_meta("r", {"run_id": 31})
    h.worker.owner, h.worker.histogram_at = True, T0
    h.worker.tick()
    assert h.db.requests[7]["status"] == "running" and "書けるまで周期ごとに再送する" in caplog.text
    assert h.worker.pending_finish is not None
    finished_at = h.worker.pending_finish[1]["finished_at"]
    h.db.requests[8] = {"id": 8, "status": "queued", "cancel_requested": False, "progress": {}, "params": PARAMS}
    h.db.fail = 2                                       # heartbeat と再送がまた失敗
    h.worker.tick()
    assert h.db.requests[7]["status"] == "running" and h.spawned == [h.spawned[0]]   # 次の依頼にはまだ進まない
    h.worker.tick()                                     # 通信が戻った周期
    row = h.db.requests[7]
    # 失敗 (待ち受けが止まった) としてではなく、本来の終了状態で届く。完了時刻も run が終わった時点のもの
    assert (row["status"], row["message"], row["finished_at"]) == ("completed", None, finished_at)
    assert h.worker.pending_finish is None
    assert h.db.requests[8]["status"] == "completed"    # 再送できた周期から、次の依頼を処理する


def test_pending_finish_drops_run_id_when_it_keeps_failing(tmp_path):
    # run_id が原因で書けない場合 (外部キー違反以外のエラーで返る場合も含む) でも、終了状態は届ける
    h = Harness(tmp_path, [{"id": 7, "status": "running", "params": PARAMS}])
    h.worker.owner, h.worker.histogram_at = True, T0
    h.worker.pending_finish = (7, {"status": "completed", "message": None, "finished_at": "t", "run_id": 99},
                               "running")
    original = h.db.update_request

    def update(client, request_id, fields, only_status=None):
        if "run_id" in fields:
            raise RuntimeError("bad request")
        return original(client, request_id, fields, only_status)
    h.db.update_request = update
    h.worker.tick()
    assert h.db.requests[7]["status"] == "running"      # この周期は回収もしない (failed で上書きしない)
    h.worker.tick()
    assert h.db.requests[7]["status"] == "completed" and "run_id" not in h.db.requests[7]


def test_deleted_run_id_is_dropped_and_request_still_completes(tmp_path, caplog):
    # meta.json の run_id が指す arbitrage_runs 行が削除済み → 外部キー違反。run_id なしで完了にする
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS, "progress": {"run_name": "r"}}])
    h.write_meta("r", {"run_id": 31})
    original = h.db.update_request

    def update(client, request_id, fields, only_status=None):
        if "run_id" in fields:
            raise ApiError("23503")
        return original(client, request_id, fields, only_status)
    h.db.update_request = update
    h.worker.tick()
    row = h.db.requests[7]
    assert row["status"] == "completed" and "run_id" not in row and h.worker.pending_finish is None
    assert "run_id なしで書く" in caplog.text


def test_running_row_without_child_is_recovered_on_any_tick(tmp_path):
    # 開始の更新が「サーバでは適用済みだが応答で例外」→ 子プロセス無しの running が残る。起動時以外でも回収する
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}])
    original = h.db.update_request

    def lost_response(client, request_id, fields, only_status=None):
        original(client, request_id, fields, only_status)
        if only_status == "queued":
            raise RuntimeError("read timeout")
    h.worker.tick()                                     # 1 周期目 (起動時の回収は済み)
    assert h.db.requests[7]["status"] == "completed"
    h.db.requests[9] = {"id": 9, "status": "queued", "cancel_requested": False, "progress": {}, "params": PARAMS}
    h.db.update_request = lost_response
    h.worker.tick()
    assert h.db.requests[9]["status"] == "running" and len(h.spawned) == 1
    h.db.update_request = original
    h.worker.tick()
    assert h.db.requests[9]["status"] == "failed" and h.db.requests[9]["message"] == MSG_ORPHANED


def test_finish_does_not_overwrite_a_row_finished_elsewhere(tmp_path, caplog):
    def on_poll(proc):
        if proc.polls == 0:
            h.db.requests[7]["status"] = "failed"       # 実行中に他 (別の待ち受けの回収など) が failed にした
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}], on_poll=on_poll)
    h.worker.tick()
    assert h.db.requests[7]["status"] == "failed" and "すでに running ではなかった" in caplog.text
    assert h.worker.pending_finish is None


def test_other_workers_fresh_heartbeat_blocks_recovery_until_stale(tmp_path, caplog):
    # 別の待ち受け (別の PC など) が依頼 5 を実行中。あとから起動した待ち受けは、それを残り物として failed にしない
    h = Harness(tmp_path, [{"id": 5, "status": "running", "params": PARAMS}])
    beat = (T0 - timedelta(seconds=5)).isoformat()
    h.db.state = {"heartbeat_at": beat}
    h.worker.tick()
    assert h.db.requests[5]["status"] == "running" and "別の待ち受けが動いている" in caplog.text
    assert h.db.state["heartbeat_at"] == beat           # 自分の heartbeat も書かない (相手の生存確認を潰さない)
    h.sleep(50)
    h.worker.tick()
    assert h.db.requests[5]["status"] == "running"
    h.sleep(10)                                         # 60 秒更新が無い = 相手は止まっている
    h.worker.tick()
    assert h.db.requests[5]["status"] == "failed" and h.db.state["heartbeat_at"] == h.now.isoformat()


def test_orphaned_run_that_is_still_alive_is_stopped_before_recovery(tmp_path):
    # 待ち受けだけが kill -9 された: 子の run は別セッションで動き続けている → 止めてから failed にする
    h = Harness(tmp_path, [{"id": 5, "status": "running", "params": PARAMS}])
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "5.pid").write_text("777", encoding="utf-8")
    h.alive.add(777)
    h.worker.tick()
    assert h.interrupted == [777] and h.db.requests[5]["status"] == "failed"
    assert not (tmp_path / "requests" / "5.pid").exists()


def test_orphaned_run_that_does_not_stop_stays_running(tmp_path, caplog):
    h = Harness(tmp_path, [{"id": 5, "status": "running", "params": PARAMS}, {"id": 6, "params": PARAMS}])
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "5.pid").write_text("777", encoding="utf-8")
    h.alive.add(777)
    h.dies_after_interrupt = False
    h.worker.tick()
    # 動いている間は running のまま (failed にすると、同じ条件の 2 本目を起動できてしまう)
    assert h.db.requests[5]["status"] == "running" and "まだ終わらない" in caplog.text
    assert h.now >= T0 + timedelta(seconds=60) and h.db.state["heartbeat_at"] is not None
    h.alive.clear()
    h.worker.tick()
    assert h.db.requests[5]["status"] == "failed"


def test_pid_file_is_written_while_running_and_removed_after(tmp_path):
    seen = []

    def on_poll(proc):
        seen.append((tmp_path / "requests" / "7.pid").read_text(encoding="utf-8"))
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}], on_poll=on_poll)
    h.worker.tick()
    assert seen[0] == "4321" and not (tmp_path / "requests" / "7.pid").exists()


def test_run_pid_alive_checks_the_command_line():
    import os
    assert not run_pid_alive(os.getpid(), 7)            # 別のコマンド (PID の使い回し) は run とみなさない
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "arbitrage.run",
                             "--date", "2026-10-11", "--request-id", "7"])
    try:
        assert run_pid_alive(proc.pid, 7) and not run_pid_alive(proc.pid, 70)
    finally:
        proc.kill()
        proc.wait()
    assert not run_pid_alive(proc.pid, 7)


@pytest.mark.parametrize("params", [
    {"purchase_date": "2026-10-11", "reach_slack": 10 ** 400},      # jsonb の 1e400 は巨大な int で届く
    {"purchase_date": "2026-10-11", "min_profit": 10 ** 400},
])
def test_huge_numbers_fail_the_request(tmp_path, params):
    h = Harness(tmp_path, [{"id": 7, "params": params}])
    h.worker.tick()
    assert h.db.requests[7]["status"] == "failed" and h.spawned == []


def test_unexpected_validation_error_fails_the_request_instead_of_looping(tmp_path, monkeypatch):
    def broken(raw):
        raise OverflowError("int too large to convert to float")
    monkeypatch.setattr(worker_module, "parse_params", broken)
    h = Harness(tmp_path, [{"id": 7, "params": PARAMS}])
    h.worker.tick()
    row = h.db.requests[7]
    assert row["status"] == "failed" and row["message"] == "依頼の条件が不正: 検証できない値 (OverflowError)"
    assert h.spawned == []


def test_worker_lock_allows_only_one_holder(tmp_path):
    first = lock_file(tmp_path / "sub" / "worker.lock")
    assert first is not None and lock_file(tmp_path / "sub" / "worker.lock") is None
    first.close()
    again = lock_file(tmp_path / "sub" / "worker.lock")
    assert again is not None
    again.close()


def test_main_refuses_to_start_a_second_worker(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(worker_module, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(worker_module, "load_dotenv", lambda path: None)
    monkeypatch.setattr(worker_module.db, "get_client", lambda: "client")
    monkeypatch.setattr(worker_module.logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(worker_module.signal, "signal", lambda sig, handler: None)
    started = []
    monkeypatch.setattr(Worker, "run_forever", lambda self: started.append(1) or 0)
    held = lock_file(tmp_path / "worker.lock")
    assert worker_module.main([]) == 2 and started == [] and "すでに起動している" in caplog.text
    held.close()
    assert worker_module.main([]) == 0 and started == [1]
