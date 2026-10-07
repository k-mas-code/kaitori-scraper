from datetime import datetime, timedelta, timezone

import pytest

from arbitrage import progress
from arbitrage.progress import (STOP_CANCELLED, STOP_DEADLINE, ProgressReporter, SpeedMeter, Throttle,
                                forecast_eta, parse_deadline, supabase_sink)
from arbitrage.research import DEFAULT_RATES

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def test_throttle_passes_only_after_the_interval():
    t = Throttle(20)
    assert t.due(at(0))
    t.mark(at(0))
    assert not t.due(at(19.9)) and t.due(at(20))
    t.mark(at(20))
    assert not t.due(at(39)) and t.due(at(45))


def test_speed_meter_uses_recent_window():
    m = SpeedMeter(window=600, min_span=30)
    assert m.rate() is None
    m.add(at(0), 100)
    m.add(at(10), 105)
    assert m.rate() is None                              # 30 秒に満たない実測は使わない
    m.add(at(60), 130)
    assert m.rate() == pytest.approx(0.5)                # 30 件 / 60 秒
    for i in range(1, 21):                               # その後は 1 分に 6 件へ減速
        m.add(at(60 + i * 60), 130 + i * 6)
    assert m.rate() == pytest.approx(0.1)                # 古い速い区間は窓の外
    m.add(at(5000), 250)                                 # 進んでいない間は足さない
    assert m.rate() == pytest.approx(0.1)


def state(**kw):
    return {"stage": "api", "jan_total": 0, "jan_done": 0, "candidates": 0, "pages_total": 0, "pages_done": 0, **kw}


def test_forecast_eta_api_stage_adds_page_stage():
    s = state(jan_total=1000, jan_done=400, candidates=200)
    # 残り 600 JAN ÷ 0.5 件/秒 = 1,200 秒。候補は 200 × 1000/400 = 500 件 × 0.28 × 4.1 秒
    eta = forecast_eta(T0, s, 0.5, 0.28)
    assert eta == T0 + timedelta(seconds=1200 + 500 * 0.28 * DEFAULT_RATES["seconds_per_check"])
    # 実測が無いうちは既定の速度 (27.6 JAN/分)
    no_rate = forecast_eta(T0, s, None, 1.0)
    assert no_rate == T0 + timedelta(seconds=600 / (27.6 / 60) + 500 * 4.1)


def test_forecast_eta_pages_saving_and_deadline():
    s = state(stage="pages", pages_total=100, pages_done=40)
    assert forecast_eta(T0, s, 0.25, 0.28) == at(240)
    assert forecast_eta(T0, s, None, 0.28) == T0 + timedelta(seconds=60 * 4.1)
    assert forecast_eta(T0, s, 0.25, 0.28, deadline=at(100)) == at(100)       # 上限より後にはならない
    assert forecast_eta(T0, state(stage="saving"), None, 1) == T0
    assert forecast_eta(T0, state(stage="starting"), None, 1) is None
    assert forecast_eta(T0, state(stage="done"), None, 1) is None


def test_reporter_writes_at_most_every_20_seconds_but_always_on_stage_change():
    clock, written = Clock(), []
    r = ProgressReporter("run1", lambda p: written.append(p) or False, clock=clock)
    r.set_stage("api", jan_total=100, jan_done=0, candidates=0)
    for done in range(1, 11):                            # 5 秒ごとに 1 件 → 50 秒
        clock.advance(5)
        assert r.check(jan_done=done, candidates=done * 2) is None
    assert [p["jan_done"] for p in written] == [0, 4, 8]          # 0 秒 (切り替わり), 20 秒, 40 秒
    clock.advance(1)
    r.set_stage("pages", pages_total=20, pages_done=0)            # 間隔に関係なく書く
    r.finish("おわり", results=3)
    assert [p["stage"] for p in written] == ["api", "api", "api", "pages", "done"]
    last = written[-1]
    assert set(last) == {"stage", "run_name", "jan_total", "jan_done", "candidates", "pages_total", "pages_done",
                         "results", "eta", "updated_at", "note"}
    assert (last["run_name"], last["results"], last["note"], last["eta"]) == ("run1", 3, "おわり", None)
    assert last["updated_at"] == clock.now.isoformat() and written[1]["eta"] is not None


def test_reporter_stops_on_cancel_and_deadline():
    clock, cancel = Clock(), []
    r = ProgressReporter("run1", lambda p: bool(cancel), clock=clock)
    r.set_stage("api", jan_total=10)
    clock.advance(5)
    cancel.append(True)
    assert r.check(jan_done=1) is None                   # まだ書く間隔ではないので読んでいない
    clock.advance(20)
    assert r.check(jan_done=2) == STOP_CANCELLED

    clock = Clock()
    r = ProgressReporter("run1", None, deadline=at(60), clock=clock)
    assert r.check() is None
    clock.advance(59)
    assert r.check() is None
    clock.advance(1)
    assert r.check() == STOP_DEADLINE


def test_reporter_survives_sink_failure_and_does_nothing_without_sink(caplog):
    def broken(p):
        raise RuntimeError("network down")
    clock = Clock()
    r = ProgressReporter("run1", broken, clock=clock)
    r.set_stage("api", jan_total=10)
    clock.advance(30)
    assert r.check(jan_done=1) is None and "進捗を書けなかった" in caplog.text
    silent = ProgressReporter("run1", None, clock=clock)
    silent.set_stage("api", jan_total=10)
    silent.finish()
    assert silent.check() is None


def test_supabase_sink_updates_progress_and_reads_cancel(monkeypatch):
    calls = []

    def fake_update(client, request_id, fields):
        calls.append((client, request_id, fields))
        return {"id": request_id, "cancel_requested": len(calls) > 1}
    monkeypatch.setattr(progress.db, "update_request", fake_update)
    sink = supabase_sink("client", 7)
    assert sink({"stage": "api"}) is False and sink({"stage": "pages"}) is True
    assert calls[0] == ("client", 7, {"progress": {"stage": "api"}})
    monkeypatch.setattr(progress.db, "update_request", lambda *a: None)      # 行が無い
    assert sink({"stage": "api"}) is False


def test_build_reporter_without_request_id_never_touches_db(monkeypatch):
    def no_client():
        raise AssertionError("DB を使ってはいけない")
    monkeypatch.setattr(progress.db, "get_client", no_client)
    r = progress.build_reporter("run1", None, None, "reachable")
    assert r.sink is None and r.page_fraction == DEFAULT_RATES["reachable_fraction"]
    # --request-id があるのに接続情報が無い: 警告して進捗なしで続ける
    def unconfigured():
        raise progress.db.DbConfigError("SUPABASE_URL / SUPABASE_KEY が未設定")
    monkeypatch.setattr(progress.db, "get_client", unconfigured)
    assert progress.build_reporter("run1", 5, None, "all").sink is None


def test_parse_deadline_treats_naive_as_local_time():
    aware = parse_deadline("2026-10-05T23:00:00+09:00")
    assert aware == datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    naive = parse_deadline("2026-10-05T23:00")
    assert naive.tzinfo is not None and naive.replace(tzinfo=None) == datetime(2026, 10, 5, 23, 0)
    with pytest.raises(ValueError):
        parse_deadline("tonight")
