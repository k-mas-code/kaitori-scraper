from datetime import date
from decimal import Decimal

import pytest

from arbitrage.buyback import Buyback
from arbitrage.config import BsPlusConfig, Campaign, CampaignConfig, ManualCoupon
from arbitrage.points import PointComponent
from arbitrage.points import TAX_INCLUDED, optimistic_effective_price
from arbitrage.prefilter import (campaign_applies, max_price_rate, optimistic_components,
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
    in_list, not_in_list = Campaign(comp, "stores", store_ids=("s1",)), Campaign(comp, "stores", store_ids=("zz",))
    assert campaign_applies(in_list, store(), None) and not campaign_applies(not_in_list, store(), None)
    page = Campaign(comp, "page", page_title="日曜日")
    assert campaign_applies(page, store(), None)                       # 未取得なら甘く通す
    assert campaign_applies(page, store(), ["プレミアムな日曜日 ＋5％"])
    assert not campaign_applies(page, store(), ["ストアポイント"])


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


def test_bsplus_good_needs_both_slot_and_good_store():
    c = Campaign(PointComponent("c", D("0.03")), "bsplus_good")
    assert campaign_applies(c, store(good=True, bsplus="0.04"), None)
    assert not campaign_applies(c, store(good=True), None)             # 優良だが枠が無い
    assert not campaign_applies(c, store(bsplus="0.09"), None)         # 枠はあるが優良でない


def test_global_bonus_is_two_independent_campaigns():
    # 全体加算日の +2% (枠のある店) と +3% (枠があり優良) は別々の上限を持つ 2 行。優良ストアには両方付く。
    # 店舗リスト 1 行目の表記 (labels) は計算に使わない
    c = cfg(campaigns=[Campaign(PointComponent("plus2", D("0.02"), cap=100), "bsplus"),
                       Campaign(PointComponent("plus3", D("0.03"), cap=5000), "bsplus_good")])
    def names(s):
        return [x.name for x in optimistic_components(s, c)]
    assert names(store(good=True, bsplus="0.09")) == ["plus2", "plus3", "ストアポイント", "ボーナスストアPlus"]
    assert names(store(bsplus="0.04")) == ["plus2", "ストアポイント", "ボーナスストアPlus"]
    assert names(store(good=True)) == ["ストアポイント"]
    rates = {x.name: x.rate for x in optimistic_components(store(good=True, bsplus="0.09"), c)}
    assert rates["ボーナスストアPlus"] == D("0.09")                     # 枠の率そのまま (加算を混ぜない)
    stores = store_list(store(good=True, bsplus="0.09", upsell="0.14"), labels="+2/+3")
    assert max_price_rate(c, stores) == max_price_rate(c, store_list(store(good=True, bsplus="0.09", upsell="0.14")))


def test_manual_coupon_widens_prefilter():
    # 10,000 円: 余地 0 なら 10,000 − ストアポイント 税抜9,091×1% = 9,910 で通らない。
    # 手持ちクーポン 1,500 円 OFF があれば 8,410 で通る (その店・最低購入額を満たすときだけ)
    stores = store_list(store("s1"), store("s2"))
    coupon = ManualCoupon("c", "fixed", 1500, min_purchase=10000, target="stores", store_ids=("s1",))
    c = CampaignConfig((), (), BsPlusConfig(), coupons=(coupon,))
    hits = [hit(price=10000), hit(seller="s2", code="s2_a", price=10000), hit(code="s1_b", price=9999)]
    got = select_candidates(hits, BUYBACK, c, stores, 0.0)
    assert [(x["item_code"], x["optimistic_effective_price"]) for x in got] == [("s1_a", 8410)]
    # 値引きは「余地」と手持ちクーポンの大きい方 (足さない)
    assert optimistic_effective_price(10000, [], 0.2, 1500) == 8000
    assert optimistic_effective_price(10000, [], 0.1, 1500) == 8500


CUTOFF_COUPON_SETS = [
    (),
    (ManualCoupon("fixed", "fixed", 2000, min_purchase=12000, target="stores", store_ids=("s2",)),),
    (ManualCoupon("big", "fixed", 30000, min_purchase=60000),
     ManualCoupon("pct", "percent", D("0.35"), max_discount=4000, min_purchase=5000)),
    (ManualCoupon("pct-free", "percent", D("0.5"), target="stores", store_ids=("s1",)),),
]


@pytest.mark.parametrize("coupons", CUTOFF_COUPON_SETS)
def test_price_cutoff_never_drops_a_passing_price(coupons):
    # 打ち切り判定が True になった価格以上では、どの店でも絞り込みを通らない
    # (税込ベースの枠・全体加算のキャンペーン・手持ちクーポンの定額/定率込み)
    stores = store_list(store("s1", good=True, bsplus="0.09", upsell="0.14"), store("s2"))
    c = CampaignConfig(
        (PointComponent("card", D("0.03"), base=TAX_INCLUDED), PointComponent("lyp", D("0.02"))),
        (Campaign(PointComponent("sun", D("0.05")), "page", page_title="日曜"),
         Campaign(PointComponent("plus2", D("0.02")), "bsplus"),
         Campaign(PointComponent("plus3", D("0.03")), "bsplus_good")),
        BsPlusConfig(), coupons=coupons)
    rate = max_price_rate(c, stores)
    for buyback_price in (900, 9000, 90000):
        b = Buyback("4900000000000", buyback_price, "rudeya", "2026-10-04")
        cut = False
        for price in range(buyback_price // 2, buyback_price * 4, max(1, buyback_price // 300)):
            cut = cut or price_can_never_pass(price, buyback_price, rate, 0.2, coupons)
            if cut:   # 一度打ち切ったら、それより高い価格はすべて読まれない
                for s in stores.stores.values():
                    assert select_candidates([hit(seller=s.store_id, code=f"{s.store_id}_a", price=price)],
                                             b, c, stores, 0.2) == []
        # 大きな値引きのクーポンが無ければ、この価格帯のどこかで打ち切りが働く (判定が常に False ではない)
        assert cut or coupons in CUTOFF_COUPON_SETS[2:]


def test_price_cutoff_waits_for_coupon_that_becomes_usable_later():
    # 買取 50,000 円。55,000 円の時点では通らないが、60,000 円以上で 30,000 円 OFF が使えるので打ち切らない
    coupon = ManualCoupon("big", "fixed", 30000, min_purchase=60000)
    assert price_can_never_pass(55000, 50000, 0.01, 0.0)
    assert not price_can_never_pass(55000, 50000, 0.01, 0.0, (coupon,))
    assert not price_can_never_pass(80000, 50000, 0.01, 0.0, (coupon,))      # 80,000 − 30,000 < 50,000
    assert price_can_never_pass(81000, 50000, 0.01, 0.0, (coupon,))


def test_top_genre_falls_back():
    assert top_genre({"genreCategory": {"name": "x"}}) == "x" and top_genre({}) is None
