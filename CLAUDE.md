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
  ├─ python -m arbitrage.run → Yahoo!ショッピング商品検索API + 商品ページ
  │                          → Supabase (arbitrage_runs / arbitrage_results)
  └─ python -m arbitrage.worker (待ち受け。起動したままにする)
       結果ページの「リサーチ」タブ → Supabase (arbitrage_research_requests) の依頼を 10 秒ごとに確認
       → arbitrage.run を子プロセスで実行 → 進捗を依頼行に書き戻す
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
.venv/bin/python -m arbitrage.run --coupon-margin 0.2              # 全件 (約1.6万 JAN。API だけで 9〜10 時間)。設定は Supabase から読む
.venv/bin/python -m arbitrage.run --limit 100 --no-db              # 試行: 全体から均等に 100 JAN、DB 保存なし
.venv/bin/python -m arbitrage.run --min-buyback 5000 --max-buyback 60000   # 買取価格の範囲で対象を絞る
.venv/bin/python -m arbitrage.run --category 家電 --category ゲーム      # 商品分類 (大分類) で対象を絞る (繰り返し可。名前は arbitrage/categories.py の GROUPS)
.venv/bin/python -m arbitrage.run --stage api                      # 商品検索APIでの絞り込みまで
.venv/bin/python -m arbitrage.run --min-profit -1000               # 1,000 円までの赤字も結果に載せる (既定は黒字のみ。下限 -5000)
.venv/bin/python -m arbitrage.run --run-name NAME --stage save      # 新しい検索・確認はせず、確認済みの分だけ DB に保存 (途中経過を結果ページに出す)
.venv/bin/python -m arbitrage.run --check-config                   # 設定の解釈を表示 (読み込み元・購入予定日・その日に有効なキャンペーン/クーポン)
.venv/bin/python -m arbitrage.run --date 2026-10-18                # 設定の購入予定日ではなく、この日付で計算する
.venv/bin/python -m arbitrage.run --config-file config/campaigns.yaml   # 設定を Supabase ではなくファイル (YAML/JSON) から読む
.venv/bin/python -m arbitrage.run --page-scope all                 # 確定判定を全候補に広げる (既定 reachable = 届きそうな候補だけ)
.venv/bin/python -m arbitrage.run --reach-slack 0.05               # 「届きそう」の余裕 (販売価格比。既定 0.03、0〜0.2)
.venv/bin/python -m arbitrage.run --deadline 2026-10-05T23:00      # 終了時刻の上限。過ぎたら打ち切って確認済みの分を保存 (終了コード 0)
.venv/bin/python -m arbitrage.worker                               # 待ち受け: 結果ページの「リサーチ実行」を受けて run を起動する (Ctrl+C で停止)
.venv/bin/python -m pytest -q tests                                # 単体テスト
```

- 判定式: `買取価格 > (販売価格 − クーポン) − 付与ポイント`。クーポンは 1 注文 1 枚 (併用なし): ページの公開クーポンと手持ちクーポンのうち利益が最大になる 1 枚、または使わない。ポイントは「1 つだけ買う」前提で、枠ごとに `min(floor(クーポン後の税抜価格 × 率), 上限)` を合計。税抜 = 税込 − floor(税込 × 10/110)
- 利益率 = (買取価格 − 実質価格) / 実質価格。送料・手数料・減額リスクは考えない
- 流れ (`arbitrage/run.py`)
  1. 買取価格 (`buyback.py`): `price_history` の直近7日の新品価格から、JAN ごとに「各買取店の最新価格」の最高値。`products.category` を大分類 (`categories.py`) に寄せて `Buyback.category` に持つ (古い run の `buyback.json` には無く None。`--category` で絞るとその JAN は対象外・警告 1 回)
  2. 商品検索API v3 (`yahoo_api.py`): `jan_code` で新品・在庫ありを価格の安い順に取得。**実測の制限は時計の 1 分ごとに 30 回** (公式の記載は 1 秒 1 回) → 2.15 秒間隔。429 は次の分の変わり目まで待って再試行
  3. 絞り込み (`prefilter.py`): 店舗リストの店だけ残し、甘い実質価格 `販売価格 − max(販売価格 × クーポン余地, 使える手持ちクーポンの最大値引き額) − クーポン前の価格で付きうるポイント` が買取価格を下回るものを候補にする
  4. 確定判定 (`scope.py` / `yahoo_page.py` / `finalize.py`): **届きそうな候補だけを、届きやすい順に**商品ページで確認し、実際の上乗せ率とクーポンで再計算。黒字だけを結果にする
     - 背景 (2026-10-04 の実測): 絞り込みはクーポン余地 20% 込みの甘い判定で、以前はその利益額の順に全件確認していた。この順だとポイント上限で届かない高額品ばかり先に見てしまい、黒字に届く候補は全体の 2〜3 割しかなかった
     - 見積もり `realistic_gap` (`prefilter.py`) = 買取価格 − クーポン余地なしの実質価格 (上乗せ率は店の最大値、値引きは使える手持ちクーポンの最大額だけ。ページの公開クーポンは見込まない)
     - 届きそう = `realistic_gap + 販売価格 × reach_slack >= min(min_profit, 0)`。確認順は `realistic_gap / 販売価格` の大きい順。`--page-scope all` は届きそうな候補を先に、残りを同じ順で続ける
     - 範囲外にした候補は「未確認」のエラーにしない (件数を INFO ログに出して正常終了)。`--page-scope` / `--reach-slack` / `--min-profit` / `--deadline` は再開条件 (meta) に含めないので、同じ run のまま範囲を広げて続きを確認できる
  5. 保存 (`db.py`): `arbitrage_runs` / `arbitrage_results` に service_role キーで upsert
- 設定 (`config.py`): 結果ページの「設定」画面で編集 → Supabase `arbitrage_settings` (1 行、`config` jsonb。スキーマは `db/arbitrage_settings.sql`) → 実行時に service_role キーで読む。行が無いとエラーで止まる
  - 中身: `purchase_date` (購入予定日)、`common` (毎日付く分)、`campaigns` (日付指定。`dates` に実行日を含むものだけ有効)、`coupons` (手持ちクーポン。有効期間内のものだけ有効)、`bsplus` (上限のみ)。不明なキー・範囲外の値はエラーで止まる
  - **実行日 = `--date` があればそれ、無ければ `purchase_date`**。その日のボーナスストアPlus枠・キャンペーン・クーポンで計算する。買取価格は実行時点の直近 7 日分
  - `--config-file` 用のひな形と各キーの説明: `config/campaigns.example.yaml` (書式は Supabase の設定 JSON と同じ)
- 入力ファイル (Git に含めない)
  - `data/yahoo_pp_stores_5genres.xlsx`: 店舗リスト。「全ジャンル」(日別のボーナスストアPlus枠 +4/+9、優良ストア)、「ポイント上乗せ調査」(最大上乗せ率。無い店は 14% と仮定)、「除外リスト（非適格）」。**日別枠は掲載期間内の日付しか無い** → 期間外の日付で実行するとエラー。新しいリストに差し替える
  - `.env`: `YAHOO_CLIENT_ID` (商品検索API)、`SUPABASE_URL` / `SUPABASE_KEY` (service_role。設定の読み込みと結果の保存)
- ポイントの出どころ
  - ストアポイント (基本 1% + 上乗せ): 商品ページ。上乗せ率は専用項目が無く `totalPointRatio − 標準で付く各行の率の合計`
  - ボーナスストアPlus (+4% / +9%): 店舗リストの実行日の枠
  - それ以外 (PayPay、LYP、日曜 +5% など): 設定の `common` / `campaigns`。対象ストア限定のものは `target: page` (商品ページの内訳にその行が出ている店)
  - **全体加算日の +2% / +3% は自動では付かない**。別々の上限を持つ独立したキャンペーンなので `campaigns` に `target: bsplus` (+2%) と `target: bsplus_good` (+3%、枠あり かつ 優良ストア) の 2 行で登録する (優良ストアには両方付く)。店舗リスト 1 行目の「+2」「+2/+3」表記は計算に使わない
- 手持ちクーポン (`coupons.py`): 設定の `coupons` (定額 / 定率、最低購入額、対象店、有効期間)。値引き額が販売価格以上になる商品には使わない。結果行の `coupon` は `source` が `manual` (手持ち) か `page` (ページの公開クーポン)
- クーポン一覧: 確定判定で見つけたクーポンは、その商品に使えないものも含めて `data/arbitrage/{run名}/coupons.csv` に出力する (店舗 × クーポン単位)
- 再開: 状態は `data/arbitrage/{run名}/` (`meta.json` / `buyback.json` / `api.jsonl` / `pages.jsonl`)。中断時のログに出る「再開」コマンドで続きから。条件 (実行日・その日に有効な設定・店舗リスト) を変えたら `--run-name` で別名にする (日付外のキャンペーン・クーポンの編集は影響しない)
- 結果ページ `docs/arbitrage.html`: Supabase Auth のメールリンクでログイン。RLS でオーナー (`arbitrage_owner` の 1 行) だけが読める。スキーマは `db/arbitrage.sql`

- 設定の補足: 金額帯で率が変わる企画は `min_purchase` / `max_purchase` で帯ごとに 1 行ずつ登録する。購入予定日が今日でない場合、公開クーポンは期限が購入予定日まで残っているものだけ使い、`target: page` は今日のページで判定される (警告が出る)。既定の run 名は `{実行した日}-buy{購入予定日}`
- 設定テーブルは `db/arbitrage_settings.sql` (オーナーだけ読み書き可)。カレンダー (貴社内限定の資料) 由来の日程は公開リポジトリに入れず、Supabase の設定にだけ置く

### リサーチタブと待ち受け (`worker.py` / `research.py` / `progress.py`)

結果ページの「リサーチ」タブで条件を決めて「リサーチ実行」→ この PC の待ち受けが run を起動する。**待ち受けを起動していないと依頼は queued のまま**。

- スキーマ: `db/arbitrage_research.sql` (`db/arbitrage.sql` の後に実行。再実行可)
  - `arbitrage_research_requests`: 依頼 1 行 = 1 回の実行。`status` は queued → running → completed / failed / cancelled。**待ち・実行中の依頼は同時に 1 件だけ** (部分ユニークインデックス)。オーナーが書けるのは INSERT の `params` と UPDATE の `cancel_requested` だけ (列単位 GRANT)
  - `arbitrage_worker_state` (1 行): `heartbeat_at` (10 秒ごと)、`buyback_histogram` (買取価格 1,000 円刻みの JAN 数 `counts` と大分類ごとの `by_group`。30 分ごと)、`rates` (実測の速度)
- 依頼の `params` (ページと `research.parse_params` の両方で検証。不明なキー・範囲外は拒否): `purchase_date` (必須。`--date` に渡す)、`min_buyback` / `max_buyback`、`min_profit` (-5000〜100000)、`page_scope`、`reach_slack` (0〜0.2)、`deadline` (タイムゾーン付き ISO 日時)、`categories` (大分類名の配列。null / 空 = すべて。要素ごとに `--category` に渡す)
- 商品分類は大分類 (`arbitrage/categories.py` の対応表 `category_group(source, category)`。GROUPS の名前と順序は `docs/arbitrage.js` と同じ) で絞る。`products.category` は最後に書いたサイトの値なので、同じ JAN でもサイトによって分類が変わりうる。対応表に無い値 (rudeya の新カテゴリなど) は「その他」。`--category` は `--min-buyback` と同じく対象を絞るだけで再開条件 (meta) には含めない
- 待ち受けの動き: いちばん古い queued を 1 件取り、`[python, -m, arbitrage.run, *build_run_args()]` を子プロセスで起動 (シェルを通さない。引数は検証済みの値から作り直す)。出力は `data/arbitrage/requests/{依頼ID}.log`
  - 終了コード 0 → completed / 3 → cancelled (中止の依頼) / それ以外 → failed (`message` はログ末尾の ERROR 行。「再開:」の行は飛ばす)
  - 毎周期、子プロセスを持っていないのに running の依頼を failed にする (PID ファイル `data/arbitrage/requests/{依頼ID}.pid` の run がまだ動いていれば SIGINT で止めてから)。ただし `arbitrage_worker_state.heartbeat_at` が 60 秒以内なら他の待ち受けが動いているとみなし、古くなるまで何もしない。同じ PC での二重起動は `data/arbitrage/worker.lock` (flock) で防ぐ
  - 終了状態の書き込みは 3 回やり直し、だめなら `pending_finish` に残して周期ごとに再送する (run_id の外部キー違反なら run_id を外す)。終了状態の更新は `status='running'` の行だけ
  - Ctrl+C (SIGTERM / SIGHUP も同じ) は子プロセスに SIGINT を送って待ち、依頼を failed にする → **同じ条件で実行し直すと続きから再開**
  - 完了した run が JAN 200 件以上を処理していたら、その実測 (`meta.json` の `stats`) で `rates` を更新する
- run 側 (`--request-id N`): 進捗 (`stage` = starting / api / pages / saving / done、件数、`eta`) を 20 秒以上の間隔で依頼行の `progress` に書き、同じタイミングで `cancel_requested` を読む。中止なら確認済みの分を保存して終了コード 3。進捗の書き込み失敗では止めない
  - `--deadline` を過ぎたとき・中止のときは `--stage save` と同じ扱い (API 段階の途中なら検索済みの JAN だけを対象に保存。確定判定はしない)
  - 同じ日に設定を変えて依頼し直すと、既定の run 名が別条件で使われている → 依頼からの実行に限り `{run名}-c{条件のハッシュ8桁}` にする (同じ条件なら同じ名前になり、中断後も続きから再開できる。手動実行は従来どおりエラー)
  - run フォルダは `run.lock` (flock) で排他。別プロセスが同じ run を実行中なら終了コード 2
- 所要時間の予測 (ページの JS と `research.estimate` で同じ式)
  - 対象 JAN 数 n = histogram のうち買取価格の範囲に入る帯の合計 (帯の途中は按分)。`categories` があれば `by_group` の該当分類の合計、無ければ `counts`
  - API 段階 (分) = n / `jan_per_min`、確定判定 (秒) = n × `candidates_per_jan` × (reachable なら `reachable_fraction`、all なら 1) × `seconds_per_check`
  - 既定の rates (2026-10-04 の実測): jan_per_min 27.6 / candidates_per_jan 1.1 / reachable_fraction 0.28 / seconds_per_check 4.1
  - 所要時間に効くのは 買取価格の範囲 (JAN 数)・`page_scope`・`reach_slack` / `min_profit` (届きそうの広さ)・`deadline`

### 商品ページの落とし穴 (2026-10-04 調査)
- ストアクーポンは HTML に無い。`POST /syene-bff/v1/pc/coupon/v3/{店舗ID}/{商品コード}` で取る。ページが発行する匿名 Cookie と `page.crumb` が必要 (無いと 401)
- **ログインなしでは見えないクーポンがある**: アカウントで獲得済みの限定クーポン (例: Joshin の「60,000円以上で 2,000円OFF」) は、価格や会員フラグを変えて問い合わせても返らない → `coupons.csv` には載らない。設定の `coupons` (手持ちクーポン) に手で登録する
- クーポン一覧は商品単位で絞り込み済み。詳細ページの対象商品リストは一部しか載らないので、対象判定に使わない
- ページのポイント数はクーポン適用前の価格で計算されている → 率だけ採って再計算する
- ページの「上限到達」フラグは上限で切られていても false のまま → 上限は設定 (`cap_per_order` / `cap_per_period`) で持つ
- ページの合計率には未ログイン時の仮の表示 (LINE 連携 2.5%、PayPay 残高 0.5%) が含まれる → そのまま使うと二重計上
- 日曜 +5% は行が出ていても、安い商品 (5,000円前後未満) には付かない → `min_purchase` で指定
- 色・サイズ違いのある商品 (`individualItemList`) は確定判定から除外している
- 結果ページでログインすると同じサイトの検索ページもログイン状態になり、公開テーブルが 0 件になる → `arbitrage.js` はログイン情報の保存先 (`storageKey`) を分けている

## DBスキーマ概要

- `products` (jan_code PK, name, source, category, ...)
- `price_history` (jan_code, source, condition, scraped_date, price, ...) - 主キー4列
- `arbitrage_runs` / `arbitrage_results` / `arbitrage_owner` (`db/arbitrage.sql`): 利益商品の結果。オーナーだけ SELECT 可、anon は常に 0 件
- `arbitrage_research_requests` / `arbitrage_worker_state` (`db/arbitrage_research.sql`): リサーチの依頼と待ち受けの状態。オーナーだけ SELECT 可
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
