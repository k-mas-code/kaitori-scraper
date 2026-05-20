# kaitori-scraper

中古買取サイトの商品・買取価格を毎日スクレイピングし、Supabase に蓄積する。

## 対象サイト

| サイト | スクリプト | 出力ファイル |
|---|---|---|
| 買取商店 (kaitorishouten-co.jp) | `scraper.py` | `output/kaitorishouten_{YYYYMMDD}.json` |
| 買取ルデヤ (kaitori-rudeya.com) | `rudeya_scraper.py` | `output/rudeya_{YYYYMMDD}.json` |

## アーキテクチャ

```
GitHub Actions (毎日12:00 JST)
  ├─ scraper.py        → Supabase (products / price_history)
  ├─ rudeya_scraper.py → Supabase (products / price_history)
  └─ output/*.json     → Artifact (30日保持)
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
