"""設定 (ポイント付与・キャンペーン日程・手持ちクーポン・購入予定日) の検証と、実行日での絞り込み。

設定は結果ページの設定画面で編集し、Supabase の arbitrage_settings に保存する (読み込みは db.fetch_settings)。
--config-file を付けたときは、同じ仕様の YAML / JSON ファイルを読む。不正な設定は実行前に落とす。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from .points import TAX_EXCLUDED, TAX_INCLUDED, PointComponent

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_VERSION = 1

# キャンペーンの対象
TARGET_ALL = "all"                  # すべての店
TARGET_BSPLUS = "bsplus"            # 実行日にボーナスストアPlus枠がある店
TARGET_BSPLUS_GOOD = "bsplus_good"  # 枠があり、かつ優良ストア
TARGET_GOOD_STORE = "good_store"    # 優良ストア
TARGET_PAGE = "page"                # 商品ページのポイント内訳にその行が出ている店
TARGET_STORES = "stores"            # store_ids の店
TARGET_KINDS = (TARGET_ALL, TARGET_BSPLUS, TARGET_BSPLUS_GOOD, TARGET_GOOD_STORE, TARGET_PAGE, TARGET_STORES)
COUPON_TARGETS = (TARGET_ALL, TARGET_STORES)

COUPON_FIXED = "fixed"      # 定額
COUPON_PERCENT = "percent"  # 定率
COUPON_TYPES = (COUPON_FIXED, COUPON_PERCENT)

TOP_KEYS = {"version", "purchase_date", "common", "campaigns", "coupons", "bsplus", "store_upsell"}
COMPONENT_KEYS = {"name", "rate", "base", "cap_per_order", "cap_per_period", "min_purchase",
                  "max_purchase"}
CAMPAIGN_KEYS = COMPONENT_KEYS | {"entry_required", "target", "page_title", "store_ids", "dates"}
COUPON_KEYS = {"name", "type", "value", "max_discount", "min_purchase", "target", "store_ids",
               "valid_from", "valid_until"}
BSPLUS_KEYS = {"cap_per_order", "cap_per_period"}
STORE_UPSELL_KEYS = {"store_id", "rate", "note", "valid_from", "valid_until"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Campaign:
    component: PointComponent
    target: str = TARGET_ALL
    entry_required: bool = False
    page_title: str | None = None        # target=page のとき、内訳の行名に含まれる文字列
    store_ids: tuple[str, ...] = ()      # target=stores のときの対象店
    dates: tuple[date, ...] = ()         # 有効な日 (設定上は 1 件以上)


@dataclass(frozen=True)
class ManualCoupon:
    """手持ちクーポン (ログインしないと見えないもの等を手で登録したもの)"""

    name: str
    type: str                            # COUPON_FIXED / COUPON_PERCENT
    value: int | Decimal                 # fixed: 円 / percent: 率 (0.1 = 10%)
    max_discount: int | None = None      # percent の値引き上限 (円)
    min_purchase: int = 0                # 最低購入額 (税込・円)
    target: str = TARGET_ALL
    store_ids: tuple[str, ...] = ()
    valid_from: date | None = None       # 有効期間 (両端を含む)
    valid_until: date | None = None


@dataclass(frozen=True)
class BsPlusConfig:
    cap: int | None = None               # ボーナスストアPlus分の付与上限 (pt)


@dataclass(frozen=True)
class StoreUpsell:
    """店ごとのストアポイント上乗せ率 (ログインしないと商品ページに出ない分を手で登録したもの)"""

    store_id: str
    rate: Decimal                        # ログイン時に表示される上乗せ率そのもの (14% なら 0.14)
    note: str | None = None
    valid_from: date | None = None       # 有効期間 (両端を含む)
    valid_until: date | None = None


@dataclass(frozen=True)
class Settings:
    """設定の全体 (日付で絞る前)"""

    purchase_date: date
    common: tuple[PointComponent, ...]
    campaigns: tuple[Campaign, ...]
    coupons: tuple[ManualCoupon, ...]
    bsplus: BsPlusConfig
    store_upsell: tuple[StoreUpsell, ...] = ()
    raw: dict = field(compare=False, default_factory=dict)   # 設定 JSON そのまま (日付は文字列)


@dataclass(frozen=True)
class CampaignConfig:
    """実行日で絞り込み済みの設定 (絞り込み・確定判定はこれを受け取る)"""

    common: tuple[PointComponent, ...]
    campaigns: tuple[Campaign, ...]
    bsplus: BsPlusConfig
    coupons: tuple[ManualCoupon, ...] = ()
    store_upsell: tuple[StoreUpsell, ...] = ()               # 実行日に有効なものだけ (店ID は重複しない)
    raw: dict = field(compare=False, default_factory=dict)   # 保存用 (実行日に有効な分だけの設定 JSON)

    def upsell_rate(self, store_id: str) -> Decimal:
        """設定に登録した上乗せ率 (無ければ 0)"""
        for u in self.store_upsell:
            if u.store_id == store_id:
                return u.rate
        return Decimal(0)


# ---- 値の検証 ----

def _check_keys(entry: dict, allowed: set[str], where: str) -> None:
    """キー名の書き間違い (cap, min_purchace 等) を「上限なし」として黙って通さない"""
    unknown = set(entry) - allowed
    if unknown:
        raise ConfigError(f"{where}: 不明なキー {sorted(map(str, unknown))} (使えるキー: {sorted(allowed)})")


def _bool(entry: dict, key: str, where: str, default: bool = False) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} は true / false。値: {value!r}")
    return value


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _rate(entry: dict, key: str, where: str) -> Decimal:
    value = entry.get(key)
    if not _is_number(value) or not 0 < value <= 1:
        raise ConfigError(f"{where}: {key} は 0 より大きく 1 以下の小数で指定する (5% なら 0.05)。値: {value!r}")
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


def _date(value: object, where: str) -> date:
    """YYYY-MM-DD の文字列。YAML が日付として読んだ値も受け付ける"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and DATE_RE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(f"{where}: 日付は YYYY-MM-DD で指定する (実在する日付)。値: {value!r}")


def _opt_date(entry: dict, key: str, where: str) -> date | None:
    return None if entry.get(key) is None else _date(entry[key], f"{where}: {key}")


def _list(raw: dict, key: str, where: str) -> list:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where}: {key} はリストにする。値: {value!r}")
    return value


def _named(entry: object, where: str, allowed: set[str]) -> str:
    """項目がマッピングで name を持つことを確かめ、以降のエラー表示に使う場所の文字列を返す"""
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: 項目はマッピング (キー: 値 の組) にする。値: {entry!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: name (空でない文字列) が必要")
    where = f"{where} ({name})"
    _check_keys(entry, allowed, where)
    return where


def _target(entry: dict, kinds: tuple[str, ...], where: str) -> tuple[str, tuple[str, ...]]:
    """target と store_ids。target=stores 以外のときの store_ids は使わない (画面の入力残りを許す)"""
    target = entry.get("target", TARGET_ALL)
    if isinstance(target, list):
        raise ConfigError(f"{where}: 店を指定するときは target: stores と store_ids: [店ID, ...] で書く")
    if target not in kinds:
        raise ConfigError(f"{where}: target は {list(kinds)} のどれか。値: {target!r}")
    store_ids = entry.get("store_ids") or []
    if not isinstance(store_ids, list) or not all(isinstance(s, str) and s.strip() for s in store_ids):
        raise ConfigError(f"{where}: store_ids は店ID (文字列) のリスト。値: {entry.get('store_ids')!r}")
    if target == TARGET_STORES and not store_ids:
        raise ConfigError(f"{where}: target=stores には store_ids (1 件以上) が必要")
    return target, tuple(s.strip() for s in store_ids)


# ---- 項目ごとの読み取り ----

def _component(entry: object, where: str, allowed: set[str] = COMPONENT_KEYS) -> PointComponent:
    where = _named(entry, where, allowed)
    base = entry.get("base") or TAX_EXCLUDED
    if base not in (TAX_EXCLUDED, TAX_INCLUDED):
        raise ConfigError(f"{where}: base は {TAX_EXCLUDED} か {TAX_INCLUDED}。値: {base!r}")
    min_purchase = _opt_int(entry, "min_purchase", where) or 0
    max_purchase = _opt_int(entry, "max_purchase", where)
    if max_purchase is not None and max_purchase < min_purchase:
        raise ConfigError(f"{where}: max_purchase ({max_purchase}) は min_purchase ({min_purchase}) 以上にする")
    return PointComponent(
        name=entry["name"],
        rate=_rate(entry, "rate", where),
        cap=_cap(entry, where),
        base=base,
        min_purchase=min_purchase,
        max_purchase=max_purchase,
    )


def _dates(entry: dict, where: str) -> tuple[date, ...]:
    values = entry.get("dates")
    if not isinstance(values, list) or not values:
        raise ConfigError(f"{where}: dates (キャンペーンの日付のリスト、1 件以上) が必要")
    dates = tuple(_date(v, f"{where}: dates[{i}]") for i, v in enumerate(values))
    if len(set(dates)) != len(dates):
        raise ConfigError(f"{where}: dates に同じ日付が 2 回ある")
    return dates


def _campaign(entry: object, where: str) -> Campaign:
    component = _component(entry, where, CAMPAIGN_KEYS)
    where = f"{where} ({component.name})"
    target, store_ids = _target(entry, TARGET_KINDS, where)
    page_title = entry.get("page_title")
    if page_title is not None and not isinstance(page_title, str):
        raise ConfigError(f"{where}: page_title は文字列か null。値: {page_title!r}")
    if target == TARGET_PAGE and not (page_title or "").strip():
        raise ConfigError(f"{where}: target=page には page_title (内訳の行名に含まれる文字列) が必要")
    return Campaign(component=component, target=target,
                    entry_required=_bool(entry, "entry_required", where),
                    page_title=page_title or None, store_ids=store_ids, dates=_dates(entry, where))


def _coupon_value(entry: dict, where: str) -> tuple[int | Decimal, int | None]:
    """(value, max_discount)。type ごとに意味が違う"""
    value = entry.get("value")
    if entry.get("type") == COUPON_FIXED:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{where}: type=fixed の value は値引き額 (1 以上の整数、円)。値: {value!r}")
        if entry.get("max_discount") is not None:
            raise ConfigError(f"{where}: max_discount は type=percent のときだけ指定する (fixed では null)")
        return value, None
    if entry.get("type") == COUPON_PERCENT:
        if not _is_number(value) or not 0 < value <= 1:
            raise ConfigError(f"{where}: type=percent の value は 0 より大きく 1 以下の小数 (10% なら 0.1)。"
                              f"値: {value!r}")
        return Decimal(str(value)), _opt_int(entry, "max_discount", where)
    raise ConfigError(f"{where}: type は {list(COUPON_TYPES)} のどちらか。値: {entry.get('type')!r}")


def _coupon(entry: object, where: str) -> ManualCoupon:
    where = _named(entry, where, COUPON_KEYS)
    value, max_discount = _coupon_value(entry, where)
    target, store_ids = _target(entry, COUPON_TARGETS, where)
    valid_from, valid_until = _period(entry, where)
    return ManualCoupon(name=entry["name"], type=entry["type"], value=value, max_discount=max_discount,
                        min_purchase=_opt_int(entry, "min_purchase", where) or 0,
                        target=target, store_ids=store_ids, valid_from=valid_from, valid_until=valid_until)


def _period(entry: dict, where: str) -> tuple[date | None, date | None]:
    """valid_from / valid_until (両端を含む、null は制限なし)"""
    valid_from, valid_until = _opt_date(entry, "valid_from", where), _opt_date(entry, "valid_until", where)
    if valid_from and valid_until and valid_from > valid_until:
        raise ConfigError(f"{where}: valid_from ({valid_from}) が valid_until ({valid_until}) より後")
    return valid_from, valid_until


def _store_upsell(entry: object, where: str) -> StoreUpsell:
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: 項目はマッピング (キー: 値 の組) にする。値: {entry!r}")
    store_id = entry.get("store_id")
    if not isinstance(store_id, str) or not store_id.strip():
        raise ConfigError(f"{where}: store_id (店ID、空でない文字列) が必要")
    where = f"{where} ({store_id.strip()})"
    _check_keys(entry, STORE_UPSELL_KEYS, where)
    note = entry.get("note")
    if note is not None and not isinstance(note, str):
        raise ConfigError(f"{where}: note は文字列か null。値: {note!r}")
    valid_from, valid_until = _period(entry, where)
    return StoreUpsell(store_id=store_id.strip(), rate=_rate(entry, "rate", where), note=note or None,
                       valid_from=valid_from, valid_until=valid_until)


def _check_unique(names: list[str], where: str) -> None:
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise ConfigError(f"{where}: name が重複している: {duplicated}")


def _periods_overlap(a: StoreUpsell, b: StoreUpsell) -> bool:
    start = max(d for d in (a.valid_from, b.valid_from, date.min) if d is not None)
    end = min(d for d in (a.valid_until, b.valid_until, date.max) if d is not None)
    return start <= end


def _check_upsell_unique(items: tuple[StoreUpsell, ...], where: str) -> None:
    """同じ店に有効期間の重なる行が 2 つあると、どちらの率を使うか決められない"""
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.store_id == b.store_id and _periods_overlap(a, b):
                raise ConfigError(f"{where}: store_id が重複している (有効期間が重なる): {a.store_id}")


def _jsonable(value: object) -> object:
    """YAML が日付として読んだ値を文字列に戻す (meta.json / DB に保存できる形にする)"""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


# ---- 全体 ----

def parse_settings(raw: dict) -> Settings:
    """設定 JSON を検証して Settings にする。違反は場所と理由を付けた ConfigError"""
    if not isinstance(raw, dict):
        raise ConfigError("設定: トップレベルはマッピング (キー: 値 の組) にする")
    _check_keys(raw, TOP_KEYS, "設定")
    if raw.get("version") != SETTINGS_VERSION or isinstance(raw.get("version"), bool):
        raise ConfigError(f"設定: version は {SETTINGS_VERSION} のみ対応。値: {raw.get('version')!r}")
    if raw.get("purchase_date") is None:
        raise ConfigError("設定: purchase_date (購入予定日 YYYY-MM-DD) が必要")
    purchase_date = _date(raw["purchase_date"], "設定: purchase_date")

    common = tuple(_component(e, f"common[{i}]") for i, e in enumerate(_list(raw, "common", "設定")))
    campaigns = tuple(_campaign(e, f"campaigns[{i}]") for i, e in enumerate(_list(raw, "campaigns", "設定")))
    coupons = tuple(_coupon(e, f"coupons[{i}]") for i, e in enumerate(_list(raw, "coupons", "設定")))
    _check_unique([c.name for c in common] + [c.component.name for c in campaigns], "common / campaigns")
    _check_unique([c.name for c in coupons], "coupons")
    store_upsell = tuple(_store_upsell(e, f"store_upsell[{i}]")
                         for i, e in enumerate(_list(raw, "store_upsell", "設定")))
    _check_upsell_unique(store_upsell, "store_upsell")

    bs_raw = raw.get("bsplus") or {}
    if not isinstance(bs_raw, dict):
        raise ConfigError("bsplus: マッピングにする")
    _check_keys(bs_raw, BSPLUS_KEYS, "bsplus")
    return Settings(purchase_date=purchase_date, common=common, campaigns=campaigns, coupons=coupons,
                    bsplus=BsPlusConfig(cap=_cap(bs_raw, "bsplus")), store_upsell=store_upsell,
                    raw=_jsonable(raw))


def load_settings_file(path: Path) -> Settings:
    """--config-file 用: YAML / JSON ファイル (Supabase に保存するものと同じ仕様) を読む"""
    if not path.exists():
        raise ConfigError(f"{path} が無い (ひな形は config/campaigns.example.yaml)")
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: 書式エラーで読めない: {e}") from e
    try:
        return parse_settings(raw if raw is not None else {})
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e


# ---- 実行日での絞り込み ----

def coupon_valid_on(coupon: ManualCoupon | StoreUpsell, day: date) -> bool:
    return ((coupon.valid_from is None or coupon.valid_from <= day)
            and (coupon.valid_until is None or day <= coupon.valid_until))


def active_campaigns(settings: Settings, day: date) -> tuple[Campaign, ...]:
    """その日が dates に含まれるキャンペーン"""
    return tuple(c for c in settings.campaigns if day in c.dates)


def active_coupons(settings: Settings, day: date) -> tuple[ManualCoupon, ...]:
    """その日が有効期間内の手持ちクーポン"""
    return tuple(c for c in settings.coupons if coupon_valid_on(c, day))


def active_store_upsell(settings: Settings, day: date) -> tuple[StoreUpsell, ...]:
    """その日が有効期間内の店ごとの上乗せ率 (検証済みなので同じ店は高々 1 件)"""
    return tuple(u for u in settings.store_upsell if coupon_valid_on(u, day))


def active_config(settings: Settings, day: date) -> CampaignConfig:
    """実行日に有効なものだけの設定。raw も同じ仕様の JSON に絞る (日付外の項目を変えても再開できるように)"""
    campaigns, coupons = active_campaigns(settings, day), active_coupons(settings, day)
    store_upsell = active_store_upsell(settings, day)
    campaign_names = {c.component.name for c in campaigns}
    coupon_names = {c.name for c in coupons}
    raw = {
        **settings.raw,
        "purchase_date": day.isoformat(),
        "campaigns": [e for e in settings.raw.get("campaigns") or [] if e["name"] in campaign_names],
        "coupons": [e for e in settings.raw.get("coupons") or [] if e["name"] in coupon_names],
    }
    # 店ID は期間違いで複数行ありうるので、行ごとに有効期間で選ぶ (name のような一意キーが無い)
    filtered = [e for e in settings.raw.get("store_upsell") or []
                if coupon_valid_on(_store_upsell(e, "store_upsell"), day)]
    # キーの有無ではなく中身で再開条件を決める: その日に有効な行が無ければ raw からキーごと省く
    # (設定画面は常に store_upsell: [] を書くので、この機能より前に始めた run の meta.config と一致させる)
    raw.pop("store_upsell", None)
    if filtered:
        raw["store_upsell"] = filtered
    return CampaignConfig(common=settings.common, campaigns=campaigns, bsplus=settings.bsplus,
                          coupons=coupons, store_upsell=store_upsell, raw=raw)
