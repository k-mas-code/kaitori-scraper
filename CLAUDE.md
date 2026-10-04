# kaitori-scraper

中古買取サイトの商品価格を毎日スクレイピング → Supabase に蓄積するプロジェクト。

## 全体構成

```
GitHub Actions (毎日12:00 JST)
  ├─ scraper.py             → Supabase (products / price_history)
  ├─ rudeya_scraper.py      → Supabase (products / price_history)
  ├─ kaitoriwiki_scraper.py → Supabase (products / price_history)
  └─ output/*.json          → Artifact (30日保持)

GitHub Pages (docs/)
  └─ Vanilla JS + Tailwind CDN
      ├─ index.html  検索UI (検索 + 候補 + 価格テーブル + カメラスキャナ)
      ├─ app.js      Supabase JS SDK 経由で products / price_history を読み取り
      ├─ scanner.js  html5-qrcode (JANバーコード読み取り)
      ├─ config.js   SUPABASE_URL + anon public key (RLSでSELECTのみ許可)
      └─ arbitrage.html / arbitrage.js  利益商品リスト (ログインしたオーナーだけが読める)

ローカル実行のみ (WSL から月3〜4回。GitHub Actions には載せない)
  └─ python -m arbitrage.run → Yahoo!ショッピング商品検索API + 商品ページ
                             → Supabase (arbitrage_runs / arbitrage_results)
```

| 項目 | 値 |
|---|---|
| GitHub | https://github.com/k-mas-code/kaitori-scraper |
| Supabase Project | `lpndrpxllzpykwpachvh` |
| Supabase URL | https://lpndrpxllzpykwpachvh.supabase.co |
| ローカル開発パス | `/home/mas/projects/kaitori` (1台目) / `/home/user/projects/kaitori` (2台目, WSL) |
| GitHub アカウント | `k-mas-code` (active) |

## 対象サイトと挙動

### kaitorishouten-co.jp (`scraper.py`)
- サイトは React SPA (2026年のリニューアル以降)。HTML ではなく公開 JSON API を巡回する
  - `/api/v1/categories` で最上位カテゴリ ID を名前から引く (keitai=スマホ / kaden=家電 / nitiyouhin=お酒)
  - `/api/v1/products?per_page=100&page=N&category_id=ID` を全ページ取得 (親ID指定で配下サブカテゴリも全部返る。`per_page` 上限100・省略時20)
- 価格は `rank_options[].base` (`rank_type_id` 1=新品 / 2=中古)、減額は `rank_options[].options[].price_change`
- JAN は数字のみ・8/12/13/14桁だけ採用。キャリア版スマホは `840353956209dc` のような接尾辞つきで JAN なし扱い (DB に入らない)
- `detail_url` は `/products/list?name={JAN}` (旧 `/?name=` は新サイトでは検索にならない)
- **約7,450件 (うち JAN あり約6,850件) / 約2.5分**。「おもちゃ」カテゴリ (約190件) は対象外
- 503 (レート制限) 時は 30 秒待機

### kaitori-rudeya.com (`rudeya_scraper.py`)
- トップページから 170+ カテゴリを取得 (`/category/detail/{id}` の直リンクのみ。`/product{n}` 付きは商品リンクなので除外)
- 各カテゴリ `/category/detail/{id}` を順次クロール (SSR・ページネーションなし)
- 商品は `article.pgrid-card` 1つ = 1件 (`product-card-*` クラス)。ページ先頭の「買取強化中」は再掲なので商品IDで除外
- 同名カテゴリ (EPSON が2つ) があるので名前キーは結合する
- **約3,200件 / 約6分**

### kaitori.wiki (`kaitoriwiki_scraper.py`)
- ハブサイト: 商品URLは外部サブドメイン (`iphonekaitori.tokyo` / `gamekaitori.jp` / `kadenkaitori.tokyo` / `pckaitori.tokyo` / `ipadkaitori.jp` / `camerakaitori.tokyo` / `cosmekaitori.jp`)
- 商品データ自体は kaitori.wiki の検索ページ上で完結。**2種類の一覧を巡回して合算**する (約406ページ / 約17分 / 約12,000件)
  - `/search/{page}/name/all` (絞り込みなし, 約222ページ): 5万円超も出る。ただし**サイト側の並び順が不安定**で、約150ページ目以降に既出商品が再掲され、その分 5万円以下の約2,000件が一度も出てこない (何度巡回しても同じ結果。2026-10-04 調査)
  - `/search/{page}/price/5/name/all` (50,000円以下, 約184ページ): 価格順でほぼ欠けなし。`price/{1..5}` は「N円以下」(5,000 / 10,000 / 20,000 / 30,000 / 50,000)
  - 経緯: 以前は `price/5` のみで 5万円超が全く取れていなかった → 2026-10-04 に合算方式へ
  - **残課題**: 5万円超のうち推定約600件は絞り込みなし一覧にも出ず未取得。検索フォームの種別絞り込み (`type`: スマートフォン/家電 等7種) ごとの巡回で埋まる可能性あり (未検証)
- カテゴリは商品URLのホスト名から逆引き
- 商品名末尾の 13 桁数字を JAN として抽出 (SIMフリースマホ等は JAN無し→DB には入らない)
- 価格は「最高買取価格」1列。新品の買取価格だけを扱うサイトなので condition は `new` で保存 (2026-10-04 以前の行は `used` で入っていたのを一括修正)

### 件数下限チェック (全スクレイパー共通)
- 取得件数が `MIN_EXPECTED_PRODUCTS` (kaitorishouten 4,000 / rudeya 1,500 / kaitoriwiki 5,000) 未満なら終了コード 1
  - 取れた分は DB に書いたうえで失敗にする
- `scrape.yml` は各ステップ `continue-on-error: true` + 最後の「Fail job if any scraper failed」ステップでジョブを赤にする (1サイト失敗でも残りは実行)
- 背景: 2026-10 に kaitorishouten / rudeya がリニューアルされ、HTTP 200 のまま抽出0件で「成功」扱いになっていた

## 利益商品の探索 (`arbitrage/`)

Yahoo!ショッピングで買って買取店に売ると利益が出る商品を探す。**ローカル実行専用** (商品ページの取得が Actions の IP だとブロックされやすいため)。

```bash
.venv/bin/python -m arbitrage.run --coupon-margin 0.2              # 全件 (API だけで 4.5 時間以上)
.venv/bin/python -m arbitrage.run --limit 100 --no-db              # 試行: 買取価格の高い順に 100 JAN、DB 保存なし
.venv/bin/python -m arbitrage.run --stage api                      # 商品検索APIでの絞り込みまで
.venv/bin/python -m pytest -q tests                                # 単体テスト
```

- 判定式: `買取価格 > (販売価格 − ストアクーポン) − 付与ポイント`。ポイントは「1 つだけ買う」前提で、枠ごとに `min(floor(クーポン後の税抜価格 × 率), 上限)` を合計。税抜 = 税込 − floor(税込 × 10/110)
- 利益率 = (買取価格 − 実質価格) / 実質価格。送料・手数料・減額リスクは考えない
- 流れ (`arbitrage/run.py`)
  1. 買取価格 (`buyback.py`): `price_history` の直近7日の新品価格から、JAN ごとに「各買取店の最新価格」の最高値
  2. 商品検索API v3 (`yahoo_api.py`): `jan_code` で新品・在庫ありを価格の安い順に取得。1 秒 1 回、429 は待って再試行
  3. 絞り込み (`prefilter.py`): 店舗リストの店だけ残し、甘い実質価格 `販売価格 × (1 − クーポン余地) − クーポン前の価格で付きうるポイント` が買取価格を下回るものを候補にする
  4. 確定判定 (`yahoo_page.py` / `finalize.py`): 商品ページを取得し、実際の上乗せ率とクーポンで再計算。黒字だけを結果にする
  5. 保存 (`db.py`): `arbitrage_runs` / `arbitrage_results` に service_role キーで upsert
- 入力ファイル (どちらも Git に含めない)
  - `data/yahoo_pp_stores_5genres.xlsx`: 店舗リスト。「全ジャンル」(日別のボーナスストアPlus枠 +4/+9、優良ストア)、「ポイント上乗せ調査」(最大上乗せ率。無い店は 14% と仮定)、「除外リスト（非適格）」。**日別枠は掲載期間内の日付しか無い** → 期間外の日付で実行するとエラー。新しいリストに差し替える
  - `config/campaigns.yaml`: 共通分・キャンペーンの率/上限/最低購入額/対象。実行のたびに編集する。ひな形は `config/campaigns.example.yaml`。キー名を間違えるとエラーで止まる
  - `.env`: `YAHOO_CLIENT_ID` (商品検索API)、`SUPABASE_URL` / `SUPABASE_KEY` (service_role、保存用)
- ポイントの出どころ
  - ストアポイント (基本 1% + 上乗せ): 商品ページ。上乗せ率は専用項目が無く `totalPointRatio − 標準で付く各行の率の合計`
  - ボーナスストアPlus (+4% / +9%): 店舗リストの実行日の枠。全体加算日は枠のある店 +2%、「+2/+3」の日の優良ストアは +3%
  - それ以外 (PayPay、LYP、日曜 +5% など): `campaigns.yaml`。対象ストア限定のものは `target: page` (商品ページの内訳にその行が出ている店)
- 再開: 状態は `data/arbitrage/{run名}/` (`meta.json` / `buyback.json` / `api.jsonl` / `pages.jsonl`)。中断時のログに出る「再開」コマンドで続きから。条件 (日付・設定・店舗リスト) を変えたら `--run-name` で別名にする
- 結果ページ `docs/arbitrage.html`: Supabase Auth のメールリンクでログイン。RLS でオーナー (`arbitrage_owner` の 1 行) だけが読める。スキーマは `db/arbitrage.sql`

### 商品ページの落とし穴 (2026-10-04 調査)
- ストアクーポンは HTML に無い。`POST /syene-bff/v1/pc/coupon/v3/{店舗ID}/{商品コード}` で取る。ページが発行する匿名 Cookie と `page.crumb` が必要 (無いと 401)
- クーポン一覧は商品単位で絞り込み済み。詳細ページの対象商品リストは一部しか載らないので、対象判定に使わない
- ページのポイント数はクーポン適用前の価格で計算されている → 率だけ採って再計算する
- ページの「上限到達」フラグは上限で切られていても false のまま → 上限は `campaigns.yaml` で持つ
- ページの合計率には未ログイン時の仮の表示 (LINE 連携 2.5%、PayPay 残高 0.5%) が含まれる → そのまま使うと二重計上
- 日曜 +5% は行が出ていても、安い商品 (5,000円前後未満) には付かない → `min_purchase` で指定
- 色・サイズ違いのある商品 (`individualItemList`) は確定判定から除外している
- 結果ページでログインすると同じサイトの検索ページもログイン状態になり、公開テーブルが 0 件になる → `arbitrage.js` はログイン情報の保存先 (`storageKey`) を分けている

## DBスキーマ概要

- `products` (jan_code PK, name, source, category, ...)
- `price_history` (jan_code, source, condition, scraped_date, price, ...) - 主キー4列
- `arbitrage_runs` / `arbitrage_results` / `arbitrage_owner` (`db/arbitrage.sql`): 利益商品の結果。オーナーだけ SELECT 可、anon は常に 0 件
- 価格推移SQLは README 参照
- **落とし穴**: `products` の主キーは `jan_code` のみ。`source` は最後に書いたサイトで上書きされるので、サイト別の件数は `products` ではなく **`price_history` で数える** (`where scraped_date = ... group by source`)

## 重要な学び・トラップ

### Supabase
- API キーには 2 形式存在: **Legacy JWT** (`eyJ...`, role=service_role) と **新形式** (`sb_secret_xxx`)
- `supabase-py 2.9.1` は新形式に**未対応** → Legacy JWT を使う（**Settings > API Keys > Legacy anon, service_role API keys** タブから取得）
- 新形式に切り替えるなら `supabase` を 2.20+ に上げる
- RLS 有効でも `service_role` キーはバイパスするので書き込み可能
- **Free プランは1週間ほどアクティビティが少ないとプロジェクトが一時停止 (pause)** する。スクレイプが止まると DB も止まる → ダッシュボードから Restore
- 同一バッチ内で同じ主キーが2回登場すると `21000 ON CONFLICT DO UPDATE command cannot affect row a second time` エラー → アプリ側で dedup 必須

### GitHub Actions
- `secrets` の登録は **Web UI 推奨**。CLI (`gh secret set`) は Claude Code の `!` プレフィックス経由だと TTY 不在でコマンド文字列そのものが登録される事故あり
- workflow ファイルを push するには PAT に `workflow` スコープが必要 (`gh auth refresh -s workflow`)
- 60日リポジトリに push が無いと scheduled workflow が自動停止する (実際に 2026-07-21 を最後に停止 → 2026-10-04 に再有効化)
  - 対策: `.github/workflows/keepalive.yml` が毎月1日に `.github/keepalive` をコミットする
  - 止まった場合は Actions 画面で workflow を Enable し直す
- `runs-on` は `ubuntu-24.04` 固定 (`scrape.yml`)。`ubuntu-latest` は 2026-10-19 から Ubuntu 26 に移行するため

### git/gh
- snap版 `gh` 経由の `git clone` は `remote-https` ヘルパー問題で失敗することがある → `/usr/bin/git` を直接使う
- このマシンの `gh` には 2アカウント認証: `k-mas-code` (active) と `takefu21ss-ctrl`

## ローカル実行

2台運用 (`/home/mas/projects/kaitori` と WSL の `/home/user/projects/kaitori`)。**作業前に `git pull`、作業後に `git push`**。

```bash
cd /home/mas/projects/kaitori   # 2台目は /home/user/projects/kaitori
pip install -r requirements.txt
# 2台目 (WSL) は uv の venv (Python 3.11):
#   uv venv --python 3.11 && uv pip install -r requirements.txt  → .venv/bin/python scraper.py

# DB書き込みなし (JSON のみ)
python scraper.py

# DB書き込みあり
export SUPABASE_URL=https://lpndrpxllzpykwpachvh.supabase.co
export SUPABASE_KEY=eyJ...  # Legacy service_role JWT
python scraper.py
```

## ワークフロー一覧

| ファイル | 用途 |
|---|---|
| `.github/workflows/scrape.yml` | 本番。毎日12:00JST + workflow_dispatch (3スクレイパーを順次実行) |
| `.github/workflows/db_test.yml` | Supabase接続テスト（テストレコード upsert→select→delete） |
| `.github/workflows/scrape_rudeya_only.yml` | rudeya のみ手動実行（kaitorishouten スキップで時短検証用） |
| `.github/workflows/scrape_kaitoriwiki_only.yml` | kaitoriwiki のみ手動実行（検証用） |
| `.github/workflows/keepalive.yml` | 毎月1日に `.github/keepalive` をコミット（60日無 push による schedule 自動停止の防止） |

## 進捗 / 次のステップ

### 完了 (フェーズ1 + フェーズ2の一部)
- ✅ 3サイトのスクレイパー実装 (kaitorishouten / rudeya / kaitoriwiki)
- ✅ Supabase スキーマ + RLS (anon SELECT のみ許可)
- ✅ GitHub Actions cron 自動実行
- ✅ DB upsert 動作確認
- ✅ ローカルcron停止（重複防止）
- ✅ ダッシュボード = GitHub Pages の検索UI (`docs/`)

### 未着手 (フェーズ2候補)
- 価格変動アラート（前日比 -X% 以上で通知）
- Google Sheets 連携で最新版を共有
- 価格推移グラフ (現在は最新1点のみ表示)
- 503 エラーへのより洗練された対策（指数バックオフ、ジッタ）
- 別の買取サイトの追加
- kaitoriwiki の 5万円超の取りこぼし (推定約600件) を種別ごとの巡回で埋める
- kaitorishouten の「おもちゃ」カテゴリ追加 / キャリア版スマホ (JAN 接尾辞つき) の扱い
