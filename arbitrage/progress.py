"""進捗の書き戻し・中止の依頼・終了時刻の上限 (--request-id / --deadline)。

ProgressReporter は run の各ループから 1 件ごとに呼ばれ、
- 20 秒以上の間隔で進捗 (arbitrage_research_requests.progress) を書き、同時に cancel_requested を読む
- 終了時刻の上限を過ぎていないかを見る
間隔の制御・速度の実測・完了予測は DB を使わない純粋な部分 (Throttle / SpeedMeter / forecast_eta)。
Supabase への書き込みは sink (関数) に分けてあり、--request-id が無ければ sink なしで何も書かない。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

from . import db
from .research import DEFAULT_RATES
from .scope import SCOPE_REACHABLE

logger = logging.getLogger("arbitrage")

MIN_INTERVAL = 20.0      # 進捗を書く間隔の下限 (秒)
SPEED_WINDOW = 600.0     # 速度はこの秒数ぶんの直近の実測から出す
MIN_SPEED_SPAN = 30.0    # 実測がこの秒数に満たないうちは既定の速度を使う

STAGE_STARTING, STAGE_API, STAGE_PAGES, STAGE_SAVING, STAGE_DONE = "starting", "api", "pages", "saving", "done"

STOP_DEADLINE = "deadline"
STOP_CANCELLED = "cancelled"
STOP_NOTES = {STOP_DEADLINE: "終了時刻の上限に達したため打ち切った",
              STOP_CANCELLED: "中止の依頼を受けて打ち切った"}
EXIT_CANCELLED = 3       # 中止の依頼で終わったときの終了コード

Clock = Callable[[], datetime]
# 進捗 1 件を書き、その時点の cancel_requested を返す
Sink = Callable[[dict], bool]


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_deadline(text: str) -> datetime:
    """--deadline の値。タイムゾーン無しはローカル時刻として扱う"""
    return datetime.fromisoformat(text).astimezone()


class Throttle:
    """前回から interval 秒以上たったときだけ通す"""

    def __init__(self, interval: float = MIN_INTERVAL):
        self.interval = interval
        self.last: datetime | None = None

    def due(self, now: datetime) -> bool:
        return self.last is None or (now - self.last).total_seconds() >= self.interval

    def mark(self, now: datetime) -> None:
        self.last = now


class SpeedMeter:
    """直近 window 秒の実測速度 (件/秒)"""

    def __init__(self, window: float = SPEED_WINDOW, min_span: float = MIN_SPEED_SPAN):
        self.window, self.min_span = window, min_span
        self.samples: deque[tuple[datetime, int]] = deque()

    def add(self, now: datetime, done: int) -> None:
        if self.samples and self.samples[-1][1] == done:
            return   # 進んでいない間は足さない (待ち時間は次の 1 件の所要時間に含まれる)
        self.samples.append((now, done))
        while len(self.samples) > 2 and (now - self.samples[1][0]).total_seconds() >= self.window:
            self.samples.popleft()

    def rate(self) -> float | None:
        """件/秒。実測が足りなければ None"""
        if len(self.samples) < 2:
            return None
        (t0, d0), (t1, d1) = self.samples[0], self.samples[-1]
        span = (t1 - t0).total_seconds()
        return (d1 - d0) / span if span >= self.min_span and d1 > d0 else None


def forecast_eta(now: datetime, progress: dict, rate: float | None, page_fraction: float,
                 deadline: datetime | None = None) -> datetime | None:
    """完了予測。rate はいまの段階の実測速度 (件/秒。None なら既定の速度)。

    API 段階では、このあとの確定判定の見込み (ここまでの候補数を全 JAN に引き伸ばした数 × 確認する割合 ×
    既定の 1 件あたり秒数) も足す。終了時刻の上限があれば、それより後にはならない。
    """
    stage = progress["stage"]
    if stage == STAGE_API:
        left = max(progress["jan_total"] - progress["jan_done"], 0)
        seconds = left / (rate or DEFAULT_RATES["jan_per_min"] / 60)
        done = progress["jan_done"]
        candidates = (progress["candidates"] * progress["jan_total"] / done if done
                      else progress["jan_total"] * DEFAULT_RATES["candidates_per_jan"])
        seconds += candidates * page_fraction * DEFAULT_RATES["seconds_per_check"]
    elif stage == STAGE_PAGES:
        left = max(progress["pages_total"] - progress["pages_done"], 0)
        seconds = left / (rate or 1 / DEFAULT_RATES["seconds_per_check"])
    elif stage == STAGE_SAVING:
        seconds = 0.0
    else:
        return None
    eta = now + timedelta(seconds=seconds)
    return min(eta, deadline) if deadline is not None else eta


class ProgressReporter:
    """run の進捗をまとめ、sink があれば間隔を空けて書く。打ち切るべきか (上限時刻・中止の依頼) も答える"""

    def __init__(self, run_name: str, sink: Sink | None = None, deadline: datetime | None = None,
                 clock: Clock = local_now, page_fraction: float = 1.0, interval: float = MIN_INTERVAL):
        self.sink, self.deadline, self.clock, self.page_fraction = sink, deadline, clock, page_fraction
        self.throttle = Throttle(interval)
        self.meter = SpeedMeter()
        self.cancelled = False
        self.state = {"stage": STAGE_STARTING, "run_name": run_name, "jan_total": 0, "jan_done": 0,
                      "candidates": 0, "pages_total": 0, "pages_done": 0, "results": 0, "note": None}

    def _done_count(self) -> int:
        return self.state["pages_done"] if self.state["stage"] == STAGE_PAGES else self.state["jan_done"]

    def snapshot(self, now: datetime) -> dict:
        """依頼行の progress に書く形"""
        eta = forecast_eta(now, self.state, self.meter.rate(), self.page_fraction, self.deadline)
        return {**self.state, "eta": eta.isoformat() if eta else None, "updated_at": now.isoformat()}

    def _write(self, now: datetime) -> None:
        self.throttle.mark(now)
        if self.sink is None:
            return
        try:
            self.cancelled = bool(self.sink(self.snapshot(now))) or self.cancelled
        except Exception as e:   # 進捗が書けなくてもリサーチ本体は止めない
            logger.warning("進捗を書けなかった (リサーチは続ける): %s", e)

    def set_stage(self, stage: str, **counts: int) -> None:
        """段階の切り替わり。間隔に関係なく書く"""
        self.state.update(counts, stage=stage)
        self.meter = SpeedMeter()
        now = self.clock()
        self.meter.add(now, self._done_count())
        self._write(now)

    def check(self, **counts: int) -> str | None:
        """各件の処理前に呼ぶ。進捗を更新し (書くのは間隔が空いたときだけ)、打ち切る理由があれば返す"""
        self.state.update(counts)
        now = self.clock()
        self.meter.add(now, self._done_count())
        if self.throttle.due(now):
            self._write(now)
        if self.cancelled:
            return STOP_CANCELLED
        if self.deadline is not None and now >= self.deadline:
            return STOP_DEADLINE
        return None

    def finish(self, note: str | None = None, **counts: int) -> None:
        """終了時。間隔に関係なく書く"""
        self.state.update(counts, stage=STAGE_DONE, note=note)
        self._write(self.clock())


def supabase_sink(client, request_id: int) -> Sink:
    """依頼行の progress を更新し、返ってきた行の cancel_requested を返す"""
    def write(progress: dict) -> bool:
        row = db.update_request(client, request_id, {"progress": progress})
        return bool(row and row.get("cancel_requested"))
    return write


def build_reporter(run_name: str, request_id: int | None, deadline: datetime | None, page_scope: str,
                   clock: Clock = local_now) -> ProgressReporter:
    """run 用の ProgressReporter。--request-id が無ければ何も書かない (上限時刻だけ見る)"""
    sink = None
    if request_id is not None:
        try:
            sink = supabase_sink(db.get_client(), request_id)
        except db.DbConfigError as e:
            logger.warning("%s。進捗の書き戻しと中止の確認はしない", e)
    fraction = DEFAULT_RATES["reachable_fraction"] if page_scope == SCOPE_REACHABLE else 1.0
    return ProgressReporter(run_name, sink, deadline, clock, fraction)
