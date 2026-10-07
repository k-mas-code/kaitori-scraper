"""商品分類: 買取店ごとにばらばらな products.category を、共通の大分類 (GROUPS) に寄せる。純粋関数のみ。

products の主キーは jan_code だけで、category は最後に書いたサイトの値 (CLAUDE.md の「落とし穴」)。
同じ JAN でもサイトによって分類が変わりうるので、対応表はサイト (source) ごとに持つ。
どの規則にも当たらない値 (新しいカテゴリなど) は落とさず「その他」にする。
結果ページの JS (docs/arbitrage.js) も同じ GROUPS を同じ順序で持つ。
"""

from __future__ import annotations

GROUP_KADEN = "家電"
GROUP_MOBILE = "スマホ・タブレット"
GROUP_PC = "パソコン・周辺機器"
GROUP_CAMERA = "カメラ"
GROUP_GAME = "ゲーム"
GROUP_AUDIO = "オーディオ"
GROUP_WEARABLE = "ウェアラブル"
GROUP_HOBBY = "トレカ・ホビー"
GROUP_LIQUOR = "お酒"
GROUP_COSME = "化粧品"
GROUP_OTHER = "その他"

GROUPS: tuple[str, ...] = (GROUP_KADEN, GROUP_MOBILE, GROUP_PC, GROUP_CAMERA, GROUP_GAME, GROUP_AUDIO,
                           GROUP_WEARABLE, GROUP_HOBBY, GROUP_LIQUOR, GROUP_COSME, GROUP_OTHER)

# サイトごとの完全一致の表
EXACT: dict[str, dict[str, str]] = {
    "kaitorishouten": {"kaden": GROUP_KADEN, "keitai": GROUP_MOBILE, "nitiyouhin": GROUP_LIQUOR},
    "kaitoriwiki": {"家電": GROUP_KADEN, "パソコン・周辺機器": GROUP_PC, "カメラ": GROUP_CAMERA,
                    "ゲーム": GROUP_GAME, "化粧品": GROUP_COSME,
                    # kaitoriwiki_scraper.DOMAIN_TO_CATEGORY の実在値 (iphonekaitori.tokyo / ipadkaitori.jp)
                    "スマートフォン": GROUP_MOBILE, "タブレット": GROUP_MOBILE,
                    "スマートフォン・タブレット": GROUP_MOBILE},
}

# rudeya: 完全一致 (表記ゆれは正規化後の値で引く)
RUDEYA_EXACT: dict[str, str] = {
    # スマホ・タブレット
    "Google (グーグル)": GROUP_MOBILE, "SAMSUNG": GROUP_MOBILE, "HUAWEI": GROUP_MOBILE,
    "FCNT": GROUP_MOBILE, "Apple Pencil": GROUP_MOBILE, "AirTag": GROUP_MOBILE,
    # パソコン・周辺機器
    "Mac book": GROUP_PC, "Macデスクトップ": GROUP_PC, "Surface": GROUP_PC, "メモリー": GROUP_PC,
    "AMD Ryzen": GROUP_PC, "Intel Core": GROUP_PC, "Steam": GROUP_PC, "ASUS ROG": GROUP_PC, "Legion Go": GROUP_PC,
    # カメラ
    "デジタル一眼カメラ": GROUP_CAMERA, "レンズ": GROUP_CAMERA, "デジタルカメラ": GROUP_CAMERA,
    "チェキ・インスタントカメラ": GROUP_CAMERA, "ビデオカメラ": GROUP_CAMERA, "カメラバッテリー": GROUP_CAMERA,
    "カメラケース": GROUP_CAMERA,
    # ゲーム
    "ゲームソフト": GROUP_GAME, "Xbox": GROUP_GAME, "Meta Quest": GROUP_GAME, "メガドライブミニ": GROUP_GAME,
    # オーディオ
    "Bluetoothスピーカー": GROUP_AUDIO, "JBL": GROUP_AUDIO, "HomePod": GROUP_AUDIO, "Linkbuds": GROUP_AUDIO,
    "SONY (ソニー)": GROUP_AUDIO,
    # トレカ・ホビー
    "ワンピースカード": GROUP_HOBBY, "ポケモンカード": GROUP_HOBBY, "ドラゴンボールカード": GROUP_HOBBY,
    "遊戯王": GROUP_HOBBY, "ベイブレード": GROUP_HOBBY, "ボンボンドロップシール": GROUP_HOBBY,
    "スクイーズ": GROUP_HOBBY, "ポケモン(Pokémon)": GROUP_HOBBY,
    # お酒
    "響": GROUP_LIQUOR, "竹鶴": GROUP_LIQUOR, "山崎": GROUP_LIQUOR, "厚岸": GROUP_LIQUOR, "白州": GROUP_LIQUOR,
    "ニッカ": GROUP_LIQUOR, "マッカラン": GROUP_LIQUOR, "モエエシャンドン": GROUP_LIQUOR, "ベルエポック": GROUP_LIQUOR,
    "ヘネシー": GROUP_LIQUOR, "ヴーヴ クリコ": GROUP_LIQUOR, "碧 Ao": GROUP_LIQUOR, "知多": GROUP_LIQUOR,
    # 家電
    "マッサージ器": GROUP_KADEN, "炊飯器": GROUP_KADEN, "掃除機": GROUP_KADEN, "空気清浄機": GROUP_KADEN,
    "ブルーレイ・DVDレコーダ": GROUP_KADEN, "カーナビ": GROUP_KADEN, "Fire TV Stick": GROUP_KADEN,
    "Apple TV": GROUP_KADEN, "体重計・体組成計": GROUP_KADEN, "ヘアアイロン": GROUP_KADEN, "シェーバー": GROUP_KADEN,
    "光美容器": GROUP_KADEN, "ドライヤー": GROUP_KADEN, "温水洗浄便座": GROUP_KADEN, "電気ポット": GROUP_KADEN,
    "テレビドアホン": GROUP_KADEN, "電子レンジ・オーブンレンジ": GROUP_KADEN, "充電アクセサリ": GROUP_KADEN,
    "ユーティリティー": GROUP_KADEN,
}

# rudeya: 前方一致 (長いものを先に見る必要は無い。どれも同じ大分類に行くよう重複しないキーにしてある)
RUDEYA_PREFIX: tuple[tuple[str, str], ...] = (
    ("iPhone", GROUP_MOBILE), ("iPad", GROUP_MOBILE), ("HUAWEI", GROUP_MOBILE),
    ("GPU", GROUP_PC), ("Kindle", GROUP_PC),
    ("PlayStation", GROUP_GAME), ("Nintendo Switch", GROUP_GAME), ("PICO 4", GROUP_GAME), ("FACEBOOK  VR", GROUP_GAME),
    ("BOSE", GROUP_AUDIO), ("audio-technica", GROUP_AUDIO), ("Shokz", GROUP_AUDIO), ("AirPods", GROUP_AUDIO),
    ("WF-", GROUP_AUDIO), ("WH-", GROUP_AUDIO), ("デジタルオーディオプレーヤー", GROUP_AUDIO),
    ("Apple Watch", GROUP_WEARABLE), ("GARMIN", GROUP_WEARABLE), ("Zepp Health", GROUP_WEARABLE),
    ("UNION ARENA", GROUP_HOBBY), ("たまごっち", GROUP_HOBBY),
    ("IQOS", GROUP_KADEN), ("Echo", GROUP_KADEN), ("ChromeCast", GROUP_KADEN), ("NestHub", GROUP_KADEN),
    ("Google Home", GROUP_KADEN), ("Nebula", GROUP_KADEN), ("SHARP", GROUP_KADEN), ("CASIO", GROUP_KADEN),
    ("ANKER", GROUP_KADEN),
)


def _normalize(text: str) -> str:
    """全角括弧・全角スペースを半角に寄せ、前後の空白を落とす (「HUAWEI（ファーウェイ）」などの表記ゆれ用)"""
    return text.replace("（", " (").replace("）", ")").replace("　", " ").strip()


def _rudeya_group(category: str) -> str:
    text = _normalize(category)
    if text in RUDEYA_EXACT:
        return RUDEYA_EXACT[text]
    lowered = text.lower()
    for prefix, group in RUDEYA_PREFIX:
        if lowered.startswith(prefix.lower()):
            return group
    return GROUP_OTHER


def category_group(source: str | None, category: str | None) -> str:
    """(source, category) → 大分類。どれにも当たらなければ「その他」(None / 空も同じ)"""
    if not isinstance(category, str) or not category.strip():
        return GROUP_OTHER
    if source == "rudeya":
        return _rudeya_group(category)
    return EXACT.get(source or "", {}).get(category.strip(), GROUP_OTHER)
