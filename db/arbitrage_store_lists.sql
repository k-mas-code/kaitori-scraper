-- ============================================================
-- 店舗リスト (data/yahoo_pp_stores_5genres.xlsx) のスナップショット
--
-- python -m tools.yahoo_pp.refresh (service_role キー) が店舗リストを作り直すたびに 1 行 INSERT する。
-- 結果ページの「店舗リスト」タブ (docs/arbitrage-stores.js) は id 降順の 1 行だけを読む。
-- db/arbitrage_research.sql (is_arbitrage_owner() / arbitrage_research_requests) の後に実行する。再実行可能。
-- ============================================================

create table if not exists arbitrage_store_lists (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),
  fetched_on    date not null,                 -- 取得日
  period_start  date not null,                 -- 掲載期間 (一覧ページに出ていた最初の日)
  period_end    date not null,                 -- 掲載期間 (最後の日)
  -- 掲載日の ISO 文字列の配列。stores[].pat の各文字に対応する
  dates         jsonb not null check (jsonb_typeof(dates) = 'array'),
  -- {"total", "qualified", "excluded", "gold", "surveyed", "by_genre": {ジャンル名: {"qualified", "gold", "excluded"}}}
  summary       jsonb not null check (jsonb_typeof(summary) = 'object'),
  -- 1 要素 = 1 店 (非適格も含む。inv で区別):
  --   {"id", "name", "genre", "cat", "gold", "inv": "Y"|"N"|"?", "reg",
  --    "max_upsell" (%, 未調査は null), "unknown", "n", "n_up", "reviews": [星5..星1] or null,
  --    "days", "d9", "d4", "first", "last", "pat": "9-4..."}
  stores        jsonb not null check (jsonb_typeof(stores) = 'array'),
  source_sha    text                           -- xlsx の sha256 先頭 16 桁 (無ければ null)
);

create index if not exists idx_arbitrage_store_lists_id_desc on arbitrage_store_lists (id desc);

alter table arbitrage_store_lists enable row level security;

-- オーナーは読むだけ。書くのは service_role (refresh) だけ。anon には権限を付けない (ポリシーも無い)
revoke all on arbitrage_store_lists from anon, authenticated;
grant select on arbitrage_store_lists to authenticated;
grant select, insert on arbitrage_store_lists to service_role;

drop policy if exists "owner read arbitrage_store_lists" on arbitrage_store_lists;
create policy "owner read arbitrage_store_lists"
  on arbitrage_store_lists for select to authenticated
  using ((select is_arbitrage_owner()));
