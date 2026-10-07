from datetime import date
from decimal import Decimal

import pytest

import arbitrage.run as run_module
from arbitrage.buyback import Buyback
from arbitrage.config import BsPlusConfig, CampaignConfig, ConfigError, ManualCoupon, active_config
from arbitrage.prefilter import max_price_rate
from arbitrage.run import (load_settings, parse_args, print_config, search_one_jan, search_variants,
                           select_targets)
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


def test_select_targets_filters_by_category_and_drops_uncategorized():
    items = [Buyback("1", 100, "rudeya", "d", "家電"), Buyback("2", 200, "rudeya", "d", "ゲーム"),
             Buyback("3", 300, "rudeya", "d", "その他"), Buyback("4", 400, "rudeya", "d", None)]   # 4 = 古い run の保存分
    assert [b.jan_code for b in select_targets(items, None, None, None)] == ["4", "3", "2", "1"]
    assert [b.jan_code for b in select_targets(items, None, None, None, [])] == ["4", "3", "2", "1"]
    assert [b.jan_code for b in select_targets(items, None, None, None, ["家電"])] == ["1"]
    assert [b.jan_code for b in select_targets(items, None, None, None, ["ゲーム", "家電"])] == ["2", "1"]
    assert [b.jan_code for b in select_targets(items, None, None, None, ["その他"])] == ["3"]   # None は「その他」に入れない
    assert [b.jan_code for b in select_targets(items, 150, None, None, ["ゲーム", "家電"])] == ["2"]


# ---- 設定の読み込み元と実行日 ----

def test_date_defaults_to_none_so_purchase_date_is_used():
    assert parse_args([]).date is None and parse_args([]).config_file is None
    args = parse_args(["--date", "2026-10-05", "--config-file", "x.yaml"])
    assert args.date == date(2026, 10, 5) and str(args.config_file) == "x.yaml"


def test_load_settings_from_file(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("version: 1\npurchase_date: 2026-10-11\n", encoding="utf-8")
    settings, source = load_settings(path)
    assert settings.purchase_date == date(2026, 10, 11) and "ファイル" in source and "s.yaml" in source


def test_load_settings_from_supabase(monkeypatch):
    config = {"version": 1, "purchase_date": "2026-10-11"}
    monkeypatch.setattr(run_module.db, "get_client", lambda: "client")
    monkeypatch.setattr(run_module.db, "fetch_settings", lambda client: config)
    settings, source = load_settings(None)
    assert settings.purchase_date == date(2026, 10, 11) and "Supabase" in source
    # Supabase に保存された設定が不正なら、どこの設定かを添えて止める
    monkeypatch.setattr(run_module.db, "fetch_settings", lambda client: {**config, "version": 9})
    with pytest.raises(ConfigError, match="Supabase の設定.*version"):
        load_settings(None)


def test_load_settings_without_credentials_suggests_config_file(monkeypatch):
    def no_client():
        raise run_module.db.DbConfigError("SUPABASE_URL / SUPABASE_KEY が未設定")
    monkeypatch.setattr(run_module.db, "get_client", no_client)
    with pytest.raises(ConfigError, match="--config-file"):
        load_settings(None)


def test_check_config_shows_source_date_and_active_items(tmp_path, capsys):
    path = tmp_path / "s.yaml"
    path.write_text(
        "version: 1\npurchase_date: 2026-10-11\n"
        "common:\n  - {name: card, rate: 0.01, base: tax_included}\n"
        "campaigns:\n"
        "  - {name: 日曜, rate: 0.05, dates: [2026-10-04, 2026-10-11]}\n"
        "  - {name: 加算, rate: 0.02, target: bsplus, dates: [2026-10-05]}\n"
        "coupons:\n"
        "  - {name: 手持ち, type: fixed, value: 2000, min_purchase: 60000, target: stores, store_ids: [zz]}\n"
        "  - {name: 期限切れ, type: percent, value: 0.1, valid_until: 2026-10-05}\n"
        "store_upsell:\n"
        "  - {store_id: s1, rate: 0.14, note: ログイン時のみ, valid_from: 2026-10-01, valid_until: 2026-10-05}\n"
        "  - {store_id: zz, rate: 0.09}\n", encoding="utf-8")
    settings, source = load_settings(path)
    stores = StoreList(STORES.stores, date(2026, 10, 4), None, "x.xlsx")
    print_config(settings, active_config(settings, date(2026, 10, 4)), stores, source)
    out = capsys.readouterr().out
    assert "s.yaml" in out and "購入予定日 (設定): 2026-10-11 / 実行日: 2026-10-04 ← --date で上書き" in out
    assert "日曜: 5%" in out and "加算" not in out and "日付外で無効: 1 件" in out
    assert "手持ち: 2,000円OFF、60,000円以上" in out and "店舗リストに無い店ID: zz" in out
    assert "期限切れ: 10%OFF" in out and "期間外で無効: 0 件" in out
    assert "[store_upsell]" in out
    assert "- s1: 上乗せ 14% (ログイン時のみ)、期間 2026-10-01 〜 2026-10-05" in out
    assert "- zz: 上乗せ 9%、期間 指定なし 〜 指定なし  ※店舗リストに無い店ID" in out
    print_config(settings, active_config(settings, date(2026, 10, 11)),
                 StoreList(STORES.stores, date(2026, 10, 11), None, "x.xlsx"), source)
    out = capsys.readouterr().out
    assert "上書き" not in out and "期限切れ" not in out and "期間外で無効: 1 件" in out
    assert "- s1: 上乗せ" not in out and "- zz: 上乗せ 9%" in out


def test_manual_coupon_keeps_paging_alive():
    # 手持ちクーポン (15,000 円 OFF) があると、余地 20% では打ち切られる価格帯も読み続ける
    hits = [hit(8000 + i * 100, f"s1_{i}") for i in range(200)]     # 8,000〜27,900 円
    coupon = ManualCoupon("big", "fixed", 15000, min_purchase=25000)
    cfg = CampaignConfig((), (), BsPlusConfig(), coupons=(coupon,))
    client = FakeClient({"4900000000000": hits})
    b = Buyback("4900000000000", 9000, "rudeya", "2026-10-04")
    record = search_one_jan(client, b, cfg, STORES, 0.2, max_price_rate(cfg, STORES))
    assert len(client.calls) == 4
    assert any(c["sale_price"] >= 25000 for c in record["candidates"])
