"""絞り込み (甘めの判定): API の検索結果から、黒字になりうる (商品 × 店舗) を候補に残す"""

from __future__ import annotations

from decimal import Decimal

from .buyback import Buyback
from .config import (TARGET_ALL, TARGET_BSPLUS, TARGET_BSPLUS_GOOD, TARGET_GOOD_STORE, TARGET_PAGE,
                     TARGET_STORES, Campaign, CampaignConfig, ManualCoupon)
from .coupons import best_manual_discount, discount_upper_bound
from .points import TAX_INCLUDED, PointComponent, optimistic_effective_price
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
    if target == TARGET_BSPLUS_GOOD:
        return store.bsplus_rate > 0 and store.good_store
    if target == TARGET_GOOD_STORE:
        return store.good_store
    if target == TARGET_PAGE:
        if page_titles is None:
            return True
        return any(campaign.page_title in title for title in page_titles)
    if target == TARGET_STORES:
        return store.store_id in campaign.store_ids
    raise ValueError(f"未対応の target: {target!r}")


def bsplus_component(store: Store, cfg: CampaignConfig) -> PointComponent | None:
    """ボーナスストアPlus (率は店舗リストの実行日の枠)。全体加算日の +2% / +3% は campaigns 側で持つ"""
    if store.bsplus_rate <= 0:
        return None
    return PointComponent(name=BSPLUS_NAME, rate=store.bsplus_rate, cap=cfg.bsplus.cap)


def max_upsell(store: Store, cfg: CampaignConfig) -> Decimal:
    """その店で付きうる上乗せ率の上限: 店舗リストの最大上乗せと、設定の store_upsell の大きい方"""
    return max(store.max_upsell, cfg.upsell_rate(store.store_id))


def optimistic_components(store: Store, cfg: CampaignConfig) -> list[PointComponent]:
    """その店で付きうる枠をすべて並べる (上乗せ率は店の最大値)"""
    components = list(cfg.common)
    components += [c.component for c in cfg.campaigns if campaign_applies(c, store, None)]
    components.append(PointComponent(name=STORE_POINT_NAME, rate=STORE_POINT_BASE + max_upsell(store, cfg)))
    bsplus = bsplus_component(store, cfg)
    if bsplus:
        components.append(bsplus)
    return components


def max_price_rate(cfg: CampaignConfig, stores: StoreList) -> float:
    """どの店でも超えない「税込価格に対する付与率」(上限・条件・対象を無視)。

    検索結果 (価格昇順) のページ送り打ち切りに使う。税抜ベースの枠は 率/1.1、税込ベースの枠は率そのまま。
    """
    def per_price(c: PointComponent) -> float:
        return float(c.rate) if c.base == TAX_INCLUDED else float(c.rate) / 1.1

    configured = sum(per_price(c) for c in cfg.common) + sum(per_price(c.component) for c in cfg.campaigns)
    best_store = max((max_upsell(s, cfg) + s.bsplus_rate for s in stores.stores.values()), default=Decimal(0))
    return configured + float(STORE_POINT_BASE + best_store) / 1.1


def price_can_never_pass(price: int, buyback_price: int, price_rate: float, coupon_margin: float,
                         coupons: tuple[ManualCoupon, ...] = ()) -> bool:
    """この価格以上の商品は、どの店でも絞り込みを通らないか (価格昇順の打ち切り判定)。

    optimistic_effective_price の下限で判定する。税抜価格の端数 (最大 +1 円) の分だけ余裕を持たせる。
    """
    if price * (1 - coupon_margin - price_rate) - 2 < buyback_price:
        return False
    # 手持ちクーポンは、この先それが使える最初の価格 (最低購入額) で見る。店の条件は無視する。
    # そこで下限が買取価格以上なら、それより高い価格でも下回らない (値引きは定額か上限つきの定率なので)
    for coupon in coupons:
        start = max(price, coupon.min_purchase)
        if start * (1 - price_rate) - discount_upper_bound(coupon, start) - 2 < buyback_price:
            return False
    return True


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
            price, optimistic_components(store, cfg), coupon_margin,
            best_manual_discount(cfg.coupons, store.store_id, price))
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


def realistic_gap(candidate: dict, store: Store, cfg: CampaignConfig) -> float:
    """クーポン余地なしの現実的な見積もりでの利益額 (円) = 買取価格 − 実質価格の見積もり。

    絞り込みと同じ甘い式だが、ページの公開クーポンは見込まない (値引きは使える手持ちクーポンの最大額だけ)。
    上乗せ率は店の最大値のまま。確定判定で「届きそうな候補」を選び、届きやすい順に並べるのに使う。
    """
    price = candidate["sale_price"]
    realistic = optimistic_effective_price(
        price, optimistic_components(store, cfg), 0,
        best_manual_discount(cfg.coupons, store.store_id, price))
    return candidate["buyback_price"] - realistic
