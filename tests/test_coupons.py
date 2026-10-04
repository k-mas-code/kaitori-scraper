"""手持ちクーポンの適用条件と、確定判定でのクーポン選び (1 注文 1 枚、併用なし)"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from arbitrage.config import BsPlusConfig, Campaign, CampaignConfig, ManualCoupon, active_coupons, parse_settings
from arbitrage.coupons import (best_manual_discount, discount_upper_bound, manual_coupon_json, manual_discount,
                               usable_manual_coupons)
from arbitrage.finalize import build_result
from arbitrage.points import PointComponent
from arbitrage.stores import Store
from arbitrage.yahoo_page import parse_coupon_detail, parse_item_page, parse_usable_coupons

D = Decimal
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "yahoo_pages.json").read_text(encoding="utf-8"))


def test_store_condition():
    c = ManualCoupon("c", "fixed", 2000, target="stores", store_ids=("joshin", "yamada"))
    assert manual_discount(c, "joshin", 60000) == 2000 and manual_discount(c, "other", 60000) == 0
    assert manual_discount(ManualCoupon("c", "fixed", 2000), "other", 60000) == 2000      # target=all


def test_min_purchase_is_on_sale_price_inclusive():
    c = ManualCoupon("c", "fixed", 2000, min_purchase=60000)
    assert manual_discount(c, "s", 59999) == 0 and manual_discount(c, "s", 60000) == 2000


def test_percent_is_floored_and_capped():
    assert manual_discount(ManualCoupon("c", "percent", D("0.1")), "s", 54789) == 5478          # 切り捨て
    capped = ManualCoupon("c", "percent", D("0.1"), max_discount=3000)
    assert manual_discount(capped, "s", 54789) == 3000 and manual_discount(capped, "s", 20000) == 2000
    assert manual_discount(ManualCoupon("c", "percent", D("0.07")), "s", 10) == 0               # 1 円未満は値引きなし


def test_discount_must_be_below_price():
    c = ManualCoupon("c", "fixed", 2000)
    assert manual_discount(c, "s", 2000) == 0 and manual_discount(c, "s", 1500) == 0 and manual_discount(c, "s", 2001) == 2000
    assert manual_discount(ManualCoupon("c", "percent", D("1")), "s", 5000) == 0                # 100% OFF は適用しない


def test_validity_period_is_checked_by_date_filter():
    raw = {"version": 1, "purchase_date": "2026-10-05", "coupons": [
        {"name": "期限あり", "type": "fixed", "value": 2000, "valid_until": "2026-10-05"},
        {"name": "これから", "type": "fixed", "value": 500, "valid_from": "2026-10-06"}]}
    s = parse_settings(raw)
    assert [c.name for c in active_coupons(s, date(2026, 10, 5))] == ["期限あり"]       # 最終日は使える
    assert [c.name for c in active_coupons(s, date(2026, 10, 6))] == ["これから"]


def test_best_and_bound():
    coupons = (ManualCoupon("a", "fixed", 2000, min_purchase=60000, target="stores", store_ids=("joshin",)),
               ManualCoupon("b", "percent", D("0.05"), max_discount=1500),
               ManualCoupon("c", "fixed", 300))
    assert [(c.name, d) for c, d in usable_manual_coupons(coupons, "joshin", 60000)] == [("a", 2000), ("b", 1500), ("c", 300)]
    assert best_manual_discount(coupons, "joshin", 60000) == 2000
    assert best_manual_discount(coupons, "other", 60000) == 1500
    assert best_manual_discount(coupons, "other", 4000) == 300 and best_manual_discount((), "x", 4000) == 0
    # 打ち切り用の上限は店の条件などを見ないので、実際の値引き以上
    for c in coupons:
        for price in (100, 4000, 59999, 60000, 200000):
            assert discount_upper_bound(c, price) >= manual_discount(c, "joshin", price)
    assert discount_upper_bound(coupons[1], 10000) == 500 and discount_upper_bound(coupons[1], 100000) == 1500


def test_manual_coupon_json():
    fixed = ManualCoupon("Joshin 2,000円OFF", "fixed", 2000, min_purchase=60000, valid_until=date(2026, 10, 5))
    assert manual_coupon_json(fixed, 2000) == {
        "source": "manual", "name": "Joshin 2,000円OFF", "type": "fixed", "value": 2000, "discount": 2000,
        "min_purchase": 60000, "valid_until": "2026-10-05"}
    pct = manual_coupon_json(ManualCoupon("p", "percent", D("0.1")), 5478)
    assert pct["value"] == 0.1 and pct["discount"] == 5478 and pct["valid_until"] is None
    json.dumps([manual_coupon_json(fixed, 2000), pct])   # jsonb に保存できる


# ---- 確定判定でのクーポン選び (joshin: 54,780 円、ページの公開クーポン 2,000 円 OFF) ----

def joshin():
    p = parse_item_page(FIXTURES["joshin"]["pageProps"])
    store = Store(p.store_id, p.store_name, "家電", False, D("0"), D("0.14"))
    coupon = parse_usable_coupons(FIXTURES["joshin"]["coupons"])[0]
    detail = parse_coupon_detail(FIXTURES["joshin"]["couponDetail"], p.item_code)
    return p, store, coupon, detail


def candidate(p, buyback_price):
    return {"jan_code": p.jan_code, "store_id": p.store_id, "item_code": p.item_code, "item_url": p.url,
            "item_name": p.name, "category": "家電", "buyback_price": buyback_price,
            "buyback_source": "rudeya", "buyback_date": "2026-10-04", "store_name": p.store_name}


def cfg(*coupons, campaigns=()):
    return CampaignConfig((), tuple(campaigns), BsPlusConfig(), coupons=tuple(coupons))


def test_manual_coupon_wins_when_bigger_and_is_not_combined():
    p, store, page_coupon, detail = joshin()
    manual = ManualCoupon("手持ち 3,000円OFF", "fixed", 3000, min_purchase=50000, target="stores",
                          store_ids=(p.store_id,), valid_until=date(2026, 10, 5))
    row = build_result(candidate(p, 53000), p, store, cfg(manual), page_coupon, detail)
    # 54,780 − 3,000 = 51,780 (税抜 47,073) × 1% = 470pt → 実質 51,310。ページの 2,000 円は足さない
    assert row["coupon_discount"] == 3000 and row["points_total"] == 470 and row["effective_price"] == 51310
    assert row["coupon"] == {"source": "manual", "name": "手持ち 3,000円OFF", "type": "fixed", "value": 3000,
                             "discount": 3000, "min_purchase": 50000, "valid_until": "2026-10-05"}
    assert row["profit"] == 1690


def test_page_coupon_wins_when_bigger():
    p, store, page_coupon, detail = joshin()
    manual = ManualCoupon("手持ち 1,000円OFF", "fixed", 1000)
    row = build_result(candidate(p, 53000), p, store, cfg(manual), page_coupon, detail)
    assert row["coupon_discount"] == 2000 and row["effective_price"] == 52301
    assert row["coupon"]["source"] == "page" and row["coupon"]["fund_type"] == "STORE"


def test_best_of_several_manual_coupons_without_page_coupon():
    p, store, _, _ = joshin()
    coupons = (ManualCoupon("定額", "fixed", 2500),
               ManualCoupon("定率", "percent", D("0.1"), max_discount=4000),      # 5,478 → 上限 4,000
               ManualCoupon("別の店", "fixed", 9000, target="stores", store_ids=("other",)),
               ManualCoupon("高額のみ", "fixed", 8000, min_purchase=60000))
    row = build_result(candidate(p, 53000), p, store, cfg(*coupons), None, None)
    assert row["coupon"]["name"] == "定率" and row["coupon_discount"] == 4000
    assert row["coupon"]["type"] == "percent" and row["coupon"]["value"] == 0.1
    assert row["effective_price"] == 54780 - 4000 - 461      # 税抜 46,164 × 1%


def test_unusable_manual_coupon_does_not_make_it_profitable():
    p, store, _, _ = joshin()
    manual = ManualCoupon("高額のみ", "fixed", 8000, min_purchase=60000)
    assert build_result(candidate(p, 53000), p, store, cfg(manual), None, None) is None    # 実質 54,283 > 53,000


def test_no_coupon_is_chosen_when_coupons_break_min_purchase():
    # クーポンで最低購入額を割るとキャンペーンが外れる → どのクーポンも使わない方が得ならクーポンなしを採る
    p = parse_item_page(FIXTURES["anker"]["pageProps"])       # 3,990 円、ページのクーポン 700 円 OFF
    store = Store(p.store_id, p.store_name, "家電", False, D("0"), D("0.14"))
    page_coupon = parse_usable_coupons(FIXTURES["anker"]["coupons"])[0]
    big = Campaign(PointComponent("big", D("0.30"), min_purchase=3500), "all")
    manual = ManualCoupon("手持ち 600円OFF", "fixed", 600)
    row = build_result(candidate(p, 3500), p, store, cfg(manual, campaigns=[big]), page_coupon, None)
    assert row["coupon"] is None and row["coupon_discount"] == 0
    assert row["points_total"] == 1124 and row["effective_price"] == 2866      # 税抜 3,628 × (30% + 1%)
    # 最低購入額を割らない小さなクーポンなら使う (3,990 − 400 = 3,590 ≥ 3,500)
    small = ManualCoupon("手持ち 400円OFF", "fixed", 400)
    row = build_result(candidate(p, 3500), p, store, cfg(manual, small, campaigns=[big]), page_coupon, None)
    assert row["coupon"]["name"] == "手持ち 400円OFF" and row["coupon_discount"] == 400
