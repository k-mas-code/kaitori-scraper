-- ============================================================
-- ec-scraper schema
-- 中古買取サイトの商品マスタと日次価格履歴を管理する
-- ============================================================

-- 商品マスタ（JANコード単位で一意）
create table if not exists products (
  jan_code     text primary key,
  name         text not null,
  image_url    text,
  source       text not null,            -- 'kaitorishouten' or 'rudeya'
  category     text,                     -- 'keitai', 'kaden', 'Nintendo Switch 2' 等
  detail_url   text,                     -- 商品詳細ページ (rudeya 用)
  first_seen   date not null default current_date,
  last_seen    date not null default current_date,
  updated_at   timestamptz default now()
);

create index if not exists idx_products_source on products(source);
create index if not exists idx_products_category on products(category);

-- 価格履歴（毎日積む）
-- 1商品が new/used 別価格を持つ場合は別レコードとして保存
create table if not exists price_history (
  jan_code      text not null,
  source        text not null,           -- 'kaitorishouten' or 'rudeya'
  condition     text not null,           -- 'new' or 'used'
  scraped_date  date not null,
  price         int,
  note          text,                    -- 備考（減額条件等）
  primary key (jan_code, source, condition, scraped_date)
);

create index if not exists idx_price_history_date on price_history(scraped_date desc);
create index if not exists idx_price_history_jan on price_history(jan_code);

-- ============================================================
-- Row Level Security
-- service_role キー (GitHub Actions で使用) は RLS をバイパスするため
-- そのまま読み書き可能。
-- フロントエンド (anon key) には SELECT のみ許可。
-- INSERT/UPDATE/DELETE はポリシー無しで全て拒否される。
-- ============================================================
alter table products enable row level security;
alter table price_history enable row level security;

create policy "anon read products"
  on products for select to anon using (true);
create policy "anon read price_history"
  on price_history for select to anon using (true);

-- ============================================================
-- 便利ビュー: 最新の価格一覧
-- ============================================================
create or replace view latest_prices as
select
  p.jan_code,
  p.name,
  p.source,
  p.category,
  ph.condition,
  ph.price,
  ph.scraped_date
from products p
join lateral (
  select condition, price, scraped_date
  from price_history
  where jan_code = p.jan_code and source = p.source
  order by scraped_date desc
  limit 4
) ph on true;
