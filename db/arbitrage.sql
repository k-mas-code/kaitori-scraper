-- ============================================================
-- Yahoo!ショッピング → 買取店 の利益商品 (arbitrage) 用スキーマ
--
-- 既存の products / price_history (anon に公開) とは別に、
-- 「ログインしたオーナー 1 人だけが読める」テーブルを 2 つ追加する。
-- 書き込みは service_role キー (RLS をバイパス) のみ。
--
-- 手順 (Supabase Dashboard の SQL Editor 専用):
--   1. Authentication > Users でオーナーのユーザーを作成する
--   2. 下の 00000000-0000-0000-0000-000000000000 をそのユーザーの UID に置き換える
--      (置き換え忘れると外部キー違反で止まり、何も作られない)
--   3. この SQL を実行する (再実行可能)
--   4. Authentication の設定で新規登録 (Allow new users to sign up) を無効にする
-- ============================================================

-- ------------------------------------------------------------
-- オーナー判定
-- ポリシーに UID を直書きせず 1 行テーブルに置く。
-- RLS 有効 + ポリシー無し = anon / authenticated からは一切読めない。
-- ------------------------------------------------------------
-- singleton 列で「常に 1 行だけ」を強制する (別の UID で再実行するとオーナーが入れ替わる)。
create table if not exists arbitrage_owner (
  singleton boolean primary key default true check (singleton),
  user_id   uuid not null references auth.users (id) on delete cascade
);
alter table arbitrage_owner enable row level security;
revoke all on arbitrage_owner from anon, authenticated;

insert into arbitrage_owner (user_id)
values ('00000000-0000-0000-0000-000000000000'::uuid)
on conflict (singleton) do update set user_id = excluded.user_id;

-- security definer: arbitrage_owner を RLS/権限に関係なく参照するため。
-- search_path を固定し、引数を取らず真偽値しか返さないので情報は漏れない。
create or replace function is_arbitrage_owner()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.arbitrage_owner o where o.user_id = (select auth.uid())
  );
$$;
revoke all on function is_arbitrage_owner() from public, anon;
grant execute on function is_arbitrage_owner() to authenticated;

-- ------------------------------------------------------------
-- 実行 1 回 = 1 行
-- ------------------------------------------------------------
create table if not exists arbitrage_runs (
  id               bigint generated always as identity primary key,
  started_at       timestamptz not null default now(),
  finished_at      timestamptz,                       -- 完了時に入る (null = 実行中 / 中断)
  status           text not null default 'running'
                   check (status in ('running', 'completed', 'failed')),
  config           jsonb not null,                    -- 実行時の config/campaigns.yaml の中身
  coupon_margin    numeric(4, 3) not null,            -- --coupon-margin の値
  jan_count        int,                               -- 対象 JAN 数
  candidate_count  int,                               -- 絞り込みを通った候補数
  result_count     int                                -- 黒字として保存した件数
);

-- ------------------------------------------------------------
-- 黒字になった (商品 × 店舗) 1 件 = 1 行
-- ------------------------------------------------------------
create table if not exists arbitrage_results (
  id                 bigint generated always as identity primary key,
  run_id             bigint not null references arbitrage_runs (id) on delete cascade,
  jan_code           text not null,
  item_name          text not null,                   -- Yahoo!ショッピング側の商品名
  category           text,                            -- Yahoo!ショッピング側の最上位ジャンル名 (絞り込み用)
  store_id           text not null,                   -- seller.sellerId
  store_name         text,
  item_code          text not null,                   -- Yahoo!ショッピングの商品コード (店舗内で一意)
  item_url           text not null,
  sale_price         int not null,                    -- 販売価格 (税込)
  coupon             jsonb,                           -- 適用したクーポン (種別・値・条件・期限・URL)。無ければ null
  coupon_discount    int not null default 0,          -- クーポン値引き額 (円)
  points             jsonb not null,                  -- ポイント内訳 [{name, rate, points, cap, capped}, ...]
  points_total       int not null,                    -- 付与ポイント合計
  capped_campaigns   text[] not null default '{}',    -- 上限に達したキャンペーン名
  effective_price    int not null check (effective_price > 0),  -- 実質価格 = 販売価格 − クーポン − ポイント
  buyback_price      int not null,                    -- 買取価格 (新品、直近の最高値)
  buyback_source     text not null,                   -- 買取店 ('kaitorishouten' / 'rudeya' / 'kaitoriwiki')
  buyback_date       date,                            -- その買取価格の取得日
  profit             int not null,                    -- 利益額 = 買取価格 − 実質価格
  profit_rate        numeric(8, 4) not null,          -- 利益率 = 利益額 / 実質価格 (比率。0.1234 = 12.34%)
  checked_at         timestamptz not null default now(),  -- 商品ページを確認した時刻
  -- 再開時は同じ run_id に upsert する (on_conflict="run_id,store_id,item_code")
  unique (run_id, store_id, item_code)
);

create index if not exists idx_arbitrage_results_run_rate
  on arbitrage_results (run_id, profit_rate desc);

-- ------------------------------------------------------------
-- Row Level Security: オーナーだけ SELECT 可
-- 権限は SELECT だけを明示的に付け、ポリシーはオーナー用の 1 つだけ作る。
--   anon / オーナー以外のログインユーザー → SELECT はできるが RLS で常に 0 件
--   INSERT / UPDATE / DELETE → 権限もポリシーも無い (service_role だけが書ける)
-- ------------------------------------------------------------
alter table arbitrage_runs enable row level security;
alter table arbitrage_results enable row level security;

revoke all on arbitrage_runs, arbitrage_results from anon, authenticated;
grant select on arbitrage_runs, arbitrage_results to anon, authenticated;

drop policy if exists "owner read arbitrage_runs" on arbitrage_runs;
create policy "owner read arbitrage_runs"
  on arbitrage_runs for select to authenticated
  using ((select is_arbitrage_owner()));

drop policy if exists "owner read arbitrage_results" on arbitrage_results;
create policy "owner read arbitrage_results"
  on arbitrage_results for select to authenticated
  using ((select is_arbitrage_owner()));

-- ============================================================
-- kaitoriwiki の condition 修正 (上とは別に、1 回だけ実行)
-- kaitoriwiki は新品の買取価格だけを扱うが、これまで 'used' で保存していた。
-- スクレイパー側の修正 ('new' で保存) を main に入れた後に実行すること
-- (先に実行すると、翌日の定期実行がまた 'used' を書く)。
-- 同じ日に 'used' と 'new' の両方がある行は主キーが衝突するので、古い 'used' を先に消す。
-- ============================================================
-- select condition, count(*) from price_history where source = 'kaitoriwiki' group by 1;  -- 実行前後の確認
--
-- begin;
-- delete from price_history u
--  where u.source = 'kaitoriwiki' and u.condition = 'used'
--    and exists (select 1 from price_history n
--                 where n.jan_code = u.jan_code and n.source = u.source
--                   and n.scraped_date = u.scraped_date and n.condition = 'new');
-- update price_history set condition = 'new'
--  where source = 'kaitoriwiki' and condition = 'used';
-- commit;
