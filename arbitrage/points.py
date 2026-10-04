"""ポイント・実質価格の計算 (純粋関数のみ。I/O なし)

前提: その商品を 1 つだけ買う。ポイントはクーポン値引き後の価格に対して付く。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

TAX_EXCLUDED = "tax_excluded"
TAX_INCLUDED = "tax_included"


@dataclass(frozen=True)
class PointComponent:
    """1 つの付与枠 (共通分・キャンペーン・ストアポイント・ボーナスストアPlus)"""

    name: str
    rate: Decimal                 # 0.05 = 5%
    cap: int | None = None        # 付与上限 (pt)。None = 上限なし
    base: str = TAX_EXCLUDED      # どの金額に率を掛けるか
    min_purchase: int = 0         # 最低購入額 (税込・クーポン後)。未満なら付与 0
    max_purchase: int | None = None   # 最高購入額 (税込・クーポン後)。超えたら付与 0 (金額帯で率が変わる企画用)


def tax_excluded(price: int) -> int:
    """税込価格 → 税抜価格 (10%)。Yahoo!ショッピングの表示と同じく消費税を切り捨てて引く"""
    return price - price * 10 // 110


def calc_points(paid_price: int, components: list[PointComponent],
                ignore_max_purchase: bool = False) -> tuple[int, list[dict], list[str]]:
    """クーポン後の支払額に対する付与ポイントを計算する。

    戻り値: (合計pt, 内訳 [{name, rate, base, points, cap, capped}], 上限に達した枠の名前)
    """
    bases = {TAX_EXCLUDED: tax_excluded(paid_price), TAX_INCLUDED: paid_price}
    breakdown: list[dict] = []
    capped_names: list[str] = []
    total = 0
    for c in components:
        if paid_price < c.min_purchase:
            continue
        if not ignore_max_purchase and c.max_purchase is not None and paid_price > c.max_purchase:
            continue
        raw = int(bases[c.base] * c.rate)  # 切り捨て
        points = raw if c.cap is None else min(raw, c.cap)
        capped = c.cap is not None and raw >= c.cap
        if capped:
            capped_names.append(c.name)
        total += points
        breakdown.append({
            "name": c.name,
            "rate": float(c.rate),
            "base": c.base,
            "points": points,
            "cap": c.cap,
            "capped": capped,
        })
    return total, breakdown, capped_names


def evaluate(sale_price: int, coupon_discount: int, buyback_price: int,
             components: list[PointComponent]) -> dict:
    """本来の判定式: 実質価格 = (販売価格 − クーポン) − 付与ポイント"""
    paid = sale_price - coupon_discount
    points_total, breakdown, capped = calc_points(paid, components)
    effective = paid - points_total
    profit = buyback_price - effective
    return {
        "paid_price": paid,
        "points_total": points_total,
        "points": breakdown,
        "capped_campaigns": capped,
        "effective_price": effective,
        "profit": profit,
        "profit_rate": profit / effective if effective > 0 else None,
    }


def optimistic_effective_price(sale_price: int, components: list[PointComponent],
                               coupon_margin: float, manual_discount: int = 0) -> float:
    """絞り込み用の甘い実質価格
    = 販売価格 − max(販売価格 × クーポン余地, 使える手持ちクーポンの最大値引き額) − クーポン前の価格で付くポイント。

    ページのクーポンが「販売価格 × クーポン余地」以下であるかぎり、確定判定の実質価格を上回らない
    (クーポンは 1 枚だけなので値引きは大きい方で見積もる。ポイントはクーポン前の価格で数えるので実際以上、
    上限は確定している事実なので適用する)。
    """
    # 最高購入額は無視する: クーポンで価格が下がると、販売価格では対象外だった枠が付くことがあるため
    points_total, _, _ = calc_points(sale_price, components, ignore_max_purchase=True)
    return sale_price - max(sale_price * coupon_margin, manual_discount) - points_total
