// 買取価格チェッカー - メインロジック
import { isSupported, startScanner, stopScanner } from './scanner.js';

// ---------- 定数 ----------
const SOURCE_LABELS = {
  kaitorishouten: '買取商店',
  rudeya: '買取ルデヤ',
  kaitoriwiki: '買取wiki',
  morimori: '森森買取',
  ichoume: '買取一丁目',
  mobichi: 'モバイル一番',
  kensaku: '買取けんさく君',
};
const CONDITION_LABELS = { new: '新品', used: '中古' };

// 価格DBには無いがJAN検索リンクだけ提供する外部買取店
// { label, urlBuilder, copyJan: true ならクリック時にJANをクリップボードへコピー }
const EXTERNAL_SHOPS = [
  {
    key: 'morimori',
    label: '森森買取',
    urlBuilder: (jan) => `https://www.morimori-kaitori.jp/search/${encodeURIComponent(jan)}`,
  },
  {
    key: 'ichoume',
    label: '買取一丁目',
    // SPAでURLからJANを渡せないため、検索ページに飛ばしJANを自動コピー
    urlBuilder: () => 'https://www.1-chome.com/searchResult',
    copyJan: true,
  },
  {
    key: 'mobichi',
    label: 'モバイル一番',
    urlBuilder: () => 'https://www.mobile-ichiban.com/',
    copyJan: true,
    note: 'トップを開きJANをコピーします。検索欄にペーストしてください。',
  },
  {
    key: 'kensaku',
    label: '買取けんさく君',
    urlBuilder: (jan) => `https://www.kaitorikensakukun.com/itemsearch/?product_name=${encodeURIComponent(jan)}`,
  },
];
const DEBOUNCE_MS = 250;
const MAX_SUGGESTIONS = 20;
const STORAGE_KEY = 'kw_search_history_v1';
const MAX_HISTORY = 20;

// ---------- Supabase 初期化 ----------
if (!window.SUPABASE_URL || !window.SUPABASE_ANON_KEY ||
    window.SUPABASE_ANON_KEY.startsWith('__')) {
  document.getElementById('status').innerHTML =
    '<span class="text-red-600">⚠ config.js に Supabase の anon key が設定されていません。</span>';
}
const supabase = window.supabase.createClient(
  window.SUPABASE_URL,
  window.SUPABASE_ANON_KEY,
);

// ---------- DOM参照 ----------
const $input = document.getElementById('search-input');
const $clear = document.getElementById('clear-btn');
const $suggestions = document.getElementById('suggestions');
const $status = document.getElementById('status');
const $resultList = document.getElementById('result-list');
const $resultToolbar = document.getElementById('result-toolbar');
const $historyCount = document.getElementById('history-count');
const $clearHistoryBtn = document.getElementById('clear-history-btn');
const $summaryCard = document.getElementById('summary-card');
const $summaryTbody = document.getElementById('summary-tbody');
const $summaryToggle = document.getElementById('summary-toggle');
const $summaryToggleIcon = document.getElementById('summary-toggle-icon');
const $summaryBody = document.getElementById('summary-body');
const $summaryBest = document.getElementById('summary-best');
const SUMMARY_OPEN_KEY = 'kw_summary_open_v1';
const $scanBtn = document.getElementById('scan-btn');
const $scannerModal = document.getElementById('scanner-modal');
const $scannerClose = document.getElementById('scanner-close');
const $scannerError = document.getElementById('scanner-error');

// ---------- 集計用ステート ----------
// jan_code -> { quantity, maxBySource: Map<source, price> }
const cardState = new Map();

// ---------- 履歴ストレージ ----------
function loadHistory() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveHistory(items) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items.slice(0, MAX_HISTORY)));
  } catch (e) {
    console.warn('localStorage 保存に失敗:', e);
  }
}

function addToHistory(product, quantity = 1, externalPrices = {}) {
  const entry = {
    jan_code: product.jan_code,
    name: product.name || '',
    image_url: product.image_url || '',
    category: product.category || '',
    quantity,
    externalPrices,
  };
  const items = [entry, ...loadHistory().filter((p) => p.jan_code !== entry.jan_code)];
  saveHistory(items);
  return items;
}

function updateHistoryQuantity(jan, quantity) {
  const items = loadHistory().map((p) =>
    p.jan_code === jan ? { ...p, quantity } : p
  );
  saveHistory(items);
}

function updateHistoryExternalPrice(jan, source, price) {
  const items = loadHistory().map((p) => {
    if (p.jan_code !== jan) return p;
    const externalPrices = { ...(p.externalPrices || {}) };
    if (price && price > 0) {
      externalPrices[source] = price;
    } else {
      delete externalPrices[source];
    }
    return { ...p, externalPrices };
  });
  saveHistory(items);
}

function removeFromHistory(jan) {
  const items = loadHistory().filter((p) => p.jan_code !== jan);
  saveHistory(items);
  return items;
}

function clearAllHistory() {
  localStorage.removeItem(STORAGE_KEY);
}

function updateToolbar() {
  const count = $resultList.children.length;
  if (count === 0) {
    $resultToolbar.classList.add('hidden');
    return;
  }
  $resultToolbar.classList.remove('hidden');
  $historyCount.textContent = `履歴 ${count} 件`;
}

// ---------- 検索 (候補) ----------
let debounceTimer = null;
let activeIndex = -1;
let currentSuggestions = [];

$input.addEventListener('input', () => {
  const q = $input.value.trim();
  $clear.classList.toggle('hidden', q.length === 0);
  clearTimeout(debounceTimer);
  if (q.length < 2) {
    hideSuggestions();
    return;
  }
  debounceTimer = setTimeout(() => fetchSuggestions(q), DEBOUNCE_MS);
});

$input.addEventListener('keydown', (e) => {
  if ($suggestions.classList.contains('hidden')) return;
  const items = $suggestions.querySelectorAll('li');
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    activeIndex = Math.min(activeIndex + 1, items.length - 1);
    updateActiveItem(items);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    activeIndex = Math.max(activeIndex - 1, 0);
    updateActiveItem(items);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const target = activeIndex >= 0 ? currentSuggestions[activeIndex] : currentSuggestions[0];
    if (target) selectProduct(target);
  } else if (e.key === 'Escape') {
    hideSuggestions();
  }
});

$clear.addEventListener('click', () => {
  $input.value = '';
  $clear.classList.add('hidden');
  hideSuggestions();
  $status.textContent = '';
  $input.focus();
});

document.addEventListener('click', (e) => {
  if (!$suggestions.contains(e.target) && e.target !== $input) {
    hideSuggestions();
  }
});

// JANコピー (event delegation)
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.jan-copy');
  if (!btn) return;
  e.preventDefault();
  const jan = btn.dataset.jan;
  if (!jan) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(jan);
    } else {
      legacyCopy(jan);
    }
    showCopyFeedback(btn, '✓ コピー済');
  } catch (err) {
    console.warn('JAN copy failed:', err);
    showCopyFeedback(btn, '✗ 失敗', 'text-red-600');
  }
});

// data-external-jan 付きリンクは新タブで開きつつ、JAN をクリップボードへコピー
document.addEventListener('click', async (e) => {
  const link = e.target.closest('a.external-link[data-external-jan]');
  if (!link) return;
  const jan = link.dataset.externalJan;
  if (!jan) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(jan);
    } else {
      legacyCopy(jan);
    }
  } catch (err) {
    console.warn('external JAN copy failed:', err);
  }
});

// 外部店の手入力価格 → cardState 反映 & 永続化
document.addEventListener('input', (e) => {
  const $el = e.target.closest('.external-price-input');
  if (!$el) return;
  const card = $el.closest('article.result-card');
  if (!card) return;
  const jan = card.dataset.jan;
  const source = $el.dataset.source;
  const raw = parseInt($el.value, 10);
  const val = Number.isFinite(raw) && raw > 0 ? Math.min(raw, 9999999) : null;

  const state = cardState.get(jan);
  if (state) {
    if (val) state.maxBySource.set(source, val);
    else state.maxBySource.delete(source);
  }
  updateHistoryExternalPrice(jan, source, val);

  // 該当行の data-unit と 小計を更新
  const $sub = card.querySelector(`.card-subtotal[data-source="${cssEscape(source)}"]`);
  if ($sub) {
    $sub.dataset.unit = val || 0;
    $sub.textContent = val ? `小計 ¥${(val * (state?.quantity || 1)).toLocaleString()}` : '';
  }
  recalcSummary();
});

function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } finally { ta.remove(); }
}

function showCopyFeedback(btn, label, extraClass = 'text-emerald-600') {
  const origText = btn.textContent;
  const origClasses = btn.className;
  btn.textContent = label;
  btn.className = `${origClasses.replace(/text-(blue|red|emerald)-\d+/g, '')} ${extraClass}`.trim();
  setTimeout(() => {
    btn.textContent = origText;
    btn.className = origClasses;
  }, 1500);
}

function updateActiveItem(items) {
  items.forEach((li, i) => li.classList.toggle('active', i === activeIndex));
  if (activeIndex >= 0) items[activeIndex].scrollIntoView({ block: 'nearest' });
}

function hideSuggestions() {
  $suggestions.classList.add('hidden');
  activeIndex = -1;
}

async function fetchSuggestions(q) {
  $status.textContent = '検索中…';
  const isDigit = /^\d+$/.test(q);
  const escaped = q.replace(/[%_,]/g, '\\$&');
  const filter = isDigit
    ? `jan_code.ilike.${escaped}%`
    : `name.ilike.%${escaped}%,jan_code.ilike.%${escaped}%`;

  const { data, error } = await supabase
    .from('products')
    .select('jan_code,name,image_url,category,source')
    .or(filter)
    .limit(MAX_SUGGESTIONS);

  if (error) {
    console.error(error);
    $status.innerHTML = `<span class="text-red-600">検索エラー: ${error.message}</span>`;
    return;
  }

  const byJan = new Map();
  for (const row of data || []) {
    if (!byJan.has(row.jan_code)) byJan.set(row.jan_code, row);
  }
  currentSuggestions = Array.from(byJan.values());
  renderSuggestions(currentSuggestions);
  $status.textContent = currentSuggestions.length === 0
    ? `「${q}」に該当する商品が見つかりません。`
    : `${currentSuggestions.length} 件ヒット`;
}

function renderSuggestions(items) {
  $suggestions.innerHTML = '';
  activeIndex = -1;
  if (items.length === 0) {
    hideSuggestions();
    return;
  }
  for (const p of items) {
    const li = document.createElement('li');
    li.className = 'flex items-center gap-2 px-3 py-2 border-b border-slate-100 last:border-b-0';
    const img = p.image_url
      ? `<img src="${escapeHtml(p.image_url)}" alt="" class="w-10 h-10 object-contain bg-slate-50 rounded shrink-0" onerror="this.style.visibility='hidden'">`
      : `<div class="w-10 h-10 bg-slate-100 rounded shrink-0"></div>`;
    li.innerHTML = `
      ${img}
      <div class="min-w-0 flex-1">
        <div class="text-sm font-medium truncate">${escapeHtml(p.name || '(名前なし)')}</div>
        <div class="text-xs text-slate-500 font-mono">${escapeHtml(p.jan_code)} ${p.category ? '・ ' + escapeHtml(p.category) : ''}</div>
      </div>
    `;
    li.addEventListener('click', () => selectProduct(p));
    $suggestions.appendChild(li);
  }
  $suggestions.classList.remove('hidden');
}

// ---------- 商品選択 → カード追加 ----------
async function selectProduct(product, { persist = true, scroll = true, quantity = 1, externalPrices = {} } = {}) {
  hideSuggestions();
  $input.value = '';
  $clear.classList.add('hidden');
  $status.textContent = '';

  // 既存の同JANカードを削除 (関連する state もクリア)
  const existing = $resultList.querySelector(`[data-jan="${cssEscape(product.jan_code)}"]`);
  if (existing) {
    existing.remove();
    cardState.delete(product.jan_code);
  }

  // 履歴に保存 (LocalStorage)
  if (persist) addToHistory(product, quantity, externalPrices);

  // カード生成して先頭に挿入
  const card = createCard(product, quantity, externalPrices);
  $resultList.prepend(card);
  updateToolbar();

  if (scroll) card.scrollIntoView({ behavior: 'smooth', block: 'start' });

  // 価格テーブルを埋める (非同期)
  fillCardPrices(card, product.jan_code, quantity, externalPrices);
}

function createCard(product, quantity = 1, externalPrices = {}) {
  const card = document.createElement('article');
  card.className = 'result-card bg-white rounded-lg shadow p-4';
  card.dataset.jan = product.jan_code;

  const imgHtml = product.image_url
    ? `<img src="${escapeHtml(product.image_url)}" alt="" class="w-20 h-20 object-contain rounded bg-slate-100 shrink-0" onerror="this.style.visibility='hidden'">`
    : `<div class="w-20 h-20 bg-slate-100 rounded shrink-0"></div>`;

  card.innerHTML = `
    <div class="flex gap-3 items-start mb-3">
      ${imgHtml}
      <div class="min-w-0 flex-1">
        <h2 class="font-semibold leading-tight">${escapeHtml(product.name || '(名前なし)')}</h2>
        <p class="text-xs text-slate-500 mt-1 flex items-center gap-1 flex-wrap">
          <span>JAN:</span>
          <span class="font-mono">${escapeHtml(product.jan_code)}</span>
          <button type="button" class="jan-copy text-blue-600 hover:underline"
                  data-jan="${escapeHtml(product.jan_code)}"
                  title="JANをコピー" aria-label="JANをコピー">📋 コピー</button>
        </p>
        <p class="text-xs text-slate-500">
          カテゴリ: ${escapeHtml(product.category || '—')}
        </p>
      </div>
      <button class="card-remove text-slate-400 hover:text-red-600 text-xl leading-none shrink-0 px-2 -mr-2"
              aria-label="この履歴を削除" title="この履歴を削除">✕</button>
    </div>

    <div class="flex items-center gap-2 mb-3 text-sm">
      <label class="text-slate-600" for="qty-${escapeHtml(product.jan_code)}">数量:</label>
      <button type="button" class="qty-dec w-8 h-8 rounded border border-slate-300 hover:bg-slate-100" aria-label="減らす">−</button>
      <input id="qty-${escapeHtml(product.jan_code)}" type="number" inputmode="numeric"
             class="quantity-input w-16 text-center px-2 py-1 border border-slate-300 rounded outline-none focus:border-blue-500"
             value="${Number(quantity) || 1}" min="1" max="999">
      <button type="button" class="qty-inc w-8 h-8 rounded border border-slate-300 hover:bg-slate-100" aria-label="増やす">＋</button>
      <span class="text-xs text-slate-400 ml-1">点</span>
    </div>

    <div class="price-section">
      <div class="bg-slate-50 rounded p-3 text-sm text-slate-500 text-center loading">価格を取得中</div>
    </div>
  `;

  card.querySelector('.card-remove').addEventListener('click', () => {
    card.remove();
    cardState.delete(product.jan_code);
    removeFromHistory(product.jan_code);
    updateToolbar();
    recalcSummary();
  });

  const $qty = card.querySelector('.quantity-input');
  $qty.addEventListener('input', () => {
    const v = Math.max(1, Math.min(999, parseInt($qty.value, 10) || 1));
    if (String(v) !== $qty.value) $qty.value = String(v);
    const state = cardState.get(product.jan_code);
    if (state) state.quantity = v;
    updateHistoryQuantity(product.jan_code, v);
    updateCardSubtotals(card);
    recalcSummary();
  });
  card.querySelector('.qty-dec').addEventListener('click', () => {
    $qty.value = Math.max(1, (parseInt($qty.value, 10) || 1) - 1);
    $qty.dispatchEvent(new Event('input'));
  });
  card.querySelector('.qty-inc').addEventListener('click', () => {
    $qty.value = Math.min(999, (parseInt($qty.value, 10) || 1) + 1);
    $qty.dispatchEvent(new Event('input'));
  });

  return card;
}

const DB_SOURCES = ['kaitorishouten', 'rudeya', 'kaitoriwiki'];

async function fillCardPrices(card, jan, quantity = 1, externalPrices = {}) {
  const $section = card.querySelector('.price-section');
  const [prices, productsFallback] = await Promise.all([
    supabase
      .from('price_history')
      .select('source,condition,price,scraped_date,note,detail_url')
      .eq('jan_code', jan)
      .order('scraped_date', { ascending: false }),
    supabase
      .from('products')
      .select('source,detail_url')
      .eq('jan_code', jan),
  ]);

  if (prices.error) {
    $section.innerHTML = `<div class="text-sm text-red-600 px-3 py-2">価格取得エラー: ${escapeHtml(prices.error.message)}</div>`;
    return;
  }

  const fallbackUrlBySource = new Map();
  for (const row of productsFallback.data || []) {
    if (row.detail_url) fallbackUrlBySource.set(row.source, row.detail_url);
  }

  // (source, condition) ごとに最新行
  const latestByKey = new Map();
  for (const row of prices.data || []) {
    const key = `${row.source}|${row.condition}`;
    if (!latestByKey.has(key)) latestByKey.set(key, row);
  }

  // sourceごとに最高価格行 (condition問わず最大値)
  const dbBySource = new Map();
  for (const row of latestByKey.values()) {
    if (row.price == null) continue;
    const cur = dbBySource.get(row.source);
    if (!cur || row.price > cur.price) {
      dbBySource.set(row.source, {
        price: row.price,
        scraped_date: row.scraped_date,
        detail_url: row.detail_url,
      });
    }
  }

  // maxBySource: DB店 + 外部店 (手入力)
  const maxBySource = new Map();
  for (const [src, info] of dbBySource) maxBySource.set(src, info.price);
  for (const [src, p] of Object.entries(externalPrices || {})) {
    const n = Number(p);
    if (Number.isFinite(n) && n > 0) maxBySource.set(src, n);
  }
  cardState.set(jan, { quantity, maxBySource });

  $section.innerHTML = renderPriceTable({
    jan, quantity, dbBySource, fallbackUrlBySource, externalPrices,
  });

  recalcSummary();
}

function renderPriceTable({ jan, quantity, dbBySource, fallbackUrlBySource, externalPrices }) {
  const dbRows = DB_SOURCES.map((src) => {
    const info = dbBySource.get(src);
    if (!info) return null;
    let url = info.detail_url || fallbackUrlBySource.get(src);
    if (src === 'kaitorishouten') {
      url = `https://www.kaitorishouten-co.jp/?name=${encodeURIComponent(jan)}`;
    }
    return {
      src, price: info.price, scraped_date: info.scraped_date, url, manual: false,
    };
  }).filter(Boolean);

  const externalRows = EXTERNAL_SHOPS.map((shop) => ({
    src: shop.key,
    price: Number(externalPrices?.[shop.key]) || null,
    scraped_date: null,
    url: shop.urlBuilder(jan),
    manual: true,
    copyJan: !!shop.copyJan,
    jan,
  }));

  const rows = [...dbRows, ...externalRows];

  const trs = rows.map((r) => priceRowHtml(r, quantity)).join('');
  return `
    <div class="bg-slate-50 rounded overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-100 text-slate-600 text-left">
          <tr>
            <th class="px-3 py-2 font-medium">買取店</th>
            <th class="px-3 py-2 font-medium text-right">価格</th>
            <th class="px-3 py-2 font-medium hidden sm:table-cell">取得日</th>
            <th class="px-3 py-2 font-medium"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100 bg-white">${trs}</tbody>
      </table>
    </div>
  `;
}

function priceRowHtml(r, quantity) {
  const label = escapeHtml(SOURCE_LABELS[r.src] || r.src);
  const dateCell = r.scraped_date
    ? escapeHtml(r.scraped_date)
    : '<span class="text-slate-300">—</span>';

  const copyAttr = r.copyJan && r.jan
    ? ` data-external-jan="${escapeHtml(r.jan)}"`
    : '';
  const linkBtn = `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer"${copyAttr}
                      class="external-link text-blue-600 hover:underline text-xs">元ページ ↗</a>`;

  let priceCell;
  if (r.manual) {
    const v = r.price ? String(r.price) : '';
    const subtotal = r.price ? `小計 ¥${(r.price * quantity).toLocaleString()}` : '';
    priceCell = `
      <div class="flex items-center justify-end gap-1">
        <input type="number" inputmode="numeric"
               class="external-price-input w-24 text-right px-2 py-1 border border-slate-300 rounded outline-none focus:border-blue-500 tabular-nums"
               data-source="${escapeHtml(r.src)}"
               placeholder="入力" min="0" max="9999999" value="${escapeHtml(v)}">
        <span class="text-xs text-slate-400">円</span>
      </div>
      <div class="card-subtotal text-xs text-emerald-700 mt-0.5 text-right" data-source="${escapeHtml(r.src)}" data-unit="${r.price || 0}">${subtotal}</div>
    `;
  } else {
    priceCell = `
      <div class="font-semibold tabular-nums">¥${r.price.toLocaleString()}</div>
      <div class="card-subtotal text-xs text-emerald-700 mt-0.5" data-source="${escapeHtml(r.src)}" data-unit="${r.price}">小計 ¥${(r.price * quantity).toLocaleString()}</div>
    `;
  }

  return `
    <tr>
      <td class="px-3 py-2 font-medium">${label}</td>
      <td class="px-3 py-2 text-right">${priceCell}</td>
      <td class="px-3 py-2 text-slate-500 text-xs hidden sm:table-cell">${dateCell}</td>
      <td class="px-3 py-2 text-right">${linkBtn}</td>
    </tr>
  `;
}

// 数量変更時に各「小計」表示を更新
function updateCardSubtotals(card) {
  const jan = card.dataset.jan;
  const state = cardState.get(jan);
  if (!state) return;
  card.querySelectorAll('.card-subtotal').forEach(($el) => {
    const unit = parseInt($el.dataset.unit, 10) || 0;
    $el.textContent = unit > 0 ? `小計 ¥${(unit * state.quantity).toLocaleString()}` : '';
  });
}

// 店舗別 合計の再計算と描画
function recalcSummary() {
  const totals = new Map(); // source -> { total, items }
  for (const state of cardState.values()) {
    if (state.maxBySource.size === 0) continue;
    for (const [src, price] of state.maxBySource) {
      const cur = totals.get(src) || { total: 0, items: 0 };
      cur.total += price * state.quantity;
      cur.items += state.quantity;
      totals.set(src, cur);
    }
  }
  if (totals.size === 0) {
    $summaryCard.classList.add('hidden');
    $summaryTbody.innerHTML = '';
    $summaryBest.textContent = '';
    return;
  }
  const sorted = [...totals.entries()].sort((a, b) => b[1].total - a[1].total);
  const best = sorted[0][1].total;
  const [bestSrc, bestRow] = sorted[0];
  $summaryBest.textContent = `(1位: ${SOURCE_LABELS[bestSrc] || bestSrc} ¥${bestRow.total.toLocaleString()})`;
  $summaryTbody.innerHTML = sorted.map(([src, { total, items }], i) => {
    const isBest = total === best && i === 0;
    const bestBadge = isBest ? '<span class="ml-2 text-xs bg-emerald-600 text-white rounded px-1.5 py-0.5">BEST</span>' : '';
    return `
      <tr class="${isBest ? 'bg-emerald-50' : ''}">
        <td class="px-3 py-2 font-medium">${escapeHtml(SOURCE_LABELS[src] || src)}${bestBadge}</td>
        <td class="px-3 py-2 text-center text-slate-600">${items} 点</td>
        <td class="px-3 py-2 text-right font-semibold tabular-nums ${isBest ? 'text-emerald-700' : ''}">¥${total.toLocaleString()}</td>
      </tr>
    `;
  }).join('');
  $summaryCard.classList.remove('hidden');
}

// サマリーの開閉
function setSummaryOpen(open) {
  if (open) {
    $summaryBody.classList.remove('hidden');
    $summaryToggleIcon.textContent = '▼';
    $summaryToggle.setAttribute('aria-expanded', 'true');
  } else {
    $summaryBody.classList.add('hidden');
    $summaryToggleIcon.textContent = '▶';
    $summaryToggle.setAttribute('aria-expanded', 'false');
  }
  try { localStorage.setItem(SUMMARY_OPEN_KEY, open ? '1' : '0'); } catch {}
}

$summaryToggle.addEventListener('click', () => {
  const isOpen = !$summaryBody.classList.contains('hidden');
  setSummaryOpen(!isOpen);
});

// 初期状態: LocalStorageに保存された開閉状態を復元 (なければ閉じ)
(function initSummaryOpenState() {
  let saved = null;
  try { saved = localStorage.getItem(SUMMARY_OPEN_KEY); } catch {}
  setSummaryOpen(saved === '1');
})();

// ---------- 履歴全削除 ----------
$clearHistoryBtn.addEventListener('click', () => {
  const count = $resultList.children.length;
  if (count === 0) return;
  if (!confirm(`履歴 ${count} 件をすべて削除しますか？`)) return;
  clearAllHistory();
  $resultList.innerHTML = '';
  cardState.clear();
  updateToolbar();
  recalcSummary();
});

// ---------- バーコードスキャナ ----------
$scanBtn.addEventListener('click', async () => {
  if (!isSupported()) {
    alert('お使いの端末/ブラウザはカメラ読み取りに対応していません。商品名やJANを直接入力してください。');
    return;
  }
  $scannerError.classList.add('hidden');
  $scannerModal.classList.remove('hidden');
  await startScanner({
    elementId: 'reader',
    onDetect: async (jan) => {
      await stopScanner();
      $scannerModal.classList.add('hidden');
      await directJanLookup(jan);
    },
    onError: (e) => {
      console.error(e);
      $scannerError.textContent = `カメラ起動エラー: ${e.message || e}`;
      $scannerError.classList.remove('hidden');
    },
  });
});

$scannerClose.addEventListener('click', async () => {
  await stopScanner();
  $scannerModal.classList.add('hidden');
});

async function directJanLookup(jan) {
  $status.textContent = `JAN ${jan} の商品を取得中…`;
  const { data, error } = await supabase
    .from('products')
    .select('jan_code,name,image_url,category,source')
    .eq('jan_code', jan)
    .limit(1);
  if (error) {
    $status.innerHTML = `<span class="text-red-600">取得エラー: ${escapeHtml(error.message)}</span>`;
    return;
  }
  if (!data || data.length === 0) {
    $status.innerHTML = `JAN <span class="font-mono">${escapeHtml(jan)}</span> に該当する商品が見つかりません。`;
    return;
  }
  selectProduct(data[0]);
}

// ---------- 初期化: 履歴を復元 ----------
function restoreHistory() {
  const items = loadHistory();
  if (items.length === 0) return;
  // LocalStorage の先頭が新しい順なので append でループ
  for (const p of items) {
    const quantity = Number.isFinite(p.quantity) && p.quantity >= 1 ? p.quantity : 1;
    const externalPrices = p.externalPrices && typeof p.externalPrices === 'object' ? p.externalPrices : {};
    const card = createCard(p, quantity, externalPrices);
    $resultList.appendChild(card);
    fillCardPrices(card, p.jan_code, quantity, externalPrices);
  }
  updateToolbar();
}

restoreHistory();

// ---------- ユーティリティ ----------
function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function cssEscape(s) {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}
