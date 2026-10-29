"""取得結果から店舗リスト data/yahoo_pp_stores_5genres.xlsx を生成する (HANDOFF §6)

実行: .venv/bin/python -m tools.yahoo_pp.build_xlsx [--out PATH] [--plus2 M/D,M/D] [--plus3 M/D,...]
入力 (data/yahoo_pp/): dates.json / sched.json / names.json / invoice.json / gold.json / reviews.json / points.json
  invoice が無い店は適格扱い (除外しない)。gold / reviews / points が無い店は空欄。

arbitrage/stores.py が読む構造 (変えるときは stores.py も直す):
  全ジャンル            1 行目=全体加算/休みの印, 2 行目=ヘッダー (ジャンル/ストア名/ストアID/優良ストア/初回掲載日/最終掲載日 + 「M/D\\n曜」の日付列), 値 9 / 4 / 空欄
  ポイント上乗せ調査    4 行目=ヘッダー (ストアID / 最大上乗せ率 (小数) / 判定できなかった商品数)
  除外リスト（非適格）  2 行目=ヘッダー (ストアID)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import logging
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .common import ROOT

IN = Path(os.environ.get("YAHOO_PP_DIR") or ROOT / "data" / "yahoo_pp")
DEFAULT_OUT = ROOT / "data" / "yahoo_pp_stores_5genres.xlsx"
CATS = [("2502", "スマホ、タブレット、パソコン"), ("2504", "テレビ、オーディオ、カメラ"),
        ("2505", "家電"), ("2511", "ゲーム、おもちゃ"), ("2512", "スポーツ")]
CAT_NAME = dict(CATS)
CAT_ORDER = {c: i for i, (c, _) in enumerate(CATS)}
SHEET_NAMES = {"2502": "スマホ・タブレット・PC", "2504": "テレビ・オーディオ・カメラ",
               "2505": "家電", "2511": "ゲーム・おもちゃ", "2512": "スポーツ"}
WD = "月火水木金土日"
FONT = "メイリオ"
log = logging.getLogger(__name__)

f_base = Font(name=FONT, size=10)
f_head = Font(name=FONT, size=10, bold=True, color="FFFFFF")
f_head_s = Font(name=FONT, size=9, bold=True, color="FFFFFF")
f_mark = Font(name=FONT, size=8, bold=True, color="C00000")
f_off = Font(name=FONT, size=8, bold=True, color="595959")
f_title = Font(name=FONT, size=14, bold=True)
f_note = Font(name=FONT, size=9, color="555555")
f_link = Font(name=FONT, size=10, color="0563C1", underline="single")
f_9 = Font(name=FONT, size=9, bold=True, color="FFFFFF")
f_4 = Font(name=FONT, size=9, color="7F3F00")
fill_head = PatternFill("solid", start_color="1F3864")
fill_head_past = PatternFill("solid", start_color="7F8CA3")
fill_head_sun = PatternFill("solid", start_color="8E2A2A")
fill_band = PatternFill("solid", start_color="F2F5FA")
fill_9 = PatternFill("solid", start_color="E26B0A")
fill_4 = PatternFill("solid", start_color="FCD5B4")
fill_mark = PatternFill("solid", start_color="FFF2CC")
fill_off = PatternFill("solid", start_color="D9D9D9")
border = Border(bottom=Side(style="thin", color="D0D7E2"))
center = Alignment(horizontal="center", vertical="center")
head_align = Alignment(horizontal="center", vertical="center", wrap_text=True)


def load(name: str) -> dict:
    p = IN / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def parse_days(spec: str | None, dates: list[dt.date]) -> set[dt.date]:
    if not spec:
        return set()
    out = set()
    for tok in spec.split(","):
        m, d = (int(x) for x in tok.strip().split("/"))
        hit = [x for x in dates if (x.month, x.day) == (m, d)]
        if not hit:
            raise SystemExit(f"--plus 指定 {tok} は掲載期間に無い")
        out.add(hit[0])
    return out


def pattern_type(pat: str, off: set[int]) -> str:
    on = [k for k, ch in enumerate(pat) if ch != "-"]
    span = range(on[0], on[-1] + 1)
    g_in = [k for k in span if k in off]
    n_in = [k for k in span if k not in off]
    if len(g_in) < 3 or len(n_in) < 3:
        return "判定不可"
    rg = sum(pat[k] != "-" for k in g_in) / len(g_in)
    if rg == 0:
        return "休み日型"
    if rg >= 0.8:
        return "毎日型"
    return "不定期"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--plus2", help="全体加算 +2% の日 (M/D をカンマ区切り)")
    ap.add_argument("--plus3", help="全体加算 +3% (優良ストア) の日")
    ap.add_argument("--today", help="作成日 (YYYY-MM-DD。既定は今日)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    dates = [dt.date.fromisoformat(s) for s in load("dates.json")]
    sched = load("sched.json")
    names, inv, gold, rev, pts = load("names.json"), load("invoice.json"), load("gold.json"), load("reviews.json"), load("points.json")
    if not dates or not sched:
        log.error("dates.json / sched.json が無い。先に bsplus_fetch を実行する")
        return 1
    n_days = len(dates)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    plus2, plus3 = parse_days(args.plus2, dates), parse_days(args.plus3, dates)
    span = f"{dates[0].month}/{dates[0].day}〜{dates[-1].month}/{dates[-1].day}"

    # 休み日: 最頻パターンの不参加日 (参加店が十分多いときだけ)
    top_pat, top_n = Counter(p for _, p in sched.values()).most_common(1)[0]
    off = {k for k, ch in enumerate(top_pat) if ch == "-"} if top_n >= 20 and "-" in top_pat else set()
    off_dates = {dates[k] for k in off}

    recs = []
    for sid, (cat, pat) in sched.items():
        on = [i for i, ch in enumerate(pat) if ch != "-"]
        if not on:
            continue
        invf = inv.get(sid, {}).get("f", "?")
        reg = inv.get(sid, {}).get("reg", "-")
        recs.append({
            "genre": CAT_NAME[cat], "cat": cat, "name": names.get(sid, sid), "id": sid,
            "url": f"https://store.shopping.yahoo.co.jp/{sid}/",
            "days": len(on), "d9": pat.count("9"), "d4": pat.count("4"),
            "first": dates[on[0]], "last": dates[on[-1]],
            "future": "○" if dates[on[-1]] >= today else "",
            "next": next((dates[i] for i in on if dates[i] >= today), None),
            "rest": sum(1 for i in on if dates[i] >= today),
            "ptype": pattern_type(pat, off) if off else "",
            "gold": "○" if gold.get(sid) else "",
            "inv": invf, "reg": reg if reg != "-" else "", "pat": pat,
        })
    recs.sort(key=lambda r: (CAT_ORDER[r["cat"]], -r["days"], r["id"]))
    all_n = len(recs)
    excl = [r for r in recs if r["inv"] == "N"]
    recs = [r for r in recs if r["inv"] != "N"]

    HEAD = ["ジャンル", "ストア名", "ストアID", "ストアURL", f"参加日数\n({n_days}日中)",
            "+9%枠\n日数", "+4%枠\n日数", "初回掲載日", "最終掲載日", f"{today.month}/{today.day}以降も\n掲載予定",
            f"次回掲載日\n({today.month}/{today.day}以降)", f"{today.month}/{today.day}以降の\n掲載日数", "掲載パターン", "優良ストア",
            "インボイス\n登録番号", "星5\n件数", "星4\n件数", "星3\n件数", "星2\n件数", "星1\n件数",
            "レビュー\n件数", "平均\n評価", "星1・2の\n割合"]
    WIDTH = [29, 40, 22, 46, 10, 8, 8, 12, 12, 11, 12, 11, 12, 10, 17, 9, 8, 8, 8, 8, 9, 7, 8]
    dcol0 = len(HEAD) + 1

    def write_list(ws, data):
        ws.cell(row=1, column=1, value="日付列：9＝+9%枠、4＝+4%枠、空欄＝掲載なし。1行目「+2」「+2/+3」＝全体加算日、「休」＝休み日型ストアが一斉に不参加の日").font = f_note
        for c, h in enumerate(HEAD, 1):
            cell = ws.cell(row=2, column=c, value=h)
            cell.font, cell.fill, cell.alignment = f_head, fill_head, head_align
            ws.column_dimensions[get_column_letter(c)].width = WIDTH[c - 1]
        for k, d in enumerate(dates):
            c = dcol0 + k
            ws.column_dimensions[get_column_letter(c)].width = 5.2
            cell = ws.cell(row=2, column=c, value=f"{d.month}/{d.day}\n{WD[d.weekday()]}")
            cell.font, cell.alignment = f_head_s, head_align
            cell.fill = fill_head_past if d < today else (fill_head_sun if d.weekday() == 6 else fill_head)
            mark = "+2/+3" if d in plus2 and d in plus3 else "+2" if d in plus2 else "+3" if d in plus3 else ""
            if mark:
                m = ws.cell(row=1, column=c, value=mark)
                m.font, m.fill, m.alignment = f_mark, fill_mark, center
            elif d in off_dates:
                m = ws.cell(row=1, column=c, value="休")
                m.font, m.fill, m.alignment = f_off, fill_off, center
        ws.row_dimensions[2].height = 32
        for i, r in enumerate(data, start=3):
            vals = [r["genre"], r["name"], r["id"], r["url"], r["days"], r["d9"], r["d4"], r["first"], r["last"],
                    r["future"], r["next"], r["rest"], r["ptype"], r["gold"], r["reg"]]
            for c, v in enumerate(vals, 1):
                cell = ws.cell(row=i, column=c, value=v)
                cell.font, cell.border = f_base, border
                if i % 2 == 1:
                    cell.fill = fill_band
            u = ws.cell(row=i, column=4)
            u.hyperlink, u.font = r["url"], f_link
            for c in (8, 9, 11):
                ws.cell(row=i, column=c).number_format = "m/d"
            for c in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
                ws.cell(row=i, column=c).alignment = center
            if r["id"] in rev:
                for j, v in enumerate(rev[r["id"]]):
                    ws.cell(row=i, column=16 + j, value=v).font = f_base
                ws.cell(row=i, column=21, value=f"=SUM(P{i}:T{i})").font = f_base
                ws.cell(row=i, column=22, value=f"=IF(U{i}=0,\"\",ROUND((P{i}*5+Q{i}*4+R{i}*3+S{i}*2+T{i})/U{i},2))").font = f_base
                ws.cell(row=i, column=23, value=f"=IF(U{i}=0,\"\",(S{i}+T{i})/U{i})").font = f_base
                ws.cell(row=i, column=23).number_format = "0.0%"
            for k, ch in enumerate(r["pat"]):
                if ch == "-":
                    continue
                cell = ws.cell(row=i, column=dcol0 + k, value=int(ch))
                cell.alignment = center
                cell.font, cell.fill = (f_9, fill_9) if ch == "9" else (f_4, fill_4)
        ws.freeze_panes = "E3"
        ws.auto_filter.ref = f"A2:{get_column_letter(dcol0 + n_days - 1)}{len(data) + 2}"

    wb = Workbook()
    summ = wb.active
    summ.title = "概要"
    all_ws = wb.create_sheet("全ジャンル")
    write_list(all_ws, recs)
    for cat, _ in CATS:
        write_list(wb.create_sheet(SHEET_NAMES[cat]), [r for r in recs if r["cat"] == cat])

    # 日別掲載数
    dws = wb.create_sheet("日別掲載数")
    dh = ["日付", "曜日", "全体加算", "休み日", "+9%枠", "+4%枠", "合計"] + [n for _, n in CATS]
    for c, h in enumerate(dh, 1):
        cell = dws.cell(row=1, column=c, value=h)
        cell.font, cell.fill, cell.alignment = f_head, fill_head, head_align
        dws.column_dimensions[get_column_letter(c)].width = 12 if c > 2 else 10
    for k, d in enumerate(dates):
        row = [d, WD[d.weekday()], "+2/+3" if d in plus2 and d in plus3 else "+2" if d in plus2 else "",
               "休" if d in off_dates else "",
               sum(1 for r in recs if r["pat"][k] == "9"), sum(1 for r in recs if r["pat"][k] == "4"),
               sum(1 for r in recs if r["pat"][k] != "-")]
        row += [sum(1 for r in recs if r["cat"] == cat and r["pat"][k] != "-") for cat, _ in CATS]
        for c, v in enumerate(row, 1):
            cell = dws.cell(row=k + 2, column=c, value=v)
            cell.font = f_base
            if c == 1:
                cell.number_format = "m/d"
    dws.freeze_panes = "A2"

    # 除外リスト（非適格）: 1 行目=説明, 2 行目=ヘッダー
    xws = wb.create_sheet("除外リスト（非適格）")
    xws.cell(row=1, column=1, value=f"適格請求書発行事業者ではない {len(excl)} 店 (会社概要ページの表示で判定。確認日 {today:%Y/%m/%d})").font = f_note
    xh = ["ストアID", "ストア名", "ジャンル", "ストアURL", f"参加日数({n_days}日中)", "初回掲載日", "最終掲載日"]
    for c, h in enumerate(xh, 1):
        cell = xws.cell(row=2, column=c, value=h)
        cell.font, cell.fill, cell.alignment = f_head, fill_head, head_align
        xws.column_dimensions[get_column_letter(c)].width = [22, 40, 29, 46, 12, 12, 12][c - 1]
    for i, r in enumerate(excl, start=3):
        for c, v in enumerate([r["id"], r["name"], r["genre"], r["url"], r["days"], r["first"], r["last"]], 1):
            cell = xws.cell(row=i, column=c, value=v)
            cell.font = f_base
            if c in (6, 7):
                cell.number_format = "m/d"

    # ポイント上乗せ調査: 1〜3 行目=説明, 4 行目=ヘッダー
    pws = wb.create_sheet("ポイント上乗せ調査", 1)
    pws["A1"] = (f"ストアポイント上乗せ調査 (適格 {len(recs)} 店のうち調査済み {sum(1 for r in recs if r['id'] in pts)} 店、"
                 f"{today:%Y/%m/%d} ({WD[today.weekday()]}) 調査、ログインなしの表示)")
    pws["A1"].font = f_title
    pws["A2"] = ("店内検索 (おすすめ順) の上位 30 商品の一覧の率を集め、率の高い順に最大 3 商品の商品ページで"
                 "「上乗せ率 = 合計率 − 標準で付く各行の率」を確認した。確認した率と同じ一覧の率の商品は同じ上乗せとみなす。")
    pws["A2"].font = f_note
    pws["A3"] = "判定できなかった商品数 = 未確認で、一覧の率 − 4% が最大上乗せ率を超える商品 (この店の上乗せが最大値より高い可能性がある)"
    pws["A3"].font = f_note
    ph = ["ジャンル", "ストア名", "ストアID", "ストアURL", "レビュー件数", "平均評価", "本日のBSPlus枠", "調査商品数",
          "上乗せあり商品数", "上乗せありの割合", "最大上乗せ率", "上乗せ率の内訳", "判定できなかった商品数", "確認した商品 (一覧の率→上乗せ)"]
    pwidth = [29, 40, 22, 46, 11, 9, 12, 10, 12, 12, 12, 30, 14, 36]
    for c, h in enumerate(ph, 1):
        cell = pws.cell(row=4, column=c, value=h)
        cell.font, cell.fill, cell.alignment = f_head, fill_head, head_align
        pws.column_dimensions[get_column_letter(c)].width = pwidth[c - 1]
    pws.row_dimensions[4].height = 32
    k_today = dates.index(today) if today in dates else None
    prow = 5
    for r in sorted((r for r in recs if r["id"] in pts), key=lambda r: (-pts[r["id"]]["max"], CAT_ORDER[r["cat"]], r["id"])):
        p = pts[r["id"]]
        stars = rev.get(r["id"])
        n_rev = sum(stars) if stars else None
        avg = round(sum(s * w for s, w in zip(stars, (5, 4, 3, 2, 1))) / n_rev, 2) if n_rev else None
        slot = r["pat"][k_today] if k_today is not None else "-"
        detail = "、".join(f"{k}×{v}" for k, v in sorted(p["detail"].items(), key=lambda kv: -float(kv[0].strip("+%"))))
        checked = "、".join(f"{a:g}%→+{b:g}%" for a, b in p.get("checked", []))
        vals = [r["genre"], r["name"], r["id"], r["url"], n_rev, avg, f"+{slot}%" if slot != "-" else "",
                p["n"], p["n_up"], (p["n_up"] / p["n"]) if p["n"] else None, p["max"] / 100, detail, p["unknown"], checked]
        for c, v in enumerate(vals, 1):
            cell = pws.cell(row=prow, column=c, value=v)
            cell.font = f_base
        pws.cell(row=prow, column=4).hyperlink = r["url"]
        pws.cell(row=prow, column=4).font = f_link
        pws.cell(row=prow, column=10).number_format = "0%"
        pws.cell(row=prow, column=11).number_format = "0%"
        prow += 1
    pws.freeze_panes = "E5"

    # 概要
    summ.column_dimensions["A"].width = 30
    for c in "BCDEFG":
        summ.column_dimensions[c].width = 16
    summ["A1"] = "PP加入ストア一覧（5ジャンル・適格請求書発行事業者のみ）＋ボーナスストアPlus掲載スケジュール"
    summ["A1"].font = f_title
    summ["A2"] = (f"取得日：{today:%Y/%m/%d}　出典：ボーナスストアPlus 参加ストア一覧（{dates[0]:%Y/%m/%d}〜{dates[-1]:%m/%d} 開催分・{n_days}日、"
                  f"{today.month}/{today.day}以降は掲載予定）")
    summ["A2"].font = f_note
    sh = ["ジャンル", "ストア数\n(適格のみ)", f"うち{today.month}/{today.day}以降も\n掲載予定", "うち\n優良ストア", "上乗せ調査済み", "除外した\n非適格ストア"]
    for c, h in enumerate(sh, 1):
        cell = summ.cell(row=4, column=c, value=h)
        cell.font, cell.fill, cell.alignment = f_head, fill_head, head_align
    summ.row_dimensions[4].height = 32
    for i, (cat, name) in enumerate(CATS, start=5):
        rs = [r for r in recs if r["cat"] == cat]
        vals = [name, len(rs), sum(1 for r in rs if r["future"]), sum(1 for r in rs if r["gold"]),
                sum(1 for r in rs if r["id"] in pts), sum(1 for r in excl if r["cat"] == cat)]
        for c, v in enumerate(vals, 1):
            summ.cell(row=i, column=c, value=v).font = f_base
    tot = 5 + len(CATS)
    summ.cell(row=tot, column=1, value="合計").font = Font(name=FONT, size=10, bold=True)
    for c in range(2, 7):
        col = get_column_letter(c)
        summ.cell(row=tot, column=c, value=f"=SUM({col}5:{col}{tot - 1})").font = Font(name=FONT, size=10, bold=True)
    notes = [
        "",
        "このリストの意味",
        "・ボーナスストアPlus (BSPlus) の参加ストアは「プロモーションパッケージ加入 + PRオプション設定」が条件なので、PP加入ストアの一覧として使う。",
        f"・日付列はその日の BSPlus 枠 (9 = +9%、4 = +4%、空欄 = 不参加)。{dates[-1]:%m/%d} までの予定が一覧ページに出ている分を収録。",
        "・インボイス未確認 (会社概要が取れなかった) 店は除外せずに残している (登録番号は空欄)。",
        "・優良ストア・上乗せ率は店内検索の先頭商品ページで判定。空欄は未調査。",
        f"・休み日: 最も多い掲載パターン ({top_n} 店が同一) の不参加日。" if off else "・休み日: 共通パターンが見つからなかったため未設定。",
    ]
    for j, text in enumerate(notes, start=tot + 1):
        cell = summ.cell(row=j, column=1, value=text)
        cell.font = Font(name=FONT, size=10, bold=True) if text in ("このリストの意味",) else f_base
    args.out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)
    log.info("書き出し: %s (全 %d 店 → 適格 %d / 非適格 %d、上乗せ調査 %d、優良 %d、評価 %d)", args.out, all_n, len(recs),
             len(excl), sum(1 for r in recs if r["id"] in pts), sum(1 for r in recs if r["gold"]), sum(1 for r in recs if r["id"] in rev))
    return 0


if __name__ == "__main__":
    sys.exit(main())
