"""--check-config の表示: 設定をどう解釈したかを、人が読める形で出す"""

from __future__ import annotations

from .config import CampaignConfig, Settings
from .stores import StoreList

TARGET_LABELS = {"all": "すべての店", "bsplus": "その日ボーナスストアPlus枠がある店",
                 "bsplus_good": "その日ボーナスストアPlus枠があり、かつ優良ストア", "good_store": "優良ストア"}


def describe_stores(store_ids: tuple[str, ...], stores: StoreList) -> str:
    text = "指定の店: " + ", ".join(store_ids)
    unknown = [s for s in store_ids if s not in stores.stores]
    return text + (f"  ※店舗リストに無い店ID: {', '.join(unknown)}" if unknown else "")


def describe_coupon(c) -> str:
    if c.type == "fixed":
        value = f"{c.value:,}円OFF"
    else:
        limit = "" if c.max_discount is None else f" (値引き上限 {c.max_discount:,}円)"
        value = f"{float(c.value) * 100:g}%OFF{limit}"
    minimum = purchase_range_text(c)
    period = f"、期間 {c.valid_from or '指定なし'} 〜 {c.valid_until or '指定なし'}"
    return f"{value}{minimum}{period}"


def purchase_range_text(c) -> str:
    """最低・最高購入額の表示 (例: 「、5,000〜19,999円」)"""
    if getattr(c, "max_purchase", None) is not None:   # 手持ちクーポンには最高購入額が無い
        return f"、{c.min_purchase:,}〜{c.max_purchase:,}円"
    return f"、{c.min_purchase:,}円以上" if c.min_purchase else ""


def print_config(settings: Settings, cfg: CampaignConfig, stores: StoreList, source: str) -> None:
    """設定をどう解釈したかを、人が読める形で表示する"""
    def describe(c) -> str:
        cap = "上限なし" if c.cap is None else f"上限 {c.cap:,}pt"
        base = "税込" if c.base == "tax_included" else "税抜"
        minimum = purchase_range_text(c)
        return f"{float(c.rate) * 100:g}% ({base}価格に対して、{cap}{minimum})"

    print(f"設定の読み込み元: {source}")
    override = "" if stores.run_date == settings.purchase_date else " ← --date で上書き"
    print(f"購入予定日 (設定): {settings.purchase_date} / 実行日: {stores.run_date}{override}")
    print(f"店舗リスト: {stores.source_file}")
    print("\n[common] 毎日付く分" + ("" if cfg.common else " (なし)"))
    for c in cfg.common:
        print(f"  - {c.name}: {describe(c)}")
    print(f"\n[campaigns] {stores.run_date} に有効なキャンペーン" + ("" if cfg.campaigns else " (なし)"))
    for c in cfg.campaigns:
        if c.target == "page":
            target = f"商品ページの内訳に「{c.page_title}」を含む行が出ている店"
        elif c.target == "stores":
            target = describe_stores(c.store_ids, stores)
        else:
            target = TARGET_LABELS[c.target]
        entry = "、要エントリー" if c.entry_required else ""
        print(f"  - {c.component.name}: {describe(c.component)}{entry}\n      対象: {target}")
    print(f"  日付外で無効: {len(settings.campaigns) - len(cfg.campaigns)} 件")
    print(f"\n[coupons] {stores.run_date} に使える手持ちクーポン (1 注文 1 枚、併用なし)"
          + ("" if cfg.coupons else " (なし)"))
    for c in cfg.coupons:
        target = describe_stores(c.store_ids, stores) if c.target == "stores" else TARGET_LABELS["all"]
        print(f"  - {c.name}: {describe_coupon(c)}\n      対象: {target}")
    print(f"  期間外で無効: {len(settings.coupons) - len(cfg.coupons)} 件")
    print(f"\n[store_upsell] {stores.run_date} に有効な店ごとのポイント上乗せ (ログイン時だけ表示される分。"
          "ページの率と大きい方を使う)" + ("" if cfg.store_upsell else " (なし)"))
    for u in cfg.store_upsell:
        unknown = "" if u.store_id in stores.stores else "  ※店舗リストに無い店ID"
        note = f" ({u.note})" if u.note else ""
        print(f"  - {u.store_id}: 上乗せ {float(u.rate) * 100:g}%{note}、"
              f"期間 {u.valid_from or '指定なし'} 〜 {u.valid_until or '指定なし'}{unknown}")
    print(f"  期間外で無効: {len(settings.store_upsell) - len(cfg.store_upsell)} 件")
    slots = sum(1 for s in stores.stores.values() if s.bsplus_rate > 0)
    cap = "上限なし" if cfg.bsplus.cap is None else f"上限 {cfg.bsplus.cap:,}pt"
    print(f"\n[bsplus] ボーナスストアPlus: {cap}。この日の枠あり {slots} 店 / 全 {len(stores.stores)} 店")
    if stores.global_bonus_labels:
        print(f"  店舗リストではこの日は全体加算日 ({stores.global_bonus_labels})。加算分は自動では付けないので、"
              "campaigns に target=bsplus / bsplus_good の行があるか確認する")
    print("\n[自動で読む分] ストアポイント 1% + 上乗せ (商品ページ)、ボーナスストアPlus の率 (店舗リスト)")
    print("\n設定に問題は見つかりませんでした。")
