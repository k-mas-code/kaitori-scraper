# 店舗リスト (data/yahoo_pp_stores_5genres.xlsx) の再取得

ボーナスストアPlus の参加ストア一覧から、arbitrage が読む店舗リストを作り直す。経緯と抽出方法の詳細は `HANDOFF.md`。

```bash
.venv/bin/python -m tools.yahoo_pp.probe          # 各 URL を 1 件ずつ取得して抽出できるか確認 (約 15 秒)
.venv/bin/python -m tools.yahoo_pp.bsplus_fetch   # 日付 × 枠 × ジャンルの参加ストア → data/yahoo_pp/sched.json (約 20 分)
.venv/bin/python -m tools.yahoo_pp.enrich         # 適格請求書・上乗せ率・優良・評価 (約 4 時間。途中で止めても再実行で続きから)
.venv/bin/python -m tools.yahoo_pp.build_xlsx --out data/yahoo_pp/new_stores.xlsx [--plus2 11/5,11/15 --plus3 ...]
```

- リクエストは 1 並列・2.5 秒間隔。HTTP 489 (アクセス制限) が出たら止まるので、時間をおいて再実行する。取得済みの本文は `data/yahoo_pp/cache/` にあり二度叩かない
- 一覧ページには約 5 週間分 (今日の 2 週間前〜3 週間後) しか出ない。掲載期間の外の日付で run すると `stores.py` がエラーにするので、月 1〜2 回作り直す
- `enrich` の上乗せ調査は日曜・5 のつく日を避ける (日曜特典が混ざる)。今週のように商品ごとのキャンペーン (爆買WEEK 等) がある週も、率の高い順に最大 3 商品の商品ページで正確な上乗せを確認するので影響しない
- `build_xlsx` の出力は `arbitrage/stores.py` が読む構造 (シート名・ヘッダー行の位置・列名・日付列「M/D\n曜」) に合わせてある。全体加算日 (+2 / +3) は自動では分からないので `--plus2` / `--plus3` で渡す (計算には使わない。印だけ)
- 差し替え前に `arbitrage.stores.load_stores(date.today(), Path(新ファイル))` が読めることを確認する
