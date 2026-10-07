"""run.main を通した確認: 確定判定の範囲と順序、--deadline と中止の依頼での打ち切り (通信はすべて偽物)"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import arbitrage.run as run_module
import arbitrage.state as state_module
from arbitrage.buyback import Buyback
from arbitrage.progress import ProgressReporter
from arbitrage.run import count_results, parse_args
from arbitrage.state import lock_file
from arbitrage.stores import Store, StoreList

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
JANS = [f"49000000000{i:02d}" for i in range(5)]


class World:
    """偽の時計・API・商品ページ。API 1 回 / ページ 1 件ごとに時計が 60 秒進む"""

    def __init__(self):
        self.now = T0
        self.searched: list[str] = []
        self.checked: list[str] = []

    def clock(self):
        return self.now

    def search_jan(self, jan_code, start=1):
        self.now += timedelta(seconds=60)
        self.searched.append(jan_code)
        # 10,000 円 = クーポン余地なしでも届く / 11,500 円 = 余地 20% なら通るが、余裕 3% では届かない
        hits = [{"name": "item", "url": f"https://store.shopping.yahoo.co.jp/s1/{jan_code}-{price}.html",
                 "code": f"s1_{jan_code}_{price}", "price": price, "inStock": True, "condition": "new",
                 "seller": {"sellerId": "s1", "name": "店"}} for price in (10000, 11500)]
        return {"totalResultsAvailable": len(hits), "hits": hits}

    def check_candidate(self, pages, candidate, cfg, stores):
        self.now += timedelta(seconds=60)
        self.checked.append(candidate["item_code"])
        profit = 500 if candidate["sale_price"] == 10000 else -200
        row = {"profit": profit, "profit_rate": profit / 10000, "item_name": "item", "store_id": "s1",
               "item_code": candidate["item_code"], "effective_price": 8500, "buyback_source": "rudeya",
               "buyback_price": 9000}
        return {"store_id": "s1", "item_code": candidate["item_code"], "jan_code": candidate["jan_code"],
                "status": "result" if profit > 0 else "not_profitable", "row": row}


@pytest.fixture
def world(monkeypatch, tmp_path, caplog):
    w = World()
    caplog.set_level(logging.INFO)
    stores = StoreList({"s1": Store("s1", "s1", "家電", False, Decimal("0.04"), Decimal("0.14"))},
                       date(2026, 10, 4), None, "x.xlsx")
    client = type("FakeApi", (), {"request_count": 0, "search_jan": staticmethod(w.search_jan)})
    monkeypatch.setattr(run_module, "load_dotenv", lambda path: None)      # .env は読まない
    monkeypatch.setattr(run_module, "load_stores", lambda run_date: stores)
    monkeypatch.setattr(run_module, "ItemSearchClient", lambda client_id: client)
    monkeypatch.setattr(run_module, "load_buyback_prices",
                        lambda today: {j: Buyback(j, 9000, "rudeya", "2026-10-04") for j in JANS})
    monkeypatch.setattr(run_module, "PageClient", lambda: type("FakePages", (), {"request_count": 0})())
    monkeypatch.setattr(run_module, "check_candidate", w.check_candidate)
    monkeypatch.setattr(run_module.logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(state_module, "RUNS_DIR", tmp_path)
    config = tmp_path / "s.yaml"
    config.write_text("version: 1\npurchase_date: 2026-10-04\n", encoding="utf-8")
    w.dir = tmp_path / "t"
    w.argv = ["--config-file", str(config), "--run-name", "t", "--no-db"]
    w.main = lambda *extra: run_module.main(w.argv + list(extra), clock=w.clock)
    w.pages = lambda: [json.loads(line) for line in (w.dir / "pages.jsonl").read_text().splitlines()]
    w.caplog = caplog
    return w


def test_new_options_parse_and_validate():
    args = parse_args([])
    assert (args.page_scope, args.reach_slack, args.deadline, args.request_id) == ("reachable", 0.03, None, None)
    args = parse_args(["--page-scope", "all", "--reach-slack", "0.2", "--request-id", "9",
                       "--deadline", "2026-10-05T23:00:00+09:00"])
    assert (args.page_scope, args.reach_slack, args.request_id) == ("all", 0.2, 9)
    assert args.deadline == datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    for bad in (["--reach-slack", "0.21"], ["--reach-slack", "-0.1"], ["--page-scope", "some"],
                ["--deadline", "tonight"]):
        with pytest.raises(SystemExit):
            parse_args(bad)


def test_count_results_counts_rows_at_or_above_min_profit():
    records = [{"status": "result", "row": {"profit": 500}}, {"status": "not_profitable", "row": {"profit": -200}},
               {"status": "gone"}]
    assert count_results(records, 1) == 1 and count_results(records, -200) == 2 and count_results(records, 501) == 0


def test_reachable_scope_checks_only_reachable_candidates_and_exits_ok(world):
    assert world.main() == 0
    assert len(world.searched) == 5
    assert len(world.checked) == 5 and all(code.endswith("_10000") for code in world.checked)
    assert "届きそうにない候補 5 件は確認しない" in world.caplog.text
    assert "未確認" not in world.caplog.text                     # 範囲外は「未確認」の扱いにしない
    stats = json.loads((world.dir / "meta.json").read_text())["stats"]
    assert stats == {"request_id": None, "jan_total": 5, "candidates": 10, "reachable": 5, "jan_processed": 5,
                     "api_seconds": 300.0, "pages_processed": 5, "pages_seconds": 300.0}
    # 条件 (meta) には含まれないので、同じ run のまま範囲を広げて続きを確認できる。届きそうな候補は確認済み
    assert world.main("--page-scope", "all") == 0
    assert len(world.checked) == 10 and all(code.endswith("_11500") for code in world.checked[5:])
    assert len(world.searched) == 5


def test_scope_all_checks_reachable_first(world):
    assert world.main("--page-scope", "all") == 0
    assert [code.rsplit("_", 1)[1] for code in world.checked] == ["10000"] * 5 + ["11500"] * 5
    assert "届きそうにない" not in world.caplog.text


def test_min_profit_widens_reachable_scope(world):
    # 11,500 円の候補は見積もりで −169 円。1,000 円までの赤字も載せるなら届きそうに入る
    assert world.main("--min-profit", "-1000") == 0
    assert len(world.checked) == 10
    assert "結果 10 件" in world.caplog.text


def test_deadline_in_api_stage_saves_only_searched_jans(world):
    # 1 件 60 秒。150 秒後が上限 → 3 件目を終えた時点 (180 秒) で打ち切る
    assert world.main("--deadline", (T0 + timedelta(seconds=150)).isoformat()) == 0
    assert len(world.searched) == 3 and world.checked == []       # 確定判定は行わない
    assert "終了時刻の上限に達したため打ち切った (API 3/5 JAN 完了)" in world.caplog.text
    assert "未確認の候補 3 件を残したまま、確認済みの分を保存する" in world.caplog.text
    assert (world.dir / "coupons.csv").exists()                   # 保存の段階まで進んでいる
    # 上限なしでやり直すと、残りの JAN から続く
    assert world.main() == 0
    assert len(world.searched) == 5 and len(world.checked) == 5


def test_deadline_in_page_stage_keeps_checked_rows(world):
    assert world.main("--stage", "api") == 0
    start = world.now
    assert world.main("--deadline", (start + timedelta(seconds=100)).isoformat()) == 0
    assert len(world.checked) == 2 and len(world.pages()) == 2
    assert "終了時刻の上限に達したため打ち切った (商品ページ 2/5 件 完了)" in world.caplog.text
    assert "未確認の候補 3 件を残したまま" in world.caplog.text
    assert world.main() == 0
    assert len(world.checked) == 5 and len(world.searched) == 5


def test_deadline_already_passed_does_no_new_work(world):
    assert world.main("--deadline", (T0 - timedelta(seconds=1)).isoformat()) == 0
    assert world.searched == [] and world.checked == []


def test_deadline_with_stage_api_just_stops(world):
    assert world.main("--stage", "api", "--deadline", (T0 + timedelta(seconds=30)).isoformat()) == 0
    assert len(world.searched) == 1 and not (world.dir / "coupons.csv").exists()


def test_resume_command_carries_non_default_options_but_not_the_deadline(world):
    world.main("--deadline", (T0 + timedelta(seconds=30)).isoformat(), "--page-scope", "all", "--reach-slack", "0.1",
               "--min-profit", "-500")
    resume = next(r.getMessage() for r in world.caplog.records if "続き:" in r.getMessage())
    assert "--page-scope all" in resume and "--reach-slack 0.1" in resume and "--min-profit -500" in resume
    assert "--deadline" not in resume and "--request-id" not in resume
    world.caplog.clear()
    world.argv[world.argv.index("t")] = "t2"
    world.main("--deadline", (T0 - timedelta(seconds=1)).isoformat())
    resume = next(r.getMessage() for r in world.caplog.records if "続き:" in r.getMessage())
    assert "--page-scope" not in resume and "--reach-slack" not in resume and "--min-profit" not in resume


def reporter_with_sink(world, monkeypatch, cancel_after_writes=None, request_ids=(42,)):
    """--request-id のときの書き戻しを偽の sink に差し替える (間隔 0 = 毎回書く)"""
    written = []

    def sink(progress):
        written.append(progress)
        return cancel_after_writes is not None and len(written) >= cancel_after_writes

    def build(run_name, request_id, deadline, page_scope, clock):
        assert request_id in request_ids
        return ProgressReporter(run_name, sink, deadline, clock, interval=0)
    monkeypatch.setattr(run_module, "build_reporter", build)
    return written


def test_progress_is_reported_through_all_stages(world, monkeypatch):
    written = reporter_with_sink(world, monkeypatch)
    assert world.main("--request-id", "42") == 0
    stages = [p["stage"] for p in written]
    assert stages[0] == "starting" and stages[-2:] == ["saving", "done"]
    assert [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s] == [
        "starting", "api", "pages", "saving", "done"]
    last = written[-1]
    assert (last["run_name"], last["jan_total"], last["jan_done"], last["candidates"]) == ("t", 5, 5, 10)
    assert (last["pages_total"], last["pages_done"], last["results"], last["note"]) == (5, 5, 5, None)
    mid_pages = next(p for p in written if p["stage"] == "pages" and p["pages_done"] == 3)
    assert mid_pages["results"] == 3 and mid_pages["eta"] is not None
    assert json.loads((world.dir / "meta.json").read_text())["stats"]["request_id"] == 42


def test_cancel_request_stops_saves_and_exits_3(world, monkeypatch):
    # 書き込み: starting, api 切り替え, 1 件目の前, 2 件目の前, 3 件目の前 (ここで中止の依頼を読む)
    written = reporter_with_sink(world, monkeypatch, cancel_after_writes=5)
    assert world.main("--request-id", "42") == 3
    assert len(world.searched) == 2 and world.checked == []
    assert "中止の依頼を受けて打ち切った" in world.caplog.text
    assert written[-1]["stage"] == "done" and written[-1]["note"] == "中止の依頼を受けて打ち切った"
    assert (world.dir / "coupons.csv").exists()


def test_request_with_changed_conditions_uses_a_name_derived_from_the_conditions(world, monkeypatch, tmp_path):
    # 同じ日に設定を変えて結果ページから実行し直した場合: エラーにせず、条件から決まる別名で新しく始める
    reporter_with_sink(world, monkeypatch, request_ids=(42, 43, 44))
    monkeypatch.setattr(run_module, "date", type("FakeDate", (date,), {"today": classmethod(lambda cls: date(2026, 10, 5))}))
    world.argv = [a for a in world.argv if a not in ("--run-name", "t")]
    assert world.main("--request-id", "42") == 0
    assert (tmp_path / "20261005-buy1004" / "meta.json").exists()
    # 依頼 43: 条件を変えて開始し、API 3 件で打ち切られる
    start = world.now
    assert world.main("--request-id", "43", "--coupon-margin", "0.1",
                      "--deadline", (start + timedelta(seconds=150)).isoformat()) == 0
    [alias] = [d.name for d in tmp_path.glob("20261005-buy1004-*")]
    assert alias.startswith("20261005-buy1004-c") and "r43" not in alias
    assert f"run 名を {alias} にして新しく始める" in world.caplog.text
    assert len(world.searched) == 5 + 3
    # 依頼 44: 同じ条件で依頼し直す → 同じ名前になり、続きから再開する (API を最初からやり直さない)
    assert world.main("--request-id", "44", "--coupon-margin", "0.1") == 0
    assert [d.name for d in tmp_path.glob("20261005-buy1004-*")] == [alias]
    assert len(world.searched) == 5 + 5 and f"run 名を {alias} にして続きから再開する" in world.caplog.text
    # さらに別の条件は、また別の名前になる
    assert world.main("--request-id", "44", "--coupon-margin", "0.15") == 0
    assert len(list(tmp_path.glob("20261005-buy1004-*"))) == 2
    # 手動実行 (--request-id なし) は従来どおり止める
    assert world.main("--coupon-margin", "0.3") == 2


def test_run_folder_in_use_by_another_process_is_refused(world):
    # 前の run (待ち受けだけが落ちて残った子など) が同じ run フォルダを使っている間は始めない
    held = lock_file(world.dir / "run.lock")
    assert world.main() == 2
    assert world.searched == [] and "別のプロセスが実行中" in world.caplog.text
    assert not (world.dir / "meta.json").exists()
    held.close()
    assert world.main() == 0 and len(world.searched) == 5
    assert world.main("--stage", "save") == 0           # 終わればロックは外れている (早期 return の経路も含む)
