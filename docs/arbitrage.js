// 利益商品リスト - ログインしたオーナーだけが結果を読める (RLS)
import { createSettings } from './arbitrage-settings.js';
import { createResearch } from './arbitrage-research.js';

const SOURCE_LABELS = { kaitorishouten: '買取商店', rudeya: '買取ルデヤ', kaitoriwiki: '買取wiki' };
const PAGE_SIZE = 1000;   // PostgREST の 1 リクエストあたりの上限
const RESULT_COLUMNS = 'id,jan_code,item_name,category,store_id,store_name,item_url,sale_price,coupon,' +
  'coupon_discount,points,points_total,capped_campaigns,effective_price,buyback_price,buyback_source,' +
  'buyback_date,profit,profit_rate,checked_at';

// 検索ページ (index.html) は未ログインのまま使うので、ログイン情報の保存先を分ける。
// 既定の保存先だと、ここでログインした後に検索ページもログイン状態で DB を読みに行き、
// 公開テーブルのポリシー (未ログイン向け) に当たらず 0 件になる
const supabase = window.supabase.createClient(window.SUPABASE_URL, window.SUPABASE_ANON_KEY, {
  auth: { storageKey: 'kaitori-arbitrage-auth', persistSession: true, detectSessionInUrl: true },
});

const $ = (id) => document.getElementById(id);
const $loginCard = $('login-card');
const $loginForm = $('login-form');
const $loginEmail = $('login-email');
const $loginSubmit = $('login-submit');
const $loginMessage = $('login-message');
const $logout = $('logout-btn');
const $app = $('app');
const $runSelect = $('run-select');
const $minRate = $('min-rate');
const $category = $('category-select');
const $store = $('store-select');
const $status = $('status');
const $tbody = $('result-tbody');
const $tabs = $('tabs');
const $tabButtons = { results: $('tab-results'), research: $('tab-research'), settings: $('tab-settings') };
const $research = $('research');
const $settings = $('settings');

const settings = createSettings({ supabase, $root: $settings });
const research = createResearch({
  supabase,
  $root: $research,
  onEditSettings: () => switchTab('settings'),
  // 完了した実行の結果へ: 実行一覧を読み直して、その実行を選ぶ
  onShowResults: (runId) => { runsStale = false; switchTab('results'); loadRuns(runId); },
  // リサーチ画面が設定の購入予定日を書き換えた → 設定画面は次に開いたとき読み直す
  onSettingsChanged: () => settings.discard(),
  // リサーチ画面の表示中に依頼が終わった → 次に結果タブを開いたとき実行一覧を読み直す
  onRunFinished: () => { runsStale = true; },
});
// 実行一覧を読み直す必要があるか。リサーチ画面を開いたら立てる
// (離れている間はポーリングが止まるので、その間に終わった依頼は onRunFinished では分からない)
let runsStale = false;
let currentTab = 'results';

let results = [];          // 選択中の実行の全結果 (利益率の高い順)
let loadToken = 0;         // 実行日を素早く切り替えたとき、古い応答を捨てるための番号

// ---------- ログイン ----------
function showMessage(text, isError = false) {
  $loginMessage.textContent = text;
  $loginMessage.className = `text-sm mt-3 ${isError ? 'text-red-600' : 'text-emerald-700'}`;
}

$loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  $loginSubmit.disabled = true;
  showMessage('送信中…');
  const { error } = await supabase.auth.signInWithOtp({
    email: $loginEmail.value.trim(),
    options: {
      shouldCreateUser: false,   // 新規登録はしない
      emailRedirectTo: location.origin + location.pathname,
    },
  });
  $loginSubmit.disabled = false;
  if (error) {
    console.error('signInWithOtp failed', error);
    showMessage('リンクを送信できませんでした。メールアドレスを確認し、少し待ってからもう一度お試しください。', true);
    return;
  }
  showMessage('ログイン用のリンクを送りました。メールを開いてリンクを押してください。');
});

$logout.addEventListener('click', async () => {
  if (!confirmDiscard()) return;
  await supabase.auth.signOut();
});

supabase.auth.onAuthStateChange((_event, session) => {
  // コールバック内で Supabase を直接 await するとロックで固まるため、次のタスクに回す
  setTimeout(() => render(session), 0);
});

function render(session) {
  const loggedIn = Boolean(session);
  $loginCard.classList.toggle('hidden', loggedIn);
  $logout.classList.toggle('hidden', !loggedIn);
  $tabs.classList.toggle('hidden', !loggedIn);
  if (!loggedIn) {
    settings.discard();
    research.discard();
    currentTab = 'results';
  }
  showTab(loggedIn);
  if (loggedIn) {
    loadRuns();
  } else {
    results = [];
    $tbody.replaceChildren();
    const params = new URLSearchParams(location.hash.slice(1));
    if (params.get('error_description')) {
      showMessage('リンクが無効か、期限が切れています。もう一度リンクを送信してください。', true);
    }
  }
}

// ---------- タブ (利益商品リスト / リサーチ / 設定) ----------
function showTab(loggedIn = true) {
  $app.classList.toggle('hidden', !loggedIn || currentTab !== 'results');
  $research.classList.toggle('hidden', !loggedIn || currentTab !== 'research');
  $settings.classList.toggle('hidden', !loggedIn || currentTab !== 'settings');
  for (const [name, $btn] of Object.entries($tabButtons)) {
    const selected = name === currentTab;
    $btn.setAttribute('aria-selected', String(selected));
    $btn.className = `tab-btn px-4 py-2 rounded-lg text-sm font-medium ${selected
      ? 'bg-blue-600 text-white' : 'bg-white text-slate-600 shadow-sm hover:bg-slate-100'}`;
  }
  if (loggedIn && currentTab === 'settings') settings.open();
  // リサーチ画面は表示中だけ状態を読み直す (タブを離れたら止める)
  if (loggedIn && currentTab === 'research') research.open();
  else research.close();
}

// 未保存の変更があれば確認する。捨ててよければ true
function confirmDiscard() {
  if (!settings.isDirty()) return true;
  if (!confirm('設定に未保存の変更があります。保存せずに移動すると、変更は失われます。移動しますか？')) return false;
  settings.discard();
  return true;
}

// タブを切り替える。設定に未保存の変更があって移動をやめたら false
function switchTab(name) {
  if (name === currentTab) return true;
  if (currentTab === 'settings' && !confirmDiscard()) return false;
  if (name === 'research') runsStale = true;
  currentTab = name;
  showTab();
  // 新しい実行が増えているかもしれないとき (リサーチ画面を開いた後) だけ読み直す。選択中の実行は保つ
  if (name === 'results' && runsStale) loadRuns(Number($runSelect.value) || null);
  return true;
}

for (const [name, $btn] of Object.entries($tabButtons)) {
  $btn.addEventListener('click', () => switchTab(name));
}

// ---------- 実行一覧 ----------
// selectRunId: 選択した状態にする実行 (一覧に無ければ最新を選ぶ)
async function loadRuns(selectRunId = null) {
  runsStale = false;
  $status.textContent = '読み込み中…';
  const { data, error } = await supabase
    .from('arbitrage_runs')
    .select('id,started_at,finished_at,coupon_margin,jan_count,candidate_count,result_count')
    .eq('status', 'completed')
    .order('started_at', { ascending: false })
    .limit(200);
  if (error) {
    console.error('load runs failed', error);
    $status.textContent = '実行一覧を読み込めませんでした。時間をおいて再読み込みしてください。';
    return;
  }
  if (data.length === 0) {
    $runSelect.replaceChildren();
    $status.textContent = '表示できる結果がありません（まだ実行していないか、このアカウントに閲覧権限がありません）。';
    return;
  }
  $runSelect.replaceChildren(...data.map((run) => {
    const opt = document.createElement('option');
    opt.value = run.id;
    opt.textContent = `${formatDateTime(run.started_at)}（${run.result_count ?? 0}件）`;
    return opt;
  }));
  const selected = data.find((run) => run.id === selectRunId) ?? data[0];
  $runSelect.value = String(selected.id);
  loadResults(selected.id);
}

$runSelect.addEventListener('change', () => loadResults(Number($runSelect.value)));

// ---------- 結果 ----------
async function loadResults(runId) {
  const token = ++loadToken;
  $status.textContent = '読み込み中…';
  const rows = [];
  for (let from = 0; ; from += PAGE_SIZE) {
    const { data, error } = await supabase
      .from('arbitrage_results')
      .select(RESULT_COLUMNS)
      .eq('run_id', runId)
      .order('profit_rate', { ascending: false })
      .order('id', { ascending: true })
      .range(from, from + PAGE_SIZE - 1);
    if (token !== loadToken) return;
    if (error) {
      console.error('load results failed', error);
      $status.textContent = '結果を読み込めませんでした。時間をおいて再読み込みしてください。';
      return;
    }
    rows.push(...data);
    if (data.length < PAGE_SIZE) break;
  }
  results = rows;
  fillFilter($category, 'すべてのカテゴリ', rows.map((r) => [r.category || '', r.category || '（不明）']));
  fillFilter($store, 'すべての店舗', rows.map((r) => [r.store_id, r.store_name || r.store_id]));
  renderTable();
}

function fillFilter($select, allLabel, pairs) {
  const counts = new Map();
  for (const [value, label] of pairs) {
    const entry = counts.get(value) || { label, count: 0 };
    entry.count += 1;
    counts.set(value, entry);
  }
  const sorted = [...counts.entries()].sort((a, b) => b[1].count - a[1].count);
  const all = document.createElement('option');
  all.value = '__all__';
  all.textContent = allLabel;
  $select.replaceChildren(all, ...sorted.map(([value, { label, count }]) => {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = `${label}（${count}）`;
    return opt;
  }));
}

for (const $el of [$minRate, $category, $store]) {
  $el.addEventListener('input', renderTable);
}

function renderTable() {
  // 空欄は絞り込みなし (--min-profit で保存した赤字の商品も表示する)
  const minRate = $minRate.value.trim() === '' ? -Infinity : (Number($minRate.value) || 0) / 100;
  const rows = results.filter((r) =>
    Number(r.profit_rate) >= minRate &&
    ($category.value === '__all__' || (r.category || '') === $category.value) &&
    ($store.value === '__all__' || r.store_id === $store.value));
  $status.textContent = `${rows.length} 件を表示（全 ${results.length} 件、利益率の高い順）`;
  $tbody.innerHTML = rows.map(rowHtml).join('') ||
    '<tr><td colspan="6" class="px-3 py-6 text-center text-slate-400">条件に合う商品がありません</td></tr>';
}

function rowHtml(r) {
  const couponLabel = r.coupon?.source === 'manual' ? '手持ちクーポン' : 'クーポン';
  const coupon = r.coupon_discount > 0
    ? `<span class="ml-1 text-xs text-rose-600 whitespace-nowrap">${couponLabel} −${yen(r.coupon_discount)}</span>` : '';
  const capped = (r.capped_campaigns || []).length
    ? '<span class="ml-1 text-xs text-amber-600 whitespace-nowrap">上限到達あり</span>' : '';
  return `
    <tr class="result-row border-t border-slate-100 hover:bg-slate-50 cursor-pointer" data-id="${r.id}" tabindex="0" aria-expanded="false">
      <td class="px-3 py-2 text-right font-semibold ${r.profit > 0 ? 'text-emerald-700' : 'text-rose-600'} whitespace-nowrap">${percent(r.profit_rate)}</td>
      <td class="px-3 py-2 text-right whitespace-nowrap ${r.profit > 0 ? '' : 'text-rose-600'}">${r.profit < 0 ? '−' : '+'}${yen(Math.abs(r.profit))}</td>
      <td class="px-3 py-2">
        <div class="font-medium text-slate-700">${escapeHtml(r.item_name)}</div>
        <div class="text-xs text-slate-500">${escapeHtml(r.store_name || r.store_id)}${coupon}${capped}</div>
      </td>
      <td class="px-3 py-2 text-right whitespace-nowrap">${yen(r.sale_price)}</td>
      <td class="px-3 py-2 text-right whitespace-nowrap">${yen(r.effective_price)}</td>
      <td class="px-3 py-2 text-right whitespace-nowrap">${yen(r.buyback_price)}
        <div class="text-xs text-slate-500">${escapeHtml(SOURCE_LABELS[r.buyback_source] || r.buyback_source)}</div></td>
    </tr>`;
}

// 行を開くとポイント内訳とクーポンを表示
function toggleDetail($row) {
  const $next = $row.nextElementSibling;
  if ($next && $next.classList.contains('detail-row')) {
    $next.remove();
    $row.setAttribute('aria-expanded', 'false');
    return;
  }
  const r = results.find((x) => String(x.id) === $row.dataset.id);
  if (!r) return;
  $row.insertAdjacentHTML('afterend', detailHtml(r));
  $row.setAttribute('aria-expanded', 'true');
}

$tbody.addEventListener('click', (e) => {
  if (e.target.closest('a')) return;
  const $row = e.target.closest('.result-row');
  if ($row) toggleDetail($row);
});
$tbody.addEventListener('keydown', (e) => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('result-row')) {
    e.preventDefault();
    toggleDetail(e.target);
  }
});

function detailHtml(r) {
  const capped = new Set(r.capped_campaigns || []);
  const points = (r.points || []).map((p) => `
    <tr>
      <td class="pr-3 py-0.5">${escapeHtml(p.name)}${capped.has(p.name) ? ' <span class="text-amber-600">（上限 ' + number(p.cap) + 'pt に到達）</span>' : ''}</td>
      <td class="pr-3 py-0.5 text-right whitespace-nowrap">${percent(p.rate)}${p.base === 'tax_included' ? '（税込）' : ''}</td>
      <td class="py-0.5 text-right whitespace-nowrap">${number(p.points)} pt</td>
    </tr>`).join('');
  const couponHtml = couponDetailHtml(r);
  return `
    <tr class="detail-row bg-slate-50 border-t border-slate-100">
      <td colspan="6" class="px-3 py-3">
        <div class="grid gap-4 sm:grid-cols-2">
          <div>
            <div class="text-xs font-semibold text-slate-500 mb-1">ポイント内訳（合計 ${number(r.points_total)} pt）</div>
            <table class="text-sm"><tbody>${points}</tbody></table>
          </div>
          <div class="space-y-3">
            <div>
              <div class="text-xs font-semibold text-slate-500 mb-1">クーポン</div>
              ${couponHtml}
            </div>
            <div class="text-xs text-slate-500">
              実質 ${yen(r.effective_price)} ＝ 販売 ${yen(r.sale_price)} − クーポン ${yen(r.coupon_discount)} − ポイント ${number(r.points_total)}<br>
              買取 ${yen(r.buyback_price)}（${escapeHtml(SOURCE_LABELS[r.buyback_source] || r.buyback_source)}、${escapeHtml(r.buyback_date || '')} 時点）<br>
              JAN ${escapeHtml(r.jan_code)} ／ ${escapeHtml(r.category || 'カテゴリ不明')} ／ 確認 ${formatDateTime(r.checked_at)}
            </div>
            <div class="flex flex-wrap gap-3 text-sm">
              ${safeLink(r.item_url, '商品ページを開く')}
              <a class="text-blue-600 hover:underline" href="index.html?jan=${encodeURIComponent(r.jan_code)}" target="_blank" rel="noopener">買取価格を比較</a>
            </div>
          </div>
        </div>
      </td>
    </tr>`;
}

function couponDetailHtml(r) {
  const c = r.coupon;
  if (!c) return '<div class="text-slate-400">使えるクーポンなし</div>';
  if (c.source === 'manual') {
    // 設定画面で登録した手持ちクーポン (商品ページには出ないので、獲得済みかは自分で確認する)
    const until = c.valid_until ? `有効期限 ${escapeHtml(c.valid_until)} まで` : '有効期限の登録なし';
    return `
    <div><span class="text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 whitespace-nowrap">手持ちクーポン</span>
      ${escapeHtml(c.name || '（名前なし）')} <span class="text-rose-600">−${yen(c.discount ?? r.coupon_discount)}</span></div>
    <div class="text-xs text-slate-500">${until} / 設定画面で登録したクーポンです。購入前に獲得済みか確認してください</div>`;
  }
  return `
    <div>${safeLink(c.url, c.text || 'クーポン')} <span class="text-rose-600">−${yen(r.coupon_discount)}</span></div>
    <div class="text-xs text-slate-500">${[c.name, c.condition_text, c.limit_text, c.end_text].filter(Boolean).map(escapeHtml).join(' / ')}</div>`;
}

// ---------- ユーティリティ ----------
function yen(n) { return `¥${number(n)}`; }
function number(n) { return Number(n ?? 0).toLocaleString('ja-JP'); }
function percent(rate) { return `${(Number(rate) * 100).toFixed(1)}%`; }

function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' });
}

// DB の URL をそのまま href に入れない (https のみ許可)
function safeLink(url, label) {
  if (typeof url !== 'string' || !url.startsWith('https://')) return escapeHtml(label);
  return `<a class="text-blue-600 hover:underline" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
