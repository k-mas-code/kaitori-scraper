// 利益商品リスト - 「設定」画面 (ポイント・キャンペーン日程・手持ちクーポン・購入予定日)
// 前半は DOM に依存しない純粋関数 (node で確認できる)、後半が画面。

const BASES = [['tax_excluded', '税抜'], ['tax_included', '税込']];
const CAMPAIGN_TARGETS = [
  ['all', 'すべての店'],
  ['bsplus', 'ボーナスストアPlus枠の店'],
  ['bsplus_good', 'ボーナスストアPlus枠かつ優良ストア'],
  ['good_store', '優良ストア'],
  ['page', '商品ページに行が出ている店'],
  ['stores', '店IDを指定'],
];
const COUPON_TARGETS = [['all', 'すべての店'], ['stores', '店IDを指定']];
const COUPON_TYPES = [['fixed', '定額 (円)'], ['percent', '定率 (%)']];
const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];

const POINT_KEYS = ['name', 'rate', 'base', 'cap_per_order', 'cap_per_period', 'min_purchase', 'max_purchase'];
const KNOWN_KEYS = {
  top: ['version', 'purchase_date', 'common', 'campaigns', 'coupons', 'bsplus'],
  common: POINT_KEYS,
  campaigns: [...POINT_KEYS, 'entry_required', 'target', 'page_title', 'store_ids', 'dates'],
  coupons: ['name', 'type', 'value', 'max_discount', 'min_purchase', 'target', 'store_ids', 'valid_from', 'valid_until'],
  bsplus: ['cap_per_order', 'cap_per_period'],
};

// ---------- 純粋関数: 変換 ----------
const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const str = (v) => (v == null ? '' : String(v));
const has = (pairs, value) => pairs.some(([v]) => v === value);
// 全角数字と桁区切りのカンマ・空白を許す
const normalizeNumber = (text) => str(text).normalize('NFKC').replace(/[,\s]/g, '');

/** "% 表記の文字列" → 小数。数値でなければ null。2.5 → 0.025 (浮動小数の誤差を丸める) */
export function percentToRate(text) {
  const s = normalizeNumber(text).replace(/%$/, '');
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  return Math.round(Number(s) * 1e6) / 1e8;
}

/** 小数 → "% 表記の文字列"。0.025 → "2.5" */
export function rateToPercent(rate) {
  if (typeof rate !== 'number' || !Number.isFinite(rate)) return '';
  return String(Math.round(rate * 1e8) / 1e6);
}

/** 0 以上の整数。空欄は null。それ以外 (小数・負数・文字) は ok=false */
export function parseOptionalInt(text) {
  const s = normalizeNumber(text);
  if (s === '') return { ok: true, value: null };
  if (!/^\d+$/.test(s) || !Number.isSafeInteger(Number(s))) return { ok: false, value: null };
  return { ok: true, value: Number(s) };
}

/** 実在する YYYY-MM-DD か */
export function isValidDate(s) {
  // 年 0000 は Python 側 (date.fromisoformat) が受け付けないので弾く
  if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s) || s < '0001-01-01') return false;
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

export function isValidMonth(s) {
  return typeof s === 'string' && /^\d{4}-(0[1-9]|1[0-2])$/.test(s);
}

/** "2026-12" を delta か月ずらす */
export function shiftMonth(month, delta) {
  const [y, m] = month.split('-').map(Number);
  const index = y * 12 + (m - 1) + delta;
  return `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, '0')}`;
}

/** 日付の選択/解除 (新しい配列を昇順で返す) */
export function toggleDate(dates, date) {
  return dates.includes(date) ? dates.filter((d) => d !== date) : [...dates, date].sort();
}

/** カンマ区切り (読点・空白・改行も可) の店ID → 重複のない配列 */
export function parseStoreIds(text) {
  return [...new Set(str(text).normalize('NFKC').split(/[,、\s]+/).filter(Boolean))];
}

export function isCampaignActive(dates, purchaseDate) {
  return isValidDate(purchaseDate) && dates.includes(purchaseDate);
}

export function isCouponActive(validFrom, validUntil, purchaseDate) {
  if (!isValidDate(purchaseDate)) return false;
  if (validFrom && (!isValidDate(validFrom) || purchaseDate < validFrom)) return false;
  if (validUntil && (!isValidDate(validUntil) || validUntil < purchaseDate)) return false;
  return true;
}

/** 入れ子のオブジェクト/配列の 1 か所だけ差し替えた新しい値を返す (元は変更しない) */
export function setIn(obj, keys, value) {
  if (keys.length === 0) return value;
  const [key, ...rest] = keys;
  if (Array.isArray(obj)) return obj.map((v, i) => (i === Number(key) ? setIn(v, rest, value) : v));
  return { ...obj, [key]: setIn(obj[key], rest, value) };
}

export function emptyConfig(today) {
  return {
    version: 1, purchase_date: today, common: [], campaigns: [], coupons: [],
    bsplus: { cap_per_order: null, cap_per_period: null },
  };
}

/** 保存済み設定のうち、この画面が扱えないキー (保存すると消える) */
export function findUnknownKeys(config) {
  if (!isObject(config)) return [];
  const unknown = (obj, known, prefix) =>
    (isObject(obj) ? Object.keys(obj).filter((k) => !known.includes(k)).map((k) => prefix + k) : []);
  const rows = (name) => (Array.isArray(config[name]) ? config[name] : [])
    .flatMap((row, i) => unknown(row, KNOWN_KEYS[name], `${name}[${i}].`));
  return [
    ...unknown(config, KNOWN_KEYS.top, ''), ...rows('common'), ...rows('campaigns'), ...rows('coupons'),
    ...unknown(config.bsplus, KNOWN_KEYS.bsplus, 'bsplus.'),
  ];
}

// ---------- 純粋関数: 設定 JSON ⇔ 画面の下書き (入力欄の文字列のまま持つ) ----------
function pointDraft(row) {
  return {
    name: str(row.name), rate: rateToPercent(row.rate),
    base: row.base === 'tax_included' ? 'tax_included' : 'tax_excluded',
    cap_per_order: str(row.cap_per_order), cap_per_period: str(row.cap_per_period),
    min_purchase: str(row.min_purchase), max_purchase: str(row.max_purchase),
  };
}

const storeIdsDraft = (ids) => (Array.isArray(ids) ? ids.map(str).join(', ') : '');
const objectRows = (v) => (Array.isArray(v) ? v.filter(isObject) : []);

export function newCommonRow() {
  return pointDraft({});
}

export function newCampaignRow(purchaseDate) {
  return {
    ...pointDraft({}), entry_required: false, target: 'all', page_title: '', store_ids: '',
    dates: [], month: isValidDate(purchaseDate) ? purchaseDate.slice(0, 7) : '',
  };
}

export function newCouponRow() {
  return {
    name: '', type: 'fixed', value: '', max_discount: '', min_purchase: '',
    target: 'all', store_ids: '', valid_from: '', valid_until: '',
  };
}

/** 設定 JSON → 下書き。壊れた値は空欄にして、保存時の検証で気付けるようにする */
export function configToDraft(config, today) {
  const c = isObject(config) ? config : {};
  const purchaseDate = isValidDate(c.purchase_date) ? c.purchase_date : today;
  const bsplus = isObject(c.bsplus) ? c.bsplus : {};
  return {
    purchase_date: purchaseDate,
    common: objectRows(c.common).map(pointDraft),
    campaigns: objectRows(c.campaigns).map((row) => ({
      ...pointDraft(row),
      entry_required: row.entry_required === true,
      target: has(CAMPAIGN_TARGETS, row.target) ? row.target : 'all',
      page_title: str(row.page_title),
      store_ids: storeIdsDraft(row.store_ids),
      dates: [...new Set((Array.isArray(row.dates) ? row.dates : []).filter(isValidDate))].sort(),
      month: purchaseDate.slice(0, 7),
    })),
    coupons: objectRows(c.coupons).map((row) => ({
      name: str(row.name),
      type: row.type === 'percent' ? 'percent' : 'fixed',
      value: row.type === 'percent' ? rateToPercent(row.value) : str(row.value),
      max_discount: str(row.max_discount),
      min_purchase: str(row.min_purchase),
      target: row.target === 'stores' ? 'stores' : 'all',
      store_ids: storeIdsDraft(row.store_ids),
      valid_from: str(row.valid_from), valid_until: str(row.valid_until),
    })),
    bsplus: { cap_per_order: str(bsplus.cap_per_order), cap_per_period: str(bsplus.cap_per_period) },
  };
}

const MSG = {
  name: '名前を入力してください',
  rate: '0 より大きく 100 以下の数値 (%) で入力してください',
  int: '0 以上の整数で入力してください (小数・マイナスは不可)',
  stores: '店IDを 1 件以上入力してください',
  date: '日付を YYYY-MM-DD の形で入力してください',
};

/** 下書き → { config, errors }。errors は [{ path, where, message }] (path は入力欄の data-path) */
export function draftToConfig(draft) {
  const errors = [];
  const fail = (path, where, message) => errors.push({ path, where, message });
  const optInt = (text, path, where) => {
    const { ok, value } = parseOptionalInt(text);
    if (!ok) fail(path, where, MSG.int);
    return value;
  };
  const rate = (text, path, where) => {
    const value = percentToRate(text);
    if (value === null || !(value > 0 && value <= 1)) fail(path, where, MSG.rate);
    return value;
  };
  const uniqueName = (text, path, where, seen, scope) => {
    const name = str(text).trim();
    if (!name) fail(path, where, MSG.name);
    else if (seen.has(name)) fail(path, where, `名前「${name}」が重複しています (${scope})`);
    seen.add(name);
    return name;
  };
  const storeIds = (row, path, where) => {
    if (row.target !== 'stores') return [];
    const ids = parseStoreIds(row.store_ids);
    if (ids.length === 0) fail(path, where, MSG.stores);
    return ids;
  };

  const pointNames = new Set();
  const point = (row, prefix, where) => {
    if (!has(BASES, row.base)) fail(`${prefix}.base`, where, '対象金額を選んでください');
    const minPurchase = optInt(row.min_purchase, `${prefix}.min_purchase`, where) ?? 0;
    const maxPurchase = optInt(row.max_purchase, `${prefix}.max_purchase`, where);
    if (maxPurchase !== null && maxPurchase !== undefined && maxPurchase < minPurchase) {
      fail(`${prefix}.max_purchase`, where, '最高購入額は最低購入額以上にしてください');
    }
    return {
      name: uniqueName(row.name, `${prefix}.name`, where, pointNames, '毎日付くポイントとキャンペーンを通して別の名前にしてください'),
      rate: rate(row.rate, `${prefix}.rate`, where),
      base: row.base,
      cap_per_order: optInt(row.cap_per_order, `${prefix}.cap_per_order`, where),
      cap_per_period: optInt(row.cap_per_period, `${prefix}.cap_per_period`, where),
      min_purchase: minPurchase,
      max_purchase: maxPurchase ?? null,
    };
  };

  if (!isValidDate(draft.purchase_date)) fail('purchase_date', '購入予定日', MSG.date);

  const common = draft.common.map((row, i) => point(row, `common.${i}`, `毎日付くポイント ${i + 1} 行目`));

  const campaigns = draft.campaigns.map((row, i) => {
    const prefix = `campaigns.${i}`;
    const where = `キャンペーン ${i + 1} 行目`;
    const base = point(row, prefix, where);
    if (!has(CAMPAIGN_TARGETS, row.target)) fail(`${prefix}.target`, where, '対象を選んでください');
    const pageTitle = row.target === 'page' ? str(row.page_title).trim() : null;
    if (pageTitle === '') fail(`${prefix}.page_title`, where, '商品ページのポイント内訳の行名 (その一部) を入力してください');
    const dates = [...new Set(row.dates)].sort();
    if (dates.length === 0) fail(`${prefix}.dates`, where, '対象日を 1 日以上選んでください');
    else if (!dates.every(isValidDate)) fail(`${prefix}.dates`, where, '対象日に不正な日付が含まれています');
    return {
      ...base, entry_required: row.entry_required === true, target: row.target, page_title: pageTitle,
      store_ids: storeIds(row, `${prefix}.store_ids`, where), dates,
    };
  });

  const couponNames = new Set();
  const coupons = draft.coupons.map((row, i) => {
    const prefix = `coupons.${i}`;
    const where = `手持ちクーポン ${i + 1} 行目`;
    const name = uniqueName(row.name, `${prefix}.name`, where, couponNames, '手持ちクーポンの中で別の名前にしてください');
    if (!has(COUPON_TYPES, row.type)) fail(`${prefix}.type`, where, '種類を選んでください');
    const percent = row.type === 'percent';
    let value;
    if (percent) {
      value = rate(row.value, `${prefix}.value`, where);
    } else {
      const parsed = parseOptionalInt(row.value);
      value = parsed.value;
      if (!parsed.ok || value === null || value < 1) fail(`${prefix}.value`, where, '1 以上の整数 (円) で入力してください');
    }
    const maxDiscount = percent ? optInt(row.max_discount, `${prefix}.max_discount`, where) : null;
    if (maxDiscount === 0) fail(`${prefix}.max_discount`, where, '1 以上の整数で入力するか、空欄 (上限なし) にしてください');
    if (!has(COUPON_TARGETS, row.target)) fail(`${prefix}.target`, where, '対象を選んでください');
    const period = ['valid_from', 'valid_until'].map((key) => {
      const text = str(row[key]).trim();
      if (text && !isValidDate(text)) fail(`${prefix}.${key}`, where, MSG.date);
      return text || null;
    });
    if (period.every((d) => d && isValidDate(d)) && period[0] > period[1]) {
      fail(`${prefix}.valid_until`, where, '終了日は開始日以降にしてください');
    }
    return {
      name, type: row.type, value, max_discount: maxDiscount,
      min_purchase: optInt(row.min_purchase, `${prefix}.min_purchase`, where) ?? 0,
      target: row.target, store_ids: storeIds(row, `${prefix}.store_ids`, where),
      valid_from: period[0], valid_until: period[1],
    };
  });

  const bsplus = {
    cap_per_order: optInt(draft.bsplus.cap_per_order, 'bsplus.cap_per_order', 'ボーナスストアPlus の上限'),
    cap_per_period: optInt(draft.bsplus.cap_per_period, 'bsplus.cap_per_period', 'ボーナスストアPlus の上限'),
  };
  return { config: { version: 1, purchase_date: draft.purchase_date, common, campaigns, coupons, bsplus }, errors };
}

// ---------- 画面 ----------
const INPUT = 'mt-1 w-full px-3 py-2 rounded-lg border bg-white focus:border-blue-500 focus:ring-2 focus:ring-blue-200 outline-none';
const HELP = {
  purchase: 'この日に買う前提で計算します。キャンペーン・手持ちクーポン・ボーナスストアPlus の枠は、この日に有効なものだけが使われます。店舗リストの掲載期間外の日付を選ぶと、実行時にエラーになります。',
  common: 'いつ買っても付くポイント (LYPプレミアム、PayPay 支払いなど) を入れます。ストアポイント (基本 1% + 上乗せ) とボーナスストアPlus の率は自動で読むので、ここには入れません。',
  campaigns: '特定の日だけ付くポイントを、対象日つきで入れます。購入予定日が対象日に含まれるものだけ計算に使われます。全体加算日の「ボーナスストアPlus はさらに +2%」「ボーナスストアPlus かつ優良ストアはさらに +3%」は上限が別なので、2 行に分けて登録します (優良ストアには両方付きます)。',
  coupons: 'ログインしないと見えないクーポンやモールクーポンを手で登録します。商品ページで見つかる公開クーポンは自動で読みます。クーポンは 1 注文 1 枚で併用できないため、利益がいちばん大きくなる 1 枚 (または使わない) が選ばれます。',
  bsplus: 'ボーナスストアPlus (+4% / +9%) の率は、店舗リストの購入予定日の枠から自動で読みます。ここには付与上限だけを入れます (空欄 = 上限なし)。',
};

// DOM API だけで要素を組み立てる (DB の文字列を innerHTML に入れない)
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key in el) el[key] = value;
    else el.setAttribute(key, value);
  }
  el.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return el;
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const snapshot = (draft) => JSON.stringify(draft, (key, value) => (key === 'month' ? undefined : value));
const errorId = (path) => `settings-err-${path.replace(/\./g, '-')}`;

/** 設定画面を組み立てる。open() で読み込み、isDirty() で未保存の変更を調べる */
export function createSettings({ supabase, $root }) {
  const $ = (name) => $root.querySelector(`[data-settings="${name}"]`);
  const $status = $('status');
  const $summary = $('summary');
  const $body = $('body');
  const $save = $('save');
  const $dirty = $('dirty');
  const $updated = $('updated');

  let state = { draft: null, saved: '', errors: new Map(), loaded: false, busy: false };
  let generation = 0;   // 読み込み中にログアウト/破棄されたとき、古い応答を捨てるための番号

  const isDirty = () => state.loaded && snapshot(state.draft) !== state.saved;

  function setStatus(text, kind = 'info') {
    const color = { info: 'text-slate-500', ok: 'text-emerald-700', warn: 'text-amber-700', error: 'text-red-600' }[kind];
    $status.textContent = text;
    $status.className = `text-sm pb-3 ${color}`;
  }

  function updateButtons() {
    $save.disabled = !state.loaded || state.busy;
    $dirty.classList.toggle('hidden', !isDirty());
  }

  // ----- 入力欄の部品 -----
  function errorNode(path) {
    const message = state.errors.get(path);
    return message ? h('span', { id: errorId(path), class: 'block text-xs text-red-600 mt-1' }, message) : null;
  }

  function field(label, path, attrs = {}, span = '') {
    const error = state.errors.has(path);
    return h('label', { class: `block text-sm ${span}` },
      h('span', { class: 'text-slate-500' }, label),
      h('input', {
        type: 'text', autocomplete: 'off', ...attrs,
        class: `${INPUT} ${error ? 'border-red-500' : 'border-slate-300'}`,
        value: str(path.split('.').reduce((v, k) => v?.[k], state.draft)),
        dataset: { path, ...(attrs.dataset || {}) },
        'aria-invalid': error ? 'true' : null, 'aria-describedby': error ? errorId(path) : null,
      }),
      errorNode(path));
  }

  const intField = (label, path, placeholder = '', span = '') =>
    field(label, path, { inputmode: 'numeric', placeholder }, span);

  function selectField(label, path, options, current, span = '') {
    const $select = h('select', { class: `${INPUT} border-slate-300`, dataset: { path, rerender: '1' } },
      options.map(([value, text]) => h('option', { value }, text)));
    $select.value = current;
    return h('label', { class: `block text-sm ${span}` }, h('span', { class: 'text-slate-500' }, label), $select, errorNode(path));
  }

  const activeBadge = () =>
    h('span', { class: 'text-xs px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 whitespace-nowrap' }, '購入予定日に有効');

  function rowCard(list, index, title, badge, fields, extra) {
    return h('div', { class: 'border border-slate-200 rounded-lg p-3 mt-3' },
      h('div', { class: 'flex items-center gap-2 mb-2' },
        h('span', { class: 'text-xs font-semibold text-slate-500' }, title), badge,
        h('span', { class: 'flex-1' }),
        h('button', {
          type: 'button', class: 'text-sm text-red-600 hover:underline',
          dataset: { action: 'remove', list, index: String(index), key: `remove-${list}-${index}` },
        }, '削除')),
      h('div', { class: 'grid gap-3 sm:grid-cols-2 lg:grid-cols-6' }, fields),
      extra);
  }

  function pointFields(prefix, row) {
    return [
      field('名前', `${prefix}.name`, {}, 'lg:col-span-2'),
      field('率 (%)', `${prefix}.rate`, { inputmode: 'decimal', placeholder: '例: 5' }),
      selectField('対象金額', `${prefix}.base`, BASES, row.base),
      intField('上限 1注文 (pt)', `${prefix}.cap_per_order`, 'なし'),
      intField('上限 期間 (pt)', `${prefix}.cap_per_period`, 'なし'),
      intField('最低購入額 (円)', `${prefix}.min_purchase`, '0'),
      intField('最高購入額 (円)', `${prefix}.max_purchase`, 'なし'),
    ];
  }

  // ----- 対象日 (月ごとのトグルボタン) -----
  function datePicker(row, index) {
    const path = `campaigns.${index}.dates`;
    const shift = (delta, label) => h('button', {
      type: 'button', class: 'px-2 py-2 rounded-lg border border-slate-300 text-sm hover:bg-slate-100',
      disabled: !isValidMonth(row.month), 'aria-label': delta < 0 ? '前の月' : '次の月',
      dataset: { action: 'shift-month', index: String(index), delta: String(delta), key: `shift-${index}-${delta}` },
    }, label);
    const $month = h('input', {
      type: 'month', class: 'px-3 py-2 rounded-lg border border-slate-300 bg-white', value: row.month,
      placeholder: 'YYYY-MM', 'aria-label': '対象日を選ぶ月',
      dataset: { path: `campaigns.${index}.month`, rerender: '1', focusFor: path },
    });
    const others = row.dates.filter((d) => !d.startsWith(`${row.month}-`));
    const otherMonths = [...new Set(others.map((d) => d.slice(0, 7)))].join('、');
    return h('div', { class: 'mt-3' },
      h('div', { class: 'text-sm text-slate-500 mb-1' }, `対象日（選択中 ${row.dates.length} 日）`),
      h('div', { class: 'flex flex-wrap items-center gap-2 mb-2' }, shift(-1, '‹'), $month, shift(1, '›'),
        others.length > 0 && h('span', { class: 'text-xs text-slate-500' }, `他の月に ${others.length} 日選択済み（${otherMonths}）`)),
      isValidMonth(row.month)
        ? calendar(row, index)
        : h('p', { class: 'text-xs text-slate-500' }, '月を YYYY-MM の形で選ぶと、日付のボタンが並びます。'),
      errorNode(path));
  }

  function calendar(row, index) {
    const [year, month] = row.month.split('-').map(Number);
    const lastDay = new Date(year, month, 0).getDate();
    const firstWeekday = new Date(year, month - 1, 1).getDay();
    const weekdayColor = (w) => (w === 0 ? 'text-red-600' : w === 6 ? 'text-blue-600' : 'text-slate-700');
    const head = WEEKDAYS.map((w, i) => h('div', { class: `text-xs text-center ${weekdayColor(i)}`, 'aria-hidden': 'true' }, w));
    const blanks = Array.from({ length: firstWeekday }, () => h('div'));
    const days = Array.from({ length: lastDay }, (_, i) => {
      const date = `${row.month}-${String(i + 1).padStart(2, '0')}`;
      const weekday = (firstWeekday + i) % 7;
      const selected = row.dates.includes(date);
      const isPurchase = date === state.draft.purchase_date;
      return h('button', {
        type: 'button',
        class: `py-1.5 rounded-lg border text-sm ${selected
          ? 'bg-blue-600 border-blue-600 text-white font-semibold'
          : `bg-white border-slate-300 hover:bg-slate-100 ${weekdayColor(weekday)}`} ${isPurchase ? 'ring-2 ring-amber-400' : ''}`,
        'aria-pressed': String(selected),
        'aria-label': `${date} (${WEEKDAYS[weekday]})${isPurchase ? ' 購入予定日' : ''}`,
        title: isPurchase ? '購入予定日' : null,
        dataset: { action: 'toggle-date', index: String(index), date, key: `date-${index}-${date}` },
      }, String(i + 1));
    });
    return h('div', {},
      h('div', { class: 'grid grid-cols-7 gap-1 max-w-sm' }, head, blanks, days),
      h('p', { class: 'text-xs text-slate-400 mt-1' }, '黄色の枠は購入予定日です。'));
  }

  // ----- 各区分 -----
  function card(title, help, ...children) {
    return h('section', { class: 'bg-white rounded-lg shadow p-4 mb-4' },
      h('h2', { class: 'font-semibold text-slate-700' }, title),
      h('p', { class: 'text-sm text-slate-500 mt-1' }, help), children);
  }

  const addButton = (list, label) => h('button', {
    type: 'button', class: 'mt-3 px-3 py-2 rounded-lg border border-blue-600 text-blue-600 hover:bg-blue-50 text-sm font-medium',
    dataset: { action: 'add', list, key: `add-${list}` },
  }, label);

  function campaignRow(row, i) {
    const prefix = `campaigns.${i}`;
    const fields = [
      ...pointFields(prefix, row),
      selectField('対象', `${prefix}.target`, CAMPAIGN_TARGETS, row.target, 'lg:col-span-2'),
      row.target === 'page' && field('商品ページの行名 (その一部)', `${prefix}.page_title`, {}, 'lg:col-span-2'),
      row.target === 'stores' && field('店ID (カンマ区切り)', `${prefix}.store_ids`, { placeholder: '例: joshin, edion' }, 'lg:col-span-2'),
      h('label', { class: 'flex items-center gap-2 text-sm lg:col-span-2 sm:mt-6' },
        h('input', { type: 'checkbox', class: 'h-4 w-4', checked: row.entry_required, dataset: { path: `${prefix}.entry_required` } }),
        h('span', { class: 'text-slate-600' }, '要エントリー')),
    ];
    const badge = isCampaignActive(row.dates, state.draft.purchase_date) && activeBadge();
    return rowCard('campaigns', i, `キャンペーン ${i + 1}`, badge, fields, datePicker(row, i));
  }

  function couponRow(row, i) {
    const prefix = `coupons.${i}`;
    const percent = row.type === 'percent';
    const dateAttrs = { type: 'date', dataset: { rerender: '1' } };
    const fields = [
      field('名前', `${prefix}.name`, {}, 'lg:col-span-2'),
      selectField('種類', `${prefix}.type`, COUPON_TYPES, row.type),
      field(percent ? '値引き率 (%)' : '値引き額 (円)', `${prefix}.value`,
        { inputmode: percent ? 'decimal' : 'numeric', placeholder: percent ? '例: 10' : '例: 2000' }),
      percent && intField('値引き上限 (円)', `${prefix}.max_discount`, 'なし'),
      intField('最低購入額 (税込・円)', `${prefix}.min_purchase`, '0'),
      selectField('対象', `${prefix}.target`, COUPON_TARGETS, row.target),
      row.target === 'stores' && field('店ID (カンマ区切り)', `${prefix}.store_ids`, { placeholder: '例: joshin' }, 'lg:col-span-2'),
      field('有効期間 開始 (空欄 = 制限なし)', `${prefix}.valid_from`, dateAttrs, 'lg:col-span-2'),
      field('有効期間 終了 (空欄 = 制限なし)', `${prefix}.valid_until`, dateAttrs, 'lg:col-span-2'),
    ];
    const badge = isCouponActive(row.valid_from, row.valid_until, state.draft.purchase_date) && activeBadge();
    return rowCard('coupons', i, `クーポン ${i + 1}`, badge, fields);
  }

  function renderForm() {
    const active = document.activeElement;
    const focusKey = $body.contains(active) ? (active.dataset.path || active.dataset.key) : null;
    const d = state.draft;
    $body.replaceChildren(
      card('購入予定日', HELP.purchase,
        h('div', { class: 'mt-3 max-w-xs' }, field('購入予定日', 'purchase_date', { type: 'date', dataset: { rerender: '1' } }))),
      card('毎日付くポイント', HELP.common,
        d.common.map((row, i) => rowCard('common', i, `ポイント ${i + 1}`, null, pointFields(`common.${i}`, row))),
        addButton('common', '＋ ポイントを追加')),
      card('キャンペーン', HELP.campaigns, d.campaigns.map(campaignRow), addButton('campaigns', '＋ キャンペーンを追加')),
      card('手持ちクーポン', HELP.coupons, d.coupons.map(couponRow), addButton('coupons', '＋ クーポンを追加')),
      card('ボーナスストアPlus の上限', HELP.bsplus,
        h('div', { class: 'grid gap-3 sm:grid-cols-2 mt-3 max-w-md' },
          intField('上限 1注文 (pt)', 'bsplus.cap_per_order', 'なし'),
          intField('上限 期間 (pt)', 'bsplus.cap_per_period', 'なし'))),
    );
    if (focusKey) focusField(focusKey);
    updateButtons();
  }

  function focusField(key) {
    const $el = [...$body.querySelectorAll('[data-path],[data-key]')]
      .find((el) => el.dataset.path === key || el.dataset.key === key || el.dataset.focusFor === key);
    if ($el) $el.focus();
    return Boolean($el);
  }

  function renderSummary(errors) {
    if (errors.length === 0) {
      $summary.replaceChildren();
      $summary.classList.add('hidden');
      return;
    }
    $summary.classList.remove('hidden');
    $summary.replaceChildren(
      h('p', { class: 'font-semibold' }, `入力内容に ${errors.length} 件の誤りがあります。直してからもう一度保存してください。`),
      h('ul', { class: 'list-disc pl-5 mt-1 space-y-0.5' }, errors.map((e) => h('li', {},
        h('button', { type: 'button', class: 'text-left underline', dataset: { goto: e.path } }, `${e.where}: ${e.message}`)))));
  }

  // ----- 入力の反映 -----
  function setDraft(draft, { clearErrors = false } = {}) {
    state = { ...state, draft, errors: clearErrors ? new Map() : state.errors };
    if (clearErrors) renderSummary([]);
  }

  $body.addEventListener('input', (e) => {
    const path = e.target.dataset?.path;
    if (!path || !state.loaded) return;
    const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setDraft(setIn(state.draft, path.split('.'), value));
    updateButtons();
  });

  // 表示が変わる項目 (対象・種類・日付・月) は入力後に描き直す
  $body.addEventListener('change', (e) => {
    if (e.target.dataset?.rerender && state.loaded) renderForm();
  });

  const NEW_ROW = { common: newCommonRow, campaigns: () => newCampaignRow(state.draft.purchase_date), coupons: newCouponRow };

  $body.addEventListener('click', (e) => {
    const $btn = e.target.closest('button[data-action]');
    if (!$btn) return;
    if ($btn.dataset.action === 'reload') { load(); return; }
    if (!state.loaded) return;
    const { action, list, date } = $btn.dataset;
    const index = Number($btn.dataset.index);
    const d = state.draft;
    if (action === 'add') {
      setDraft({ ...d, [list]: [...d[list], NEW_ROW[list]()] });
    } else if (action === 'remove') {
      // 行番号がずれるので、前回の検証エラーの表示は消す
      setDraft({ ...d, [list]: d[list].filter((_, i) => i !== index) }, { clearErrors: true });
    } else if (action === 'toggle-date') {
      setDraft(setIn(d, ['campaigns', index, 'dates'], toggleDate(d.campaigns[index].dates, date)));
    } else if (action === 'shift-month') {
      setDraft(setIn(d, ['campaigns', index, 'month'], shiftMonth(d.campaigns[index].month, Number($btn.dataset.delta))));
    }
    renderForm();
    if (action === 'add') focusField(`${list}.${state.draft[list].length - 1}.name`);
  });

  $summary.addEventListener('click', (e) => {
    const $btn = e.target.closest('button[data-goto]');
    if ($btn) focusField($btn.dataset.goto);
  });

  // ----- 読み込み・保存 -----
  async function load() {
    const token = ++generation;
    state = { draft: null, saved: '', errors: new Map(), loaded: false, busy: true };
    $body.replaceChildren();
    renderSummary([]);
    $updated.textContent = '';
    updateButtons();
    setStatus('設定を読み込み中…');
    const { data, error } = await supabase
      .from('arbitrage_settings').select('config,updated_at').eq('singleton', true).maybeSingle();
    if (token !== generation) return;
    if (error) {
      // 読めていない設定を上書きしないよう、フォームは出さない
      console.error('load arbitrage_settings failed', error);
      state = { ...state, busy: false };
      setStatus('設定を読み込めませんでした。時間をおいてもう一度お試しください（このアカウントに権限が無い場合も読み込めません）。', 'error');
      $body.replaceChildren(h('button', {
        type: 'button', class: 'px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white font-medium',
        dataset: { action: 'reload' },
      }, 'もう一度読み込む'));
      updateButtons();
      return;
    }
    const today = todayIso();
    const config = data ? data.config : emptyConfig(today);
    const draft = configToDraft(config, today);
    state = { draft, saved: snapshot(draft), errors: new Map(), loaded: true, busy: false };
    showUpdatedAt(data?.updated_at);
    const unknown = findUnknownKeys(config);
    if (!data) {
      setStatus('保存済みの設定はまだありません。入力して「保存」を押してください。');
    } else if (!isObject(config) || config.version !== 1 || unknown.length > 0) {
      const detail = unknown.length > 0 ? `（${unknown.join('、')}）` : '';
      setStatus(`保存済みの設定に、この画面で扱えない形式や項目があります${detail}。このまま保存すると、その部分は失われます。`, 'warn');
    } else {
      setStatus('');
    }
    renderForm();
  }

  function showUpdatedAt(iso) {
    const d = iso ? new Date(iso) : null;
    $updated.textContent = d && !Number.isNaN(d.getTime())
      ? `最終保存: ${d.toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' })}` : '';
  }

  async function save() {
    if (!state.loaded || state.busy) return;
    const { config, errors } = draftToConfig(state.draft);
    // 1 つの入力欄に複数の違反があるときは、最初の 1 件を欄の近くに出す
    state = { ...state, errors: new Map([...errors].reverse().map((e) => [e.path, e.message])) };
    renderForm();
    renderSummary(errors);
    if (errors.length > 0) {
      setStatus(`入力内容に ${errors.length} 件の誤りがあるため、保存していません。`, 'error');
      if (!focusField(errors[0].path)) $summary.scrollIntoView({ block: 'start' });
      return;
    }
    const token = generation;
    const savedSnapshot = snapshot(state.draft);
    state = { ...state, busy: true };
    updateButtons();
    setStatus('保存中…');
    const { data, error } = await supabase
      .from('arbitrage_settings')
      .upsert({ singleton: true, config }, { onConflict: 'singleton' })
      .select('updated_at')
      .maybeSingle();
    if (token !== generation) return;
    state = { ...state, busy: false };
    if (error) {
      console.error('save arbitrage_settings failed', error);
      setStatus('保存できませんでした。通信状態とログイン状態を確認して、もう一度「保存」を押してください（入力内容は画面に残っています）。', 'error');
    } else {
      // 保存中に入力が進んでいたら、その分は未保存のまま残す
      state = { ...state, saved: savedSnapshot };
      showUpdatedAt(data?.updated_at ?? new Date().toISOString());
      setStatus('保存しました。次回の実行からこの設定が使われます。', 'ok');
    }
    updateButtons();
  }

  $save.addEventListener('click', save);

  window.addEventListener('beforeunload', (e) => {
    if (!isDirty()) return;
    e.preventDefault();
    e.returnValue = '';
  });

  return {
    isDirty,
    /** 設定タブを開いたとき。未読み込みなら読み込む (編集中の内容は上書きしない) */
    open() {
      if (!state.loaded && !state.busy) load();
    },
    /** 編集中の内容を捨てる (次に開いたとき読み直す)。ログアウト時にも呼ぶ */
    discard() {
      generation += 1;
      state = { draft: null, saved: '', errors: new Map(), loaded: false, busy: false };
      $body.replaceChildren();
      renderSummary([]);
      setStatus('');
      $updated.textContent = '';
      updateButtons();
    },
  };
}
