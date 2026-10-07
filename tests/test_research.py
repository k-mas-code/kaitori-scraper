from datetime import date, datetime, timedelta, timezone

import pytest

from arbitrage.research import (DEFAULT_RATES, ParamsError, ResearchParams, build_run_args, buyback_histogram,
                                clean_rates, count_targets, estimate, parse_params, updated_rates)
from arbitrage.run import parse_args

JST = timezone(timedelta(hours=9))


def test_parse_params_defaults():
    p = parse_params({"purchase_date": "2026-10-11"})
    assert p == ResearchParams(date(2026, 10, 11), None, None, 1, "reachable", 0.03, None)


def test_parse_params_full():
    p = parse_params({"purchase_date": "2026-10-11", "min_buyback": 5000, "max_buyback": 60000,
                      "min_profit": -1000, "page_scope": "all", "reach_slack": 0.1,
                      "deadline": "2026-10-05T14:00:00.000Z"})
    assert (p.min_buyback, p.max_buyback, p.min_profit, p.page_scope, p.reach_slack) == (5000, 60000, -1000, "all", 0.1)
    assert p.deadline == datetime(2026, 10, 5, 23, 0, tzinfo=JST)
    assert parse_params({"purchase_date": "2026-10-11", "min_buyback": None, "deadline": None}).deadline is None


@pytest.mark.parametrize("raw, match", [
    ([], "params"),
    ({}, "purchase_date"),
    ({"purchase_date": "2026/10/11"}, "purchase_date"),
    ({"purchase_date": "2026-02-30"}, "purchase_date.*存在しない"),
    ({"purchase_date": "2026-10-11; rm -rf /"}, "purchase_date"),
    ({"purchase_date": "2026-10-11", "run_name": "x"}, "不明なキー: run_name"),
    ({"purchase_date": "2026-10-11", "min_buyback": -1}, "min_buyback"),
    ({"purchase_date": "2026-10-11", "min_buyback": "5000"}, "min_buyback"),
    ({"purchase_date": "2026-10-11", "min_buyback": True}, "min_buyback"),
    ({"purchase_date": "2026-10-11", "max_buyback": 1.5}, "max_buyback"),
    ({"purchase_date": "2026-10-11", "min_buyback": 5000, "max_buyback": 4999}, "max_buyback"),
    ({"purchase_date": "2026-10-11", "min_profit": -5001}, "min_profit"),
    ({"purchase_date": "2026-10-11", "min_profit": 100001}, "min_profit"),
    ({"purchase_date": "2026-10-11", "min_profit": None}, "min_profit"),
    ({"purchase_date": "2026-10-11", "page_scope": "some"}, "page_scope"),
    ({"purchase_date": "2026-10-11", "reach_slack": 0.21}, "reach_slack"),
    ({"purchase_date": "2026-10-11", "reach_slack": -0.01}, "reach_slack"),
    ({"purchase_date": "2026-10-11", "reach_slack": "0.03"}, "reach_slack"),
    ({"purchase_date": "2026-10-11", "deadline": "2026-10-05T23:00:00"}, "deadline.*タイムゾーン"),
    ({"purchase_date": "2026-10-11", "deadline": "tonight"}, "deadline"),
    ({"purchase_date": "2026-10-11", "deadline": 1759672800}, "deadline"),
])
def test_parse_params_rejects(raw, match):
    with pytest.raises(ParamsError, match=match):
        parse_params(raw)


def test_build_run_args_minimal_and_full():
    assert build_run_args(parse_params({"purchase_date": "2026-10-11"}), 7) == [
        "--date", "2026-10-11", "--request-id", "7", "--page-scope", "reachable", "--reach-slack", "0.03",
        "--min-profit=1"]
    p = parse_params({"purchase_date": "2026-10-11", "min_buyback": 0, "max_buyback": 60000,
                      "min_profit": -1000, "page_scope": "all", "reach_slack": 0,
                      "deadline": "2026-10-05T23:00:00+09:00"})
    argv = build_run_args(p, 12)
    assert argv == ["--date", "2026-10-11", "--request-id", "12", "--page-scope", "all", "--reach-slack", "0.0",
                    "--min-profit=-1000", "--min-buyback", "0", "--max-buyback", "60000",
                    "--deadline", "2026-10-05T23:00:00+09:00"]
    assert all(isinstance(a, str) for a in argv)


def test_build_run_args_are_accepted_by_run():
    p = parse_params({"purchase_date": "2026-10-11", "min_buyback": 5000, "min_profit": -1000,
                      "reach_slack": 0.05, "deadline": "2026-10-05T23:00:00+09:00"})
    args = parse_args(build_run_args(p, 12))
    assert (args.date, args.request_id, args.page_scope, args.reach_slack) == (date(2026, 10, 11), 12, "reachable", 0.05)
    assert (args.min_profit, args.min_buyback, args.max_buyback) == (-1000, 5000, None)
    assert args.deadline == p.deadline


def test_buyback_histogram_buckets():
    assert buyback_histogram([0, 999, 1000, 1500, 59999, 60000]) == {"0": 2, "1000": 2, "59000": 1, "60000": 1}
    assert buyback_histogram([2500, 7400], bucket=5000) == {"0": 1, "5000": 1}
    assert buyback_histogram([]) == {}


HISTOGRAM = {"bucket": 1000, "as_of": "2026-10-05T03:00:00+00:00",
             "counts": {"0": 100, "1000": 200, "5000": 300, "60000": 40}}


def test_count_targets_prorates_partial_buckets():
    assert count_targets(HISTOGRAM, None, None) == 640
    assert count_targets(HISTOGRAM, 1000, 5999) == 500               # 帯の境界ちょうど
    assert count_targets(HISTOGRAM, 5000, None) == 340
    assert count_targets(HISTOGRAM, 500, 1499) == pytest.approx(50 + 100)    # 帯の半分ずつ
    assert count_targets(HISTOGRAM, None, 60000) == pytest.approx(600 + 40 / 1000)
    assert count_targets(HISTOGRAM, 70000, None) == 0
    assert count_targets(None, None, None) == 0 and count_targets({"counts": []}, None, None) == 0


def test_estimate_follows_the_formula():
    rates = {"jan_per_min": 30, "candidates_per_jan": 2, "reachable_fraction": 0.25, "seconds_per_check": 4}
    p = parse_params({"purchase_date": "2026-10-11", "min_buyback": 1000, "max_buyback": 5999})
    e = estimate(p, HISTOGRAM, rates)
    assert (e.jan_count, e.page_checks, e.page_seconds) == (500, 250, 1000)
    assert e.api_minutes == pytest.approx(500 / 30) and e.total_seconds == pytest.approx(1000 + 1000)
    everything = estimate(parse_params({"purchase_date": "2026-10-11", "min_buyback": 1000, "max_buyback": 5999,
                                        "page_scope": "all"}), HISTOGRAM, rates)
    assert everything.page_checks == 1000 and everything.page_seconds == 4000


def test_estimate_uses_default_rates_when_missing_or_broken():
    p = parse_params({"purchase_date": "2026-10-11"})
    for rates in (None, {}, {"jan_per_min": 0, "candidates_per_jan": "x", "seconds_per_check": None}):
        e = estimate(p, HISTOGRAM, rates)
        assert e.api_minutes == pytest.approx(640 / 27.6)
        assert e.page_seconds == pytest.approx(640 * 1.1 * 0.28 * 4.1)
    assert clean_rates({"jan_per_min": 20, "extra": 1}) == {**DEFAULT_RATES, "jan_per_min": 20.0}


def test_updated_rates_needs_200_jans():
    stats = {"jan_processed": 199, "api_seconds": 600, "jan_total": 199, "candidates": 300, "reachable": 60,
             "pages_processed": 60, "pages_seconds": 300}
    assert updated_rates(None, stats) is None and updated_rates(None, None) is None
    rates = updated_rates(None, {**stats, "jan_processed": 300, "jan_total": 600, "candidates": 900, "reachable": 180})
    assert rates == {"jan_per_min": 30.0, "candidates_per_jan": 1.5, "reachable_fraction": 0.2, "seconds_per_check": 5.0}
    # 確定判定が少なすぎる・候補が無いときは、その値だけ前のまま
    old = {"jan_per_min": 20, "candidates_per_jan": 1.3, "reachable_fraction": 0.4, "seconds_per_check": 3.3}
    partial = updated_rates(old, {"jan_processed": 300, "api_seconds": 600, "jan_total": 300, "candidates": 0,
                                  "reachable": 0, "pages_processed": 5, "pages_seconds": 50})
    assert partial == {**old, "jan_per_min": 30.0}


@pytest.mark.parametrize("key", ["reach_slack", "min_profit", "max_buyback"])
def test_huge_integers_are_rejected_or_kept_as_int_without_other_exceptions(key):
    # jsonb の 1e400 は Python に 401 桁の int で届く。float に変換しようとして OverflowError を出さない
    raw = {"purchase_date": "2026-10-11", key: 10 ** 400}
    if key == "max_buyback":
        assert parse_params(raw).max_buyback == 10 ** 400     # 上限なしと同じ意味 (範囲の上限は無い)
    else:
        with pytest.raises(ParamsError, match=key):
            parse_params(raw)
