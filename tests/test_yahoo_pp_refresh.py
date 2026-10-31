"""店舗リスト更新 (tools.yahoo_pp.refresh / snapshot / enrich のループ)。ネットワーク・DB はすべて偽物"""

import datetime as dt
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from arbitrage.progress import EXIT_CANCELLED
from arbitrage.stores import StoreListError
from tools.yahoo_pp import enrich, refresh
from tools.yahoo_pp.common import Cancelled, RateLimited
from tools.yahoo_pp.snapshot import build_snapshot, describe

TODAY = dt.date(2026, 10, 31)
DATES = ["2026-10-30", "2026-10-31", "2026-11-01"]
INPUTS = {
    "dates": DATES,
    "sched": {"a-store": ["2505", "9-4"], "b-store": ["2502", "44-"], "n-store": ["2505", "-9-"],
              "none": ["2512", "---"]},
    "names": {"a-store": "A 店", "b-store": "B 店", "n-store": "非適格"},
    "invoice": {"a-store": {"f": "Y", "reg": "T123"}, "b-store": {"f": "Y", "reg": "-"}, "n-store": {"f": "N", "reg": "-"}},
    "gold": {"a-store": 1, "b-store": 0},
    "reviews": {"a-store": [10, 5, 1, 0, 0]},
    "points": {"a-store": {"max": 9.0, "n": 30, "n_up": 2, "unknown": 0, "detail": {}, "checked": []}},
}


def write_inputs(data_dir, inputs=INPUTS):
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, value in inputs.items():
        (data_dir / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")


# ---- snapshot ----

def test_build_snapshot_shape_and_summary():
    snap = build_snapshot(INPUTS, TODAY, "abcd")
    assert (snap["fetched_on"], snap["period_start"], snap["period_end"]) == ("2026-10-31", DATES[0], DATES[-1])
    assert snap["dates"] == DATES and snap["source_sha"] == "abcd"
    assert snap["summary"] == {
        "total": 3, "qualified": 2, "excluded": 1, "gold": 1, "surveyed": 1,
        "by_genre": {"スマホ、タブレット、パソコン": {"qualified": 1, "gold": 0, "excluded": 0},
                     "家電": {"qualified": 1, "gold": 1, "excluded": 1}}}
    assert [s["id"] for s in snap["stores"]] == ["a-store", "b-store", "n-store"]   # 上乗せ率 降順 → ジャンル → ID
    a = snap["stores"][0]
    assert a == {"id": "a-store", "name": "A 店", "genre": "家電", "cat": "2505", "gold": True, "inv": "Y", "reg": "T123",
                 "max_upsell": 9.0, "unknown": 0, "n": 30, "n_up": 2, "reviews": [10, 5, 1, 0, 0],
                 "days": 2, "d9": 1, "d4": 1, "first": "2026-10-30", "last": "2026-11-01", "pat": "9-4"}
    b = snap["stores"][1]
    assert (b["reg"], b["max_upsell"], b["unknown"], b["reviews"], b["gold"]) == ("", None, None, None, False)
    assert snap["stores"][2]["inv"] == "N" and snap["stores"][2]["name"] == "非適格"
    assert "全 3 店 / 適格 2" in describe(snap)
    assert json.dumps(snap)   # JSON にできる


def test_build_snapshot_rejects_missing_or_inconsistent_inputs():
    with pytest.raises(ValueError, match="sched"):
        build_snapshot({"dates": DATES, "sched": {}}, TODAY)
    with pytest.raises(ValueError, match="長さ"):
        build_snapshot({**INPUTS, "sched": {"x": ["2505", "9-"]}}, TODAY)


# ---- enrich のループ (進捗と中止) ----

class FakeFetcher:
    interval = 0.0
    requests_made = 0


@pytest.fixture
def enrich_env(tmp_path, monkeypatch):
    write_inputs(tmp_path, {"sched": {s: ["2505", "9"] for s in ("s1", "s2", "s3")}})
    monkeypatch.setattr(enrich, "fetch_invoice", lambda f, sid: {"f": "Y", "reg": "T"})
    monkeypatch.setattr(enrich, "survey_points", lambda f, sid: (1, {"max": 4.0, "n": 1, "n_up": 1, "unknown": 0,
                                                                     "detail": {}, "checked": []}))
    monkeypatch.setattr(enrich, "fetch_reviews", lambda f, sid: [1, 0, 0, 0, 0])
    return tmp_path


def test_run_enrich_reports_progress_per_store(enrich_env):
    seen = []
    enrich.run_enrich("all", 0, enrich_env, FakeFetcher(), lambda st, d, t: seen.append((st, d, t)), None, TODAY)
    assert seen[:4] == [("invoice", 0, 3), ("invoice", 1, 3), ("invoice", 2, 3), ("invoice", 3, 3)]
    assert [s for s in seen if s[0] == "points"][-1] == ("points", 3, 3)
    assert [s for s in seen if s[0] == "rating"][-1] == ("rating", 3, 3)
    assert len(json.loads((enrich_env / "reviews.json").read_text())) == 3


def test_run_enrich_stops_at_store_boundary_and_saves(enrich_env):
    seen = []

    def should_stop():
        return len(seen) >= 2       # invoice の 1 件目が終わったところで中止
    with pytest.raises(Cancelled):
        enrich.run_enrich("all", 0, enrich_env, FakeFetcher(), lambda st, d, t: seen.append(d), should_stop, TODAY)
    assert json.loads((enrich_env / "invoice.json").read_text()) == {"s1": {"f": "Y", "reg": "T"}}
    # 再実行は続きから (取得済みは飛ばす)
    enrich.run_enrich("invoice", 0, enrich_env, FakeFetcher(), None, None, TODAY)
    assert len(json.loads((enrich_env / "invoice.json").read_text())) == 3


def test_run_enrich_saves_before_raising_rate_limit(enrich_env, monkeypatch):
    calls = []

    def limited(f, sid):
        calls.append(sid)
        if len(calls) == 2:
            raise RateLimited("url")
        return {"f": "Y", "reg": "T"}
    monkeypatch.setattr(enrich, "fetch_invoice", limited)
    with pytest.raises(RateLimited):
        enrich.run_enrich("invoice", 0, enrich_env, FakeFetcher(), None, None, TODAY)
    assert list(json.loads((enrich_env / "invoice.json").read_text())) == ["s1"]


def test_warn_if_bad_day(caplog):
    with caplog.at_level(logging.WARNING):
        assert enrich.warn_if_bad_day(dt.date(2026, 11, 1)) and enrich.warn_if_bad_day(dt.date(2026, 11, 5))
        assert not enrich.warn_if_bad_day(dt.date(2026, 11, 2))
    assert "日曜または 5 のつく日" in caplog.text


# ---- Progress (依頼行の progress) ----

def test_progress_writes_on_stage_change_and_every_20_seconds_and_reads_cancel():
    now = [datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)]
    written, cancel = [], []
    p = refresh.Progress(lambda s: written.append(s) or bool(cancel), clock=lambda: now[0])
    p.update("fetch", 0, 10)
    for i in range(1, 6):
        now[0] += timedelta(seconds=5)
        p.update("fetch", i, 10)
    assert [w["done"] for w in written] == [0, 4]
    assert written[1]["eta"] == (now[0] - timedelta(seconds=5) + timedelta(seconds=30)).isoformat()   # 残り 6 件 × 5 秒
    assert set(written[1]) == {"kind", "stage", "done", "total", "eta", "note", "updated_at"} and written[1]["kind"] == "store_list"
    p.update("invoice", 0, 3)                       # 段階の切り替わりは間隔に関係なく書く
    assert written[-1]["stage"] == "invoice" and written[-1]["eta"] is None
    cancel.append(True)
    assert not p.should_stop()
    now[0] += timedelta(seconds=25)
    p.update("invoice", 1, 3)
    assert p.should_stop()
    p.finish(note="x")
    assert written[-1]["stage"] == "done" and written[-1]["note"] == "x" and written[-1]["eta"] is None


def test_progress_without_sink_and_with_broken_sink(caplog):
    refresh.Progress(None).update("fetch", 1, 2)

    def broken(s):
        raise RuntimeError("down")
    p = refresh.Progress(broken)
    p.update("fetch", 1, 2)
    assert "進捗を書けなかった" in caplog.text and not p.should_stop()


# ---- refresh の流れ ----

class FakeClient:
    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == "arbitrage_store_lists"
        return self

    def insert(self, row):
        self.rows.append(row)
        return self

    def execute(self):
        return type("R", (), {"data": [{"id": len(self.rows)}]})()


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.data_dir = tmp_path / "yahoo_pp"
        self.stores_path = tmp_path / "yahoo_pp_stores_5genres.xlsx"
        self.client = FakeClient()
        self.progress = refresh.Progress(None)
        self.calls = []
        write_inputs(self.data_dir, {"sched": {"old": ["2505", "9"]}, "dates": ["2026-10-01"]})
        (self.data_dir / "cache").mkdir()
        (self.data_dir / "cache" / "x.json").write_text("{}", encoding="utf-8")
        self.stores_path.write_bytes(b"old xlsx")

        def fetch_schedule(f, today, on_progress=None, should_stop=None):
            self.calls.append("fetch")
            assert not (self.data_dir / "sched.json").exists()          # 前回の分は退避済み
            assert not (self.data_dir / "cache" / "x.json").exists()    # cache は消えている
            on_progress("fetch", 1, 1)
            return {"dates": [dt.date.fromisoformat(d) for d in DATES], "sched": INPUTS["sched"],
                    "names": INPUTS["names"], "counts": {}, "mismatch": 0}

        def run_enrich(stage, limit, out, f, on_progress, should_stop, today):
            self.calls.append("enrich")
            for name in ("invoice", "gold", "reviews", "points"):
                (out / f"{name}.json").write_text(json.dumps(INPUTS[name]), encoding="utf-8")
            on_progress("invoice", 3, 3)

        def build(out, today, inputs, plus2=None, plus3=None):
            self.calls.append("build")
            out.write_bytes(b"new xlsx")
            return {}

        def validate(path, today):
            self.calls.append("validate")
            assert path.read_bytes() == b"new xlsx"
            return 900
        monkeypatch.setattr(refresh.bsplus_fetch, "fetch_schedule", fetch_schedule)
        monkeypatch.setattr(refresh.enrich, "run_enrich", run_enrich)
        monkeypatch.setattr(refresh.build_xlsx, "build", build)
        monkeypatch.setattr(refresh, "validate_new_list", validate)
        monkeypatch.setattr(refresh, "old_list_label", lambda path: "2026-10-04")

    def run(self):
        return refresh.refresh(self.client, self.progress, TODAY, self.data_dir, self.stores_path, FakeFetcher())


def test_refresh_replaces_list_and_inserts_snapshot(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert env.run() == 0
    assert env.calls == ["fetch", "enrich", "build", "validate"]
    assert env.stores_path.read_bytes() == b"new xlsx"
    assert (tmp_path / "yahoo_pp_stores_5genres.2026-10-04.bak.xlsx").read_bytes() == b"old xlsx"
    assert not (env.data_dir / "new_stores.xlsx").exists() and not (env.data_dir / "prev").exists()
    assert json.loads((env.data_dir / "sched.json").read_text()) == INPUTS["sched"]
    row = env.client.rows[0]
    assert row["summary"]["total"] == 3 and row["fetched_on"] == "2026-10-31" and row["dates"] == DATES
    assert row["source_sha"] is not None and len(row["source_sha"]) == 16


def test_refresh_restores_previous_on_cancel_and_failure(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)

    def cancelled(stage, limit, out, f, on_progress, should_stop, today):
        (out / "invoice.json").write_text("{}", encoding="utf-8")
        raise Cancelled()
    monkeypatch.setattr(refresh.enrich, "run_enrich", cancelled)
    with pytest.raises(Cancelled):
        env.run()
    refresh.restore_previous(env.data_dir)
    assert json.loads((env.data_dir / "sched.json").read_text()) == {"old": ["2505", "9"]}
    assert not (env.data_dir / "invoice.json").exists() and not (env.data_dir / "prev").exists()
    assert env.stores_path.read_bytes() == b"old xlsx" and env.client.rows == []

    env = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(refresh, "validate_new_list", lambda p, t: (_ for _ in ()).throw(StoreListError("少なすぎる")))
    with pytest.raises(StoreListError):
        env.run()
    assert env.stores_path.read_bytes() == b"old xlsx" and env.client.rows == []


def test_validate_new_list_requires_500_stores(monkeypatch, tmp_path):
    class Fake:
        stores = {str(i): i for i in range(499)}
    monkeypatch.setattr(refresh, "load_stores", lambda today, path: Fake())
    with pytest.raises(StoreListError, match="499 < 500"):
        refresh.validate_new_list(tmp_path / "x.xlsx", TODAY)
    Fake.stores = {str(i): i for i in range(500)}
    assert refresh.validate_new_list(tmp_path / "x.xlsx", TODAY) == 500


def test_swap_in_keeps_existing_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "old_list_label", lambda path: "2026-10-04")
    target, new = tmp_path / "s.xlsx", tmp_path / "n.xlsx"
    (tmp_path / "s.2026-10-04.bak.xlsx").write_bytes(b"older")
    target.write_bytes(b"old")
    new.write_bytes(b"new")
    backup = refresh.swap_in(new, target)
    assert backup.name != "s.2026-10-04.bak.xlsx" and backup.read_bytes() == b"old"
    assert (tmp_path / "s.2026-10-04.bak.xlsx").read_bytes() == b"older" and target.read_bytes() == b"new"
    new.write_bytes(b"new2")
    assert refresh.swap_in(new, tmp_path / "fresh.xlsx") is None


def test_main_exit_codes(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(refresh, "DATA_DIR", tmp_path)
    monkeypatch.setattr(refresh, "load_dotenv", lambda path: None)
    monkeypatch.setattr(refresh.db, "get_client", lambda: "client")
    monkeypatch.setattr(refresh.logging, "basicConfig", lambda **kw: None)
    sinks = []
    monkeypatch.setattr(refresh, "supabase_sink", lambda client, rid: sinks.append(rid) or (lambda s: False))
    outcomes = {"cancel": Cancelled(), "rate": RateLimited("u"), "bad": StoreListError("少ない"),
                "boom": KeyError("k"), "ok": None}

    def fake_refresh(client, progress, today, *a, **kw):
        if outcomes[kind] is not None:
            raise outcomes[kind]
        return 0
    monkeypatch.setattr(refresh, "refresh", fake_refresh)
    kind = "ok"
    assert refresh.main(["--request-id", "5"]) == 0 and sinks == [5]
    kind = "cancel"
    assert refresh.main([]) == EXIT_CANCELLED
    kind = "rate"
    assert refresh.main([]) == 1 and "アクセス制限。時間をおいて更新し直す" in caplog.text
    kind = "bad"
    assert refresh.main([]) == 1 and "少ない" in caplog.text
    kind = "boom"
    assert refresh.main([]) == 1 and "KeyError" in caplog.text


def test_main_print_snapshot_needs_no_db(tmp_path, monkeypatch, capsys):
    write_inputs(tmp_path)
    monkeypatch.setattr(refresh, "DATA_DIR", tmp_path)
    monkeypatch.setattr(refresh, "DEFAULT_STORES_PATH", tmp_path / "none.xlsx")
    monkeypatch.setattr(refresh, "load_dotenv", lambda path: None)
    monkeypatch.setattr(refresh.logging, "basicConfig", lambda **kw: None)

    def no_db():
        raise AssertionError("DB を使ってはいけない")
    monkeypatch.setattr(refresh.db, "get_client", no_db)
    assert refresh.main(["--print-snapshot", "--today", "2026-10-31"]) == 0
    out = capsys.readouterr().out
    assert "全 3 店 / 適格 2" in out and "sha None" in out

    # --upload-only は insert する。取得日は sched.json の更新日
    client = FakeClient()
    monkeypatch.setattr(refresh.db, "get_client", lambda: client)
    assert refresh.main(["--upload-only"]) == 0
    assert client.rows[0]["fetched_on"] == dt.date.today().isoformat() and client.rows[0]["summary"]["total"] == 3
