"""確定判定: 商品ページの実際の値 (価格・上乗せ率・クーポン) で本来の式を計算する"""

from __future__ import annotations

from .config import CampaignConfig
from .coupons import manual_coupon_json, usable_manual_coupons
from .points import PointComponent, evaluate
from .prefilter import (STORE_POINT_BASE, STORE_POINT_NAME, bsplus_component, campaign_applies)
from .stores import Store
from .yahoo_page import Coupon, ItemPage

# 結果にしない理由
SKIP_GONE = "gone"                    # 商品ページが無い
SKIP_OUT_OF_STOCK = "out_of_stock"
SKIP_USED = "used"
SKIP_VARIATIONS = "variations"        # 色・サイズ違いがあり、どの SKU の価格・JAN か特定できない
SKIP_JAN_MISMATCH = "jan_mismatch"
SKIP_STORE_MISMATCH = "store_mismatch"
NOT_PROFITABLE = "not_profitable"


def same_jan(a: str | None, b: str | None) -> bool:
    """12 桁 UPC と先頭 0 付き 13 桁は同じ商品として扱う"""
    return bool(a) and bool(b) and a.lstrip("0") == b.lstrip("0")


def skip_reason(candidate: dict, page: ItemPage | None) -> str | None:
    if page is None:
        return SKIP_GONE
    if page.store_id != candidate["store_id"]:
        return SKIP_STORE_MISMATCH
    if page.is_used:
        return SKIP_USED
    if not page.in_stock:
        return SKIP_OUT_OF_STOCK
    if page.has_variations:
        return SKIP_VARIATIONS
    # ページに JAN が無い商品は API の JAN 検索の結果を信じる
    if page.jan_code and not same_jan(page.jan_code, candidate["jan_code"]):
        return SKIP_JAN_MISMATCH
    return None


def final_components(store: Store, cfg: CampaignConfig, page: ItemPage) -> list[PointComponent]:
    """その商品に実際に付く枠: 共通分 + 対象のキャンペーン + ストアポイント (1% + 上乗せ) + ボーナスストアPlus"""
    components = list(cfg.common)
    components += [c.component for c in cfg.campaigns if campaign_applies(c, store, list(page.point_titles))]
    components.append(PointComponent(name=STORE_POINT_NAME, rate=STORE_POINT_BASE + page.upsell_rate))
    bsplus = bsplus_component(store, cfg)
    if bsplus:
        components.append(bsplus)
    return components


def coupon_options(page: ItemPage, cfg: CampaignConfig, coupon: Coupon | None,
                   coupon_detail: dict | None) -> list[tuple[int, dict]]:
    """使えるクーポンの候補 [(値引き額, 結果行に入れる JSON)]。ページの公開クーポン (検証済みの 1 枚) と、
    その店・価格で使える手持ちクーポンすべて"""
    options: list[tuple[int, dict]] = []
    if coupon is not None:
        options.append((coupon.discount, {**coupon.to_json(coupon_detail), "source": "page"}))
    options += [(discount, manual_coupon_json(c, discount))
                for c, discount in usable_manual_coupons(cfg.coupons, page.store_id, page.price)]
    return options


def build_result(candidate: dict, page: ItemPage, store: Store, cfg: CampaignConfig,
                 coupon: Coupon | None, coupon_detail: dict | None, min_profit: int = 1) -> dict | None:
    """本来の式で計算し、利益が min_profit 円以上なら arbitrage_results の 1 行を返す。
    既定 (1 円以上) では赤字・トントンは None。

    クーポンは 1 注文 1 枚 (併用不可)。クーポンを使うと最低購入額を割ってキャンペーンが外れることも
    あるので、「使わない」と各クーポンをそれぞれ計算して利益がいちばん大きいものを採る。
    """
    components = final_components(store, cfg, page)
    buyback_price = candidate["buyback_price"]
    best = evaluate(page.price, 0, buyback_price, components)
    used_discount, used_coupon = 0, None
    for discount, coupon_json in coupon_options(page, cfg, coupon, coupon_detail):
        with_coupon = evaluate(page.price, discount, buyback_price, components)
        if with_coupon["profit"] > best["profit"]:
            best, used_discount, used_coupon = with_coupon, discount, coupon_json
    if best["effective_price"] <= 0 or best["profit"] < min_profit:
        return None
    return {
        "jan_code": candidate["jan_code"],
        "item_name": page.name or candidate.get("item_name") or "",
        "category": candidate.get("category"),
        "store_id": page.store_id,
        "store_name": page.store_name or candidate.get("store_name"),
        "item_code": candidate["item_code"],
        "item_url": candidate["item_url"],
        "sale_price": page.price,
        "coupon": used_coupon,
        "coupon_discount": used_discount,
        "points": best["points"],
        "points_total": best["points_total"],
        "capped_campaigns": best["capped_campaigns"],
        "effective_price": best["effective_price"],
        "buyback_price": buyback_price,
        "buyback_source": candidate["buyback_source"],
        "buyback_date": candidate.get("buyback_date"),
        "profit": best["profit"],
        "profit_rate": round(best["profit_rate"], 4),
    }
