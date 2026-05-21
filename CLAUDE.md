# kaitori-scraper

中古買取サイトの商品価格を毎日スクレイピング → Supabase に蓄積するプロジェクト。

## 全体構成

```
GitHub Actions (毎日12:00 JST)
  ├─ scraper.py        → Supabase (products / price_history)
  ├─ rudeya_scraper.py → Supabase (products / price_history)
  └─ output/*.json     → Artifact (30日保持)
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
| `.github/workflows/scrape.yml` | 本番。毎日12:00JST + workflow_dispatch |
| `.github/workflows/db_test.yml` | Supabase接続テスト（テストレコード upsert→select→delete） |
| `.github/workflows/scrape_rudeya_only.yml` | rudeya のみ手動実行（kaitorishouten スキップで時短検証用） |

## 進捗 / 次のステップ

### 完了 (フェーズ1)
- ✅ 2サイトのスクレイパー実装
- ✅ Supabase スキーマ + RLS
- ✅ GitHub Actions cron 自動実行
- ✅ DB upsert 動作確認 (11,592 products / 11,517 prices)
- ✅ ローカルcron停止（重複防止）

### 未着手 (フェーズ2候補)
- 価格変動アラート（前日比 -X% 以上で通知）
- Google Sheets 連携で最新版を共有
- ダッシュボード（Supabase Studio or 別UI）
- 503 エラーへのより洗練された対策（指数バックオフ、ジッタ）
- 別の買取サイトの追加
