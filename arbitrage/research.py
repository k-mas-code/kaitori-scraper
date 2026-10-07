"""リサーチ依頼 (arbitrage_research_requests.params) の検証・実行引数の組み立て・所要時間の予測。純粋関数のみ。

依頼は結果ページの「リサーチ」タブが作る。ブラウザから来る値なので、ここで検証したものだけを
python -m arbitrage.run の引数にする。予測式は結果ページの JS と同じ (CLAUDE.md 参照)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from .categories import GROUPS
from .config import DATE_RE
from .scope import DEFAULT_REACH_SLACK, MAX_REACH_SLACK, PAGE_SCOPES, SCOPE_REACHABLE

PARAM_KEYS = {"purchase_date", "min_buyback", "max_buyback", "min_profit", "page_scope", "reach_slack",
              "deadline", "categories"}
MIN_PROFIT_RANGE = (-5000, 100000)   # 下限は run.KEEP_ROW_MIN_PROFIT と同じ
HISTOGRAM_BUCKET = 1000

# 実測がまだ無いときの速度 (2026-10-04 の実測)
DEFAULT_RATES = {
    "jan_per_min": 27.6,           # API 段階で 1 分に処理できる JAN 数 (1 分 30 回の制限から)
    "candidates_per_jan": 1.1,     # JAN 1 件あたりの候補数
    "reachable_fraction": 0.28,    # 候補のうち「届きそう」な割合
    "seconds_per_check": 4.1,      # 確定判定 1 件の秒数
}


class ParamsError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchParams:
    purchase_date: date
    min_buyback: int | None = None      # None = 下限なし
    max_buyback: int | None = None      # None = 上限なし
    min_profit: int = 1                 # 既定は黒字のみ
    page_scope: str = SCOPE_REACHABLE
    reach_slack: float = DEFAULT_REACH_SLACK
    deadline: datetime | None = None    # 終了時刻の上限 (タイムゾーン付き)
    categories: tuple[str, ...] = ()    # 大分類 (categories.GROUPS) で対象を絞る。空 = すべて


@dataclass(frozen=True)
class Estimate:
    jan_count: float        # 対象 JAN 数 (帯の途中の境界は按分するので小数になりうる)
    api_minutes: float
    page_checks: float      # 確定判定の件数
    page_seconds: float

    @property
    def total_seconds(self) -> float:
        return self.api_minutes * 60 + self.page_seconds


def _int(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParamsError(f"{where}: 整数で指定する: {value!r}")
    return value


def _optional_amount(raw: dict, key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    value = _int(value, key)
    if value < 0:
        raise ParamsError(f"{key}: 0 以上で指定する: {value!r}")
    return value


def _purchase_date(raw: dict) -> date:
    value = raw.get("purchase_date")
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ParamsError(f"purchase_date: YYYY-MM-DD の文字列で指定する (必須): {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ParamsError(f"purchase_date: 存在しない日付: {value!r}") from e


def _deadline(raw: dict) -> datetime | None:
    value = raw.get("deadline")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ParamsError(f"deadline: ISO 8601 の日時 (文字列) か null で指定する: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise ParamsError(f"deadline: ISO 8601 の日時として読めない: {value!r}") from e
    if parsed.tzinfo is None:
        raise ParamsError(f"deadline: タイムゾーン付きで指定する (例: 2026-10-05T23:00:00+09:00): {value!r}")
    return parsed


def _categories(raw: dict) -> tuple[str, ...]:
    """大分類の配列。null / 空配列 = すべて。GROUPS に無い名前・重複・配列以外は拒否する"""
    value = raw.get("categories")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ParamsError(f"categories: 大分類名の配列か null で指定する: {value!r}")
    if len(value) > len(GROUPS):
        raise ParamsError(f"categories: 最大 {len(GROUPS)} 個: {len(value)} 個")
    unknown = [v for v in value if not isinstance(v, str) or v not in GROUPS]
    if unknown:
        raise ParamsError(f"categories: 不明な大分類: {', '.join(map(repr, unknown))} ({' / '.join(GROUPS)} のいずれか)")
    if len(set(value)) != len(value):
        raise ParamsError(f"categories: 重複がある: {value!r}")
    return tuple(value)


def parse_params(raw) -> ResearchParams:
    """依頼の params を検証する。不明なキー・範囲外の値は場所と理由を付けて拒否する"""
    if not isinstance(raw, dict):
        raise ParamsError(f"params: オブジェクトで指定する: {raw!r}")
    unknown = sorted(set(raw) - PARAM_KEYS)
    if unknown:
        raise ParamsError(f"params: 不明なキー: {', '.join(map(str, unknown))}")
    purchase_date = _purchase_date(raw)
    min_buyback, max_buyback = _optional_amount(raw, "min_buyback"), _optional_amount(raw, "max_buyback")
    if min_buyback is not None and max_buyback is not None and max_buyback < min_buyback:
        raise ParamsError(f"max_buyback: min_buyback ({min_buyback}) 以上で指定する: {max_buyback!r}")
    min_profit = _int(raw.get("min_profit", 1), "min_profit")
    low, high = MIN_PROFIT_RANGE
    if not low <= min_profit <= high:
        raise ParamsError(f"min_profit: {low} 以上 {high} 以下で指定する: {min_profit!r}")
    page_scope = raw.get("page_scope", SCOPE_REACHABLE)
    if page_scope not in PAGE_SCOPES:
        raise ParamsError(f"page_scope: {' / '.join(PAGE_SCOPES)} のどちらかで指定する: {page_scope!r}")
    reach_slack = raw.get("reach_slack", DEFAULT_REACH_SLACK)
    # 型 → 範囲の順に見る。巨大な整数 (jsonb の 1e400 は Python に 401 桁の int で届く) を float にしない
    if isinstance(reach_slack, bool) or not isinstance(reach_slack, (int, float)) \
            or (isinstance(reach_slack, float) and not math.isfinite(reach_slack)) \
            or not 0 <= reach_slack <= MAX_REACH_SLACK:
        raise ParamsError(f"reach_slack: 0 以上 {MAX_REACH_SLACK} 以下の数値で指定する: {reach_slack!r}")
    return ResearchParams(purchase_date, min_buyback, max_buyback, min_profit, page_scope,
                          float(reach_slack), _deadline(raw), _categories(raw))


def build_run_args(params: ResearchParams, request_id: int) -> list[str]:
    """python -m arbitrage.run に渡す引数 (シェルを通さないリスト)。値は検証済みの params から作り直したものだけ"""
    args = ["--date", params.purchase_date.isoformat(),
            "--request-id", str(int(request_id)),
            "--page-scope", params.page_scope,
            "--reach-slack", repr(float(params.reach_slack)),
            f"--min-profit={int(params.min_profit)}"]   # 負の値がオプションと誤読されないよう = でつなぐ
    if params.min_buyback is not None:
        args += ["--min-buyback", str(int(params.min_buyback))]
    if params.max_buyback is not None:
        args += ["--max-buyback", str(int(params.max_buyback))]
    if params.deadline is not None:
        args += ["--deadline", params.deadline.isoformat()]
    for group in params.categories:
        args += ["--category", group]
    return args


def buyback_histogram(prices: Iterable[int], bucket: int = HISTOGRAM_BUCKET) -> dict[str, int]:
    """買取価格の分布。キーは 買取価格 // bucket * bucket (文字列)、値はその帯の JAN 数"""
    counts: dict[int, int] = {}
    for price in prices:
        key = price // bucket * bucket
        counts[key] = counts.get(key, 0) + 1
    return {str(key): counts[key] for key in sorted(counts)}


def buyback_histogram_fields(buybacks, bucket: int = HISTOGRAM_BUCKET) -> dict:
    """arbitrage_worker_state.buyback_histogram の中身 (as_of を除く): 全体の counts と大分類ごとの by_group。

    buybacks は .price / .category を持つもの (Buyback)。category が GROUPS に無い (None など) ものは「その他」に数える。
    by_group はすべての大分類をキーに持つ (件数 0 なら {})。
    """
    items = list(buybacks)
    by_group = {group: buyback_histogram((b.price for b in items if _group_of(b) == group), bucket)
                for group in GROUPS}
    return {"bucket": bucket, "counts": buyback_histogram((b.price for b in items), bucket), "by_group": by_group}


def _group_of(buyback) -> str:
    return buyback.category if buyback.category in GROUPS else GROUPS[-1]


def clean_rates(rates) -> dict[str, float]:
    """rates の欠けている値・正の数でない値を既定値で埋める"""
    source = rates if isinstance(rates, dict) else {}
    cleaned = {}
    for key, default in DEFAULT_RATES.items():
        value = source.get(key)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
        cleaned[key] = float(value) if ok else default
    return cleaned


def _histogram_counts(histogram, categories) -> list[dict]:
    """数える帯の表。categories があれば by_group のその分類だけ (by_group が無い古い分布なら空 = 0 件)、無ければ counts"""
    if not categories:
        counts = histogram.get("counts")
        return [counts] if isinstance(counts, dict) else []
    by_group = histogram.get("by_group")
    if not isinstance(by_group, dict):
        return []
    return [by_group[g] for g in categories if isinstance(by_group.get(g), dict)]


def count_targets(histogram, min_buyback: int | None, max_buyback: int | None, categories=()) -> float:
    """分布から、買取価格が min_buyback〜max_buyback (両端を含む) の JAN 数を数える。

    categories (大分類名の列) があれば by_group のそれらの合計、無ければ counts (全体) で数える。
    帯 [k, k + bucket) の途中に境界があるときは、帯の中で価格が一様だとみなして按分する。
    """
    if not isinstance(histogram, dict):
        return 0.0
    bucket = histogram.get("bucket") or HISTOGRAM_BUCKET
    low = 0 if min_buyback is None else min_buyback
    high = math.inf if max_buyback is None else max_buyback + 1   # 整数の価格なので「以下」= 「+1 未満」
    total = 0.0
    for counts in _histogram_counts(histogram, categories):
        for key, count in counts.items():
            start = int(key)
            overlap = min(start + bucket, high) - max(start, low)
            if overlap > 0:
                total += count * overlap / bucket
    return total


def estimate(params: ResearchParams, histogram, rates) -> Estimate:
    """所要時間の予測。histogram は arbitrage_worker_state.buyback_histogram、rates は同 rates (欠けは既定値)"""
    r = clean_rates(rates)
    jan_count = count_targets(histogram, params.min_buyback, params.max_buyback, params.categories)
    fraction = r["reachable_fraction"] if params.page_scope == SCOPE_REACHABLE else 1.0
    page_checks = jan_count * r["candidates_per_jan"] * fraction
    return Estimate(jan_count, jan_count / r["jan_per_min"], page_checks, page_checks * r["seconds_per_check"])


MIN_JANS_FOR_RATES = 200    # これ未満の run は誤差が大きいので rates を更新しない
MIN_CHECKS_FOR_RATES = 20


def updated_rates(rates, stats) -> dict[str, float] | None:
    """完了した run の実測 (meta.json の stats) で rates を更新する。更新しないときは None。

    stats: {"jan_processed", "api_seconds", "jan_total", "candidates", "reachable",
            "pages_processed", "pages_seconds"} (jan_processed / pages_processed は今回の実行で処理した数)
    """
    if not isinstance(stats, dict):
        return None
    processed = stats.get("jan_processed") or 0
    if processed < MIN_JANS_FOR_RATES:
        return None
    new = clean_rates(rates)
    if (stats.get("api_seconds") or 0) > 0:
        new["jan_per_min"] = round(processed / (stats["api_seconds"] / 60), 2)
    jan_total, candidates = stats.get("jan_total") or 0, stats.get("candidates") or 0
    if jan_total >= MIN_JANS_FOR_RATES and candidates > 0:
        new["candidates_per_jan"] = round(candidates / jan_total, 3)
        if (stats.get("reachable") or 0) > 0:
            new["reachable_fraction"] = round(stats["reachable"] / candidates, 3)
    checks = stats.get("pages_processed") or 0
    if checks >= MIN_CHECKS_FOR_RATES and (stats.get("pages_seconds") or 0) > 0:
        new["seconds_per_check"] = round(stats["pages_seconds"] / checks, 2)
    return clean_rates(new)
