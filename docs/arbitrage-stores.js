// 利益商品リスト - 「店舗リスト」画面 (最新の店舗リストの確認と、更新の依頼)
// 店舗リストは tools/yahoo_pp/refresh.py が arbitrage_store_lists に 1 行ずつ積む (最新 = id 最大)。
// 更新の依頼は arbitrage_research_requests に params = { kind: 'store_list' } を 1 行 INSERT するだけで、
// 実際に動かすのは PC で起動しておく待ち受け (python -m arbitrage.worker)。
// arbitrage-research.js と同じく、DB の文字列は h() / textContent でだけ DOM に入れる。
import {
  ACTIVE_STATUSES, STATUS_LABELS, formatDateTime, isActiveStatus, isMissingTableError, isObject, isWorkerAlive, num,
  progressPercent, str, todayIso,
} from './arbitrage-research-calc.js';

const POLL_ACTIVE_MS = 15000;   // 更新の依頼が待ち・実行中の間
const POLL_IDLE_MS = 60000;     // それ以外は待ち受けの状態だけ、ゆっくり見直す
const WORKER_COMMAND = '.venv/bin/python -m arbitrage.worker';
const SQL_FILE = 'db/arbitrage_store_lists.sql';
const STORE_LIST_COLUMNS = 'id,created_at,fetched_on,period_start,period_end,dates,summary,stores,source_sha';
const REQUEST_COLUMNS = 'id,created_at,status,params,cancel_requested,progress,message,started_at,finished_at,updated_at';
// refresh.py が progress.stage に書く段階名
export const STORE_LIST_STAGE_LABELS = {
  fetch: '参加ストア一覧の取得', invoice: '適格請求書の確認', points: '上乗せ率の調査', rating: '評価の取得',
  build: '店舗リストの作成', done: '完了',
};
const SLOT_FILTERS = { any: 'すべて', 9: '+9% のみ', 4: '+4% のみ', has: '枠あり (+9% / +4%)' };

const INPUT = 'mt-1 w-full px-3 py-2 rounded-lg border border-slate-300 bg-white focus:border-blue-500 focus:ring-2 focus:ring-blue-200 outline-none';
const BTN_PRIMARY = 'px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 text-white font-medium';
const STATUS_COLORS = {
  queued: 'bg-slate-100 text-slate-700', running: 'bg-blue-100 text-blue-700', completed: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700', cancelled: 'bg-amber-100 text-amber-800',
};
const TH = 'px-3 py-2 font-medium whitespace-nowrap';
const TD = 'px-3 py-1.5 whitespace-nowrap';

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

// replaceChildren は null / false を文字列にして入れてしまうので、h() と同じ条件で捨ててから差し替える
function mount($el, ...children) {
  $el.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
}

// ---------- 純粋関数 ----------
export const isStoreListRequest = (params) => isObject(params) && params.kind === 'store_list';
const finiteNumber = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** DB の 1 店分を、表示に使う形に整える (壊れた値は null / 空にする) */
export function normalizeStore(raw) {
  const s = isObject(raw) ? raw : {};
  const reviews = Array.isArray(s.reviews) && s.reviews.length === 5 && s.reviews.every((n) => finiteNumber(n) !== null && n >= 0)
    ? s.reviews : null;
  return {
    id: str(s.id), name: str(s.name) || str(s.id), genre: str(s.genre), cat: str(s.cat),
    gold: s.gold === true, inv: ['Y', 'N', '?'].includes(s.inv) ? s.inv : '?', reg: str(s.reg),
    max_upsell: finiteNumber(s.max_upsell), unknown: finiteNumber(s.unknown), n: finiteNumber(s.n), n_up: finiteNumber(s.n_up),
    reviews,
    days: finiteNumber(s.days) ?? 0, d9: finiteNumber(s.d9) ?? 0, d4: finiteNumber(s.d4) ?? 0,
    first: str(s.first), last: str(s.last), pat: str(s.pat),
  };
}

/** レビューの件数と平均 ([星5,星4,星3,星2,星1] から)。無ければ null */
export function reviewStats(reviews) {
  if (!Array.isArray(reviews) || reviews.length !== 5) return null;
  const count = reviews.reduce((a, b) => a + b, 0);
  if (count <= 0) return { count: 0, average: null };
  const sum = reviews.reduce((a, b, i) => a + b * (5 - i), 0);
  return { count, average: sum / count };
}

/** その日の枠: '9' / '4' / '-' / null (掲載期間の外・不明) */
export function slotOn(store, dates, dateIso) {
  const i = Array.isArray(dates) ? dates.indexOf(dateIso) : -1;
  if (i < 0 || i >= store.pat.length) return null;
  const c = store.pat[i];
  return c === '9' || c === '4' ? c : '-';
}

/** 今日以降で最初に枠のある掲載日 (ISO)。無ければ null */
export function nextListedDate(store, dates, todayIsoText) {
  if (!Array.isArray(dates)) return null;
  for (let i = 0; i < dates.length && i < store.pat.length; i += 1) {
    if (str(dates[i]) >= todayIsoText && (store.pat[i] === '9' || store.pat[i] === '4')) return str(dates[i]);
  }
  return null;
}

/** 絞り込み。filters: { genre, qualifiedOnly, goldOnly, slot ('any'|'9'|'4'|'has'), minUpsell (数値 or null), text } */
export function filterStores(stores, dates, filters, todayIsoText) {
  const text = str(filters.text).trim().toLowerCase();
  return stores.filter((s) => {
    if (filters.genre && s.genre !== filters.genre) return false;
    if (filters.qualifiedOnly && s.inv === 'N') return false;   // 適格不明 (?) は run と同じく適格側に数える
    if (filters.goldOnly && !s.gold) return false;
    if (filters.slot !== 'any') {
      const slot = slotOn(s, dates, todayIsoText);
      if (filters.slot === 'has' ? !(slot === '9' || slot === '4') : slot !== filters.slot) return false;
    }
    if (filters.minUpsell !== null && !(s.max_upsell !== null && s.max_upsell >= filters.minUpsell)) return false;
    if (text && !s.name.toLowerCase().includes(text) && !s.id.toLowerCase().includes(text)) return false;
    return true;
  });
}

/** 並び: 最大上乗せ率 降順 (未調査は最後) → ジャンル → ID */
export function sortStores(stores) {
  return [...stores].sort((a, b) => {
    const ua = a.max_upsell ?? -Infinity;
    const ub = b.max_upsell ?? -Infinity;
    if (ua !== ub) return ub - ua;
    if (a.genre !== b.genre) return a.genre < b.genre ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

/** 購入予定日などが掲載期間の中か。期間が読めなければ true (警告しない) */
export function isWithinPeriod(list, dateIso) {
  if (!isObject(list) || !list.period_start || !list.period_end) return true;
  return str(list.period_start) <= dateIso && dateIso <= str(list.period_end);
}

const percentText = (v) => (v === null ? '—' : `${Number.isInteger(v) ? v : v.toFixed(1)}%`);
const monthDay = (iso) => {
  const m = /^\d{4}-(\d{2})-(\d{2})$/.exec(str(iso));
  return m ? `${Number(m[1])}/${Number(m[2])}` : str(iso);
};
const weekday = (iso) => {
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime()) ? '' : '日月火水木金土'[d.getDay()];
};

// ---------- 画面 ----------
const emptyState = () => ({
  open: false, loaded: false, busy: false,
  list: null,            // arbitrage_store_lists の最新行 (stores は normalizeStore 済み)
  listMissing: false,    // 行が無い
  missingTable: false,   // db/arbitrage_store_lists.sql が未実行
  worker: null,          // arbitrage_worker_state
  latestRequest: null,   // kind = store_list の最新の依頼
  activeRequest: null,   // 待ち・実行中の依頼 (kind 問わず)
  requestsError: false,
  filters: { genre: '', qualifiedOnly: true, goldOnly: false, slot: 'any', minUpsell: null, text: '' },
  expandedId: null,
});

/**
 * 店舗リスト画面を組み立てる。open() で読み込みとポーリングを始め、close() で止める。
 * onRequestQueued: 更新の依頼を出した後 (リサーチ画面の履歴に出るので、その読み直し用)
 */
export function createStores({ supabase, $root, onRequestQueued }) {
  const $ = (name) => $root.querySelector(`[data-stores="${name}"]`);
  const $status = $('status');
  const $body = $('body');

  let state = emptyState();
  let ui = null;
  let generation = 0;
  let refreshSeq = 0;
  let timer = null;

  function setStatus(text, kind = 'info') {
    const color = { info: 'text-slate-500', ok: 'text-emerald-700', warn: 'text-amber-700', error: 'text-red-600' }[kind];
    $status.textContent = text;
    $status.className = `text-sm pb-3 ${color}`;
  }

  function setMessage(text, kind = 'info') {
    if (!ui) return;
    const color = { info: 'text-slate-500', ok: 'text-emerald-700', warn: 'text-amber-700', error: 'text-red-600' }[kind];
    ui.message.textContent = text;
    ui.message.className = `text-sm ${color}`;
  }

  function card(title, ...children) {
    return h('section', { class: 'bg-white rounded-lg shadow p-4 mb-4' },
      h('h2', { class: 'font-semibold text-slate-700' }, title), children);
  }

  function stat(label, value, sub = null, cls = 'text-slate-700') {
    return h('div', { class: 'bg-slate-50 rounded-lg p-3' },
      h('div', { class: 'text-xs text-slate-500' }, label),
      h('div', { class: `text-base font-semibold mt-0.5 ${cls}` }, value),
      sub && h('div', { class: 'text-xs text-slate-500 mt-0.5' }, sub));
  }

  const badge = (status) => h('span', {
    class: `text-xs px-2 py-0.5 rounded-full whitespace-nowrap ${STATUS_COLORS[status] || STATUS_COLORS.queued}`,
  }, STATUS_LABELS[status] || str(status));

  function build() {
    const region = (name, cls = '') => h('div', { class: cls, dataset: { region: name } });
    const $update = h('button', { type: 'button', class: BTN_PRIMARY, dataset: { action: 'update' } }, '店舗リストを更新');
    const $message = h('span', { class: 'text-sm text-slate-500', role: 'status' });
    $body.replaceChildren(
      card('要約', region('summary', 'mt-2')),
      card('更新',
        region('worker', 'mt-2 text-sm'),
        h('div', { class: 'mt-3 flex flex-wrap items-center gap-3' }, $update, $message),
        h('p', { class: 'text-xs text-slate-400 mt-2' },
          '更新には約 4 時間かかり、その間リサーチは動かせません。上乗せ率の調査に日曜特典が混ざるので、日曜と 5 のつく日は避けてください。'),
        region('request', 'mt-3')),
      card('店舗一覧', region('filters', 'mt-3'), region('count', 'mt-3 text-sm text-slate-500'), region('table', 'mt-2 overflow-x-auto')));
    const $region = (name) => $body.querySelector(`[data-region="${name}"]`);
    ui = {
      update: $update, message: $message,
      summary: $region('summary'), worker: $region('worker'), request: $region('request'),
      filters: $region('filters'), count: $region('count'), table: $region('table'),
    };
    buildFilters();
  }

  // ----- 絞り込み -----
  function buildFilters() {
    const f = state.filters;
    const genres = [...new Set((state.list?.stores ?? []).map((s) => s.genre).filter(Boolean))].sort();
    const option = (value, label) => h('option', { value }, label);
    const check = (name, label, checked) => h('label', { class: 'flex items-center gap-2 text-sm' },
      h('input', { type: 'checkbox', class: 'h-4 w-4', checked, dataset: { filter: name } }), h('span', { class: 'text-slate-700' }, label));
    mount(ui.filters, 
      h('div', { class: 'grid gap-3 sm:grid-cols-2 lg:grid-cols-4' },
        h('label', { class: 'block text-sm' }, h('span', { class: 'text-slate-500' }, 'ジャンル'),
          h('select', { class: INPUT, dataset: { filter: 'genre' }, value: f.genre },
            option('', 'すべて'), genres.map((g) => option(g, g)))),
        h('label', { class: 'block text-sm' }, h('span', { class: 'text-slate-500' }, '今日の枠'),
          h('select', { class: INPUT, dataset: { filter: 'slot' } },
            Object.entries(SLOT_FILTERS).map(([value, label]) => option(value, label)))),
        h('label', { class: 'block text-sm' }, h('span', { class: 'text-slate-500' }, '最大上乗せ率 (%) 以上'),
          h('input', { type: 'number', step: '1', min: '0', inputmode: 'numeric', placeholder: '指定なし', class: INPUT, dataset: { filter: 'minUpsell' },
            value: f.minUpsell === null ? '' : String(f.minUpsell) })),
        h('label', { class: 'block text-sm' }, h('span', { class: 'text-slate-500' }, '店名 / ストアID'),
          h('input', { type: 'search', autocomplete: 'off', placeholder: '部分一致', class: INPUT, dataset: { filter: 'text' }, value: f.text }))),
      h('div', { class: 'mt-3 flex flex-wrap gap-4' },
        check('qualifiedOnly', '非適格 (インボイス) を除く', f.qualifiedOnly),
        check('goldOnly', '優良ストアのみ', f.goldOnly)));
    // select の value は option を入れた後に設定する
    ui.filters.querySelector('[data-filter="genre"]').value = genres.includes(f.genre) ? f.genre : '';
    ui.filters.querySelector('[data-filter="slot"]').value = f.slot;
  }

  function readFilters() {
    const $f = (name) => ui.filters.querySelector(`[data-filter="${name}"]`);
    const upsellText = str($f('minUpsell').value).trim();
    const upsell = upsellText === '' ? null : Number(upsellText);
    return {
      genre: $f('genre').value, slot: $f('slot').value,
      minUpsell: Number.isFinite(upsell) ? upsell : null, text: $f('text').value,
      qualifiedOnly: $f('qualifiedOnly').checked, goldOnly: $f('goldOnly').checked,
    };
  }

  // ----- 表示 -----
  function renderSummary() {
    if (state.missingTable) {
      mount(ui.summary, h('p', { class: 'text-sm text-red-600' }, `${SQL_FILE} を Supabase で実行してください。`));
      return;
    }
    const list = state.list;
    if (!list) {
      mount(ui.summary, h('p', { class: 'text-sm text-amber-700' },
        state.listMissing ? 'まだ店舗リストが登録されていません。「店舗リストを更新」で取得します。' : '店舗リストを読み込めませんでした。'));
      return;
    }
    const today = todayIso();
    const sm = isObject(list.summary) ? list.summary : {};
    const count = (key) => (typeof sm[key] === 'number' ? num(sm[key]) : '—');
    const inPeriod = isWithinPeriod(list, today);
    const dates = Array.isArray(list.dates) ? list.dates : [];
    const todaySlots = { 9: 0, 4: 0 };
    for (const s of list.stores) {
      if (s.inv === 'N') continue;   // xlsx の概要と同じく非適格は数えない
      const slot = slotOn(s, dates, today);
      if (slot === '9' || slot === '4') todaySlots[slot] += 1;
    }
    const byGenre = isObject(sm.by_genre) ? Object.entries(sm.by_genre) : [];
    mount(ui.summary, 
      h('div', { class: 'flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-slate-600' },
        h('span', {}, `取得日 ${str(list.fetched_on)}`),
        h('span', {}, `掲載期間 ${monthDay(list.period_start)}〜${monthDay(list.period_end)}`),
        list.source_sha && h('span', { class: 'text-xs text-slate-400' }, `xlsx ${str(list.source_sha)}`)),
      !inPeriod && h('p', { class: 'mt-2 text-sm font-semibold text-red-600', role: 'alert' },
        `今日 (${monthDay(today)}) は掲載期間の外。更新が必要です。`),
      h('div', { class: 'grid gap-3 grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 mt-3' },
        stat('全店', count('total')), stat('適格', count('qualified')), stat('非適格', count('excluded')),
        stat('優良', count('gold')), stat('上乗せ調査済み', count('surveyed')),
        stat('今日 +9%', inPeriod ? num(todaySlots[9]) : '—', inPeriod ? null : '期間外', 'text-rose-700'),
        stat('今日 +4%', inPeriod ? num(todaySlots[4]) : '—', inPeriod ? null : '期間外', 'text-amber-700')),
      byGenre.length > 0 && h('table', { class: 'mt-3 text-sm' },
        h('thead', { class: 'text-slate-500 text-left' }, h('tr', {},
          h('th', { class: 'pr-4 py-1 font-medium' }, 'ジャンル'), h('th', { class: 'pr-4 py-1 font-medium text-right' }, '適格'),
          h('th', { class: 'pr-4 py-1 font-medium text-right' }, '優良'), h('th', { class: 'py-1 font-medium text-right' }, '非適格'))),
        h('tbody', {}, byGenre.map(([genre, g]) => h('tr', { class: 'border-t border-slate-100' },
          h('td', { class: 'pr-4 py-1 text-slate-700' }, str(genre)),
          h('td', { class: 'pr-4 py-1 text-right' }, isObject(g) && typeof g.qualified === 'number' ? num(g.qualified) : '—'),
          h('td', { class: 'pr-4 py-1 text-right' }, isObject(g) && typeof g.gold === 'number' ? num(g.gold) : '—'),
          h('td', { class: 'py-1 text-right' }, isObject(g) && typeof g.excluded === 'number' ? num(g.excluded) : '—'))))));
  }

  function renderWorker() {
    if (isWorkerAlive(state.worker?.heartbeat_at)) {
      mount(ui.worker, h('p', { class: 'text-emerald-700 font-medium' }, '待ち受け: 動作中'));
      return;
    }
    const last = formatDateTime(state.worker?.heartbeat_at);
    mount(ui.worker, 
      h('p', { class: 'text-amber-700 font-medium' }, `待ち受け: 停止中${last ? `（最後の応答 ${last}）` : ''}`),
      h('p', { class: 'text-slate-500 mt-1' }, '依頼は出せます。この PC のプロジェクトフォルダで次のコマンドを実行して待ち受けを起動すると開始します。'),
      h('code', { class: 'block mt-1 px-3 py-2 rounded-lg bg-slate-100 text-slate-800 text-sm select-all overflow-x-auto whitespace-nowrap' }, WORKER_COMMAND));
  }

  function blockReason() {
    if (state.requestsError) return '依頼の状態を読み込めていないため、今は更新を依頼できません。';
    const a = state.activeRequest;
    if (!a) return null;
    const what = isStoreListRequest(a.params) ? '店舗リストの更新' : 'リサーチ';
    return `${STATUS_LABELS[a.status] || a.status}の${what}があるため、終わるまで更新を依頼できません。`;
  }

  function renderUpdateButton() {
    const reason = blockReason();
    ui.update.disabled = state.busy || Boolean(reason);
    ui.update.title = reason ?? '';
    if (reason) setMessage(reason, 'warn');
    else if (ui.message.classList.contains('text-amber-700')) setMessage('');
  }

  function bar(label, done, total) {
    const percent = progressPercent(done, total);
    const count = percent === null ? '—' : `${num(done)} / ${num(total)}（${percent}%）`;
    return h('div', {},
      h('div', { class: 'flex justify-between text-xs text-slate-500' }, h('span', {}, label), h('span', {}, count)),
      h('div', { class: 'mt-1 h-2 bg-slate-200 rounded-full overflow-hidden', role: 'progressbar',
        'aria-label': label, 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': percent === null ? null : String(percent) },
        h('div', { class: 'h-full bg-blue-600', style: `width: ${percent ?? 0}%` })));
  }

  function renderRequest() {
    const r = state.latestRequest;
    if (!r) {
      mount(ui.request, h('p', { class: 'text-sm text-slate-400' }, 'まだ更新の依頼はありません。'));
      return;
    }
    const p = isObject(r.progress) ? r.progress : {};
    const active = isActiveStatus(r.status);
    const alive = isWorkerAlive(state.worker?.heartbeat_at);
    const lines = [];
    if (r.status === 'queued') lines.push(alive ? '待ち受けが依頼を受け取るのを待っています。' : '待ち受けを起動すると開始します。');
    if (r.status === 'running' && !alive) lines.push('待ち受けからの応答が途絶えています。PC の待ち受けが動いているか確認してください。');
    if (active && r.cancel_requested) lines.push('中止を依頼済みです。区切りのよいところで止まります (店舗リストは前のまま)。');
    const info = [
      ['段階', r.status === 'running' ? (STORE_LIST_STAGE_LABELS[p.stage] || '準備中') : null],
      ['完了予測', r.status === 'running' && p.eta ? formatDateTime(p.eta) : null],
      ['開始', formatDateTime(r.started_at) || null],
      ['終了', formatDateTime(r.finished_at) || null],
      ['最終更新', formatDateTime(p.updated_at || r.updated_at) || null],
    ].filter(([, value]) => value);
    mount(ui.request, 
      h('div', { class: 'flex flex-wrap items-center gap-2' },
        badge(r.status),
        h('span', { class: 'text-sm text-slate-700' }, '店舗リスト更新'),
        h('span', { class: 'text-sm text-slate-500' }, `依頼 ${formatDateTime(r.created_at)}`),
        h('span', { class: 'flex-1' }),
        active && !r.cancel_requested && h('button', {
          type: 'button', disabled: state.busy,
          class: 'px-3 py-2 rounded-lg border border-red-600 text-red-600 hover:bg-red-50 disabled:opacity-50 text-sm font-medium',
          dataset: { action: 'cancel', id: String(r.id) },
        }, '中止')),
      lines.map((text) => h('p', { class: 'text-sm text-amber-700 mt-2' }, text)),
      r.status === 'running' && h('div', { class: 'mt-3' }, bar(STORE_LIST_STAGE_LABELS[p.stage] || '進み具合', p.done, p.total)),
      info.length > 0 && h('dl', { class: 'grid gap-x-4 gap-y-1 grid-cols-2 lg:grid-cols-3 text-sm mt-3' },
        info.map(([label, value]) => h('div', { class: 'flex gap-2 min-w-0' },
          h('dt', { class: 'text-slate-500 whitespace-nowrap' }, label),
          h('dd', { class: 'text-slate-700 break-all' }, value)))),
      p.note && h('p', { class: 'text-sm text-slate-600 mt-2 break-words' }, str(p.note)),
      r.message && h('p', { class: `text-sm mt-2 break-words ${r.status === 'failed' ? 'text-red-600' : 'text-slate-600'}` }, str(r.message)));
  }

  function storeRow(s, dates, today) {
    const slot = slotOn(s, dates, today);
    const slotCell = slot === '9' ? h('span', { class: 'font-semibold text-rose-700' }, '+9%')
      : slot === '4' ? h('span', { class: 'font-semibold text-amber-700' }, '+4%')
        : h('span', { class: 'text-slate-300' }, slot === null ? '期間外' : '—');
    const rv = reviewStats(s.reviews);
    const next = nextListedDate(s, dates, today);
    const href = /^[\w-]+$/.test(s.id) ? `https://store.shopping.yahoo.co.jp/${s.id}/` : null;
    const expanded = state.expandedId === s.id;
    return h('tr', {
      class: `store-row border-t border-slate-100 hover:bg-slate-50 cursor-pointer ${s.inv === 'N' ? 'text-slate-400' : ''}`,
      dataset: { id: s.id }, tabindex: '0', 'aria-expanded': String(expanded),
    },
      h('td', { class: 'px-3 py-1.5 min-w-[12rem]' },
        href ? h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'text-blue-600 hover:underline font-medium' }, s.name)
          : h('span', { class: 'font-medium' }, s.name),
        h('div', { class: 'text-xs text-slate-400' }, s.id, s.inv === 'N' ? ' / 非適格' : s.inv === '?' ? ' / 適格不明' : '')),
      h('td', { class: TD }, s.genre || '—'),
      h('td', { class: `${TD} text-center` }, slotCell),
      h('td', { class: `${TD} text-center` }, s.gold ? '○' : ''),
      h('td', { class: `${TD} text-right` }, percentText(s.max_upsell),
        s.n !== null && h('span', { class: 'text-xs text-slate-400 ml-1' }, `(${num(s.n_up ?? 0)}/${num(s.n)})`)),
      h('td', { class: `${TD} text-right` }, s.unknown === null ? '—' : num(s.unknown)),
      h('td', { class: `${TD} text-right` }, rv === null ? '—' : `${num(rv.count)} 件${rv.average === null ? '' : ` / ${rv.average.toFixed(2)}`}`),
      h('td', { class: `${TD} text-right` }, `${num(s.days)} 日`, h('span', { class: 'text-xs text-slate-400 ml-1' }, `(+9: ${num(s.d9)} / +4: ${num(s.d4)})`)),
      h('td', { class: TD }, next ? `${monthDay(next)} (${weekday(next)})` : '—'));
  }

  function detailRow(s, dates) {
    const chips = dates.map((d, i) => {
      const c = s.pat[i];
      const slot = c === '9' || c === '4' ? c : '-';
      const cls = slot === '9' ? 'bg-rose-100 text-rose-800' : slot === '4' ? 'bg-amber-100 text-amber-800' : 'bg-slate-100 text-slate-400';
      return h('span', { class: `inline-flex flex-col items-center px-1.5 py-0.5 rounded text-xs leading-tight ${cls}`, title: str(d) },
        h('span', {}, monthDay(d)), h('span', { class: 'font-semibold' }, slot === '-' ? '−' : `+${slot}`));
    });
    return h('tr', { class: 'detail-row bg-slate-50 border-t border-slate-100' },
      h('td', { colSpan: 9, class: 'px-3 py-2' },
        h('div', { class: 'flex flex-wrap gap-1' }, chips),
        h('div', { class: 'text-xs text-slate-500 mt-2' },
          `初回 ${str(s.first) || '—'} / 最終 ${str(s.last) || '—'} / カテゴリ ${s.cat || '—'}${s.reg ? ` / 登録番号 ${s.reg}` : ''}`)));
  }

  function renderTable() {
    if (!state.list) {
      ui.count.textContent = '';
      ui.table.replaceChildren();
      return;
    }
    const today = todayIso();
    const dates = Array.isArray(state.list.dates) ? state.list.dates.map(str) : [];
    const rows = sortStores(filterStores(state.list.stores, dates, state.filters, today));
    ui.count.textContent = `${num(rows.length)} 店を表示（全 ${num(state.list.stores.length)} 店、最大上乗せ率の高い順。行をクリックで日別の枠）`;
    const th = (label, cls = '') => h('th', { class: `${TH} ${cls}` }, label);
    const body = [];
    for (const s of rows) {
      body.push(storeRow(s, dates, today));
      if (state.expandedId === s.id) body.push(detailRow(s, dates));
    }
    mount(ui.table, h('table', { class: 'w-full text-sm' },
      h('thead', { class: 'bg-slate-100 text-slate-600 text-left' }, h('tr', {},
        th('ストア'), th('ジャンル'), th('今日の枠', 'text-center'), th('優良', 'text-center'), th('最大上乗せ', 'text-right'),
        th('判定不可', 'text-right'), th('レビュー 件 / 平均', 'text-right'), th('参加日数', 'text-right'), th('次回掲載'))),
      h('tbody', {}, rows.length > 0 ? body
        : h('tr', {}, h('td', { colSpan: 9, class: 'px-3 py-6 text-center text-slate-400' }, '条件に合う店舗がありません')))));
  }

  function renderData() {
    if (!ui) return;
    renderSummary();
    renderWorker();
    renderUpdateButton();
    renderRequest();
    renderTable();
  }

  // ----- 読み込み・ポーリング -----
  function schedule() {
    clearTimeout(timer);
    if (!state.open) return;
    timer = setTimeout(() => refresh(), state.activeRequest ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  }

  /** withList: 店舗リスト本体 (大きい) も読み直す。ポーリングでは依頼と待ち受けの状態だけ読む */
  async function refresh({ withList = false } = {}) {
    const token = generation;
    const seq = ++refreshSeq;
    clearTimeout(timer);
    const [listRes, workerRes, latestRes, activeRes] = await Promise.all([
      withList
        ? supabase.from('arbitrage_store_lists').select(STORE_LIST_COLUMNS).order('id', { ascending: false }).limit(1).maybeSingle()
        : Promise.resolve(null),
      supabase.from('arbitrage_worker_state').select('heartbeat_at,updated_at').eq('singleton', true).maybeSingle(),
      supabase.from('arbitrage_research_requests').select(REQUEST_COLUMNS)
        .eq('params->>kind', 'store_list').order('id', { ascending: false }).limit(1).maybeSingle(),
      supabase.from('arbitrage_research_requests').select('id,status,params,cancel_requested')
        .in('status', ACTIVE_STATUSES).order('id', { ascending: false }).limit(1).maybeSingle(),
    ]);
    if (token !== generation || seq !== refreshSeq) return;

    for (const [name, res] of [['arbitrage_store_lists', listRes], ['arbitrage_worker_state', workerRes],
      ['arbitrage_research_requests', latestRes], ['arbitrage_research_requests (active)', activeRes]]) {
      if (res?.error) console.error(`load ${name} failed`, res.error);
    }
    const requestsError = Boolean(latestRes.error || activeRes.error);
    // 更新の依頼が終わっていたら、店舗リストを読み直す
    const wasActive = state.latestRequest && isActiveStatus(state.latestRequest.status);
    const nowLatest = latestRes.error ? state.latestRequest : (latestRes.data ?? null);
    const justFinished = wasActive && nowLatest && nowLatest.id === state.latestRequest.id && !isActiveStatus(nowLatest.status);

    let listPatch = {};
    if (listRes) {
      if (listRes.error) {
        listPatch = { missingTable: isMissingTableError(listRes.error) };
      } else if (!listRes.data) {
        listPatch = { list: null, listMissing: true, missingTable: false };
      } else {
        const stores = Array.isArray(listRes.data.stores) ? listRes.data.stores.map(normalizeStore) : [];
        listPatch = { list: { ...listRes.data, stores }, listMissing: false, missingTable: false };
      }
    }
    state = {
      ...state,
      ...listPatch,
      worker: workerRes.error ? state.worker : (workerRes.data ?? null),
      latestRequest: nowLatest,
      activeRequest: activeRes.error ? state.activeRequest : (activeRes.data ?? null),
      requestsError,
    };
    if (state.missingTable) setStatus(`店舗リスト用のテーブルがまだありません。${SQL_FILE} を Supabase で実行してください。`, 'error');
    else if (listRes?.error || workerRes.error || requestsError) setStatus('最新の状態を読み込めませんでした。しばらくすると自動で読み直します。', 'warn');
    else setStatus('');

    if (!state.loaded) {
      build();
      state = { ...state, loaded: true };
    } else if (withList) {
      // ジャンルの選択肢が変わりうるので、入力値を保ったまま作り直す
      state = { ...state, filters: readFilters() };
      buildFilters();
    }
    renderData();
    if (justFinished) {
      if (nowLatest.status === 'completed') setMessage('店舗リストを更新しました。', 'ok');
      refresh({ withList: true });
      return;
    }
    schedule();
  }

  // ----- 依頼・中止 -----
  async function requestUpdate() {
    if (state.busy || ui.update.disabled) return;
    const lines = [
      '店舗リストを更新しますか？', '',
      '・約 4 時間かかります (参加ストア一覧の取得と、店ごとの適格請求書・上乗せ率・評価の調査)',
      '・その間、リサーチは実行できません',
      '・日曜と 5 のつく日は避けてください (上乗せ率の調査に日曜特典が混ざります)',
    ];
    const d = new Date();
    if (d.getDay() === 0 || d.getDate() % 5 === 0) lines.push('', `今日 (${monthDay(todayIso())}) は日曜か 5 のつく日です。`);
    if (!isWorkerAlive(state.worker?.heartbeat_at)) lines.push('', '待ち受けが停止中です。待ち受けを起動すると開始します。');
    if (!confirm(lines.join('\n'))) return;

    const token = generation;
    state = { ...state, busy: true };
    renderUpdateButton();
    setMessage('依頼を送信中…');
    try {
      const { error } = await supabase.from('arbitrage_research_requests').insert({ params: { kind: 'store_list' } });
      if (token !== generation) return;
      if (error) {
        console.error('insert store_list request failed', error);
        if (error.code === '23505') setMessage('すでに待ち・実行中の依頼があります。', 'error');
        else if (isMissingTableError(error)) setMessage('db/arbitrage_research.sql を Supabase で実行してください。', 'error');
        else setMessage('依頼を送信できませんでした。通信状態とログイン状態を確認して、もう一度お試しください。', 'error');
      } else {
        setMessage(isWorkerAlive(state.worker?.heartbeat_at) ? '依頼しました。まもなく開始します。' : '依頼しました。待ち受けを起動すると開始します。', 'ok');
        onRequestQueued?.();
      }
    } finally {
      if (token === generation) {
        state = { ...state, busy: false };
        await refresh();
      }
    }
  }

  async function cancel(id) {
    if (state.busy) return;
    if (!confirm('店舗リストの更新を中止しますか？\n店舗リストは前のままになります。')) return;
    const token = generation;
    state = { ...state, busy: true };
    renderData();
    try {
      const { data, error } = await supabase.from('arbitrage_research_requests')
        .update({ cancel_requested: true }).eq('id', id).in('status', ACTIVE_STATUSES).select('id');
      if (token !== generation) return;
      if (error) {
        console.error('cancel store_list request failed', error);
        setMessage('中止を依頼できませんでした。通信状態を確認して、もう一度お試しください。', 'error');
      } else if ((data ?? []).length === 0) {
        setMessage('この依頼はすでに終了しています。', 'warn');
      } else {
        setMessage('中止を依頼しました。区切りのよいところで止まります。', 'ok');
      }
    } finally {
      if (token === generation) {
        state = { ...state, busy: false };
        await refresh();
      }
    }
  }

  // ----- 入力・ボタン -----
  $body.addEventListener('input', (e) => {
    if (!state.loaded || !e.target.dataset?.filter) return;
    state = { ...state, filters: readFilters() };
    renderTable();
  });

  function toggleRow($row) {
    const id = $row.dataset.id;
    state = { ...state, expandedId: state.expandedId === id ? null : id };
    renderTable();
    $body.querySelector(`.store-row[data-id="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
  }

  $body.addEventListener('click', (e) => {
    const $btn = e.target.closest('button[data-action]');
    if ($btn) {
      if (!state.loaded) return;
      if ($btn.dataset.action === 'update') requestUpdate();
      else if ($btn.dataset.action === 'cancel') cancel(Number($btn.dataset.id));
      return;
    }
    if (e.target.closest('a')) return;
    const $row = e.target.closest('.store-row');
    if ($row) toggleRow($row);
  });
  $body.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('store-row')) {
      e.preventDefault();
      toggleRow(e.target);
    }
  });

  function stop() {
    generation += 1;
    clearTimeout(timer);
    timer = null;
  }

  return {
    /** 店舗リストタブを開いたとき。店舗リスト・依頼・待ち受けの状態を読み、ポーリングを始める */
    open() {
      if (state.open) return;
      state = { ...state, open: true, busy: false };
      if (!state.loaded) setStatus('読み込み中…');
      refresh({ withList: true });
    },
    /** タブを離れたとき。ポーリングを止める (絞り込みの入力は残す) */
    close() {
      if (!state.open) return;
      stop();
      state = { ...state, open: false, busy: false };
    },
    /** ログアウト時。ポーリングを止め、読み込んだ内容を捨てる */
    discard() {
      stop();
      state = emptyState();
      ui = null;
      $body.replaceChildren();
      setStatus('');
    },
  };
}
