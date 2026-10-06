from decimal import Decimal

import pytest

from arbitrage.config import BsPlusConfig, CampaignConfig, ManualCoupon
from arbitrage.points import PointComponent
from arbitrage.prefilter import realistic_gap
from arbitrage.scope import is_reachable, plan_page_checks
from arbitrage.stores import Store

D = Decimal


def store(store_id="s1", bsplus="0", upsell="0"):
    return Store(store_id, store_id, "家電", False, D(bsplus), D(upsell))


def candidate(code, price, buyback=9000, store_id="s1", optimistic=0):
    return {"jan_code": "4900000000000", "buyback_price": buyback, "store_id": store_id, "item_code": code,
            "sale_price": price, "optimistic_effective_price": optimistic}


CFG = CampaignConfig((), (), BsPlusConfig())


def test_realistic_gap_has_no_coupon_margin():
    # 10,000 円・ストアポイント 1% のみ: 実質 10,000 − 税抜 9,091×1% (90) = 9,910 → 買取 9,000 との差 −910
    assert realistic_gap(candidate("a", 10000), store(), CFG) == -910
    # 上乗せ率は店の最大値、ボーナスストアPlus と共通分も付く: 9,091 × (15% + 4% + 5%) = 1363 + 363 + 454
    cfg = CampaignConfig((PointComponent("lyp", D("0.05")),), (), BsPlusConfig())
    assert realistic_gap(candidate("a", 10000), store(bsplus="0.04", upsell="0.14"), cfg) == 9000 - (10000 - 2180)


def test_realistic_gap_counts_usable_manual_coupon_only():
    coupon = ManualCoupon("c", "fixed", 1500, min_purchase=10000, target="stores", store_ids=("s1",))
    cfg = CampaignConfig((), (), BsPlusConfig(), coupons=(coupon,))
    assert realistic_gap(candidate("a", 10000), store("s1"), cfg) == 590        # 9,000 − (10,000 − 1,500 − 90)
    assert realistic_gap(candidate("a", 10000, store_id="s2"), store("s2"), cfg) == -910   # 対象外の店
    assert realistic_gap(candidate("a", 9999), store("s1"), cfg) == 9000 - (9999 - 90)     # 最低購入額に届かない


def test_is_reachable_uses_slack_and_min_profit():
    assert not is_reachable(-910, 10000, 0.03, 1)       # −910 + 300 < 0
    assert is_reachable(-910, 10000, 0.1, 1)            # −910 + 1,000 >= 0
    assert is_reachable(-300, 10000, 0.03, 1)           # ちょうど 0 は届きそう
    assert is_reachable(-910, 10000, 0.03, -1000)       # 1,000 円までの赤字も載せるなら届く
    assert not is_reachable(-910, 10000, 0.03, 500)     # 利益の下限が正でも、基準は黒字 (0) のまま
    assert is_reachable(0, 10000, 0.0, 500)


def test_plan_orders_by_gap_ratio_and_drops_unreachable():
    stores = {"s1": store()}
    cands = [candidate("far", 10000),                   # 差 −910 (−9.1%) → 届かない
             candidate("big", 30000, buyback=30300),    # 差 +572 (+1.9%)
             candidate("small", 5000, buyback=5200),    # 差 +245 (+4.9%) → 金額は小さいが比率で先
             candidate("near", 10000, buyback=9700)]    # 差 −210 (−2.1%) → 余裕 3% の内
    plan = plan_page_checks(cands, stores, CFG, "reachable", 0.03, 1)
    assert [c["item_code"] for c in plan.ordered] == ["small", "big", "near"]
    assert (plan.reachable, plan.out_of_scope) == (3, 1)


def test_plan_scope_all_puts_reachable_first_then_the_rest_in_the_same_order():
    stores = {"s1": store()}
    cands = [candidate("far", 10000), candidate("farther", 10000, buyback=8000),
             candidate("near", 10000, buyback=9700), candidate("ok", 10000, buyback=10000)]
    plan = plan_page_checks(cands, stores, CFG, "all", 0.03, 1)
    assert [c["item_code"] for c in plan.ordered] == ["ok", "near", "far", "farther"]
    assert (plan.reachable, plan.out_of_scope) == (2, 0)
    # 赤字も載せる (--min-profit -1000) と、届きそうな範囲が広がる
    wide = plan_page_checks(cands, stores, CFG, "reachable", 0.03, -1000)
    assert [c["item_code"] for c in wide.ordered] == ["ok", "near", "far"]


def test_plan_is_stable_for_ties_and_handles_unknown_store():
    stores = {"s1": store()}
    cands = [candidate("b", 10000, buyback=10000), candidate("a", 10000, buyback=10000),
             candidate("x", 10000, buyback=10000, store_id="gone", optimistic=9000)]
    plan = plan_page_checks(cands, stores, CFG, "reachable", 0.0, 1)
    # 店舗リストに無い店は絞り込み時の見積もり (差 +1,000) で代用。同率は店・商品コード順
    assert [c["item_code"] for c in plan.ordered] == ["x", "a", "b"]
    with pytest.raises(ValueError):
        plan_page_checks(cands, stores, CFG, "some", 0.0, 1)
