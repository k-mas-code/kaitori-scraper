import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from arbitrage.config import BsPlusConfig, Campaign, CampaignConfig
from arbitrage.finalize import (NOT_PROFITABLE, SKIP_JAN_MISMATCH, SKIP_USED, SKIP_VARIATIONS, build_result,
                                final_components, same_jan, skip_reason)
from arbitrage.points import PointComponent
from arbitrage.stores import Store, StoreList
from arbitrage.yahoo_page import (PageFormatError, coupon_is_valid, extract_page_props, parse_coupon_detail,
                                  parse_item_page, parse_usable_coupons)

D = Decimal
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "yahoo_pages.json").read_text(encoding="utf-8"))


def page(tag):
    return parse_item_page(FIXTURES[tag]["pageProps"])


def coupons(tag):
    return parse_usable_coupons(FIXTURES[tag]["coupons"])


def store_for(p, bsplus="0", good=False):
    return Store(p.store_id, p.store_name, "家電", good, D(bsplus), D("0.14"))


def stores_of(*stores, labels=None):
    return StoreList({s.store_id: s for s in stores}, date(2026, 10, 4), labels, "x.xlsx")


def candidate(p, buyback_price):
    return {"jan_code": p.jan_code, "store_id": p.store_id, "item_code": p.item_code, "item_url": p.url,
            "item_name": p.name, "category": "家電", "buyback_price": buyback_price,
            "buyback_source": "rudeya", "buyback_date": "2026-10-04", "store_name": p.store_name}


@pytest.mark.parametrize("tag,price,upsell", [
    ("digimart", 58018, "0"), ("joshin", 54780, "0"), ("dyson", 53799, "0.11"),
    ("try3", 64870, "0.01"), ("kksshop", 7460, "0.14"),
])
def test_price_and_upsell_rate(tag, price, upsell):
    p = page(tag)
    assert p.price == price and p.upsell_rate == D(upsell) and p.in_stock


def test_flags():
    assert page("janpara").is_used and not page("digimart").is_used
    assert page("anker").has_variations and not page("joshin").has_variations
    assert page("kksshop").jan_code is None
    assert any("ボーナスストアPlus" in t for t in page("digimart").point_titles)


def test_stock_rules():
    def with_stock(**stock):
        props = json.loads(json.dumps(FIXTURES["joshin"]["pageProps"]))
        props["item"]["stock"].update(stock)
        return parse_item_page(props)
    assert with_stock(quantity=None).in_stock                                   # 在庫数を出さない店
    assert with_stock(quantity=0, isReserveOverdraft=True).in_stock             # 取り寄せ (カートに入れられる)
    assert not with_stock(quantity=0, isReserveOverdraft=False).in_stock        # 売り切れ
    assert not with_stock(isAvailable=False).in_stock


def test_extract_page_props_and_format_errors():
    html = '<html><script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"a":1}}}</script></html>'
    assert extract_page_props(html) == {"a": 1}
    with pytest.raises(PageFormatError):
        extract_page_props("<html></html>")
    with pytest.raises(PageFormatError):
        parse_item_page({"item": {}, "seller": {}, "point": {}})


def test_usable_coupons_exclude_login_and_unachieved():
    assert coupons("digimart") == []                                   # 初回アプリ限定 (Login) だけ
    assert [c.discount for c in coupons("joshin")] == [2000]
    assert coupons("dyson") == []                                      # 2 個以上が条件 → 未達側のみ
    assert [c.discount for c in coupons("anker")] == [700]


def test_coupon_detail_validation():
    p = page("joshin")
    detail = parse_coupon_detail(FIXTURES["joshin"]["couponDetail"], p.item_code)
    assert detail["fund_type"] == "STORE" and detail["discount_type"] == "fixed" and coupon_is_valid(detail, p)
    assert not coupon_is_valid({**detail, "fund_type": "MALL"}, p)
    assert not coupon_is_valid({**detail, "store_id": "other"}, p)
    assert not coupon_is_valid({**detail, "price_min": p.price + 1}, p)
    assert coupon_is_valid({**detail, "item_in_targets": False}, p)     # 対象商品リストは一部しか載らない
    dyson = parse_coupon_detail(FIXTURES["dyson"]["couponDetail"], page("dyson").item_code)
    assert dyson["discount_type"] == "percent" and not coupon_is_valid(dyson, page("dyson"))   # countMin=2


def test_skip_reasons():
    p = page("joshin")
    assert skip_reason(candidate(p, 1), p) is None
    assert skip_reason(candidate(p, 1), None) == "gone"
    assert skip_reason({**candidate(p, 1), "jan_code": "4900000000000"}, p) == SKIP_JAN_MISMATCH
    assert skip_reason(candidate(page("janpara"), 1), page("janpara")) == SKIP_USED
    assert skip_reason(candidate(page("anker"), 1), page("anker")) == SKIP_VARIATIONS
    assert same_jan("840353956209", "0840353956209") and not same_jan(None, "1")


def test_final_components_use_page_upsell_and_list_bsplus():
    p = page("dyson")
    s = store_for(p, bsplus="0.04")
    cfg = CampaignConfig((PointComponent("card", D("0.01")),),
                         (Campaign(PointComponent("sun", D("0.05"), cap=3000), "page", page_title="プレミアムな日曜日"),
                          Campaign(PointComponent("none", D("0.5")), "page", page_title="存在しない行")),
                         BsPlusConfig())
    comps = {c.name: c.rate for c in final_components(s, cfg, stores_of(s), p)}
    assert comps == {"card": D("0.01"), "sun": D("0.05"), "ストアポイント": D("0.12"), "ボーナスストアPlus": D("0.04")}


def test_build_result_with_coupon():
    # joshin: 54,780 円 − クーポン 2,000 = 52,780 (税抜 47,982)。ストアポイント 1% = 479pt → 実質 52,301
    p = page("joshin")
    s = store_for(p)
    cfg = CampaignConfig((), (), BsPlusConfig())
    c = coupons("joshin")[0]
    detail = parse_coupon_detail(FIXTURES["joshin"]["couponDetail"], p.item_code)
    row = build_result(candidate(p, 53000), p, s, cfg, stores_of(s), c, detail)
    assert row["sale_price"] == 54780 and row["coupon_discount"] == 2000 and row["points_total"] == 479
    assert row["effective_price"] == 52301 and row["profit"] == 699
    assert row["profit_rate"] == round(699 / 52301, 4)
    assert row["coupon"]["fund_type"] == "STORE" and row["coupon"]["discount"] == 2000
    assert build_result(candidate(p, 52301), p, s, cfg, stores_of(s), c, detail) is None     # 利益 0 は載せない


def test_coupon_is_not_used_when_it_breaks_min_purchase():
    # クーポンで最低購入額を割るとキャンペーンが外れる → 使わない方が得ならクーポンなしを採る
    p = page("anker")       # 3,990 円、700 円 OFF クーポン
    s = store_for(p)
    cfg = CampaignConfig((), (Campaign(PointComponent("big", D("0.30"), min_purchase=3500), "all"),), BsPlusConfig())
    row = build_result(candidate(p, 3500), p, s, cfg, stores_of(s), coupons("anker")[0], None)
    assert row["coupon_discount"] == 0 and row["coupon"] is None
    assert row["points_total"] == 1124 and row["effective_price"] == 2866      # 税抜 3,628 × (30% + 1%)


def test_all_coupons_include_unachieved_and_login_only():
    from arbitrage.yahoo_page import parse_all_coupons
    dyson = parse_all_coupons(FIXTURES["dyson"]["coupons"])
    assert [(c["text"], c["achieved"], c["login_required"], c["item_discount"]) for c in dyson] == [
        ("ストア内全品50%OFFクーポン", True, True, 0),
        ("対象商品8,900円OFFクーポン", False, False, 8900),
        ("対象商品10％OFFクーポン", False, False, 5379),
    ]
    assert dyson[1]["condition_text"] == "対象商品2個以上の購入"


def test_coupon_report_groups_by_store_and_coupon():
    from arbitrage.coupon_report import ALL_STORES, build_coupon_rows
    from arbitrage.yahoo_page import parse_all_coupons
    records = [
        {"store_id": tag_store, "store_name": tag_store, "item_name": f"item-{i}", "item_url": f"https://x/{i}",
         "sale_price": 1000 * (i + 1), "coupons_seen": parse_all_coupons(FIXTURES[tag]["coupons"])}
        for i, (tag, tag_store) in enumerate([("dyson", "dyson"), ("dyson", "dyson"), ("joshin", "joshin"),
                                              ("digimart", "digimart-shop")])
    ] + [{"store_id": "old", "status": "not_profitable"}]            # coupons_seen が無い古い記録
    rows = build_coupon_rows(records)
    assert [(r["店舗ID"], r["クーポン"]) for r in rows] == [
        ("dyson", "対象商品8,900円OFFクーポン"), ("dyson", "対象商品10％OFFクーポン"),
        ("joshin", "対象商品2,000円OFFクーポン"), (ALL_STORES, "ストア内全品50%OFFクーポン"),
    ]
    assert rows[0]["条件未達の商品数"] == 2 and rows[0]["1個で使えた商品数"] == 0 and rows[0]["最大値引き額"] == 8900
    assert rows[2]["1個で使えた商品数"] == 1 and rows[2]["ログイン"] == "不要"
    assert rows[3]["ログイン"] == "必要" and rows[3]["1個で使えた商品数"] == 4
