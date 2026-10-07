"""大分類の対応表 (arbitrage/categories.py): products に実在する値を全部網羅する"""

import pytest

from arbitrage.categories import GROUPS, category_group

KS = "kaitorishouten"
KW = "kaitoriwiki"
RD = "rudeya"

CASES = [
    # kaitorishouten
    (KS, "kaden", "家電"), (KS, "keitai", "スマホ・タブレット"), (KS, "nitiyouhin", "お酒"),
    # kaitoriwiki
    (KW, "家電", "家電"), (KW, "パソコン・周辺機器", "パソコン・周辺機器"), (KW, "カメラ", "カメラ"),
    (KW, "ゲーム", "ゲーム"), (KW, "スマートフォン", "スマホ・タブレット"), (KW, "タブレット", "スマホ・タブレット"),
    (KW, "スマートフォン・タブレット", "スマホ・タブレット"), (KW, "化粧品", "化粧品"),
    # rudeya: スマホ・タブレット
    (RD, "iPhone16 Pro", "スマホ・タブレット"), (RD, "iPhone 15", "スマホ・タブレット"), (RD, "iPad Pro", "スマホ・タブレット"),
    (RD, "iPad", "スマホ・タブレット"), (RD, "Google (グーグル)", "スマホ・タブレット"), (RD, "SAMSUNG", "スマホ・タブレット"),
    (RD, "HUAWEI", "スマホ・タブレット"), (RD, "HUAWEI（ファーウェイ）", "スマホ・タブレット"), (RD, "FCNT", "スマホ・タブレット"),
    (RD, "Apple Pencil", "スマホ・タブレット"), (RD, "AirTag", "スマホ・タブレット"),
    # rudeya: パソコン・周辺機器
    (RD, "Mac book", "パソコン・周辺機器"), (RD, "Macデスクトップ", "パソコン・周辺機器"), (RD, "Surface", "パソコン・周辺機器"),
    (RD, "GPU", "パソコン・周辺機器"), (RD, "GPU NVIDIA", "パソコン・周辺機器"), (RD, "メモリー", "パソコン・周辺機器"),
    (RD, "AMD Ryzen", "パソコン・周辺機器"), (RD, "Intel Core", "パソコン・周辺機器"), (RD, "Steam", "パソコン・周辺機器"),
    (RD, "ASUS ROG", "パソコン・周辺機器"), (RD, "Legion Go", "パソコン・周辺機器"), (RD, "Kindle", "パソコン・周辺機器"),
    (RD, "Kindle Paperwhite", "パソコン・周辺機器"),
    # rudeya: カメラ
    (RD, "デジタル一眼カメラ", "カメラ"), (RD, "レンズ", "カメラ"), (RD, "デジタルカメラ", "カメラ"),
    (RD, "チェキ・インスタントカメラ", "カメラ"), (RD, "ビデオカメラ", "カメラ"), (RD, "カメラバッテリー", "カメラ"),
    (RD, "カメラケース", "カメラ"),
    # rudeya: ゲーム
    (RD, "ゲームソフト", "ゲーム"), (RD, "PlayStation", "ゲーム"), (RD, "PlayStation 5", "ゲーム"), (RD, "Xbox", "ゲーム"),
    (RD, "Nintendo Switch", "ゲーム"), (RD, "Nintendo Switch 2", "ゲーム"), (RD, "Meta Quest", "ゲーム"),
    (RD, "PICO 4", "ゲーム"), (RD, "PICO 4 Ultra", "ゲーム"), (RD, "FACEBOOK  VR", "ゲーム"),
    (RD, "FACEBOOK  VR Oculus", "ゲーム"), (RD, "メガドライブミニ", "ゲーム"),
    # rudeya: オーディオ
    (RD, "BOSE", "オーディオ"), (RD, "BOSE スピーカー", "オーディオ"), (RD, "Bluetoothスピーカー", "オーディオ"),
    (RD, "JBL", "オーディオ"), (RD, "audio-technica", "オーディオ"), (RD, "audio-technica ヘッドホン", "オーディオ"),
    (RD, "Shokz", "オーディオ"), (RD, "Shokz OpenRun", "オーディオ"), (RD, "AirPods", "オーディオ"),
    (RD, "AirPods Pro", "オーディオ"), (RD, "HomePod", "オーディオ"), (RD, "Linkbuds", "オーディオ"),
    (RD, "WF-1000XM5", "オーディオ"), (RD, "WH-1000XM5", "オーディオ"), (RD, "デジタルオーディオプレーヤー", "オーディオ"),
    (RD, "デジタルオーディオプレーヤー SONY", "オーディオ"), (RD, "SONY (ソニー)", "オーディオ"),
    # rudeya: ウェアラブル
    (RD, "Apple Watch", "ウェアラブル"), (RD, "Apple Watch Ultra", "ウェアラブル"), (RD, "GARMIN", "ウェアラブル"),
    (RD, "GARMIN Forerunner", "ウェアラブル"), (RD, "Zepp Health", "ウェアラブル"), (RD, "Zepp Health Amazfit", "ウェアラブル"),
    # rudeya: トレカ・ホビー
    (RD, "ワンピースカード", "トレカ・ホビー"), (RD, "ポケモンカード", "トレカ・ホビー"), (RD, "ドラゴンボールカード", "トレカ・ホビー"),
    (RD, "UNION ARENA", "トレカ・ホビー"), (RD, "UNION ARENA ブースター", "トレカ・ホビー"), (RD, "遊戯王", "トレカ・ホビー"),
    (RD, "ベイブレード", "トレカ・ホビー"), (RD, "ボンボンドロップシール", "トレカ・ホビー"), (RD, "スクイーズ", "トレカ・ホビー"),
    (RD, "たまごっち", "トレカ・ホビー"), (RD, "たまごっち ユニ", "トレカ・ホビー"), (RD, "ポケモン(Pokémon)", "トレカ・ホビー"),
    # rudeya: お酒
    (RD, "響", "お酒"), (RD, "竹鶴", "お酒"), (RD, "山崎", "お酒"), (RD, "厚岸", "お酒"), (RD, "白州", "お酒"),
    (RD, "ニッカ", "お酒"), (RD, "マッカラン", "お酒"), (RD, "モエエシャンドン", "お酒"), (RD, "ベルエポック", "お酒"),
    (RD, "ヘネシー", "お酒"), (RD, "ヴーヴ クリコ", "お酒"), (RD, "碧 Ao", "お酒"), (RD, "知多", "お酒"),
    # rudeya: 家電
    (RD, "マッサージ器", "家電"), (RD, "炊飯器", "家電"), (RD, "掃除機", "家電"), (RD, "空気清浄機", "家電"),
    (RD, "ブルーレイ・DVDレコーダ", "家電"), (RD, "カーナビ", "家電"), (RD, "IQOS", "家電"), (RD, "IQOS ILUMA", "家電"),
    (RD, "Fire TV Stick", "家電"), (RD, "Echo", "家電"), (RD, "Echo Dot", "家電"), (RD, "ChromeCast", "家電"),
    (RD, "ChromeCast with Google TV", "家電"), (RD, "NestHub", "家電"), (RD, "NestHub 2", "家電"),
    (RD, "Google Home", "家電"), (RD, "Google Home Mini", "家電"), (RD, "Apple TV", "家電"), (RD, "Nebula", "家電"),
    (RD, "Nebula Capsule", "家電"), (RD, "体重計・体組成計", "家電"), (RD, "ヘアアイロン", "家電"), (RD, "シェーバー", "家電"),
    (RD, "光美容器", "家電"), (RD, "ドライヤー", "家電"), (RD, "温水洗浄便座", "家電"), (RD, "電気ポット", "家電"),
    (RD, "テレビドアホン", "家電"), (RD, "電子レンジ・オーブンレンジ", "家電"), (RD, "SHARP", "家電"),
    (RD, "SHARP 空気清浄機", "家電"), (RD, "CASIO", "家電"), (RD, "CASIO 電子辞書", "家電"), (RD, "充電アクセサリ", "家電"),
    (RD, "ANKER", "家電"), (RD, "ANKER モバイルバッテリー", "家電"), (RD, "ユーティリティー", "家電"),
    # 当たらない値・無い値
    (RD, "新しいカテゴリ", "その他"), (RD, "", "その他"), (RD, None, "その他"),
    (KS, "omocha", "その他"), (KW, "家電 ", "家電"), (KW, "その他", "その他"),
    ("unknown-site", "家電", "その他"), (None, "家電", "その他"), (None, None, "その他"),
]


@pytest.mark.parametrize("source, category, expected", CASES)
def test_category_group(source, category, expected):
    assert category_group(source, category) == expected


def test_groups_are_fixed_and_cover_every_expected_value():
    assert GROUPS == ("家電", "スマホ・タブレット", "パソコン・周辺機器", "カメラ", "ゲーム", "オーディオ", "ウェアラブル",
                      "トレカ・ホビー", "お酒", "化粧品", "その他")
    assert {expected for _, _, expected in CASES} <= set(GROUPS)
    assert category_group(123, 456) == "その他"   # 文字列以外も落ちない


def test_every_kaitoriwiki_scraper_category_is_mapped():
    """kaitoriwiki_scraper が実際に書く category (DOMAIN_TO_CATEGORY) のうち「その他」以外は、すべて大分類に当たること"""
    from kaitoriwiki_scraper import DOMAIN_TO_CATEGORY

    for host, category in DOMAIN_TO_CATEGORY.items():
        if category == "その他":
            continue
        assert category_group(KW, category) != "その他", f"{host}: {category!r} が未対応"
    assert category_group(KW, DOMAIN_TO_CATEGORY["iphonekaitori.tokyo"]) == "スマホ・タブレット"
    assert category_group(KW, DOMAIN_TO_CATEGORY["ipadkaitori.jp"]) == "スマホ・タブレット"
