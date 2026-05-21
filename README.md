# kaitori-scraper

中古買取サイトの商品・買取価格を毎日スクレイピングし、Supabase に蓄積する。

## 対象サイト

| サイト | スクリプト | 出力ファイル |
|---|---|---|
| 買取商店 (kaitorishouten-co.jp) | `scraper.py` | `output/kaitorishouten_{YYYYMMDD}.json` |
| 買取ルデヤ (kaitori-rudeya.com) | `rudeya_scraper.py` | `output/rudeya_{YYYYMMDD}.json` |
| 買取wiki (kaitori.wiki) | `kaitoriwiki_scraper.py` | `output/kaitoriwiki_{YYYYMMDD}.json` |

## アーキテクチャ

```
GitHub Actions (毎日12:00 JST)
  ├─ scraper.py             → Supabase (products / price_history)
  ├─ rudeya_scraper.py      → Supabase (products / price_history)
  ├─ kaitoriwiki_scraper.py → Supabase (products / price_history)
  └─ output/*.json          → Artifact (30日保持)

GitHub Pages (静的サイト)
  └─ docs/                  → 検索UI (JAN/商品名 → 各店買取価格を降順表示)
```

## フロントエンド (検索UI)

`docs/` に Vanilla JS の静的サイトを置き、GitHub Pages で配信する。

- 公開URL: <https://k-mas-code.github.io/kaitori-scraper/>
- JANコード or 商品名 (部分一致) で検索
- 入力中に候補リストを動的表示 (autocomplete)
- 選択した商品の買取店3社の最新価格を降順表示
- スマホブラウザでカメラ起動 → JANバーコード読み取りで自動検索

### 必要設定
1. Supabase RLS: `db/schema.sql` の `anon read *` ポリシーを SQL Editor で実行
2. `docs/config.js` の `SUPABASE_ANON_KEY` を Project Settings > API > `anon public` キーで差し替え

### ローカル開発
```bash
cd docs && python -m http.server 8000
# http://localhost:8000
```

## セットアップ

### 1. Supabase

1. https://supabase.com で新規プロジェクト作成
2. SQL Editor で `db/schema.sql` を実行
3. Project Settings > API から以下を取得:
   - `SUPABASE_URL`: Project URL
   - `SUPABASE_KEY`: service_role キー（書き込み権限あり）

### 2. GitHub

1. このリポジトリを GitHub に push
2. Settings > Secrets and variables > Actions で以下を登録:
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
3. Actions タブから `Daily Scrape` を手動実行してテスト

## ローカル実行

```bash
pip install -r requirements.txt

# DB 書き込みをスキップ (output/*.json のみ)
python scraper.py

# DB 書き込みあり
export SUPABASE_URL=https://xxx.supabase.co
export SUPABASE_KEY=eyJ...
python scraper.py
```

## クエリ例

```sql
-- 特定商品の価格推移
select scraped_date, condition, price
from price_history
where jan_code = '4549995649291'
order by scraped_date desc;

-- 同じJANで店舗を跨いだ価格比較
select p.name, ph.source, ph.condition, ph.price
from price_history ph
join products p using(jan_code)
where ph.scraped_date = current_date
  and ph.jan_code in (
    select jan_code from price_history
    where scraped_date = current_date
    group by jan_code having count(distinct source) > 1
  );
```
