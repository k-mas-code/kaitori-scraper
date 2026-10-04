"""手持ちクーポン (設定の coupons) の適用判定と値引き額。純粋関数のみ。

日付での絞り込みは config.active_coupons で済んでいる前提 (ここでは店と価格だけを見る)。
クーポンは 1 注文 1 枚で、ページの公開クーポンとも併用しない。
"""

from __future__ import annotations

from .config import COUPON_FIXED, TARGET_STORES, ManualCoupon


def manual_discount(coupon: ManualCoupon, store_id: str, price: int) -> int:
    """その店・販売価格 (税込) での値引き額。使えないときは 0"""
    if coupon.target == TARGET_STORES and store_id not in coupon.store_ids:
        return 0
    if price < coupon.min_purchase:
        return 0
    if coupon.type == COUPON_FIXED:
        discount = int(coupon.value)
    else:
        discount = int(price * coupon.value)   # 切り捨て
        if coupon.max_discount is not None:
            discount = min(discount, coupon.max_discount)
    # 値引きが販売価格以上になるクーポンは適用しない
    return discount if 0 < discount < price else 0


def usable_manual_coupons(coupons: tuple[ManualCoupon, ...], store_id: str,
                          price: int) -> list[tuple[ManualCoupon, int]]:
    """その店・価格で使える手持ちクーポンと値引き額"""
    pairs = [(c, manual_discount(c, store_id, price)) for c in coupons]
    return [(c, d) for c, d in pairs if d > 0]


def best_manual_discount(coupons: tuple[ManualCoupon, ...], store_id: str, price: int) -> int:
    """その店・価格で使える手持ちクーポンの最大値引き額 (無ければ 0)"""
    return max((d for _, d in usable_manual_coupons(coupons, store_id, price)), default=0)


def discount_upper_bound(coupon: ManualCoupon, price: int) -> float:
    """その価格での値引き額の上限 (店の条件・最低購入額・「販売価格未満」の条件は見ない)。打ち切り判定用"""
    if coupon.type == COUPON_FIXED:
        return float(coupon.value)
    uncapped = price * float(coupon.value)
    return uncapped if coupon.max_discount is None else min(uncapped, float(coupon.max_discount))


def manual_coupon_json(coupon: ManualCoupon, discount: int) -> dict:
    """結果行の coupon (jsonb) に入れる形"""
    return {
        "source": "manual",
        "name": coupon.name,
        "type": coupon.type,
        "value": int(coupon.value) if coupon.type == COUPON_FIXED else float(coupon.value),
        "discount": discount,
        "min_purchase": coupon.min_purchase,
        "valid_until": coupon.valid_until.isoformat() if coupon.valid_until else None,
    }
