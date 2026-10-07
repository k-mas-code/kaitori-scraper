import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from arbitrage.config import BsPlusConfig, CampaignConfig, StoreUpsell
from arbitrage.finalize import build_result, final_components
from arbitrage.stores import Store
from arbitrage.yahoo_page import parse_item_page

D = Decimal
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "yahoo_pages.json").read_text(encoding="utf-8"))


def joshin_page():
    p = parse_item_page(FIXTURES["joshin"]["pageProps"])        # 54,780 円、ページの上乗せ 0%
    return p, Store(p.store_id, p.store_name, "家電", False, D("0"), D("0.14"))


def cfg(*upsell):
    return CampaignConfig((), (), BsPlusConfig(), store_upsell=tuple(upsell))


def store_point(components):
    return next(c.rate for c in components if c.name == "ストアポイント")


def test_store_upsell_setting_is_used_when_page_shows_less():
    # ログインしないと上乗せが出ない店: ページ 0% でも設定 14% なら ストアポイント 15%
    p, store = joshin_page()
    assert p.upsell_rate == 0
    assert store_point(final_components(store, cfg(), p)) == D("0.01")
    assert store_point(final_components(store, cfg(StoreUpsell(p.store_id, D("0.14"))), p)) == D("0.15")
    # 別の店の設定は使わない。店舗リストの最大上乗せ (絞り込み用) も使わない
    assert store_point(final_components(store, cfg(StoreUpsell("other", D("0.14"))), p)) == D("0.01")


def test_page_upsell_wins_when_larger_and_rates_are_not_added():
    p, store = joshin_page()
    higher = replace(p, upsell_rate=D("0.20"))
    assert store_point(final_components(store, cfg(StoreUpsell(p.store_id, D("0.14"))), higher)) == D("0.21")


def test_build_result_with_store_upsell():
    p, store = joshin_page()
    candidate = {"jan_code": p.jan_code, "store_id": p.store_id, "item_code": p.item_code, "item_url": p.url,
                 "item_name": p.name, "category": "家電", "buyback_price": 48000,
                 "buyback_source": "rudeya", "buyback_date": "2026-10-04", "store_name": p.store_name}
    assert build_result(candidate, p, store, cfg(), None, None) is None       # 1% だけ: 実質 54,282 > 48,000
    upsell = StoreUpsell(p.store_id, D("0.14"), valid_from=date(2026, 10, 8))
    row = build_result(candidate, p, store, cfg(upsell), None, None)
    # 税抜 49,800 × 15% = 7,470pt → 実質 47,310
    assert row["points_total"] == 7470 and row["effective_price"] == 47310 and row["profit"] == 690
    assert [(c["name"], c["rate"]) for c in row["points"]] == [("ストアポイント", 0.15)]
