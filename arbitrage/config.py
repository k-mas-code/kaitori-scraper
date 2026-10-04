"""config/campaigns.yaml の読み込みと検証 (不正な設定は実行前に落とす)"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from .points import TAX_EXCLUDED, TAX_INCLUDED, PointComponent

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAMPAIGNS_PATH = ROOT / "config" / "campaigns.yaml"

# 対象の種類 (文字列)。ほかにストアIDのリストも指定できる
TARGET_ALL = "all"                # すべての店
TARGET_BSPLUS = "bsplus"          # その日ボーナスストアPlus枠がある店
TARGET_GOOD_STORE = "good_store"  # 優良ストア
TARGET_PAGE = "page"              # 商品ページのポイント内訳にその行が出ている店
TARGET_KINDS = {TARGET_ALL, TARGET_BSPLUS, TARGET_GOOD_STORE, TARGET_PAGE}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Campaign:
    component: PointComponent
    target: str | tuple[str, ...]      # TARGET_* またはストアIDのタプル
    entry_required: bool = False
    page_title: str | None = None      # target=page のとき、内訳の行名に含まれる文字列


@dataclass(frozen=True)
class BsPlusConfig:
    cap: int | None = None             # ボーナスストアPlus分の付与上限 (pt)
    # 全体加算日 (店舗リスト 1 行目の「+2」「+2/+3」) に足す率。None = 加算しない
    global_bonus: Decimal | None = None


@dataclass(frozen=True)
class CampaignConfig:
    common: tuple[PointComponent, ...]
    campaigns: tuple[Campaign, ...]
    bsplus: BsPlusConfig
    raw: dict = field(compare=False, default_factory=dict)   # DB 保存用 (yaml の中身そのまま)


def _rate(entry: dict, where: str) -> Decimal:
    value = entry.get("rate")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
        raise ConfigError(f"{where}: rate は 0〜1 の小数で指定する (5% なら 0.05)。値: {value!r}")
    return Decimal(str(value))


def _opt_int(entry: dict, key: str, where: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{where}: {key} は 0 以上の整数か null。値: {value!r}")
    return value


def _cap(entry: dict, where: str) -> int | None:
    """1 つだけ買う前提なので、1注文あたりと期間あたりの小さい方が実質の上限"""
    caps = [c for c in (_opt_int(entry, "cap_per_order", where),
                        _opt_int(entry, "cap_per_period", where)) if c is not None]
    return min(caps) if caps else None


def _component(entry: dict, where: str) -> PointComponent:
    if not isinstance(entry, dict) or not entry.get("name"):
        raise ConfigError(f"{where}: name が必要")
    where = f"{where} ({entry['name']})"
    base = entry.get("base", TAX_EXCLUDED)
    if base not in (TAX_EXCLUDED, TAX_INCLUDED):
        raise ConfigError(f"{where}: base は {TAX_EXCLUDED} か {TAX_INCLUDED}")
    return PointComponent(
        name=str(entry["name"]),
        rate=_rate(entry, where),
        cap=_cap(entry, where),
        base=base,
        min_purchase=_opt_int(entry, "min_purchase", where) or 0,
    )


def _campaign(entry: dict, where: str) -> Campaign:
    component = _component(entry, where)
    where = f"{where} ({component.name})"
    target = entry.get("target", TARGET_ALL)
    if isinstance(target, list):
        if not target or not all(isinstance(t, str) for t in target):
            raise ConfigError(f"{where}: target のストアIDリストが不正")
        target = tuple(target)
    elif target not in TARGET_KINDS:
        raise ConfigError(f"{where}: target は {sorted(TARGET_KINDS)} かストアIDのリスト。値: {target!r}")
    page_title = entry.get("page_title")
    if target == TARGET_PAGE and not page_title:
        raise ConfigError(f"{where}: target=page には page_title (内訳の行名に含まれる文字列) が必要")
    return Campaign(component=component, target=target,
                    entry_required=bool(entry.get("entry_required", False)),
                    page_title=page_title)


def load_campaigns(path: Path = DEFAULT_CAMPAIGNS_PATH) -> CampaignConfig:
    if not path.exists():
        raise ConfigError(f"{path} が無い。config/campaigns.example.yaml をコピーして編集する")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: トップレベルはマッピングにする")
    unknown = set(raw) - {"common", "campaigns", "bsplus"}
    if unknown:
        raise ConfigError(f"{path}: 不明なキー {sorted(unknown)}")

    common = tuple(_component(e, f"common[{i}]") for i, e in enumerate(raw.get("common") or []))
    campaigns = tuple(_campaign(e, f"campaigns[{i}]") for i, e in enumerate(raw.get("campaigns") or []))
    names = [c.name for c in common] + [c.component.name for c in campaigns]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: name が重複している")

    bs_raw = raw.get("bsplus") or {}
    global_bonus = bs_raw.get("global_bonus")
    bsplus = BsPlusConfig(
        cap=_cap(bs_raw, "bsplus"),
        global_bonus=None if global_bonus is None else _rate({"rate": global_bonus}, "bsplus.global_bonus"),
    )
    return CampaignConfig(common=common, campaigns=campaigns, bsplus=bsplus, raw=raw)
