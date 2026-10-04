"""確定判定で見つけたクーポンの一覧 (coupons.csv)

商品ページのクーポン一覧に出ていたものを、その商品には使えなかったものも含めてすべて載せる。
ログインなしで取得しているので、アカウントに紐づくクーポン (獲得済みの限定クーポン等) は載らない。
"""

from __future__ import annotations

import csv
from pathlib import Path

COLUMNS = ["店舗ID", "店舗名", "クーポン", "クーポン名", "利用条件", "値引き上限", "利用期限", "ログイン",
           "1個で使えた商品数", "条件未達の商品数", "最大値引き額", "例: 商品名", "例: 販売価格", "例: 商品URL",
           "クーポンURL"]
ALL_STORES = "(全店共通)"
# Excel などが数式として実行してしまう先頭文字。商品名やクーポン名は外部サイトの文字列なので無害化する
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value):
    """文字列のセルが数式として解釈されないよう、危険な先頭文字の前に ' を付ける"""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def build_coupon_rows(page_records: list[dict]) -> list[dict]:
    """pages.jsonl の記録を (店舗, クーポン) 単位にまとめる。ログイン必須の全店共通クーポンは 1 行にする"""
    rows: dict[tuple[str, str], dict] = {}
    for record in page_records:
        for c in record.get("coupons_seen") or []:
            store_id = ALL_STORES if c["login_required"] else record["store_id"]
            row = rows.setdefault((store_id, c["id"]), {
                "店舗ID": store_id,
                "店舗名": "" if c["login_required"] else record.get("store_name") or "",
                "クーポン": c["text"],
                "クーポン名": c.get("name") or "",
                "利用条件": c.get("condition_text") or "",
                "値引き上限": c.get("limit_text") or "",
                "利用期限": c.get("end_text") or "",
                "ログイン": "必要" if c["login_required"] else "不要",
                "1個で使えた商品数": 0,
                "条件未達の商品数": 0,
                "最大値引き額": 0,
                "例: 商品名": "", "例: 販売価格": "", "例: 商品URL": "",
                "クーポンURL": c.get("url") or "",
            })
            row["1個で使えた商品数" if c["achieved"] else "条件未達の商品数"] += 1
            # 例には、値引き額がいちばん大きかった商品を載せる
            if c["item_discount"] > row["最大値引き額"] or not row["例: 商品URL"]:
                row["最大値引き額"] = max(row["最大値引き額"], c["item_discount"])
                row["例: 商品名"] = record.get("item_name") or ""
                row["例: 販売価格"] = record.get("sale_price") or ""
                row["例: 商品URL"] = record.get("item_url") or ""
    return sorted(rows.values(), key=lambda r: (r["ログイン"] == "必要", -r["最大値引き額"], r["店舗ID"]))


def write_coupon_report(page_records: list[dict], path: Path) -> list[dict]:
    rows = build_coupon_rows(page_records)
    # Excel で文字化けしないよう BOM 付き UTF-8
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows({key: _safe_cell(value) for key, value in row.items()} for row in rows)
    return rows
