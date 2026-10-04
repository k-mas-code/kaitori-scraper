"""実行状態の保存と再開 (data/arbitrage/{run_name}/ 以下のローカルファイル)

- meta.json      実行条件 (日付・引数・campaigns.yaml の中身)
- buyback.json   開始時点の買取価格 (再開しても同じ価格で判定を続ける)
- api.jsonl      JAN 1 件の API 検索が終わるごとに 1 行追記 → 再開時は済みの JAN を飛ばす
- pages.jsonl    候補 1 件の商品ページ確認が終わるごとに 1 行追記 → 再開時は済みの候補を飛ばす
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
        self.buyback_path = self.dir / "buyback.json"
        self.api_path = self.dir / "api.jsonl"
        self.pages_path = self.dir / "pages.jsonl"

    def _load_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _save_json(self, path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load_meta(self) -> dict | None:
        return self._load_json(self.meta_path)

    def save_meta(self, meta: dict) -> None:
        self._save_json(self.meta_path, meta)

    def load_buyback(self) -> list[dict] | None:
        return self._load_json(self.buyback_path)

    def save_buyback(self, rows: list[dict]) -> None:
        self._save_json(self.buyback_path, rows)

    def _load_jsonl(self, path: Path) -> list[dict]:
        """書き込み途中で止まった行は捨ててファイルを書き直す (その分はやり直す)"""
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        records, good_lines = [], []
        for line in lines:
            try:
                records.append(json.loads(line))
                good_lines.append(line)
            except json.JSONDecodeError:
                continue
        if len(good_lines) != len(lines):
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(line + "\n" for line in good_lines), encoding="utf-8")
            tmp.replace(path)
        return records

    @staticmethod
    def _append_jsonl(path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()

    def load_api_records(self) -> list[dict]:
        return self._load_jsonl(self.api_path)

    def append_api_record(self, record: dict) -> None:
        self._append_jsonl(self.api_path, record)

    def load_page_records(self) -> list[dict]:
        return self._load_jsonl(self.pages_path)

    def append_page_record(self, record: dict) -> None:
        self._append_jsonl(self.pages_path, record)
