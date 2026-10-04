"""Yahoo!ショッピングの商品ページ (ログインなし) から、確定判定に必要な情報を読む

- 商品ページ HTML の __NEXT_DATA__ → 価格・JAN・在庫・ポイント内訳
- クーポン: POST /syene-bff/v1/pc/coupon/v3/{店舗ID}/{商品コード}
  (商品ページが発行する匿名 Cookie と page.crumb が必要。同じセッションで送る)
- クーポン詳細ページ → 発行元 (ストア発行か)・最低購入額・値引き上限
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal

import requests

logger = logging.getLogger(__name__)

MIN_INTERVAL = 2.0            # リクエスト間隔 (秒)。1.5 秒以上を空ける
BLOCK_STATUSES = (489, 429, 403)
BLOCK_BACKOFF = 180           # ブロック系ステータスのときの待機秒数 (回数に比例して延ばす)
MAX_BLOCK_RETRIES = 3
MAX_RETRIES = 3
COUPON_URL = "https://store.shopping.yahoo.co.jp/syene-bff/v1/pc/coupon/v3/{store_id}/{item_code}"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
COUPON_DETAIL_RE = re.compile(r"^https://shopping\.yahoo\.co\.jp/coupon/[^/]+/[^/?#]+")
STORE_POINT_TITLE = "ストアポイント"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja",
}


class Blocked(RuntimeError):
    """ブロックが続いた。実行を止める (時間を置いて再開する)"""


class PageFormatError(RuntimeError):
    """ページの構造が想定と違う (サイト側の仕様変更の疑い)"""


@dataclass(frozen=True)
class ItemPage:
    store_id: str
    store_name: str
    item_code: str
    name: str
    url: str
    price: int                    # 販売価格 (税込)
    jan_code: str | None
    in_stock: bool
    is_used: bool
    has_variations: bool          # 色・サイズ違い (SKU) がある
    upsell_rate: Decimal          # ストアポイント上乗せ率 (0.14 = +14%)
    point_titles: tuple[str, ...]  # ポイント内訳に出ている行の名前 (target=page の判定用)
    props: dict = field(compare=False, repr=False, default_factory=dict)


@dataclass(frozen=True)
class Coupon:
    coupon_id: str
    text: str                     # "対象商品2,000円OFFクーポン"
    name: str | None
    discount: int                 # この商品 1 個に適用した値引き額 (円)
    limit_text: str | None
    condition_text: str | None
    end_text: str | None
    detail_url: str

    def to_json(self, detail: dict | None = None) -> dict:
        data = {"id": self.coupon_id, "text": self.text, "name": self.name, "discount": self.discount,
                "limit_text": self.limit_text, "condition_text": self.condition_text,
                "end_text": self.end_text, "url": self.detail_url}
        if detail:
            data.update({k: detail.get(k) for k in ("fund_type", "discount_type", "discount_price",
                                                    "discount_ratio", "price_limit", "price_min")})
        return data


def extract_page_props(html: str) -> dict:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise PageFormatError("__NEXT_DATA__ が無い")
    try:
        return json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError) as e:
        raise PageFormatError(f"__NEXT_DATA__ を読めない: {e}") from e


def _campaign_rows(point: dict, key: str) -> list[dict]:
    return [row for group in point.get(key) or [] for row in group.get("partsCampaignList") or []]


def parse_item_page(props: dict) -> ItemPage:
    """pageProps → ItemPage。必須項目が無ければ PageFormatError"""
    item, seller, point = props.get("item"), props.get("seller"), props.get("point")
    if not isinstance(item, dict) or not isinstance(seller, dict) or not isinstance(point, dict):
        raise PageFormatError("pageProps に item / seller / point が無い")
    price = item.get("applicablePrice")
    if not isinstance(price, int) or price <= 0 or not seller.get("id") or not item.get("srid"):
        raise PageFormatError(f"価格・店舗ID・商品コードを読めない (price={price!r})")

    current = _campaign_rows(point, "currentCampaignList")
    conditional = _campaign_rows(point, "conditionalCampaignList")
    if not any(row.get("title") == STORE_POINT_TITLE for row in current):
        raise PageFormatError("ポイント内訳に「ストアポイント」の行が無い")
    total_ratio = point.get("totalPointRatio")
    if not isinstance(total_ratio, (int, float)):
        raise PageFormatError(f"totalPointRatio を読めない ({total_ratio!r})")
    # 上乗せ率は専用の項目が無く、合計率 − 標準で付く各行の率の合計 として現れる
    upsell_pct = Decimal(str(total_ratio)) - sum(Decimal(str(row.get("ratio") or 0)) for row in current)
    if not 0 <= upsell_pct <= 100:
        raise PageFormatError(f"上乗せ率が想定外 ({upsell_pct}%)")

    stock = item.get("stock") or {}
    in_stock = (bool(stock.get("isAvailable")) and bool(item.get("isOnSale"))
                and stock.get("quantity") != 0
                and (props.get("cartButton") or {}).get("displayState") == "Active")
    return ItemPage(
        store_id=seller["id"],
        store_name=seller.get("name") or "",
        item_code=item["srid"],
        name=item.get("name") or "",
        url=item.get("url") or "",
        price=price,
        jan_code=item.get("janCode") or None,
        in_stock=in_stock,
        is_used=bool(item.get("isUsed")),
        has_variations=bool(item.get("individualItemList")),
        upsell_rate=upsell_pct / 100,
        point_titles=tuple(row.get("title") or "" for row in current + conditional),
        props=props,
    )


def coupon_request_body(props: dict) -> dict:
    item, seller = props["item"], props["seller"]
    fee = (props.get("postage") or {}).get("fee")
    return {
        "crumb": (props.get("page") or {}).get("crumb") or "",
        "price": item["applicablePrice"],
        "subscriptionPrice": item.get("subscriptionPrice"),
        "shippingFee": 1 if fee is None else fee,
        "productCategoryId": item.get("productCategoryId"),
        "genreCategoryIdList": item.get("genreCategoryIds"),
        "brandId": item.get("brandId"),
        "predictionBrandId": item.get("predictionBrandId"),
        "janCode": item.get("janCode"),
        "itemTagList": item.get("itemTagList"),
        "mallItemTagList": item.get("mallItemTagList"),
        "isGoodDelivery": (item.get("delivery") or {}).get("isGoodDelivery"),
        "isNew": False, "isPremium": False,
        "ysrid": f"{seller['id']}_{item.get('sellerManagedItemId')}",
        "firstCookie": "", "isZozoMiniApp": False, "isShpMiniApp": False,
        "isSubscription": bool(item.get("subscriptionPrice")), "isLyLinkage": False,
        "isLineBotRelatedSeller": bool(seller.get("isLineBotRelation")),
        "isSellerOaPointPromotionDisplayed": (props.get("point") or {}).get("promotionType") == "SellerLineFriend",
        "isSellerLineFriend": False, "sellerName": seller.get("name"),
        "isWebView": False, "isCommerceStore": False,
    }


def parse_usable_coupons(data: dict) -> list[Coupon]:
    """1 個の購入ですぐ使える・ログイン不要・値引きがあるクーポンを、値引き額の大きい順に返す"""
    normal = data.get("normal")
    if not isinstance(normal, dict):
        raise PageFormatError("クーポン応答に normal が無い")
    coupons = []
    for c in (normal.get("conditionAchievedCoupons") or {}).get("items") or []:
        discount, detail_url = c.get("itemDiscountPrice"), c.get("detailUrl") or ""
        if c.get("buttonType") != "Normal" or not isinstance(discount, int) or discount <= 0:
            continue
        if not c.get("id") or not COUPON_DETAIL_RE.match(detail_url):
            continue
        coupons.append(Coupon(c["id"], c.get("content") or "", c.get("couponName"), discount,
                              c.get("discountPriceLimitText"), c.get("useConditionText"),
                              c.get("useEndDateText"), detail_url))
    return sorted(coupons, key=lambda c: -c.discount)


def parse_coupon_detail(props: dict, item_code: str) -> dict:
    coupon = (props.get("data") or {}).get("coupon")
    if not isinstance(coupon, dict):
        raise PageFormatError("クーポン詳細に data.coupon が無い")
    discount, condition = coupon.get("discount") or {}, coupon.get("orderCondition") or {}
    targets = (coupon.get("itemModule") or {}).get("targetItemList") or []
    return {
        "fund_type": coupon.get("fundType"),
        "store_id": (coupon.get("store") or {}).get("id"),
        "discount_type": {1: "fixed", 2: "percent"}.get(discount.get("type"), "unknown"),
        "discount_price": discount.get("price"),
        "discount_ratio": discount.get("ratio"),
        "price_limit": discount.get("priceLimit"),
        "price_min": condition.get("priceMin"),
        "count_min": condition.get("countMin"),
        # 参考情報。詳細ページの対象商品リストは一部しか載らない (同じ商品でも取得のたびに
        # 載ったり載らなかったりする) ので、対象かどうかの判定には使わない
        "item_in_targets": any(t.get("smid") == item_code for t in targets) if targets else None,
    }


def coupon_is_valid(detail: dict, page: ItemPage) -> bool:
    """ストア発行で、その店のもので、1 個・この価格で使えるクーポンか。

    その商品が対象かどうかは、クーポン一覧の API が商品単位で絞り込んで返すことで担保されている
    """
    return (detail["fund_type"] == "STORE"
            and detail["store_id"] == page.store_id
            and (detail["count_min"] or 1) <= 1
            and (detail["price_min"] or 0) <= page.price)


class PageClient:
    """直列・一定間隔でアクセスする。Cookie はページが発行した匿名のものだけを持つ"""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._last_request = 0.0
        self.request_count = 0

    def _request(self, method: str, url: str, **kwargs) -> requests.Response | None:
        """応答を返す。404/410 は None (商品が無い)。ブロックが続いたら Blocked"""
        blocks = 0
        attempt = 0
        while True:
            wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            self.request_count += 1
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as e:
                attempt += 1
                logger.warning("page request failed (%d/%d): %s %s", attempt, MAX_RETRIES, url, e)
                if attempt >= MAX_RETRIES:
                    raise PageFormatError(f"通信失敗: {url}") from e
                time.sleep(10 * attempt)
                continue
            if resp.status_code in BLOCK_STATUSES:
                blocks += 1
                if blocks >= MAX_BLOCK_RETRIES:
                    raise Blocked(f"HTTP {resp.status_code} が {blocks} 回続いた: {url}")
                wait = BLOCK_BACKOFF * blocks
                logger.warning("HTTP %d (ブロックの疑い), sleep %ds: %s", resp.status_code, wait, url)
                time.sleep(wait)
                continue
            if resp.status_code in (404, 410):
                return None
            if resp.status_code >= 500:
                attempt += 1
                if attempt >= MAX_RETRIES:
                    raise PageFormatError(f"HTTP {resp.status_code}: {url}")
                time.sleep(10 * attempt)
                continue
            if resp.status_code != 200:
                raise PageFormatError(f"HTTP {resp.status_code}: {url}")
            return resp

    def fetch_item(self, url: str) -> ItemPage | None:
        resp = self._request("GET", url, headers={"Accept": "text/html,application/xhtml+xml"})
        if resp is None:
            return None
        props = extract_page_props(resp.text)
        if not props.get("item"):
            return None   # 販売終了などで商品情報が無いページ
        return parse_item_page(props)

    def fetch_coupons(self, page: ItemPage) -> list[Coupon]:
        resp = self._request(
            "POST", COUPON_URL.format(store_id=page.store_id, item_code=page.item_code),
            data=json.dumps(coupon_request_body(page.props)),
            headers={"Content-Type": "application/json", "Referer": page.url,
                     "Origin": "https://store.shopping.yahoo.co.jp"})
        if resp is None:
            return []
        try:
            return parse_usable_coupons(resp.json())
        except ValueError as e:
            raise PageFormatError("クーポン応答が JSON でない") from e

    def fetch_coupon_detail(self, coupon: Coupon, item_code: str) -> dict | None:
        resp = self._request("GET", coupon.detail_url, headers={"Accept": "text/html"})
        if resp is None:
            return None
        return parse_coupon_detail(extract_page_props(resp.text), item_code)
