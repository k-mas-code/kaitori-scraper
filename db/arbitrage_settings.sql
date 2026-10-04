-- ============================================================
-- arbitrage の設定 (ポイント付与・キャンペーン日程・手持ちクーポン・購入予定日)
--
-- 結果ページ (docs/arbitrage.html) の「設定」画面から、ログインしたオーナーだけが読み書きする。
-- python -m arbitrage.run は service_role キーでこの設定を読む。
-- db/arbitrage.sql (arbitrage_owner / is_arbitrage_owner()) を実行済みであること。再実行可能。
-- ============================================================

-- 設定は常に 1 行だけ (singleton 列で強制)。中身の検証は画面と Python の両方で行う
create table if not exists arbitrage_settings (
  singleton   boolean primary key default true check (singleton),
  config      jsonb not null check (jsonb_typeof(config) = 'object'),
  updated_at  timestamptz not null default now()
);

alter table arbitrage_settings enable row level security;

-- オーナーだけが SELECT / INSERT / UPDATE できる。DELETE は誰にも許可しない。
-- anon には権限を付けない (ポリシーも無い)。
revoke all on arbitrage_settings from anon, authenticated;
grant select, insert, update on arbitrage_settings to authenticated;
-- python -m arbitrage.run (service_role キー) が設定を読むための権限。既定で付く環境でも明示しておく
grant select on arbitrage_settings to service_role;

drop policy if exists "owner read arbitrage_settings" on arbitrage_settings;
create policy "owner read arbitrage_settings"
  on arbitrage_settings for select to authenticated
  using ((select is_arbitrage_owner()));

drop policy if exists "owner insert arbitrage_settings" on arbitrage_settings;
create policy "owner insert arbitrage_settings"
  on arbitrage_settings for insert to authenticated
  with check ((select is_arbitrage_owner()));

drop policy if exists "owner update arbitrage_settings" on arbitrage_settings;
create policy "owner update arbitrage_settings"
  on arbitrage_settings for update to authenticated
  using ((select is_arbitrage_owner()))
  with check ((select is_arbitrage_owner()));

-- 保存のたびに updated_at を更新する (ブラウザから送られた値は信用しない)
create or replace function arbitrage_settings_touch()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists arbitrage_settings_touch on arbitrage_settings;
create trigger arbitrage_settings_touch
  before insert or update on arbitrage_settings
  for each row execute function arbitrage_settings_touch();
