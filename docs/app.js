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
const $result = document.getElementById('result');
const $productImage = document.getElementById('product-image');
const $productName = document.getElementById('product-name');
const $productJan = document.getElementById('product-jan');
const $productCategory = document.getElementById('product-category');
const $tbody = document.getElementById('price-tbody');
const $priceEmpty = document.getElementById('price-empty');
const $scanBtn = document.getElementById('scan-btn');
const $scannerModal = document.getElementById('scanner-modal');
const $scannerClose = document.getElementById('scanner-close');
const $scannerError = document.getElementById('scanner-error');

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
  $result.classList.add('hidden');
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
  // PostgREST の or 内ではカンマがセパレータなので、安全のため英数記号のみ受ける
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

  // 同一JANで複数ソースの行が返ることがある → JANで集約
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

// ---------- 商品選択 → 価格取得 ----------
async function selectProduct(product) {
  hideSuggestions();
  $input.value = product.name || product.jan_code;
  $clear.classList.remove('hidden');

  $productImage.src = product.image_url || '';
  $productImage.style.visibility = product.image_url ? '' : 'hidden';
  $productName.textContent = product.name || '(名前なし)';
  $productJan.textContent = product.jan_code;
  $productCategory.textContent = product.category || '—';

  $result.classList.remove('hidden');
  $status.textContent = '価格を取得中…';
  $tbody.innerHTML = '';
  $priceEmpty.classList.add('hidden');

  await renderPrices(product.jan_code);
}

async function fetchPriceHistory(jan) {
  // 価格履歴 (全ソース・全状態・全日付) を1クエリで取得
  // detail_url も price_history に保存されているので追加 join 不要
  return supabase
    .from('price_history')
    .select('source,condition,price,scraped_date,note,detail_url')
    .eq('jan_code', jan)
    .order('scraped_date', { ascending: false });
}

// products から source 別 detail_url を fallback として取得 (旧データ救済用)
async function fetchProductsFallback(jan) {
  return supabase
    .from('products')
    .select('source,detail_url')
    .eq('jan_code', jan);
}

async function renderPrices(jan) {
  const [prices, productsFallback] = await Promise.all([
    fetchPriceHistory(jan),
    fetchProductsFallback(jan),
  ]);
  if (prices.error) {
    $status.innerHTML = `<span class="text-red-600">価格取得エラー: ${prices.error.message}</span>`;
    return;
  }

  // 旧データ用 fallback: products テーブルから source -> detail_url
  const fallbackUrlBySource = new Map();
  for (const row of productsFallback.data || []) {
    if (row.detail_url) fallbackUrlBySource.set(row.source, row.detail_url);
  }

  // (source, condition) ごとに最新の1件のみ残す
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
    $priceEmpty.classList.remove('hidden');
    $status.textContent = '';
    return;
  }

  $tbody.innerHTML = rows.map((r) => {
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

  $status.textContent = `${rows.length} 件の価格データ`;
}

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
      $input.value = jan;
      $clear.classList.remove('hidden');
      // 直接検索 → 一件だけならそのまま選択
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
    $status.innerHTML = `<span class="text-red-600">取得エラー: ${error.message}</span>`;
    return;
  }
  if (!data || data.length === 0) {
    $status.innerHTML = `JAN <span class="font-mono">${jan}</span> に該当する商品が見つかりません。`;
    $result.classList.add('hidden');
    return;
  }
  selectProduct(data[0]);
}

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
