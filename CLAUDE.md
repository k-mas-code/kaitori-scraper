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
      └─ config.js   SUPABASE_URL + anon public key (RLSでSELECTのみ許可)
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
- 商品データ自体は kaitori.wiki の検索ページ `/search/{page}/name/all` (絞り込みなし) 上で完結。**約222ページ**
  - 以前は `/search/{page}/price/5/name/all` を巡回していたが、`price/5` は「50,000円以下」の絞り込みで 5万円超の商品が漏れていた → 2026-10-04 に解消
- カテゴリは商品URLのホスト名から逆引き
- 商品名末尾の 13 桁数字を JAN として抽出 (SIMフリースマホ等は JAN無し→DB には入らない)
- 価格は「最高買取価格」1列 → condition は固定で `used` 扱い

### 件数下限チェック (全スクレイパー共通)
- 取得件数が `MIN_EXPECTED_PRODUCTS` (kaitorishouten 4,000 / rudeya 1,500 / kaitoriwiki 5,000) 未満なら終了コード 1
  - 取れた分は DB に書いたうえで失敗にする
- `scrape.yml` は各ステップ `continue-on-error: true` + 最後の「Fail job if any scraper failed」ステップでジョブを赤にする (1サイト失敗でも残りは実行)
- 背景: 2026-10 に kaitorishouten / rudeya がリニューアルされ、HTTP 200 のまま抽出0件で「成功」扱いになっていた

## DBスキーマ概要

- `products` (jan_code PK, name, source, category, ...)
- `price_history` (jan_code, source, condition, scraped_date, price, ...) - 主キー4列
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
- kaitorishouten の「おもちゃ」カテゴリ追加 / キャリア版スマホ (JAN 接尾辞つき) の扱い
