// 買取価格チェッカー - メインロジック
import { isSupported, startScanner, stopScanner } from './scanner.js';

// ---------- 定数 ----------
const SOURCE_LABELS = {
  kaitorishouten: '買取商店',
  rudeya: '買取ルデヤ',
  kaitoriwiki: '買取wiki',
};
const CONDITION_LABELS = { new: '新品', used: '中古' };
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
const $scanBtn = document.getElementById('scan-btn');
const $scannerModal = document.getElementById('scanner-modal');
const $scannerClose = document.getElementById('scanner-close');
const $scannerError = document.getElementById('scanner-error');

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

function addToHistory(product) {
  const entry = {
    jan_code: product.jan_code,
    name: product.name || '',
    image_url: product.image_url || '',
    category: product.category || '',
  };
  const items = [entry, ...loadHistory().filter((p) => p.jan_code !== entry.jan_code)];
  saveHistory(items);
  return items;
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
async function selectProduct(product, { persist = true, scroll = true } = {}) {
  hideSuggestions();
  $input.value = '';
  $clear.classList.add('hidden');
  $status.textContent = '';

  // 既存の同JANカードを削除
  const existing = $resultList.querySelector(`[data-jan="${cssEscape(product.jan_code)}"]`);
  if (existing) existing.remove();

  // 履歴に保存 (LocalStorage)
  if (persist) addToHistory(product);

  // カード生成して先頭に挿入
  const card = createCard(product);
  $resultList.prepend(card);
  updateToolbar();

  if (scroll) card.scrollIntoView({ behavior: 'smooth', block: 'start' });

  // 価格テーブルを埋める (非同期)
  fillCardPrices(card, product.jan_code);
}

function createCard(product) {
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
        <p class="text-xs text-slate-500 mt-1">
          JAN: <span class="font-mono">${escapeHtml(product.jan_code)}</span>
        </p>
        <p class="text-xs text-slate-500">
          カテゴリ: ${escapeHtml(product.category || '—')}
        </p>
      </div>
      <button class="card-remove text-slate-400 hover:text-red-600 text-xl leading-none shrink-0 px-2 -mr-2"
              aria-label="この履歴を削除" title="この履歴を削除">✕</button>
    </div>
    <div class="price-section">
      <div class="bg-slate-50 rounded p-3 text-sm text-slate-500 text-center loading">価格を取得中</div>
    </div>
  `;

  card.querySelector('.card-remove').addEventListener('click', () => {
    card.remove();
    removeFromHistory(product.jan_code);
    updateToolbar();
  });

  return card;
}

async function fillCardPrices(card, jan) {
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

  // (source, condition) ごとに最新の1件
  const latestByKey = new Map();
  for (const row of prices.data || []) {
    const key = `${row.source}|${row.condition}`;
    if (!latestByKey.has(key)) latestByKey.set(key, row);
  }

  // 価格降順
  const rows = Array.from(latestByKey.values())
    .filter((r) => r.price != null)
    .sort((a, b) => b.price - a.price);

  if (rows.length === 0) {
    $section.innerHTML = `<div class="bg-slate-50 rounded p-3 text-sm text-slate-500 text-center">価格データがまだ無いか、削除されました。</div>`;
    return;
  }

  const trs = rows.map((r) => {
    const url = r.detail_url || fallbackUrlBySource.get(r.source);
    const linkBtn = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"
            class="text-blue-600 hover:underline text-xs">元ページ ↗</a>`
      : '';
    return `
      <tr>
        <td class="px-3 py-2 font-medium">${escapeHtml(SOURCE_LABELS[r.source] || r.source)}</td>
        <td class="px-3 py-2 text-slate-600">${escapeHtml(CONDITION_LABELS[r.condition] || r.condition)}</td>
        <td class="px-3 py-2 text-right font-semibold tabular-nums">¥${r.price.toLocaleString()}</td>
        <td class="px-3 py-2 text-slate-500 text-xs hidden sm:table-cell">${escapeHtml(r.scraped_date)}</td>
        <td class="px-3 py-2 text-right">${linkBtn}</td>
      </tr>
    `;
  }).join('');

  $section.innerHTML = `
    <div class="bg-slate-50 rounded overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-100 text-slate-600 text-left">
          <tr>
            <th class="px-3 py-2 font-medium">買取店</th>
            <th class="px-3 py-2 font-medium">状態</th>
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

// ---------- 履歴全削除 ----------
$clearHistoryBtn.addEventListener('click', () => {
  const count = $resultList.children.length;
  if (count === 0) return;
  if (!confirm(`履歴 ${count} 件をすべて削除しますか？`)) return;
  clearAllHistory();
  $resultList.innerHTML = '';
  updateToolbar();
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
  // 古いものから順に処理して、新しいものほど上に来るように reverse 順で prepend
  // ただし visual には保存順 (LocalStorage の先頭ほど新しい) を保ちたいので、配列を逆順に並べてprepend
  // → 簡潔に、append でループしてOK (LocalStorage先頭=新しい順)
  for (const p of items) {
    const card = createCard(p);
    $resultList.appendChild(card);
    fillCardPrices(card, p.jan_code);
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
