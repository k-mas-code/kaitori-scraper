"""sched.json の店ごとに、適格請求書・ストア評価・優良ストア・ポイント上乗せを取得する (HANDOFF §3-2〜3-5)

実行: .venv/bin/python -m tools.yahoo_pp.enrich [--stage invoice|rating|points|all] [--limit N]
出力 (data/yahoo_pp/):
  invoice.json  {storeId: {"f": "Y"/"N"/"?", "reg": "T..."/"-"}}      全店
  reviews.json  {storeId: [星5, 星4, 星3, 星2, 星1]}                   適格店
  gold.json     {storeId: 1/0}                                       適格店 (先頭商品ページのバッジ)
  points.json   {storeId: {"max": 率, "n": 調査商品数, "n_up": 上乗せあり, "unknown": 判定不可,
                           "detail": {"率": 商品数}, "checked": [[一覧の率, 上乗せ率], ...]}}
途中結果は 20 店ごとに書く。HTTP 489 で止めたら再実行で続きから (取得済みは飛ばす)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path

from .common import ROOT, Fetcher, RateLimited

OUT = ROOT / "data" / "yahoo_pp"
BASE_RATIO = 4.0            # PayPay残高 0.5 + ストアポイント 1 + LINE 連携 2.5
MAX_PRODUCT_PAGES = 3       # 上乗せ確認に開く商品ページの上限 (率の高い順・率ごとに 1 つ)
log = logging.getLogger(__name__)


def load(name: str) -> dict:
    p = OUT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save(name: str, data: dict) -> None:
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")


def next_data(text: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except json.JSONDecodeError:
        return None


# ---- 3-3 適格請求書 ----
def fetch_invoice(f: Fetcher, sid: str) -> dict:
    st, _, t = f.fetch(f"https://store.shopping.yahoo.co.jp/{sid}/info.html")
    if st != 200:
        return {"f": "?", "reg": "-", "status": st}
    m = re.search(r'"bcpf":\{"registrationNo":"?([^"},]*)"?\}', t)
    reg = m.group(1) if m else ""
    if reg and reg != "null":
        return {"f": "Y", "reg": reg}
    if "当店は、適格請求書発行事業者です。" in t:
        return {"f": "Y", "reg": "-"}
    if "当店は、適格請求書発行事業者ではありません。" in t or m:
        return {"f": "N", "reg": "-"}
    return {"f": "?", "reg": "-"}


# ---- 3-4 ストア評価 ----
def fetch_reviews(f: Fetcher, sid: str) -> list[int] | None:
    st, _, t = f.fetch(f"https://shopping.yahoo.co.jp/store_rating/{sid}/store/review/")
    if st != 200:
        return None
    found = {int(s): int(n.replace(",", "")) for s, n in re.findall(r"星([1-5])つ（([\d,]+)件）", t)}
    if len(found) != 5:
        return None
    return [found[5], found[4], found[3], found[2], found[1]]


# ---- 3-2 / 3-5 店内検索 → 商品ページ ----
def search_products(f: Fetcher, sid: str) -> list[tuple[str, float]] | None:
    st, _, t = f.fetch(f"https://store.shopping.yahoo.co.jp/{sid}/search.html")
    if st != 200:
        return None
    nd = next_data(t)
    if not nd:
        return None
    items: list[tuple[str, float]] = []
    seen: set[str] = set()

    def walk(o: object) -> None:
        if isinstance(o, dict):
            url, pt = o.get("url"), o.get("point")
            if (isinstance(url, str) and f"/{sid}/" in url and isinstance(pt, dict)
                    and isinstance(pt.get("rate"), (int, float))):
                u = url.split("?")[0]
                if u not in seen:
                    seen.add(u)
                    items.append((u, float(pt["rate"])))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(nd)
    return items


def product_page(f: Fetcher, url: str) -> tuple[bool, float | None]:
    """(優良ストアか, 上乗せ率 = totalPointRatio − 標準で付く各行の率の合計)"""
    if url.startswith("/"):
        url = "https://store.shopping.yahoo.co.jp" + url
    st, _, t = f.fetch(url)
    if st != 200:
        return False, None
    gold = "excellentStoreGold_logo" in t
    nd = next_data(t)
    pt = ((nd or {}).get("props") or {}).get("pageProps", {}).get("point") if nd else None
    if not pt or not isinstance(pt.get("totalPointRatio"), (int, float)):
        return gold, None
    base = sum(float(p.get("ratio") or 0) for c in pt.get("currentCampaignList", [])
               for p in c.get("partsCampaignList", []))
    return gold, round(float(pt["totalPointRatio"]) - base, 2)


def survey_points(f: Fetcher, sid: str) -> tuple[int | None, dict | None]:
    items = search_products(f, sid)
    if not items:
        return None, None
    rates = sorted({r for _, r in items}, reverse=True)
    verified: dict[float, float] = {}       # 一覧の率 → 上乗せ率
    gold: int | None = None
    checked: list[list[float]] = []
    for rate in rates[:MAX_PRODUCT_PAGES]:
        url = next(u for u, r in items if r == rate)
        g, up = product_page(f, url)
        if gold is None:
            gold = int(g)
        if up is not None:
            verified[rate] = up
            checked.append([rate, up])
    ups = list(verified.values())
    max_up = max(ups) if ups else 0.0
    detail: dict[str, int] = {}
    unknown = 0
    for _, r in items:
        if r in verified:
            key = f"+{verified[r]:g}%"
            detail[key] = detail.get(key, 0) + 1
        elif r - BASE_RATIO > max_up:
            # 未確認で、上乗せが max を超えうる商品 (一覧の率 − 基本 4 が上限)
            unknown += 1
    return gold, {
        "max": max_up,
        "n": len(items),
        "n_up": sum(c for k, c in detail.items() if k not in ("+0%",)),
        "unknown": unknown,
        "detail": detail,
        "checked": checked,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["invoice", "rating", "points", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0, help="各段階で処理する店数の上限 (試行用)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sched = load("sched.json")
    if not sched:
        log.error("sched.json が無い。先に bsplus_fetch を実行する")
        return 1
    ids = sorted(sched)
    f = Fetcher()
    invoice, reviews, gold, points = load("invoice.json"), load("reviews.json"), load("gold.json"), load("points.json")
    today = dt.date.today()
    if args.stage in ("points", "all") and (today.weekday() == 6 or today.day % 10 == 5):
        log.warning("今日 (%s) は日曜または 5 のつく日。上乗せの判定に日曜特典が混ざりうる (HANDOFF §3-5)", today)
    try:
        if args.stage in ("invoice", "all"):
            todo = [s for s in ids if s not in invoice][: args.limit or None]
            log.info("適格請求書: 未取得 %d / %d 店 (約 %.0f 分)", len(todo), len(ids), len(todo) * f.interval / 60)
            for i, sid in enumerate(todo, 1):
                invoice[sid] = fetch_invoice(f, sid)
                if i % 20 == 0 or i == len(todo):
                    save("invoice.json", invoice)
                    log.info("適格請求書 %d/%d (Y=%d N=%d ?=%d)", i, len(todo),
                             *[sum(1 for v in invoice.values() if v["f"] == k) for k in "YN?"])
        qualified = [s for s in ids if invoice.get(s, {}).get("f") == "Y"]
        if args.stage in ("points", "all"):
            todo = [s for s in qualified if s not in points][: args.limit or None]
            log.info("上乗せ・優良: 未取得 %d / %d 店 (最大約 %.0f 分)", len(todo), len(qualified),
                     len(todo) * (1 + MAX_PRODUCT_PAGES) * f.interval / 60)
            for i, sid in enumerate(todo, 1):
                g, p = survey_points(f, sid)
                points[sid] = p or {"max": 0.0, "n": 0, "n_up": 0, "unknown": 0, "detail": {}, "checked": [], "error": "商品が取れない"}
                if g is not None:
                    gold[sid] = g
                if i % 20 == 0 or i == len(todo):
                    save("points.json", points)
                    save("gold.json", gold)
                    log.info("上乗せ・優良 %d/%d (上乗せあり %d, 優良 %d, リクエスト %d)", i, len(todo),
                             sum(1 for v in points.values() if v["max"] > 0), sum(gold.values()), f.requests_made)
        if args.stage in ("rating", "all"):
            todo = [s for s in qualified if s not in reviews][: args.limit or None]
            log.info("ストア評価: 未取得 %d / %d 店 (約 %.0f 分)", len(todo), len(qualified), len(todo) * f.interval / 60)
            for i, sid in enumerate(todo, 1):
                r = fetch_reviews(f, sid)
                reviews[sid] = r if r is not None else [0, 0, 0, 0, 0]
                if i % 20 == 0 or i == len(todo):
                    save("reviews.json", reviews)
                    log.info("ストア評価 %d/%d", i, len(todo))
    except RateLimited as e:
        for name, d in (("invoice.json", invoice), ("points.json", points), ("gold.json", gold), ("reviews.json", reviews)):
            save(name, d)
        log.error("HTTP 489 (アクセス制限): %s。時間をおいて再実行すると続きから", e)
        return 1
    log.info("完了 (リクエスト %d)", f.requests_made)
    return 0


if __name__ == "__main__":
    sys.exit(main())
