-- ============================================================
-- arbitrage のリサーチ依頼 (結果ページの「リサーチ」タブ → この PC の待ち受け)
--
-- 結果ページ (docs/arbitrage.html) でログインしたオーナーが「リサーチ実行」を押すと、
-- arbitrage_research_requests に依頼を 1 行 INSERT する。
-- この PC で起動しておく待ち受け (python -m arbitrage.worker、service_role キー) が依頼を見つけて
-- python -m arbitrage.run を実行し、進捗を依頼行の progress に書き戻す。
-- arbitrage_worker_state は待ち受けの生存確認と、所要時間予測の材料 (買取価格の分布・実測の速度)。
-- db/arbitrage.sql (arbitrage_owner / is_arbitrage_owner() / arbitrage_runs) を実行済みであること。再実行可能。
-- ============================================================

-- ------------------------------------------------------------
-- 依頼 (1 行 = 1 回の「リサーチ実行」)
-- ------------------------------------------------------------
-- params / progress の中身の検証は画面と Python (arbitrage/research.py) の両方で行う
create table if not exists arbitrage_research_requests (
  id                bigint generated always as identity primary key,
  created_at        timestamptz not null default now(),
  status            text not null default 'queued'
                    check (status in ('queued', 'running', 'completed', 'failed', 'cancelled')),
  params            jsonb not null check (jsonb_typeof(params) = 'object'),
  cancel_requested  boolean not null default false,
  progress          jsonb not null default '{}'::jsonb,
  message           text,                       -- 失敗・中止の理由 (人が読む日本語)
  run_id            bigint references arbitrage_runs(id) on delete set null,
  started_at        timestamptz,
  finished_at       timestamptz,
  updated_at        timestamptz not null default now()
);

-- params の大きさの上限 (正しい依頼は 200 バイト前後)。数千桁の数値などを入れた行で待ち受けの読み込みが
-- 詰まらないようにする。既存のテーブルにも付くよう、作り直す形で書く (再実行可能)
alter table arbitrage_research_requests drop constraint if exists arbitrage_research_requests_params_size;
alter table arbitrage_research_requests add constraint arbitrage_research_requests_params_size
  check (octet_length(params::text) < 2048);

-- 同時に有効な依頼は 1 件だけ (待ち・実行中の行は全体で 1 行まで)。2 件目の INSERT は一意制約違反になる
create unique index if not exists arbitrage_research_requests_one_active
  on arbitrage_research_requests ((true))
  where status in ('queued', 'running');

create index if not exists idx_arbitrage_research_requests_created
  on arbitrage_research_requests (created_at desc);

alter table arbitrage_research_requests enable row level security;

-- オーナーだけが読める。書けるのは列単位で絞る:
--   INSERT → params だけ (status / progress などは既定値。ブラウザから「完了済み」の行は作れない)
--   UPDATE → cancel_requested だけ (中止の依頼)
--   DELETE → 誰にも許可しない
-- anon には権限を付けない (ポリシーも無い)。
revoke all on arbitrage_research_requests from anon, authenticated;
grant select on arbitrage_research_requests to authenticated;
grant insert (params) on arbitrage_research_requests to authenticated;
grant update (cancel_requested) on arbitrage_research_requests to authenticated;
-- 待ち受けと python -m arbitrage.run (service_role キー) が状態・進捗を書くための権限。既定で付く環境でも明示しておく
grant select, insert, update on arbitrage_research_requests to service_role;

drop policy if exists "owner read arbitrage_research_requests" on arbitrage_research_requests;
create policy "owner read arbitrage_research_requests"
  on arbitrage_research_requests for select to authenticated
  using ((select is_arbitrage_owner()));

drop policy if exists "owner insert arbitrage_research_requests" on arbitrage_research_requests;
create policy "owner insert arbitrage_research_requests"
  on arbitrage_research_requests for insert to authenticated
  with check ((select is_arbitrage_owner()));

drop policy if exists "owner update arbitrage_research_requests" on arbitrage_research_requests;
create policy "owner update arbitrage_research_requests"
  on arbitrage_research_requests for update to authenticated
  using ((select is_arbitrage_owner()))
  with check ((select is_arbitrage_owner()));

-- ------------------------------------------------------------
-- 待ち受けの状態 (常に 1 行だけ。singleton 列で強制)
-- ------------------------------------------------------------
create table if not exists arbitrage_worker_state (
  singleton          boolean primary key default true check (singleton),
  heartbeat_at       timestamptz,   -- 待ち受けが生きている印 (10 秒ごとに更新)
  -- 買取価格の分布: {"bucket": 1000, "as_of": ISO日時, "counts": {"0": 12, "1000": 340, ...}}
  --   キーは 買取価格 // 1000 * 1000 (文字列)。値はその帯の JAN 数
  buyback_histogram  jsonb,
  -- 実測の速度: {"jan_per_min": 27.6, "candidates_per_jan": 1.1, "reachable_fraction": 0.28, "seconds_per_check": 4.1}
  rates              jsonb,
  updated_at         timestamptz not null default now()
);

alter table arbitrage_worker_state enable row level security;

-- オーナーは読むだけ (所要時間予測と「待ち受けが動いているか」の表示)。書くのは service_role だけ
revoke all on arbitrage_worker_state from anon, authenticated;
grant select on arbitrage_worker_state to authenticated;
grant select, insert, update on arbitrage_worker_state to service_role;

drop policy if exists "owner read arbitrage_worker_state" on arbitrage_worker_state;
create policy "owner read arbitrage_worker_state"
  on arbitrage_worker_state for select to authenticated
  using ((select is_arbitrage_owner()));

-- ------------------------------------------------------------
-- 保存のたびに updated_at を更新する (送られた値は信用しない)
-- ------------------------------------------------------------
create or replace function arbitrage_research_touch()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists arbitrage_research_requests_touch on arbitrage_research_requests;
create trigger arbitrage_research_requests_touch
  before insert or update on arbitrage_research_requests
  for each row execute function arbitrage_research_touch();

drop trigger if exists arbitrage_worker_state_touch on arbitrage_worker_state;
create trigger arbitrage_worker_state_touch
  before insert or update on arbitrage_worker_state
  for each row execute function arbitrage_research_touch();
