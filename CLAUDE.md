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
| ローカル開発パス | `/home/mas/projects/kaitori` |
| GitHub アカウント | `k-mas-code` (active) |

## 対象サイトと挙動

### kaitorishouten-co.jp (`scraper.py`)
- 大カテゴリ `/keitai` `/kaden` `/nitiyouhin` の3つ
- 各ページから Ajax エンドポイント (`var topUrl = ...`) と サブカテゴリID を動的抽出
- 「すべて」(カード形式) + 全サブカテゴリ (テーブル形式 `tr[id^="ex-product-"]`) をクロール
- JAN コードで重複排除しながらマージ
- **約9,000件 / 約30分**
- 503 (レート制限) 時は 30 秒待機

### kaitori-rudeya.com (`rudeya_scraper.py`)
- トップページから 160+ カテゴリを取得
- 各カテゴリ `/category/detail/{id}` を順次クロール
- サーバーサイドレンダリングなので素直
- **約3,200件 / 約10分**

### kaitori.wiki (`kaitoriwiki_scraper.py`)
- ハブサイト: 商品URLは外部サブドメイン (`iphonekaitori.tokyo` / `gamekaitori.jp` / `kadenkaitori.tokyo` / `pckaitori.tokyo` / `ipadkaitori.jp` / `camerakaitori.tokyo` / `cosmekaitori.jp`)
- 商品データ自体は kaitori.wiki の検索ページ (`/search/{page}/price/{range}/name/all`) 上で完結
- `price/{range}` は「N円以下」フィルター (1=5,000円 / 2=10,000 / 3=20,000 / 4=30,000 / 5=50,000)
  - 5 が全件を含む superset なので **5 のみ巡回**で OK (約 176 ページ)
- カテゴリは商品URLのホスト名から逆引き
- 商品名末尾の 13 桁数字を JAN として抽出 (約97%でJAN取得可、SIMフリースマホ等は JAN無し→スキップ)
- 価格は「最高買取価格」1列 → condition は固定で `used` 扱い
- **約10,200件 / 約6分**, 50,000円超の商品は取得対象外

## DBスキーマ概要

- `products` (jan_code PK, name, source, category, ...)
- `price_history` (jan_code, source, condition, scraped_date, price, ...) - 主キー4列
- 価格推移SQLは README 参照

## 重要な学び・トラップ

### Supabase
- API キーには 2 形式存在: **Legacy JWT** (`eyJ...`, role=service_role) と **新形式** (`sb_secret_xxx`)
- `supabase-py 2.9.1` は新形式に**未対応** → Legacy JWT を使う（**Settings > API Keys > Legacy anon, service_role API keys** タブから取得）
- 新形式に切り替えるなら `supabase` を 2.20+ に上げる
- RLS 有効でも `service_role` キーはバイパスするので書き込み可能
- 同一バッチ内で同じ主キーが2回登場すると `21000 ON CONFLICT DO UPDATE command cannot affect row a second time` エラー → アプリ側で dedup 必須

### GitHub Actions
- `secrets` の登録は **Web UI 推奨**。CLI (`gh secret set`) は Claude Code の `!` プレフィックス経由だと TTY 不在でコマンド文字列そのものが登録される事故あり
- workflow ファイルを push するには PAT に `workflow` スコープが必要 (`gh auth refresh -s workflow`)
- 60日リポジトリに push が無いと scheduled workflow が自動停止 → 毎日コミットすれば回避可能（Artifactは別カウント）

### git/gh
- snap版 `gh` 経由の `git clone` は `remote-https` ヘルパー問題で失敗することがある → `/usr/bin/git` を直接使う
- このマシンの `gh` には 2アカウント認証: `k-mas-code` (active) と `takefu21ss-ctrl`

## ローカル実行

```bash
cd /home/mas/projects/kaitori
pip install -r requirements.txt

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
