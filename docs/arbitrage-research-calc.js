// 利益商品リスト - 「リサーチ」画面の、DOM に依存しない純粋関数 (node で確認できる)
// 画面は arbitrage-research.js。依頼の params の検証・組み立て、所要時間の予測、その日に有効な
// キャンペーン・クーポンの抽出、実行状況の判定、表示整形をここに置く。
import {
  isValidDate, isCampaignActive, isCouponActive, parseOptionalInt, percentToRate, rateToPercent,
} from './arbitrage-settings.js';

// worker_state の rates が無い・欠けているときの値 (arbitrage/research.py と同じ)
export const DEFAULT_RATES = Object.freeze({
  jan_per_min: 27.6,          // API 段階で 1 分に進む JAN 数
  candidates_per_jan: 1.1,    // 1 JAN あたりの候補数
  reachable_fraction: 0.28,   // 候補のうち「届きそう」と見なされる割合
  seconds_per_check: 4.1,     // 確定判定 1 件にかかる秒数
});
export const DEFAULT_PARAMS = Object.freeze({
  min_buyback: null, max_buyback: null, min_profit: 1, page_scope: 'reachable', reach_slack: 0.03, deadline: null, categories: null,
});
// 商品の大分類 (名前と順序は arbitrage/categories.py と同じ)
export const CATEGORY_GROUPS = Object.freeze([
  '家電', 'スマホ・タブレット', 'パソコン・周辺機器', 'カメラ', 'ゲーム', 'オーディオ', 'ウェアラブル', 'トレカ・ホビー', 'お酒', '化粧品', 'その他',
]);
const PARAM_KEYS = ['purchase_date', 'min_buyback', 'max_buyback', 'min_profit', 'page_scope', 'reach_slack', 'deadline', 'categories'];
const PAGE_SCOPES = ['reachable', 'all'];
export const MIN_PROFIT_RANGE = [-5000, 100000];
const MAX_REACH_SLACK = 0.2;
const HEARTBEAT_ALIVE_SECONDS = 90;
export const ACTIVE_STATUSES = ['queued', 'running'];

export const STATUS_LABELS = { queued: '待機中', running: '実行中', completed: '完了', failed: '失敗', cancelled: '中止' };
export const STAGE_LABELS = { starting: '準備中', api: 'API 段階', pages: '確定判定', saving: '保存', done: '完了' };

// ---------- 純粋関数: 変換 ----------
export const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
export const str = (v) => (v == null ? '' : String(v));
export const isInt = (v) => typeof v === 'number' && Number.isSafeInteger(v);
export const num = (n) => Number(n ?? 0).toLocaleString('ja-JP');
const pad2 = (n) => String(n).padStart(2, '0');

/** 符号つきの整数。空欄・小数・文字は null (全角数字・桁区切り・全角マイナスを許す) */
export function parseSignedInt(text) {
  const s = str(text).normalize('NFKC').replace(/[,\s]/g, '').replace(/^[−ー‐]/, '-');
  if (!/^-?\d+$/.test(s) || !Number.isSafeInteger(Number(s))) return null;
  return Number(s);
}

/** "HH:MM" (24 時間表記) か */
export function isValidTime(s) {
  return typeof s === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(s);
}

/** "HH:MM" → 現在時刻より後で最初に来るその時刻 (端末の時間帯) の ISO 文字列。不正なら null */
export function nextOccurrence(hhmm, now = new Date()) {
  if (!isValidTime(hhmm)) return null;
  const [hours, minutes] = hhmm.split(':').map(Number);
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hours, minutes, 0, 0);
  if (d.getTime() <= now.getTime()) d.setDate(d.getDate() + 1);
  return d.toISOString();
}

/** タイムゾーン付きの ISO 8601 日時か */
export function isValidDeadline(s) {
  return typeof s === 'string'
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$/.test(s)
    && !Number.isNaN(new Date(s).getTime());
}

// ---------- 純粋関数: 商品分類 ----------
/**
 * 大分類の配列 → params.categories。一覧にある名前だけを一覧の順に残し、重複は捨てる。
 * 配列でない・空・残るものが無い → null (= すべて)
 */
export function normalizeCategories(list) {
  if (!Array.isArray(list)) return null;
  const picked = CATEGORY_GROUPS.filter((g) => list.includes(g));
  return picked.length === 0 ? null : picked;
}

/** params.categories が契約どおりか (null / 一覧の文字列だけの配列・重複なし・空でない) */
export function categoriesError(value) {
  if (value == null) return null;
  if (!Array.isArray(value)) return '商品分類の形式が正しくありません';
  if (value.length === 0) return null;   // 空配列 = すべて (契約で許す)
  if (value.some((g) => !CATEGORY_GROUPS.includes(g))) return '商品分類に不明な名前があります';
  if (new Set(value).size !== value.length) return '商品分類が重複しています';
  return null;
}

/** 条件の要約に出す分類: 「分類: 家電, カメラ」/ 「分類: すべて」 */
export function describeCategories(value) {
  const list = normalizeCategories(value);
  return `分類: ${list ? list.join(', ') : 'すべて'}`;
}

// ---------- 純粋関数: 依頼の params ----------
/** 契約どおりの params か調べる。[{ path, message }] (空なら合格)。不明なキー・範囲外は拒否 */
export function validateParams(params) {
  if (!isObject(params)) return [{ path: '', message: '条件の形式が正しくありません' }];
  const errors = [];
  const fail = (path, message) => errors.push({ path, message });
  for (const key of Object.keys(params)) {
    if (!PARAM_KEYS.includes(key)) fail(key, `不明な項目です: ${key}`);
  }
  if (!isValidDate(params.purchase_date)) fail('purchase_date', '日付を YYYY-MM-DD の形で入力してください');
  const min = params.min_buyback ?? null;
  const max = params.max_buyback ?? null;
  if (min !== null && !(isInt(min) && min >= 0)) fail('min_buyback', '0 以上の整数で入力するか、空欄 (指定なし) にしてください');
  if (max !== null && !(isInt(max) && max >= 0)) fail('max_buyback', '0 以上の整数で入力するか、空欄 (指定なし) にしてください');
  else if (max !== null && isInt(min) && max < min) fail('max_buyback', '上限は下限以上にしてください');
  if (!(isInt(params.min_profit) && params.min_profit >= MIN_PROFIT_RANGE[0] && params.min_profit <= MIN_PROFIT_RANGE[1])) {
    fail('min_profit', `${num(MIN_PROFIT_RANGE[0])} 以上 ${num(MIN_PROFIT_RANGE[1])} 以下の整数で入力してください`);
  }
  if (!PAGE_SCOPES.includes(params.page_scope)) fail('page_scope', '確定判定の範囲を選んでください');
  const slack = params.reach_slack;
  if (!(typeof slack === 'number' && Number.isFinite(slack) && slack >= 0 && slack <= MAX_REACH_SLACK)) {
    fail('reach_slack', '0 以上 20 以下の数値 (%) で入力してください');
  }
  if ((params.deadline ?? null) !== null && !isValidDeadline(params.deadline)) {
    fail('deadline', '時刻を HH:MM の形で入力するか、空欄 (上限なし) にしてください');
  }
  const categoriesMessage = categoriesError(params.categories);
  if (categoriesMessage) fail('categories', categoriesMessage);
  return errors;
}

/**
 * 入力欄の文字列 → { params, errors }。errors は [{ path, message }] (path は入力欄の data-field)。
 * form: { purchase_date, min_buyback, max_buyback, min_profit, page_scope, reach_slack (%), deadline_time (HH:MM),
 *         categories (大分類名の配列。空 = すべて) }
 */
export function buildParams(form, now = new Date()) {
  const errors = [];
  const fail = (path, message) => errors.push({ path, message });
  const optInt = (text, path) => {
    const { ok, value } = parseOptionalInt(text);
    if (!ok) fail(path, '0 以上の整数で入力するか、空欄 (指定なし) にしてください');
    return ok ? value : null;
  };
  const minBuyback = optInt(form.min_buyback, 'min_buyback');
  const maxBuyback = optInt(form.max_buyback, 'max_buyback');
  const minProfit = parseSignedInt(form.min_profit);
  const pageScope = str(form.page_scope);
  // 「全候補」のときは余地を使わないので、入力が不正でも既定値で送る
  let reachSlack = percentToRate(form.reach_slack);
  if (pageScope === 'all' && !(reachSlack !== null && reachSlack <= MAX_REACH_SLACK)) reachSlack = DEFAULT_PARAMS.reach_slack;
  const time = str(form.deadline_time).trim();
  const deadline = time === '' ? null : nextOccurrence(time, now);
  if (time !== '' && deadline === null) fail('deadline', '時刻を HH:MM の形で入力するか、空欄 (上限なし) にしてください');

  const params = {
    purchase_date: str(form.purchase_date), min_buyback: minBuyback, max_buyback: maxBuyback,
    min_profit: minProfit, page_scope: pageScope, reach_slack: reachSlack, deadline,
    categories: normalizeCategories(form.categories),
  };
  // 1 つの入力欄につき最初の 1 件だけ残す
  for (const e of validateParams(params)) {
    if (!errors.some((x) => x.path === e.path)) errors.push(e);
  }
  return { params, errors };
}

/** 前回の依頼の params → 入力欄の文字列。壊れた値は既定値にする。終了時刻は引き継がない (毎回決め直す) */
export function paramsToForm(params, purchaseDate) {
  const p = isObject(params) ? params : {};
  const intText = (v) => (isInt(v) && v >= 0 ? String(v) : '');
  const slack = typeof p.reach_slack === 'number' && p.reach_slack >= 0 && p.reach_slack <= MAX_REACH_SLACK
    ? p.reach_slack : DEFAULT_PARAMS.reach_slack;
  return {
    purchase_date: str(purchaseDate),
    min_buyback: intText(p.min_buyback), max_buyback: intText(p.max_buyback),
    min_profit: String(isInt(p.min_profit) ? p.min_profit : DEFAULT_PARAMS.min_profit),
    page_scope: PAGE_SCOPES.includes(p.page_scope) ? p.page_scope : DEFAULT_PARAMS.page_scope,
    reach_slack: rateToPercent(slack),
    deadline_time: '',
    categories: normalizeCategories(p.categories) ?? [],
  };
}

/** 履歴に出す条件の要約 (1 行) */
export function summarizeParams(params) {
  const p = isObject(params) ? params : {};
  const min = isInt(p.min_buyback) ? p.min_buyback : null;
  const max = isInt(p.max_buyback) ? p.max_buyback : null;
  const range = min === null && max === null ? '買取 指定なし'
    : `買取 ${min === null ? '' : num(min)}〜${max === null ? '' : num(max)}円`;
  const parts = [`購入 ${str(p.purchase_date) || '?'}`, range, describeCategories(p.categories)];
  if (isInt(p.min_profit)) parts.push(`利益 ${num(p.min_profit)}円以上`);
  parts.push(p.page_scope === 'all' ? '全候補'
    : `届きそうな候補${typeof p.reach_slack === 'number' ? ` (余地 ${rateToPercent(p.reach_slack)}%)` : ''}`);
  const time = formatTime(p.deadline);
  if (time) parts.push(`${time} まで`);
  return parts.join(' / ');
}

// ---------- 純粋関数: 所要時間の予測 (式は arbitrage/research.py と同じ) ----------
/** rates の欠け・不正な値を既定値で埋める */
export function effectiveRates(rates) {
  const r = isObject(rates) ? rates : {};
  const positive = (key, max = Infinity) =>
    (typeof r[key] === 'number' && Number.isFinite(r[key]) && r[key] > 0 && r[key] <= max ? r[key] : DEFAULT_RATES[key]);
  return {
    jan_per_min: positive('jan_per_min'),
    candidates_per_jan: positive('candidates_per_jan'),
    reachable_fraction: positive('reachable_fraction', 1),
    seconds_per_check: positive('seconds_per_check'),
  };
}

/** 1 つの counts ({ "帯の下端": JAN 数 }) のうち、買取価格が lo 以上 hi 未満の JAN 数。帯の途中の境界は按分する */
function countInRange(counts, bucket, lo, hi) {
  let total = 0;
  for (const [key, value] of Object.entries(counts)) {
    const start = Number(key);
    const count = Number(value);
    if (!Number.isFinite(start) || !Number.isFinite(count) || count <= 0) continue;
    const overlap = Math.min(start + bucket, hi) - Math.max(start, lo);
    if (overlap > 0) total += count * (overlap / bucket);
  }
  return total;
}

const histogramBucket = (histogram) =>
  (typeof histogram.bucket === 'number' && histogram.bucket > 0 ? histogram.bucket : 1000);
const rangeBounds = (minBuyback, maxBuyback) =>
  [minBuyback ?? 0, maxBuyback === null ? Infinity : maxBuyback + 1];   // 整数の円なので max を含む半開区間にする

/** histogram に大分類ごとの件数 (by_group) があるか (古い待ち受けの行には無い) */
export function hasGroupCounts(histogram) {
  return isObject(histogram) && isObject(histogram.by_group);
}

/**
 * 買取価格が min〜max (両端を含む。null = 指定なし) の JAN 数。
 * histogram: { bucket, counts: { "帯の下端": JAN 数 }, by_group?: { 大分類: counts } }。使えない形なら null。
 * categories (大分類の配列) があり by_group もあれば、その大分類の合計。by_group が無ければ全体 (counts) で数える
 */
export function countJans(histogram, minBuyback = null, maxBuyback = null, categories = null) {
  if (!isObject(histogram) || !isObject(histogram.counts)) return null;
  const bucket = histogramBucket(histogram);
  const [lo, hi] = rangeBounds(minBuyback, maxBuyback);
  const groups = normalizeCategories(categories);
  if (groups && hasGroupCounts(histogram)) {
    return groups.reduce((sum, g) => sum + (isObject(histogram.by_group[g]) ? countInRange(histogram.by_group[g], bucket, lo, hi) : 0), 0);
  }
  return countInRange(histogram.counts, bucket, lo, hi);
}

/**
 * 大分類ごとの JAN 数 (買取価格の範囲を反映)。{ 大分類: 件数 } (すべての大分類をキーに持つ)。
 * by_group が無ければ null
 */
export function countJansByGroup(histogram, minBuyback = null, maxBuyback = null) {
  if (!hasGroupCounts(histogram)) return null;
  const bucket = histogramBucket(histogram);
  const [lo, hi] = rangeBounds(minBuyback, maxBuyback);
  return Object.fromEntries(CATEGORY_GROUPS.map((g) =>
    [g, isObject(histogram.by_group[g]) ? countInRange(histogram.by_group[g], bucket, lo, hi) : 0]));
}

/**
 * 所要時間の予測。histogram が無ければ null。
 * categoriesApplied: 分類の指定を件数に反映できたか (指定なし、または by_group が無いときは false)
 */
export function estimateDuration({ histogram, rates, minBuyback = null, maxBuyback = null, pageScope = 'reachable', categories = null }) {
  const jans = countJans(histogram, minBuyback, maxBuyback, categories);
  if (jans === null) return null;
  const r = effectiveRates(rates);
  const apiSeconds = (jans / r.jan_per_min) * 60;
  const checks = jans * r.candidates_per_jan * (pageScope === 'all' ? 1 : r.reachable_fraction);
  const checkSeconds = checks * r.seconds_per_check;
  const categoriesApplied = normalizeCategories(categories) !== null && hasGroupCounts(histogram);
  return { jans, apiSeconds, checks, checkSeconds, totalSeconds: apiSeconds + checkSeconds, categoriesApplied };
}

/**
 * 終了時刻の上限と予測の関係。
 * level: 'ok' (間に合う) / 'pages' (確定判定の途中で切れる) / 'api' (API 段階も終わらない)
 */
export function checkDeadline(estimate, deadlineIso, now = new Date()) {
  if (!estimate || !deadlineIso) return { level: 'ok', checksPossible: null };
  const available = (new Date(deadlineIso).getTime() - now.getTime()) / 1000;
  if (Number.isNaN(available) || available >= estimate.totalSeconds) return { level: 'ok', checksPossible: null };
  if (available < estimate.apiSeconds) return { level: 'api', checksPossible: 0 };
  const perCheck = estimate.checks > 0 ? estimate.checkSeconds / estimate.checks : 0;
  return { level: 'pages', checksPossible: perCheck > 0 ? Math.floor((available - estimate.apiSeconds) / perCheck) : 0 };
}

/** 秒 → 「約 5 時間 10 分」 */
export function formatDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '不明';
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return '1 分未満';
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `約 ${m} 分`;
  return m === 0 ? `約 ${h} 時間` : `約 ${h} 時間 ${m} 分`;
}

// ---------- 純粋関数: その日に有効なキャンペーン・クーポン ----------
const objectRows = (v) => (Array.isArray(v) ? v.filter(isObject) : []);

/** 設定のうち、その日に使われるもの */
export function activeOnDate(config, date) {
  const c = isObject(config) ? config : {};
  return {
    common: objectRows(c.common),
    campaigns: objectRows(c.campaigns).filter((row) => isCampaignActive(Array.isArray(row.dates) ? row.dates : [], date)),
    coupons: objectRows(c.coupons).filter((row) => isCouponActive(row.valid_from, row.valid_until, date)),
  };
}

/** ポイント (毎日付く分・キャンペーン) の 1 行表示: 名前 / +5% / 上限など */
export function describePoint(row) {
  const notes = [];
  if (isInt(row.cap_per_order)) notes.push(`上限 ${num(row.cap_per_order)}pt/注文`);
  if (isInt(row.cap_per_period)) notes.push(`上限 ${num(row.cap_per_period)}pt/期間`);
  if (!isInt(row.cap_per_order) && !isInt(row.cap_per_period)) notes.push('上限なし');
  if (isInt(row.min_purchase) && row.min_purchase > 0) notes.push(`${num(row.min_purchase)}円以上`);
  if (isInt(row.max_purchase)) notes.push(`${num(row.max_purchase)}円以下`);
  if (row.entry_required === true) notes.push('要エントリー');
  return { name: str(row.name) || '（名前なし）', value: `+${rateToPercent(row.rate) || '?'}%`, note: notes.join('、') };
}

/** 手持ちクーポンの 1 行表示: 名前 / −2,000円 / 条件 */
export function describeCoupon(row) {
  const percent = row.type === 'percent';
  const notes = [];
  if (percent) notes.push(isInt(row.max_discount) ? `上限 ${num(row.max_discount)}円` : '上限なし');
  if (isInt(row.min_purchase) && row.min_purchase > 0) notes.push(`${num(row.min_purchase)}円以上`);
  if (row.target === 'stores') notes.push('対象店のみ');
  if (row.valid_until) notes.push(`${str(row.valid_until)} まで`);
  return {
    name: str(row.name) || '（名前なし）',
    value: percent ? `−${rateToPercent(row.value) || '?'}%` : `−${isInt(row.value) ? num(row.value) : '?'}円`,
    note: notes.join('、'),
  };
}

// ---------- 純粋関数: 実行状況 ----------
export function isActiveStatus(status) {
  return ACTIVE_STATUSES.includes(status);
}

/** 「結果を見る」を出せるか: 終了していて、保存済みの実行 (run_id) がある。中止・途中で失敗した依頼も含む */
export function canShowResults(request) {
  return isObject(request) && request.run_id != null
    && ['completed', 'cancelled', 'failed'].includes(request.status);
}

/** 前回は有効 (待機中・実行中) だった依頼が、今回は終了しているか (実行一覧の読み直しの合図) */
export function hasNewlyFinished(before, after) {
  const wasActive = new Set(objectRows(before).filter((r) => isActiveStatus(r.status)).map((r) => r.id));
  return objectRows(after).some((r) => wasActive.has(r.id) && !isActiveStatus(r.status));
}

/**
 * タブに戻ってきたとき、入力欄の購入予定日を設定の値で上書きするか。上書きする日付、しないなら null。
 * 上書きするのは、設定の購入予定日が前回読んだ値 (lastSeen) から変わったときだけ
 * (リサーチ画面で入力した日付を、変わっていない設定の値で黙って戻さない)。
 */
export function purchaseDateToApply(lastSeen, settingsDate, fieldValue) {
  if (!settingsDate || settingsDate === lastSeen || settingsDate === fieldValue) return null;
  return settingsDate;
}

/** 待ち受けが生きているか (heartbeat が 90 秒以内) */
export function isWorkerAlive(heartbeatAt, now = new Date()) {
  if (!heartbeatAt) return false;
  const t = new Date(heartbeatAt).getTime();
  return !Number.isNaN(t) && now.getTime() - t <= HEARTBEAT_ALIVE_SECONDS * 1000;
}

/** 進捗バーの割合 (0〜100 の整数)。件数が不明なら null */
export function progressPercent(done, total) {
  const d = Number(done);
  const t = Number(total);
  if (!Number.isFinite(d) || !Number.isFinite(t) || t <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((d / t) * 100)));
}

/** テーブルがまだ作られていない (SQL 未実行) ときのエラーか */
export function isMissingTableError(error) {
  return Boolean(error) && (error.code === 'PGRST205' || error.code === '42P01');
}

export function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' });
}

function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

export function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}
