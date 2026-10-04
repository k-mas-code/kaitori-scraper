"""絞り込み (甘めの判定): API の検索結果から、黒字になりうる (商品 × 店舗) を候補に残す"""

from __future__ import annotations

import re
from decimal import Decimal

from .buyback import Buyback
from .config import (TARGET_ALL, TARGET_BSPLUS, TARGET_GOOD_STORE, TARGET_PAGE, Campaign,
                     CampaignConfig)
from .points import PointComponent, optimistic_effective_price
from .stores import Store, StoreList

STORE_POINT_BASE = Decimal("0.01")   # ストアポイントの基本 1%
STORE_POINT_NAME = "ストアポイント"
BSPLUS_NAME = "ボーナスストアPlus"


def campaign_applies(campaign: Campaign, store: Store, page_titles: list[str] | None) -> bool:
    """キャンペーンがその店に付くか。page_titles=None は商品ページ未取得 (絞り込み段階) で、
    target=page は「付くかもしれない」として通す"""
    target = campaign.target
    if target == TARGET_ALL:
        return True
    if target == TARGET_BSPLUS:
        return store.bsplus_rate > 0
    if target == TARGET_GOOD_STORE:
        return store.good_store
    if target == TARGET_PAGE:
        if page_titles is None:
            return True
        return any(campaign.page_title in title for title in page_titles)
    return store.store_id in target


def max_global_bonus(labels: str | None) -> Decimal:
    """全体加算の表記 ("+2" / "+2/+3") のうち最大の率。絞り込み用"""
    numbers = [int(n) for n in re.findall(r"\+(\d+)", labels or "")]
    return Decimal(max(numbers)) / 100 if numbers else Decimal(0)


def bsplus_component(store: Store, cfg: CampaignConfig, global_bonus: Decimal) -> PointComponent | None:
    if store.bsplus_rate <= 0:
        return None
    return PointComponent(name=BSPLUS_NAME, rate=store.bsplus_rate + global_bonus, cap=cfg.bsplus.cap)


def optimistic_components(store: Store, cfg: CampaignConfig, stores: StoreList) -> list[PointComponent]:
    """その店で付きうる枠をすべて並べる (上乗せ率は店の最大値、全体加算は表記の最大値)"""
    components = list(cfg.common)
    components += [c.component for c in cfg.campaigns if campaign_applies(c, store, None)]
    components.append(PointComponent(name=STORE_POINT_NAME, rate=STORE_POINT_BASE + store.max_upsell))
    bsplus = bsplus_component(store, cfg, max_global_bonus(stores.global_bonus_labels))
    if bsplus:
        components.append(bsplus)
    return components


def max_total_rate(cfg: CampaignConfig, stores: StoreList) -> Decimal:
    """どの店でも超えない合計付与率 (上限・条件を無視)。検索結果のページ送り打ち切りに使う"""
    best_store = max((s.max_upsell + s.bsplus_rate for s in stores.stores.values()), default=Decimal(0))
    return (sum((c.rate for c in cfg.common), Decimal(0))
            + sum((c.component.rate for c in cfg.campaigns), Decimal(0))
            + STORE_POINT_BASE + best_store + max_global_bonus(stores.global_bonus_labels))


def price_can_never_pass(price: int, buyback_price: int, total_rate: Decimal, coupon_margin: float) -> bool:
    """この価格以上の商品は、どの店でも絞り込みを通らないか (価格昇順の打ち切り判定)"""
    # optimistic_effective_price の下限: 価格 × (1 − 余地) − 価格 × 率/1.1
    floor_price = float(price) * (1 - coupon_margin - float(total_rate) / 1.1)
    return floor_price >= buyback_price


def top_genre(hit: dict) -> str | None:
    parents = sorted(hit.get("parentGenreCategories") or [], key=lambda g: g.get("depth") or 0)
    named = [g.get("name") for g in parents if g.get("name")]
    return named[0] if named else (hit.get("genreCategory") or {}).get("name")


def select_candidates(hits: list[dict], buyback: Buyback, cfg: CampaignConfig,
                      stores: StoreList, coupon_margin: float) -> list[dict]:
    """店舗リストの店・在庫あり・新品で、甘い実質価格が買取価格を下回るものを返す"""
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        seller = hit.get("seller") or {}
        store = stores.stores.get(seller.get("sellerId"))
        price, code = hit.get("price"), hit.get("code")
        if store is None or not hit.get("inStock") or hit.get("condition") not in (None, "new"):
            continue
        if not isinstance(price, int) or price <= 0 or not code or not hit.get("url"):
            continue
        if (store.store_id, code) in seen:
            continue
        seen.add((store.store_id, code))
        optimistic = optimistic_effective_price(
            price, optimistic_components(store, cfg, stores), coupon_margin)
        if optimistic >= buyback.price:
            continue
        candidates.append({
            "jan_code": buyback.jan_code,
            "buyback_price": buyback.price,
            "buyback_source": buyback.source,
            "buyback_date": buyback.scraped_date,
            "store_id": store.store_id,
            "store_name": seller.get("name") or store.name,
            "item_code": code,
            "item_name": hit.get("name"),
            "item_url": hit["url"],
            "sale_price": price,
            "category": top_genre(hit),
            "optimistic_effective_price": round(optimistic),
        })
    return candidates
