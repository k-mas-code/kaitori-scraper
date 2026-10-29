"""ボーナスストアPlus の参加ストア一覧を日付 × 枠 × ジャンルで取得し sched.json を作る (HANDOFF §3-1)

実行: .venv/bin/python -m tools.yahoo_pp.bsplus_fetch
出力 (data/yahoo_pp/):
  dates.json   取得した日付 (ISO) の並び
  sched.json   {storeId: [カテゴリコード, "9-4..."]}  各文字 = 9 / 4 / - (dates.json の順)
  names.json   {storeId: 店名}
  counts.json  {日付: {eventId: {カテゴリ: [取得件数, 表示件数]}}}  照合結果
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import sys

from .common import ROOT, Fetcher, RateLimited

OUT = ROOT / "data" / "yahoo_pp"
LIST_URL = "https://shopping.yahoo.co.jp/promotion/campaign/bsplus/list/"
EVENT_URL = "https://shopping.yahoo.co.jp/promotion/campaign/bsplus/list/event/?archive=on&eid={a},{b}"
API_URL = "https://shopping.yahoo.co.jp/promotion/campaign/buybuy/api/acstores/"
CATEGORIES = {"2502": "スマホ、タブレット、パソコン", "2504": "テレビ、オーディオ、カメラ",
              "2505": "家電", "2511": "ゲーム、おもちゃ", "2512": "スポーツ"}
log = logging.getLogger(__name__)


def infer_year(month: int, today: dt.date) -> int:
    """一覧ページに年が無いので、今日から前後半年の範囲に収まる年を選ぶ"""
    if month - today.month > 6:
        return today.year - 1
    if today.month - month > 6:
        return today.year + 1
    return today.year


def list_dates(f: Fetcher, today: dt.date) -> list[tuple[dt.date, str, str]]:
    st, _, t = f.fetch(LIST_URL, use_cache=False)
    if st != 200:
        raise RuntimeError(f"一覧ページ status={st}")
    pairs = re.findall(r'eid=(\d+),(\d+)"[^>]*>\s*<p class="elText">(\d+)月(\d+)日開催分', t)
    out = []
    for a, b, m, d in pairs:
        out.append((dt.date(infer_year(int(m), today), int(m), int(d)), a, b))
    out.sort()
    if not out:
        raise RuntimeError("一覧ページから日付リンクが取れない")
    return out


def event_sections(f: Fetcher, a: str, b: str) -> dict[str, tuple[str, dict[str, int]]]:
    """日付ページ → {eventId: (枠 '9'/'4', {カテゴリ: 表示件数})}"""
    st, _, t = f.fetch(EVENT_URL.format(a=a, b=b))
    if st != 200:
        raise RuntimeError(f"日付ページ eid={a},{b} status={st}")
    out: dict[str, tuple[str, dict[str, int]]] = {}
    titles = re.findall(r'elDecoration">\s*\+(\d)%対象ストア', t)
    selects = list(re.finditer(r"'eventId': (\d+)[^<]*>(.*?)</select>", t, re.S))
    # 見出しの順と select の順は同じ (各セクションに select は 1 つ。SP 用の複製は </select> 後に出ない)
    seen: list[str] = []
    for m in selects:
        eid = m.group(1)
        if eid in seen:
            continue
        seen.append(eid)
        opts = {c: int(n) for c, n in re.findall(r'value="(\d+)"[^>]*>[^（]+（(\d+)）', m.group(2))}
        out[eid] = ("", opts)
    if len(titles) != len(out):
        raise RuntimeError(f"日付ページ eid={a},{b}: 見出し {titles} と select {list(out)} の数が合わない")
    for eid, tier in zip(out, titles):
        out[eid] = (tier, out[eid][1])
    return out


def api_stores(f: Fetcher, eid: str, cat: str) -> dict[str, str]:
    st, _, t = f.fetch(API_URL, method="POST",
                       json_body={"eventId": eid, "campaignType": "point", "category": cat})
    if st not in (200, 201):
        raise RuntimeError(f"acstores eid={eid} cat={cat} status={st}")
    frag = json.loads(t)["data"]["html"]
    stores = {}
    for sid, name in re.findall(r'href="https://store\.shopping\.yahoo\.co\.jp/([A-Za-z0-9_-]+)/"[^>]*>([^<]*)</a>', frag):
        stores[sid] = html.unescape(name).strip()
    return stores


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = dt.date.today()
    f = Fetcher()
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        dates = list_dates(f, today)
        log.info("日付 %d 件: %s 〜 %s", len(dates), dates[0][0], dates[-1][0])
        sched: dict[str, list] = {}
        names: dict[str, str] = {}
        cats: dict[str, set[str]] = {}
        counts: dict[str, dict] = {}
        mismatch = 0
        for i, (day, a, b) in enumerate(dates):
            sections = event_sections(f, a, b)
            counts[day.isoformat()] = {}
            for eid, (tier, shown) in sections.items():
                counts[day.isoformat()][eid] = {}
                for cat in CATEGORIES:
                    stores = api_stores(f, eid, cat)
                    exp = shown.get(cat)
                    counts[day.isoformat()][eid][cat] = [len(stores), exp]
                    if exp is not None and exp != len(stores):
                        mismatch += 1
                        log.warning("%s +%s%% %s: 取得 %d 件 / 表示 %d 件", day, tier, CATEGORIES[cat], len(stores), exp)
                    for sid, name in stores.items():
                        names.setdefault(sid, name)
                        cats.setdefault(sid, set()).add(cat)
                        pat = sched.setdefault(sid, [cat, ["-"] * len(dates)])[1]
                        if pat[i] != "-" and pat[i] != tier:
                            log.warning("%s %s: 同じ日に +%s と +%s", day, sid, pat[i], tier)
                        pat[i] = tier
            log.info("%s 完了 (%d/%d, 店舗 %d, リクエスト %d)", day, i + 1, len(dates), len(sched), f.requests_made)
    except RateLimited as e:
        log.error("HTTP 489 (アクセス制限): %s。時間をおいて再実行すると続きから (キャッシュ済みは飛ばす)", e)
        return 1
    multi = {s: sorted(c) for s, c in cats.items() if len(c) > 1}
    if multi:
        log.warning("複数ジャンルに出た店 %d 件 (最初に出たジャンルを採用): %s", len(multi), list(multi.items())[:5])
    (OUT / "dates.json").write_text(json.dumps([d.isoformat() for d, _, _ in dates]), encoding="utf-8")
    (OUT / "sched.json").write_text(json.dumps({s: [c, "".join(p)] for s, (c, p) in sched.items()},
                                               ensure_ascii=False, indent=0), encoding="utf-8")
    (OUT / "names.json").write_text(json.dumps(names, ensure_ascii=False, indent=0), encoding="utf-8")
    (OUT / "counts.json").write_text(json.dumps(counts, indent=1), encoding="utf-8")
    by_cat = {c: sum(1 for v in sched.values() if v[0] == c) for c in CATEGORIES}
    log.info("店舗 %d 件 (%s)。件数不一致 %d。出力: %s", len(sched),
             ", ".join(f"{CATEGORIES[c]} {n}" for c, n in by_cat.items()), mismatch, OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
