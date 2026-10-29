"""HANDOFF §3 の各 URL を 1 件ずつ取得し、WSL から直接取れるか・抽出できるかを確かめる

実行: .venv/bin/python -m tools.yahoo_pp.probe [--no-cache]
"""

from __future__ import annotations

import json
import re
import sys

from .common import Fetcher

KOJIMA = "y-kojima"
ANDO = "as-y"


def show(title: str, status: int, url: str, text: str) -> None:
    print(f"\n=== {title}\n  status={status} len={len(text)} url={url}")


def find_next_data(text: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    return json.loads(m.group(1)) if m else None


def main() -> int:
    f = Fetcher()
    nocache = "--no-cache" in sys.argv

    # 3-1 A: 一覧ページ → eventId
    st, u, t = f.fetch("https://shopping.yahoo.co.jp/promotion/campaign/bsplus/list/", use_cache=not nocache)
    show("3-1A bsplus/list", st, u, t)
    eids = sorted(set(re.findall(r"eid=(\d+)", t)))
    print("  eid (eid= パラメータ):", len(eids), eids[:6], "...")
    evt = sorted(set(re.findall(r"eventId[\"']?\s*[:=]\s*[\"']?(\d+)", t)))
    print("  eventId (JS/JSON 内):", len(evt), evt[:6])
    sel = re.findall(r"<option[^>]*value=\"?(\d+)\"?[^>]*>([^<]*)</option>", t)
    print("  <option>:", len(sel), sel[:6])
    dates = re.findall(r"(\d{1,2}月\d{1,2}日)[^<]{0,20}", t)
    print("  日付表記:", len(dates), dates[:5])
    cnt = re.findall(r"([\d,]+)\s*(?:店|件|ストア)", t)
    print("  件数らしき数:", cnt[:10])

    # 3-1 B: acstores API (eventId が取れたら最初の 1 件、カテゴリ 2505)
    if eids:
        st, u, t2 = f.fetch("https://shopping.yahoo.co.jp/promotion/campaign/buybuy/api/acstores/",
                            method="POST", json_body={"eventId": eids[0], "campaignType": "point", "category": "2505"},
                            use_cache=not nocache)
        show(f"3-1B acstores eid={eids[0]} cat=2505", st, u, t2)
        ids = re.findall(r"store\.shopping\.yahoo\.co\.jp/([A-Za-z0-9_-]+)/", t2)
        print("  店舗ID:", len(set(ids)), sorted(set(ids))[:8])
        print("  先頭:", t2[:300].replace("\n", " "))

    # 3-3 適格請求書
    st, u, t = f.fetch(f"https://store.shopping.yahoo.co.jp/{KOJIMA}/info.html", use_cache=not nocache)
    show("3-3 info.html (y-kojima)", st, u, t)
    m = re.search(r'"bcpf":\{"registrationNo":"?([^"},]*)"?\}', t)
    print("  registrationNo:", m.group(1) if m else None, "(期待 T2060001001667)")
    print("  備考:", re.findall(r"当店は、適格請求書発行事業者(?:です|ではありません)。", t)[:1])

    # 3-4 ストア評価
    st, u, t = f.fetch(f"https://shopping.yahoo.co.jp/store_rating/{KOJIMA}/store/review/", use_cache=not nocache)
    show("3-4 store_rating (y-kojima)", st, u, t)
    stars = re.findall(r"星([1-5])つ\s*([\d,]+)件", t)
    print("  星別:", stars, "(期待 163,620/31,495/9,111/2,332/5,918 ※増えているはず)")

    # 3-2 / 3-5 店内検索 → __NEXT_DATA__ → 商品
    st, u, t = f.fetch(f"https://store.shopping.yahoo.co.jp/{ANDO}/search.html", use_cache=not nocache)
    show("3-5① search.html (as-y)", st, u, t)
    nd = find_next_data(t)
    items: list[tuple[str, float]] = []
    if nd:
        def walk(o):
            if isinstance(o, dict):
                url = o.get("url")
                pt = o.get("point")
                if isinstance(url, str) and f"/{ANDO}/" in url and isinstance(pt, dict) and isinstance(pt.get("rate"), (int, float)):
                    items.append((url, pt["rate"]))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(nd)
    print("  __NEXT_DATA__:", bool(nd), "商品:", len(items), items[:3])

    # 3-2 / 3-5②: 先頭商品ページ
    if items:
        purl = items[0][0]
        if purl.startswith("/"):
            purl = "https://store.shopping.yahoo.co.jp" + purl
        st, u, t = f.fetch(purl, use_cache=not nocache)
        show("3-5② 商品ページ", st, u, t)
        print("  excellentStoreGold_logo:", "excellentStoreGold_logo" in t)
        nd = find_next_data(t)
        pt = (nd or {}).get("props", {}).get("pageProps", {}).get("point") if nd else None
        if pt:
            cur = [(p.get("title"), p.get("ratio")) for c in pt.get("currentCampaignList", []) for p in c.get("partsCampaignList", [])]
            cond = [(p.get("title"), p.get("ratio")) for c in pt.get("conditionalCampaignList", []) for p in c.get("partsCampaignList", [])]
            print("  totalPointRatio:", pt.get("totalPointRatio"), "current:", cur, "conditional:", cond)
            print("  上乗せ =", pt.get("totalPointRatio"), "-", sum(r for _, r in cur if r), "=",
                  (pt.get("totalPointRatio") or 0) - sum(r for _, r in cur if r))
        else:
            print("  pageProps.point なし。keys:", list((nd or {}).get("props", {}).get("pageProps", {}).keys())[:20])
    print(f"\nリクエスト数: {f.requests_made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
