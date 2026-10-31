"""取得結果 (data/yahoo_pp/*.json) から arbitrage_store_lists の 1 行 (スナップショット) を組み立てる。純粋関数のみ。

行の形は db/arbitrage_store_lists.sql と docs/arbitrage-stores.js の契約:
  fetched_on / period_start / period_end / dates / summary / stores / source_sha
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from .build_xlsx import CAT_NAME, CAT_ORDER

MIN_STORES = 500    # 新しいリストで読める店 (除外後) がこれ未満なら差し替えない


def _reg(inv: dict) -> str:
    reg = inv.get("reg", "-")
    return "" if reg in ("-", None) else str(reg)


def store_row(sid: str, cat: str, pat: str, dates: list[str], inputs: dict) -> dict | None:
    """1 店ぶん。掲載日が 1 日も無い店は None"""
    on = [i for i, ch in enumerate(pat) if ch != "-"]
    if not on:
        return None
    inv = inputs["invoice"].get(sid) or {}
    pts = inputs["points"].get(sid)
    reviews = inputs["reviews"].get(sid)
    return {
        "id": sid, "name": inputs["names"].get(sid, sid), "genre": CAT_NAME.get(cat, cat), "cat": cat,
        "gold": bool(inputs["gold"].get(sid)), "inv": inv.get("f", "?"), "reg": _reg(inv),
        "max_upsell": float(pts["max"]) if pts else None,
        "unknown": pts.get("unknown") if pts else None,
        "n": pts.get("n") if pts else None,
        "n_up": pts.get("n_up") if pts else None,
        "reviews": list(reviews) if reviews else None,
        "days": len(on), "d9": pat.count("9"), "d4": pat.count("4"),
        "first": dates[on[0]], "last": dates[on[-1]], "pat": pat,
    }


def summarize(stores: list[dict]) -> dict:
    qualified = [s for s in stores if s["inv"] != "N"]
    by_genre: dict[str, dict] = {}
    for s in sorted(stores, key=lambda s: CAT_ORDER.get(s["cat"], 99)):
        g = by_genre.setdefault(s["genre"], {"qualified": 0, "gold": 0, "excluded": 0})
        if s["inv"] == "N":
            g["excluded"] += 1
        else:
            g["qualified"] += 1
            g["gold"] += int(s["gold"])
    return {
        "total": len(stores), "qualified": len(qualified), "excluded": len(stores) - len(qualified),
        "gold": sum(1 for s in qualified if s["gold"]),
        "surveyed": sum(1 for s in qualified if s["max_upsell"] is not None),
        "by_genre": by_genre,
    }


def build_snapshot(inputs: dict, fetched_on: dt.date, source_sha: str | None = None) -> dict:
    """inputs は build_xlsx.load_inputs() の形。dates / sched が無ければ ValueError"""
    dates: list[str] = list(inputs.get("dates") or [])
    sched: dict = inputs.get("sched") or {}
    if not dates or not sched:
        raise ValueError("dates.json / sched.json が無い (先に bsplus_fetch を実行する)")
    stores = []
    for sid, (cat, pat) in sched.items():
        if len(pat) != len(dates):
            raise ValueError(f"{sid}: 掲載パターンの長さ {len(pat)} が日付数 {len(dates)} と合わない")
        row = store_row(sid, cat, pat, dates, inputs)
        if row is not None:
            stores.append(row)
    stores.sort(key=lambda s: (-(s["max_upsell"] if s["max_upsell"] is not None else -1),
                               CAT_ORDER.get(s["cat"], 99), s["id"]))
    return {
        "fetched_on": fetched_on.isoformat(),
        "period_start": dates[0], "period_end": dates[-1],
        "dates": dates, "summary": summarize(stores), "stores": stores,
        "source_sha": source_sha,
    }


def file_sha(path: Path) -> str | None:
    """xlsx の sha256 先頭 16 桁 (arbitrage.stores.load_stores の source_file と同じ)。無ければ None"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def describe(snapshot: dict) -> str:
    """--print-snapshot 用の要約 (stores 本体は出さない)"""
    s = snapshot["summary"]
    lines = [f"取得日 {snapshot['fetched_on']}  掲載期間 {snapshot['period_start']}〜{snapshot['period_end']} "
             f"({len(snapshot['dates'])} 日)  sha {snapshot['source_sha']}",
             f"全 {s['total']} 店 / 適格 {s['qualified']} / 非適格 {s['excluded']} / 優良 {s['gold']} / 上乗せ調査済み {s['surveyed']}"]
    for genre, g in s["by_genre"].items():
        lines.append(f"  {genre}: 適格 {g['qualified']} 優良 {g['gold']} 非適格 {g['excluded']}")
    top = [x for x in snapshot["stores"] if x["max_upsell"]][:5]
    if top:
        lines.append("上乗せ率 上位: " + ", ".join(f"{x['id']} +{x['max_upsell']:g}%" for x in top))
    return "\n".join(lines)
