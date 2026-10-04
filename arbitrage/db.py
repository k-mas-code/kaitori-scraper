"""Supabase への保存 (arbitrage_runs / arbitrage_results)。service_role キーで書き込む"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from supabase import Client, create_client

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
ON_CONFLICT = "run_id,store_id,item_code"


class DbConfigError(RuntimeError):
    pass


def get_client() -> Client:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise DbConfigError("SUPABASE_URL / SUPABASE_KEY が未設定 (.env に service_role キーを置く)。"
                            "保存せずに試すなら --no-db")
    return create_client(url, key)


def create_run(client: Client, config: dict, coupon_margin: float, jan_count: int) -> int:
    row = {"config": config, "coupon_margin": coupon_margin, "jan_count": jan_count, "status": "running"}
    data = client.table("arbitrage_runs").insert(row).execute().data
    return data[0]["id"]


def upsert_results(client: Client, run_id: int, rows: list[dict]) -> int:
    """同じ run に何度書いても二重にならない (再開用)。バッチ内の同一キーは先勝ちで除く"""
    unique: dict[tuple, dict] = {}
    for row in rows:
        unique.setdefault((row["store_id"], row["item_code"]), {**row, "run_id": run_id})
    payload = list(unique.values())
    for i in range(0, len(payload), BATCH_SIZE):
        client.table("arbitrage_results").upsert(payload[i:i + BATCH_SIZE], on_conflict=ON_CONFLICT).execute()
    return len(payload)


def finish_run(client: Client, run_id: int, candidate_count: int, result_count: int) -> None:
    client.table("arbitrage_runs").update({
        "status": "completed",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": candidate_count,
        "result_count": result_count,
    }).eq("id", run_id).execute()
