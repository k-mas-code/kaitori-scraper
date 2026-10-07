import copy
import json
from datetime import date
from decimal import Decimal

import pytest

from arbitrage.config import (ROOT, ConfigError, active_campaigns, active_config, active_coupons,
                              active_store_upsell, load_settings_file, parse_settings)

D = Decimal


def base() -> dict:
    """仕様どおりの設定 JSON (テストごとに一部を書き換える)"""
    return {
        "version": 1,
        "purchase_date": "2026-10-11",
        "common": [
            {"name": "LYP", "rate": 0.02, "base": "tax_excluded", "cap_per_order": None,
             "cap_per_period": 5000, "min_purchase": 0},
        ],
        "campaigns": [
            {"name": "日曜", "rate": 0.05, "base": "tax_excluded", "cap_per_order": None, "cap_per_period": 3000,
             "min_purchase": 5000, "entry_required": True, "target": "all", "page_title": None, "store_ids": [],
             "dates": ["2026-10-04", "2026-10-11", "2026-10-18"]},
            {"name": "全体加算+2", "rate": 0.02, "target": "bsplus", "dates": ["2026-10-05"]},
            {"name": "全体加算+3", "rate": 0.03, "target": "bsplus_good", "dates": ["2026-10-05"]},
        ],
        "coupons": [
            {"name": "Joshin 2,000円OFF", "type": "fixed", "value": 2000, "max_discount": None,
             "min_purchase": 60000, "target": "stores", "store_ids": ["joshin"],
             "valid_from": None, "valid_until": "2026-10-05"},
            {"name": "モール 10%", "type": "percent", "value": 0.1, "max_discount": 3000,
             "valid_from": "2026-10-10", "valid_until": "2026-10-12"},
            {"name": "いつでも", "type": "fixed", "value": 300},
        ],
        "store_upsell": [
            {"store_id": "denkichiweb", "rate": 0.14, "note": "ログイン時のみ表示",
             "valid_from": "2026-10-08", "valid_until": "2026-10-31"},
            {"store_id": "denkichiweb", "rate": 0.09, "valid_until": "2026-10-07"},    # 期間が重ならない同じ店
            {"store_id": "joshin", "rate": 0.05},
        ],
        "bsplus": {"cap_per_order": None, "cap_per_period": None},
    }


def changed(path: list, value) -> dict:
    """base() の path の位置を value にした設定 (value が DELETE ならキーを消す)"""
    raw = base()
    node = raw
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return raw


DELETE = object()


def test_valid_settings_are_parsed():
    s = parse_settings(base())
    assert s.purchase_date == date(2026, 10, 11)
    assert s.common[0].rate == D("0.02") and s.common[0].cap == 5000
    sunday = s.campaigns[0]
    assert sunday.component.min_purchase == 5000 and sunday.entry_required and sunday.target == "all"
    assert sunday.dates == (date(2026, 10, 4), date(2026, 10, 11), date(2026, 10, 18))
    assert [c.target for c in s.campaigns] == ["all", "bsplus", "bsplus_good"]
    joshin, mall, anytime = s.coupons
    assert (joshin.type, joshin.value, joshin.min_purchase, joshin.store_ids) == ("fixed", 2000, 60000, ("joshin",))
    assert joshin.valid_from is None and joshin.valid_until == date(2026, 10, 5)
    assert (mall.type, mall.value, mall.max_discount, mall.target) == ("percent", D("0.1"), 3000, "all")
    assert anytime.min_purchase == 0 and anytime.valid_until is None
    assert s.bsplus.cap is None and s.raw == base()
    denkichi, old, joshin_up = s.store_upsell
    assert (denkichi.store_id, denkichi.rate, denkichi.note) == ("denkichiweb", D("0.14"), "ログイン時のみ表示")
    assert denkichi.valid_from == date(2026, 10, 8) and denkichi.valid_until == date(2026, 10, 31)
    assert old.valid_from is None and old.valid_until == date(2026, 10, 7) and old.note is None
    assert joshin_up.rate == D("0.05") and joshin_up.valid_from is None and joshin_up.valid_until is None


def test_cap_is_min_of_order_and_period_and_null_min_purchase_is_zero():
    raw = changed(["common", 0], {"name": "a", "rate": 1, "cap_per_order": 3000, "cap_per_period": 1000,
                                  "min_purchase": None, "base": "tax_included"})
    c = parse_settings(raw).common[0]
    assert c.cap == 1000 and c.min_purchase == 0 and c.base == "tax_included" and c.rate == 1
    assert parse_settings(changed(["bsplus"], {"cap_per_order": 800, "cap_per_period": 2000})).bsplus.cap == 800


def test_sections_can_be_omitted():
    s = parse_settings({"version": 1, "purchase_date": "2026-10-11"})
    assert s.common == () and s.campaigns == () and s.coupons == () and s.bsplus.cap is None
    assert s.store_upsell == () and active_config(s, s.purchase_date).store_upsell == ()


@pytest.mark.parametrize("path,value,message", [
    # version / purchase_date / トップレベル
    (["version"], 2, "version"),
    (["version"], DELETE, "version"),
    (["version"], "1", "version"),
    (["purchase_date"], DELETE, "purchase_date"),
    (["purchase_date"], "2026/10/11", "YYYY-MM-DD"),
    (["purchase_date"], "2026-02-30", "YYYY-MM-DD"),
    (["extra"], 1, "不明なキー"),
    (["campaigns"], {"name": "x"}, "リスト"),
    # 不明なキー (綴り間違いを黙って通さない)
    (["common", 0, "cap"], 3000, "不明なキー"),
    (["campaigns", 0, "min_purchace"], 5000, "不明なキー"),
    (["coupons", 0, "discount"], 100, "不明なキー"),
    (["bsplus", "cap"], 100, "不明なキー"),
    (["bsplus", "good_store_bonus_stacks"], True, "不明なキー"),     # 廃止したキー
    (["common", 0, "dates"], ["2026-10-11"], "不明なキー"),          # common に日付は書けない
    # rate の範囲
    (["common", 0, "rate"], 5, "rate"),                              # 5% を 5 と書いた
    (["common", 0, "rate"], 0, "rate"),
    (["common", 0, "rate"], -0.01, "rate"),
    (["common", 0, "rate"], "0.05", "rate"),
    (["common", 0, "rate"], True, "rate"),
    (["common", 0, "rate"], DELETE, "rate"),
    # base / 上限 / 最低購入額 / name
    (["common", 0, "base"], "gross", "base"),
    (["common", 0, "cap_per_period"], -1, "cap_per_period"),
    (["common", 0, "cap_per_order"], 1.5, "cap_per_order"),
    (["campaigns", 0, "min_purchase"], "5000", "min_purchase"),
    (["common", 0, "name"], "", "name"),
    (["campaigns", 0, "name"], DELETE, "name"),
    (["campaigns", 0, "name"], "LYP", "重複"),                       # common と campaigns を通して重複不可
    (["coupons", 1, "name"], "いつでも", "重複"),
    (["campaigns", 0, "entry_required"], "no", "entry_required"),
    # dates の形式・重複・空
    (["campaigns", 0, "dates"], DELETE, "dates"),
    (["campaigns", 0, "dates"], [], "dates"),
    (["campaigns", 0, "dates"], "2026-10-11", "dates"),
    (["campaigns", 0, "dates"], ["10/11"], "YYYY-MM-DD"),
    (["campaigns", 0, "dates"], ["2026-10-32"], "YYYY-MM-DD"),
    (["campaigns", 0, "dates"], ["2026-10-11", "2026-10-11"], "同じ日付"),
    # target ごとの必須項目
    (["campaigns", 0, "target"], "everyone", "target"),
    (["campaigns", 0, "target"], ["joshin"], "store_ids"),           # 旧形式 (target にリスト)
    (["campaigns", 0, "target"], "page", "page_title"),
    (["campaigns", 0, "target"], "stores", "store_ids"),
    (["campaigns", 0, "store_ids"], "joshin", "store_ids"),
    (["campaigns", 0, "store_ids"], [""], "store_ids"),
    (["campaigns", 0, "page_title"], 5, "page_title"),
    # クーポンの type / value
    (["coupons", 0, "type"], "yen", "type"),
    (["coupons", 0, "type"], DELETE, "type"),
    (["coupons", 0, "value"], 0, "value"),
    (["coupons", 0, "value"], 0.5, "value"),                         # fixed は整数
    (["coupons", 0, "value"], DELETE, "value"),
    (["coupons", 0, "max_discount"], 1000, "max_discount"),          # fixed では null
    (["coupons", 1, "value"], 10, "value"),                          # 10% を 10 と書いた
    (["coupons", 1, "value"], 0, "value"),
    (["coupons", 1, "max_discount"], -1, "max_discount"),
    (["coupons", 0, "min_purchase"], -1, "min_purchase"),
    (["coupons", 0, "target"], "bsplus", "target"),                  # クーポンは all / stores のみ
    (["coupons", 0, "store_ids"], [], "store_ids"),
    (["coupons", 0, "valid_until"], "10/05", "YYYY-MM-DD"),
    (["coupons", 1, "valid_from"], "2026-10-13", "valid_from"),      # 開始が終了より後
    # 店ごとの上乗せ
    (["store_upsell"], {"store_id": "x"}, "リスト"),
    (["store_upsell", 0], "denkichiweb", "マッピング"),
    (["store_upsell", 0, "store_id"], DELETE, "store_id"),
    (["store_upsell", 0, "store_id"], "", "store_id"),
    (["store_upsell", 0, "store_id"], 5, "store_id"),
    (["store_upsell", 0, "rate"], DELETE, "rate"),
    (["store_upsell", 0, "rate"], 14, "rate"),                       # 14% を 14 と書いた
    (["store_upsell", 0, "rate"], 0, "rate"),
    (["store_upsell", 0, "rate"], 1.5, "rate"),
    (["store_upsell", 0, "rate"], "0.14", "rate"),
    (["store_upsell", 0, "upsell"], 0.14, "不明なキー"),
    (["store_upsell", 0, "name"], "デンキチ", "不明なキー"),
    (["store_upsell", 0, "note"], 1, "note"),
    (["store_upsell", 0, "valid_from"], "10/08", "YYYY-MM-DD"),
    (["store_upsell", 0, "valid_from"], "2026-11-01", "valid_from"),  # 開始が終了より後
    (["store_upsell", 1, "valid_until"], "2026-10-08", "重複"),      # 同じ店で期間が重なる
    (["store_upsell", 1, "valid_until"], None, "重複"),
    (["store_upsell", 2, "store_id"], "denkichiweb", "重複"),        # 期間なし = 常に重なる
    (["store_upsell", 2, "store_id"], " denkichiweb ", "重複"),      # 前後の空白は無視
])
def test_invalid_settings_are_rejected(path, value, message):
    with pytest.raises(ConfigError, match=message):
        parse_settings(changed(path, value))


def test_error_names_the_place():
    with pytest.raises(ConfigError, match=r"campaigns\[1\] \(全体加算\+2\)"):
        parse_settings(changed(["campaigns", 1, "rate"], 2))
    with pytest.raises(ConfigError, match=r"coupons\[0\] \(Joshin 2,000円OFF\)"):
        parse_settings(changed(["coupons", 0, "value"], -5))
    with pytest.raises(ConfigError, match=r"store_upsell\[0\] \(denkichiweb\)"):
        parse_settings(changed(["store_upsell", 0, "rate"], 14))
    with pytest.raises(ConfigError):
        parse_settings([])


def test_target_specific_fields_are_accepted():
    page = parse_settings(changed(["campaigns", 0], {"name": "p", "rate": 0.05, "target": "page",
                                                     "page_title": "日曜日", "dates": ["2026-10-11"]})).campaigns[0]
    assert page.target == "page" and page.page_title == "日曜日"
    shops = parse_settings(changed(["campaigns", 0], {"name": "s", "rate": 0.05, "target": "stores",
                                                      "store_ids": ["joshin", "yamada"],
                                                      "dates": ["2026-10-11"]})).campaigns[0]
    assert shops.target == "stores" and shops.store_ids == ("joshin", "yamada")


def test_filtering_by_date():
    s = parse_settings(base())
    names = lambda items: [getattr(c, "component", c).name for c in items]   # noqa: E731
    assert names(active_campaigns(s, date(2026, 10, 11))) == ["日曜"]
    assert names(active_campaigns(s, date(2026, 10, 5))) == ["全体加算+2", "全体加算+3"]
    assert names(active_campaigns(s, date(2026, 10, 12))) == []
    # クーポンの有効期間は両端を含む。null は制限なし
    assert names(active_coupons(s, date(2026, 10, 5))) == ["Joshin 2,000円OFF", "いつでも"]
    assert names(active_coupons(s, date(2026, 10, 6))) == ["いつでも"]
    assert names(active_coupons(s, date(2026, 10, 10))) == ["モール 10%", "いつでも"]
    assert names(active_coupons(s, date(2026, 10, 12))) == ["モール 10%", "いつでも"]
    assert names(active_coupons(s, date(2026, 10, 13))) == ["いつでも"]
    # 店ごとの上乗せも有効期間 (両端を含む) で絞る。同じ店は期間違いで 1 件に決まる
    rates = lambda day: {u.store_id: u.rate for u in active_store_upsell(s, day)}   # noqa: E731
    assert rates(date(2026, 10, 7)) == {"denkichiweb": D("0.09"), "joshin": D("0.05")}
    assert rates(date(2026, 10, 8)) == {"denkichiweb": D("0.14"), "joshin": D("0.05")}
    assert rates(date(2026, 10, 31)) == {"denkichiweb": D("0.14"), "joshin": D("0.05")}
    assert rates(date(2026, 11, 1)) == {"joshin": D("0.05")}


def test_active_config_keeps_only_that_day():
    s = parse_settings(base())
    cfg = active_config(s, date(2026, 10, 11))
    assert [c.component.name for c in cfg.campaigns] == ["日曜"] and cfg.common == s.common
    assert [c.name for c in cfg.coupons] == ["モール 10%", "いつでも"]
    assert [(u.store_id, u.rate) for u in cfg.store_upsell] == [("denkichiweb", D("0.14")), ("joshin", D("0.05"))]
    assert cfg.upsell_rate("denkichiweb") == D("0.14") and cfg.upsell_rate("unknown") == 0
    # raw は同じ仕様の JSON で、その日に有効な分だけ (保存・再開時の一致チェック用)
    assert cfg.raw["purchase_date"] == "2026-10-11" and cfg.raw["version"] == 1
    assert [c["name"] for c in cfg.raw["campaigns"]] == ["日曜"]
    assert [c["name"] for c in cfg.raw["coupons"]] == ["モール 10%", "いつでも"]
    assert [(u["store_id"], u["rate"]) for u in cfg.raw["store_upsell"]] == [("denkichiweb", 0.14), ("joshin", 0.05)]
    assert parse_settings(cfg.raw).campaigns == cfg.campaigns
    assert parse_settings(cfg.raw).store_upsell == cfg.store_upsell
    # 日付外の項目だけを変えても、その日の設定 (raw) は変わらない
    edited = copy.deepcopy(base())
    edited["campaigns"][1]["rate"] = 0.5
    edited["purchase_date"] = "2026-10-18"
    edited["store_upsell"][1]["rate"] = 0.3                       # 10-07 までの行
    assert active_config(parse_settings(edited), date(2026, 10, 11)).raw == cfg.raw
    # 期間違いの行が選ばれる日は、その行だけが raw に残る
    assert [u["rate"] for u in active_config(s, date(2026, 10, 7)).raw["store_upsell"]] == [0.09, 0.05]
    # 再開条件はキーの有無ではなく中身で決める: その日に有効な行が無ければ raw からキーごと省く。
    # store_upsell を省いた設定 (この機能より前に始めた run の meta.config) と、設定画面の保存で必ず書かれる
    # store_upsell: [] と、期間外の行だけの設定は、どれも同じ raw になる
    without = copy.deepcopy(base()); del without["store_upsell"]
    empty = copy.deepcopy(base()); empty["store_upsell"] = []
    expired = copy.deepcopy(base())
    expired["store_upsell"] = [{"store_id": "denkichiweb", "rate": 0.09, "valid_until": "2026-10-07"}]
    raws = [active_config(parse_settings(x), date(2026, 10, 11)).raw for x in (without, empty, expired)]
    assert raws[0] == raws[1] == raws[2] and "store_upsell" not in raws[0]
    assert active_config(parse_settings(empty), date(2026, 10, 11)).store_upsell == ()
    # --date で別の日にすれば、その日の分になる
    other = active_config(s, date(2026, 10, 5))
    assert [c.component.name for c in other.campaigns] == ["全体加算+2", "全体加算+3"]
    assert other.raw["purchase_date"] == "2026-10-05"


def test_example_file_is_valid():
    s = load_settings_file(ROOT / "config" / "campaigns.example.yaml")
    assert s.common and s.campaigns and s.coupons and s.store_upsell
    cfg = active_config(s, s.purchase_date)
    assert cfg.campaigns and cfg.coupons and cfg.store_upsell
    json.dumps(cfg.raw)   # YAML が日付として読んだ値も、保存できる形 (文字列) になっている


def test_settings_file_yaml_and_json(tmp_path):
    y = tmp_path / "s.yaml"
    y.write_text("version: 1\npurchase_date: 2026-10-11\ncampaigns:\n"
                 "  - {name: a, rate: 0.05, dates: [2026-10-11, '2026-10-18']}\n", encoding="utf-8")
    s = load_settings_file(y)
    assert s.purchase_date == date(2026, 10, 11) and s.campaigns[0].dates == (date(2026, 10, 11), date(2026, 10, 18))
    assert s.raw["purchase_date"] == "2026-10-11" and s.raw["campaigns"][0]["dates"] == ["2026-10-11", "2026-10-18"]
    j = tmp_path / "s.json"
    j.write_text(json.dumps(base(), ensure_ascii=False), encoding="utf-8")
    assert load_settings_file(j) == parse_settings(base())


def test_settings_file_errors_name_the_file(tmp_path):
    with pytest.raises(ConfigError, match="が無い"):
        load_settings_file(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\n  purchase_date: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bad.yaml"):
        load_settings_file(bad)
    old = tmp_path / "old.yaml"   # 旧形式 (version / purchase_date なし)
    old.write_text("common:\n  - {name: a, rate: 0.01}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="old.yaml.*version"):
        load_settings_file(old)
    empty = tmp_path / "empty.json"
    empty.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty.json"):
        load_settings_file(empty)
