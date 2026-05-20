"""Supabase へ商品マスタと価格履歴を upsert する共通モジュール"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Iterable

from supabase import Client, create_client

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def upsert_products(client: Client, products: list[dict], source: str) -> int:
    """products テーブルへ upsert。jan_code を主キーに last_seen / 名前更新も行う"""
    today = date.today().isoformat()
    rows = []
    for p in products:
        jan = p.get("jan_code")
        if not jan:
            continue
        rows.append({
            "jan_code": jan,
            "name": p.get("name"),
            "image_url": p.get("image_url"),
            "source": source,
            "category": p.get("category"),
            "detail_url": p.get("detail_url"),
            "last_seen": today,
        })

    inserted = 0
    for chunk in _chunks(rows, BATCH_SIZE):
        client.table("products").upsert(chunk, on_conflict="jan_code").execute()
        inserted += len(chunk)
    logger.info("upserted %d products (source=%s)", inserted, source)
    return inserted


def insert_price_history(client: Client, products: list[dict], source: str) -> int:
    """price_history テーブルへ insert。
    kaitorishouten: prices = {new: X, used: Y} を condition='new'/'used' の2行に展開
    rudeya: condition フィールドがあるのでそのまま使用
    """
    today = date.today().isoformat()
    rows = []
    for p in products:
        jan = p.get("jan_code")
        if not jan:
            continue
        note = "; ".join(
            f"{o['label']}{o['amount']:+d}円"
            for o in p.get("deduction_options", [])
        ) or p.get("note")

        # kaitorishouten 形式: prices に new/used
        if "prices" in p and isinstance(p["prices"], dict):
            for cond, price in p["prices"].items():
                if price is None:
                    continue
                rows.append({
                    "jan_code": jan,
                    "source": source,
                    "condition": cond,
                    "scraped_date": today,
                    "price": price,
                    "note": note,
                })
        # rudeya 形式: condition + price
        elif "price" in p and p["price"] is not None:
            cond = "new" if p.get("condition") == "新品" else "used"
            rows.append({
                "jan_code": jan,
                "source": source,
                "condition": cond,
                "scraped_date": today,
                "price": p["price"],
                "note": note,
            })

    inserted = 0
    for chunk in _chunks(rows, BATCH_SIZE):
        client.table("price_history").upsert(
            chunk, on_conflict="jan_code,source,condition,scraped_date"
        ).execute()
        inserted += len(chunk)
    logger.info("upserted %d price rows (source=%s)", inserted, source)
    return inserted


def save_to_db(products_by_category: dict[str, list[dict]], source: str) -> dict:
    """カテゴリ別商品リストを受け取り、products と price_history を保存する"""
    client = get_client()

    # カテゴリ情報を商品に付与してフラット化
    flat: list[dict] = []
    for category, products in products_by_category.items():
        for p in products:
            p2 = dict(p)
            p2["category"] = category
            flat.append(p2)

    products_count = upsert_products(client, flat, source)
    prices_count = insert_price_history(client, flat, source)
    return {"products": products_count, "prices": prices_count}
