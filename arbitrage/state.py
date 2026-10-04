"""実行状態の保存と再開 (data/arbitrage/{run_name}/ 以下のローカルファイル)

- meta.json      実行条件 (日付・引数・campaigns.yaml の中身)
- api.jsonl      JAN 1 件の API 検索が終わるごとに 1 行追記 → 再開時は済みの JAN を飛ばす
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT

RUNS_DIR = ROOT / "data" / "arbitrage"


class RunState:
    def __init__(self, run_name: str):
        self.dir = RUNS_DIR / run_name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "meta.json"
        self.api_path = self.dir / "api.jsonl"

    def load_meta(self) -> dict | None:
        if not self.meta_path.exists():
            return None
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def save_meta(self, meta: dict) -> None:
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_api_records(self) -> list[dict]:
        if not self.api_path.exists():
            return []
        records = []
        for line in self.api_path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # 書き込み途中で止まった最終行。その JAN はやり直す
                continue
        return records

    def append_api_record(self, record: dict) -> None:
        with self.api_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
