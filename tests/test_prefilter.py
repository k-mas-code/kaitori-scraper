from datetime import date
from decimal import Decimal

from arbitrage.buyback import Buyback
from arbitrage.config import BsPlusConfig, Campaign, CampaignConfig
from arbitrage.points import PointComponent
from arbitrage.prefilter import (campaign_applies, max_global_bonus, max_total_rate,
                                 price_can_never_pass, select_candidates, top_genre)
from arbitrage.stores import Store, StoreList

D = Decimal


def store(store_id="s1", good=False, bsplus="0", upsell="0"):
    return Store(store_id, store_id, "家電", good, D(bsplus), D(upsell))


def store_list(*stores, labels=None):
    return StoreList({s.store_id: s for s in stores}, date(2026, 10, 4), labels, "x.xlsx")


def cfg(campaigns=(), common=()):
    return CampaignConfig(tuple(common), tuple(campaigns), BsPlusConfig())


def hit(seller="s1", price=10000, code="s1_a", in_stock=True, condition="new"):
    return {"name": "item", "url": f"https://store.shopping.yahoo.co.jp/{seller}/a.html", "code": code,
            "price": price, "inStock": in_stock, "condition": condition,
            "seller": {"sellerId": seller, "name": "店"},
            "parentGenreCategories": [{"depth": 2, "name": "掃除機"}, {"depth": 1, "name": "家電"}]}


BUYBACK = Buyback("4900000000000", 9000, "rudeya", "2026-10-04")


def test_campaign_targets():
    comp = PointComponent("c", D("0.05"))
    s = store(good=True, bsplus="0.04")
    assert campaign_applies(Campaign(comp, "all"), store(), None)
    assert campaign_applies(Campaign(comp, "bsplus"), s, None) and not campaign_applies(Campaign(comp, "bsplus"), store(), None)
    assert campaign_applies(Campaign(comp, "good_store"), s, None) and not campaign_applies(Campaign(comp, "good_store"), store(), None)
    assert campaign_applies(Campaign(comp, ("s1",)), store(), None) and not campaign_applies(Campaign(comp, ("zz",)), store(), None)
    page = Campaign(comp, "page", page_title="日曜日")
    assert campaign_applies(page, store(), None)                       # 未取得なら甘く通す
    assert campaign_applies(page, store(), ["プレミアムな日曜日 ＋5％"])
    assert not campaign_applies(page, store(), ["ストアポイント"])


def test_max_global_bonus():
    assert max_global_bonus(None) == 0 and max_global_bonus("+2") == D("0.02") and max_global_bonus("+2/+3") == D("0.03")


def test_select_candidates_filters_store_stock_condition_and_price():
    stores = store_list(store("s1", bsplus="0.09", upsell="0.14"))
    hits = [
        hit(price=10000),                              # 通る: 10000×0.8 − 税抜9091×24% = 8000 − 2181 < 9000
        hit(price=10000),                              # 同じ商品コードの重複
        hit(seller="other", code="other_a"),           # リストに無い店
        hit(code="s1_b", in_stock=False),              # 在庫なし
        hit(code="s1_c", condition="used"),            # 中古
        hit(code="s1_d", price=20000),                 # 高すぎる
    ]
    got = select_candidates(hits, BUYBACK, cfg(), stores, 0.2)
    assert [c["item_code"] for c in got] == ["s1_a"]
    assert got[0]["category"] == "家電" and got[0]["store_id"] == "s1" and got[0]["buyback_source"] == "rudeya"


def test_price_can_never_pass_is_consistent_with_candidates():
    stores = store_list(store("s1", bsplus="0.09", upsell="0.14"), labels="+2/+3")
    c = cfg(common=[PointComponent("p", D("0.03"))])
    rate = max_total_rate(c, stores)
    assert rate == D("0.03") + D("0.01") + D("0.14") + D("0.09") + D("0.03")
    for price in range(9000, 20000, 250):
        if price_can_never_pass(price, BUYBACK.price, rate, 0.2):
            assert select_candidates([hit(price=price)], BUYBACK, c, stores, 0.2) == []


def test_top_genre_falls_back():
    assert top_genre({"genreCategory": {"name": "x"}}) == "x" and top_genre({}) is None
