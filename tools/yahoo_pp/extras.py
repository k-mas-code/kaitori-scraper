"""手動で追加する店 (data/yahoo_pp/extra_stores.json)。ボーナスストアPlus の一覧に出ない店をリストに含めるため。

  extra_stores.json  {storeId: {"cat": カテゴリコード, "name": 店名, "note": 省略可}}

bsplus_fetch が sched.json / names.json を書くときに合流させる (掲載パターンは全日 "-" = 枠なし)。
refresh の取り直しでも消えない (STATE_FILES に含めない)。enrich / build_xlsx / snapshot はこの店を
「掲載日 0 日」でも落とさない。

実行: .venv/bin/python -m tools.yahoo_pp.extras --apply   いまの sched.json / names.json に合流させる
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .common import ROOT

OUT = ROOT / "data" / "yahoo_pp"
FILE_NAME = "extra_stores.json"
log = logging.getLogger("yahoo_pp.extras")


def load_extras(data_dir: Path = OUT) -> dict[str, dict]:
    p = data_dir / FILE_NAME
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: トップレベルは {{storeId: {{cat, name}}}} のマッピング")
    out: dict[str, dict] = {}
    for sid, v in raw.items():
        if not isinstance(v, dict) or not isinstance(v.get("cat"), str) or not v.get("cat"):
            raise ValueError(f"{p}: {sid}: cat (カテゴリコード) が必要")
        out[str(sid).strip()] = {"cat": v["cat"], "name": str(v.get("name") or sid)}
    return out


def merge_extras(sched: dict, names: dict, n_days: int, extras: dict[str, dict]) -> tuple[dict, dict]:
    """sched / names に手動追加の店を足した新しい dict を返す。一覧に出ている店はそのまま (上書きしない)"""
    new_sched, new_names = dict(sched), dict(names)
    for sid, e in extras.items():
        if sid not in new_sched:
            new_sched[sid] = [e["cat"], "-" * n_days]
        new_names.setdefault(sid, e["name"])
    return new_sched, new_names


def apply(data_dir: Path = OUT) -> int:
    extras = load_extras(data_dir)
    dates = json.loads((data_dir / "dates.json").read_text(encoding="utf-8"))
    sched = json.loads((data_dir / "sched.json").read_text(encoding="utf-8"))
    names = json.loads((data_dir / "names.json").read_text(encoding="utf-8"))
    new_sched, new_names = merge_extras(sched, names, len(dates), extras)
    added = sorted(set(new_sched) - set(sched))
    (data_dir / "sched.json").write_text(json.dumps(new_sched, ensure_ascii=False, indent=0), encoding="utf-8")
    (data_dir / "names.json").write_text(json.dumps(new_names, ensure_ascii=False, indent=0), encoding="utf-8")
    log.info("手動追加 %d 店 (新規 %d: %s)", len(extras), len(added), ", ".join(added) or "なし")
    return len(added)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="sched.json / names.json に合流させる")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.apply:
        apply()
    else:
        for sid, e in load_extras().items():
            print(sid, e["cat"], e["name"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
