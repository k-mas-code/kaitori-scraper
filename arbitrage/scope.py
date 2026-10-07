"""確定判定の範囲と順序: 届きそうな候補だけを、届きやすい順に確認する。純粋関数のみ。

背景 (2026-10-04 の実測): 絞り込みはクーポン余地 20% 込みの甘い判定なので、候補の 7〜8 割は
ポイント上限などで黒字に届かない。甘い利益額の順に確認すると、届かない高額品ばかり先に見てしまう。
→ クーポン余地なしの見積もり (prefilter.realistic_gap) で「届きそう」かを分け、販売価格に対する比率の順に並べる。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CampaignConfig
from .prefilter import realistic_gap
from .stores import Store

SCOPE_REACHABLE = "reachable"   # 届きそうな候補だけ確認する (既定)
SCOPE_ALL = "all"               # 全候補を確認する (届きそうな候補を先に)
PAGE_SCOPES = (SCOPE_REACHABLE, SCOPE_ALL)
DEFAULT_REACH_SLACK = 0.03      # 公開クーポンなどで縮まると見込む幅 (販売価格に対する比率)
MAX_REACH_SLACK = 0.2


@dataclass(frozen=True)
class PagePlan:
    ordered: tuple[dict, ...]   # 確認する候補 (確認する順)
    reachable: int              # 届きそうな候補の数
    out_of_scope: int           # 範囲外として確認しない候補の数 (scope=all なら 0)


def is_reachable(gap: float, sale_price: int, reach_slack: float, min_profit: int) -> bool:
    """届きそうか: 現実的な見積もりの利益 + 販売価格 × 余裕 が、求める利益 (黒字のみなら 0) 以上"""
    return gap + sale_price * reach_slack >= min(min_profit, 0)


def plan_page_checks(candidates: list[dict], stores: dict[str, Store], cfg: CampaignConfig,
                     page_scope: str, reach_slack: float, min_profit: int) -> PagePlan:
    """候補を「届きそう」と「それ以外」に分け、それぞれ 見積もりの利益 / 販売価格 の大きい順に並べる"""
    if page_scope not in PAGE_SCOPES:
        raise ValueError(f"未対応の page_scope: {page_scope!r}")
    reachable: list[tuple[tuple, dict]] = []
    others: list[tuple[tuple, dict]] = []
    for c in candidates:
        store = stores.get(c["store_id"])
        # 店舗リストに無い店の候補は本来存在しない。あれば絞り込み時の甘い見積もりで代用する
        gap = (realistic_gap(c, store, cfg) if store is not None
               else c["buyback_price"] - c["optimistic_effective_price"])
        key = (-gap / c["sale_price"], c["store_id"], c["item_code"])   # 同率は店・商品コード順で固定
        (reachable if is_reachable(gap, c["sale_price"], reach_slack, min_profit) else others).append((key, c))
    ordered = [c for _, c in sorted(reachable, key=lambda pair: pair[0])]
    if page_scope == SCOPE_ALL:
        ordered += [c for _, c in sorted(others, key=lambda pair: pair[0])]
    return PagePlan(tuple(ordered), len(reachable), len(candidates) - len(ordered))
