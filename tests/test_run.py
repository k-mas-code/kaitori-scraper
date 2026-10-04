from datetime import date
from decimal import Decimal

from arbitrage.buyback import Buyback
from arbitrage.config import BsPlusConfig, CampaignConfig
from arbitrage.prefilter import max_price_rate
from arbitrage.run import search_one_jan, search_variants, select_targets
from arbitrage.stores import Store, StoreList

STORES = StoreList({"s1": Store("s1", "s1", "家電", False, Decimal("0.04"), Decimal("0.14"))},
                   date(2026, 10, 4), None, "x.xlsx")
CFG = CampaignConfig((), (), BsPlusConfig())


def hit(price, code):
    return {"name": "item", "url": "https://store.shopping.yahoo.co.jp/s1/a.html", "code": code,
            "price": price, "inStock": True, "condition": "new", "seller": {"sellerId": "s1", "name": "店"}}


class FakeClient:
    """価格昇順の検索結果を 50 件ずつ返す"""

    def __init__(self, hits_by_jan):
        self.hits_by_jan, self.calls = hits_by_jan, []

    def search_jan(self, jan_code, start=1):
        self.calls.append((jan_code, start))
        hits = self.hits_by_jan.get(jan_code, [])
        return {"totalResultsAvailable": len(hits), "hits": hits[start - 1:start - 1 + 50]}


def run(client, jan="4900000000000", price=9000):
    b = Buyback(jan, price, "rudeya", "2026-10-04")
    return search_one_jan(client, b, CFG, STORES, 0.2, max_price_rate(CFG, STORES))


def test_search_variants():
    assert search_variants("4900000000000") == ["4900000000000"]
    assert search_variants("840353956209") == ["0840353956209"]       # 12 桁のままだと API が 400 を返す


def test_paging_stops_when_price_can_no_longer_pass():
    hits = [hit(8000 + i * 100, f"s1_{i}") for i in range(200)]     # 8,000〜27,900 円
    client = FakeClient({"4900000000000": hits})
    record = run(client)
    assert len(client.calls) < 4                                    # 全 4 ページは読まない
    assert record["candidates"] and all(c["sale_price"] < 16000 for c in record["candidates"])
    assert record["hits_total"] == 200


def test_all_pages_are_read_while_prices_can_pass():
    hits = [hit(8000, f"s1_{i}") for i in range(120)]
    client = FakeClient({"4900000000000": hits})
    record = run(client)
    assert [c[1] for c in client.calls] == [1, 51, 101] and len(record["candidates"]) == 120


def test_upc_is_searched_as_zero_padded_jan():
    client = FakeClient({"0840353956209": [hit(8000, "s1_a")]})
    record = run(client, jan="840353956209")
    assert [c[0] for c in client.calls] == ["0840353956209"]
    assert record["jan_code"] == "840353956209" and len(record["candidates"]) == 1


def test_select_targets_filters_by_price_and_spreads_limit():
    items = [Buyback(f"{i:013d}", i * 100, "rudeya", "2026-10-04") for i in range(1, 1001)]   # 100〜100,000 円
    in_range = select_targets(items, 5000, 60000, None)
    assert len(in_range) == 551 and in_range[0].price == 60000 and in_range[-1].price == 5000
    picked = select_targets(items, 5000, 60000, 100)
    assert len(picked) == 100 and len({b.jan_code for b in picked}) == 100
    assert picked[0].price == 60000 and picked[-1].price < 6000          # 価格帯の上から下まで選ばれる
    assert select_targets(items, None, None, 5000) == select_targets(items, None, None, None)
