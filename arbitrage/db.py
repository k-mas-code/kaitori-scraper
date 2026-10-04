"""Supabase の読み書き: 設定 (arbitrage_settings) の読み込みと、結果 (arbitrage_runs / arbitrage_results) の保存。

service_role キーを使う (RLS を通らない)。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from supabase import Client, create_client

from .config import ConfigError

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
ON_CONFLICT = "run_id,store_id,item_code"


class DbConfigError(RuntimeError):
    pass


def get_client() -> Client:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise DbConfigError("SUPABASE_URL / SUPABASE_KEY が未設定 (.env に service_role キーを置く)")
    return create_client(url, key)


def fetch_settings(client: Client) -> dict:
    """arbitrage_settings の 1 行から設定 JSON を読む (検証は config.parse_settings)"""
    try:
        data = client.table("arbitrage_settings").select("config").limit(1).execute().data
    except Exception as e:   # 通信・権限・テーブル未作成など。原因を付けて設定エラーとして報告する
        raise ConfigError(f"Supabase から設定 (arbitrage_settings) を読めない: {e}。"
                          "テーブルが無ければ db/arbitrage_settings.sql を実行する") from e
    if not data:
        raise ConfigError("Supabase に設定がまだ無い。結果ページの設定画面で保存してください "
                          "(ファイルで指定するなら --config-file)")
    config = data[0].get("config")
    if not isinstance(config, dict):
        raise ConfigError("Supabase の設定 (arbitrage_settings.config) が壊れている。"
                          "結果ページの設定画面で保存し直してください")
    return config


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
