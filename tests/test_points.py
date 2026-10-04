from decimal import Decimal

import pytest

from arbitrage.points import (TAX_INCLUDED, PointComponent, calc_points, evaluate,
                              optimistic_effective_price, tax_excluded)

D = Decimal


@pytest.mark.parametrize("price,expected", [
    # 商品ページの item.taxExcludedApplicablePrice で確認した実例
    (58018, 52744), (54780, 49800), (3990, 3628), (4902, 4457), (64870, 58973), (69850, 63500),
])
def test_tax_excluded_matches_yahoo(price, expected):
    assert tax_excluded(price) == expected


def test_points_are_floored_on_tax_excluded_base():
    # 実例: 税抜 52,744 × 4% = 2,109 / 税込 58,018 × 0.5% = 290
    total, breakdown, capped = calc_points(58018, [
        PointComponent("bsplus", D("0.04")),
        PointComponent("paypay", D("0.005"), base=TAX_INCLUDED),
    ])
    assert [b["points"] for b in breakdown] == [2109, 290]
    assert total == 2399 and capped == []


def test_store_point_with_upsell_is_floored_once():
    # 実例 (try3): 税抜 58,973、基本1% + 上乗せ1% → 1,179 (589 + 589 ではない)
    total, _, _ = calc_points(64870, [PointComponent("store", D("0.02"))])
    assert total == 1179


def test_cap_is_applied_and_recorded():
    # 実例 (dyson): 税抜 63,500 × 5% = 3,175 → 上限 3,000
    total, breakdown, capped = calc_points(69850, [PointComponent("sunday", D("0.05"), cap=3000)])
    assert total == 3000 and capped == ["sunday"] and breakdown[0]["capped"] is True


def test_cap_not_reached_is_not_recorded():
    total, _, capped = calc_points(11000, [PointComponent("sunday", D("0.05"), cap=3000)])
    assert total == 500 and capped == []


def test_min_purchase_excludes_campaign():
    comp = PointComponent("sunday", D("0.05"), min_purchase=5000)
    assert calc_points(4999, [comp])[0] == 0
    assert calc_points(5000, [comp])[0] == 227   # 税抜 4,546 × 5%


def test_evaluate_applies_coupon_before_points():
    # 55,000 円 − クーポン 2,000 = 53,000 (税抜 48,182) × 10% = 4,818pt → 実質 48,182
    r = evaluate(55000, 2000, 50000, [PointComponent("a", D("0.10"))])
    assert r["paid_price"] == 53000 and r["points_total"] == 4818
    assert r["effective_price"] == 48182 and r["profit"] == 1818
    assert r["profit_rate"] == pytest.approx(1818 / 48182)


def test_evaluate_matches_formula_without_caps():
    # 判定式: 実質 = (価格 − クーポン) × (1 − 率/1.1)。切り捨ての分だけ 1 円未満〜数円ずれる
    r = evaluate(110000, 10000, 0, [PointComponent("a", D("0.11"))])
    assert r["effective_price"] == pytest.approx(100000 * (1 - 0.11 / 1.1), abs=1)


def test_optimistic_price_is_never_above_real_effective_price():
    # クーポンが余地 (価格 × margin) 以内なら、甘い実質価格 ≤ 確定の実質価格 (上限・最低購入額があっても)
    comps = [PointComponent("a", D("0.05"), cap=300, min_purchase=2500), PointComponent("b", D("0.15"))]
    margin = 0.2
    for price in (3000, 30000, 300000):
        for coupon in (0, price // 10, price // 5):
            real = evaluate(price, coupon, 0, comps)["effective_price"]
            assert optimistic_effective_price(price, comps, margin) <= real


def test_optimistic_price_is_at_most_spec_formula():
    # 依頼の絞り込み式 価格 × (1 − 率/1.1) × (1 − 余地) 以下になる (= より甘い)
    comps = [PointComponent("a", D("0.20"))]
    assert optimistic_effective_price(110000, comps, 0.2) <= 110000 * (1 - 0.20 / 1.1) * 0.8 + 1
