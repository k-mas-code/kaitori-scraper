// 利益商品リスト - 「リサーチ」画面 (条件を決めて実行を依頼し、進み具合を見る)
// 依頼は arbitrage_research_requests に 1 行 INSERT するだけ。実際に動かすのは、PC で起動しておく
// 待ち受け (python -m arbitrage.worker)。スキーマは db/arbitrage_research.sql。
// DOM に依存しない純粋関数は arbitrage-research-calc.js にあり、ここは画面だけを持つ。
import { isValidDate } from './arbitrage-settings.js';
import {
  ACTIVE_STATUSES, MIN_PROFIT_RANGE, STAGE_LABELS, STATUS_LABELS,
  activeOnDate, buildParams, canShowResults, checkDeadline, describeCoupon, describePoint, estimateDuration,
  formatDateTime, formatDuration, hasNewlyFinished, isActiveStatus, isInt, isMissingTableError, isObject,
  isWorkerAlive, num, paramsToForm, progressPercent, purchaseDateToApply, str, summarizeParams, todayIso,
} from './arbitrage-research-calc.js';

const POLL_ACTIVE_MS = 15000;   // 有効な依頼がある間
const POLL_IDLE_MS = 60000;     // 無い間は待ち受けの状態だけ、ゆっくり見直す
const HISTORY_LIMIT = 5;
const WORKER_COMMAND = '.venv/bin/python -m arbitrage.worker';
const SQL_FILE = 'db/arbitrage_research.sql';

// ---------- 画面 ----------
const INPUT = 'mt-1 w-full px-3 py-2 rounded-lg border border-slate-300 bg-white focus:border-blue-500 focus:ring-2 focus:ring-blue-200 outline-none disabled:bg-slate-100 disabled:text-slate-400';
const BTN_PRIMARY = 'px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 text-white font-medium';
const BTN_OUTLINE = 'px-3 py-2 rounded-lg border border-blue-600 text-blue-600 hover:bg-blue-50 text-sm font-medium';
const STATUS_COLORS = {
  queued: 'bg-slate-100 text-slate-700', running: 'bg-blue-100 text-blue-700', completed: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700', cancelled: 'bg-amber-100 text-amber-800',
};
const REQUEST_COLUMNS = 'id,created_at,status,params,cancel_requested,progress,message,run_id,started_at,finished_at,updated_at';

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

const emptyState = () => ({
  open: false,          // リサーチタブを表示中か (ポーリングはこの間だけ)
  loaded: false,        // フォームを組み立て済みか (入力内容はタブを移動しても残す)
  busy: false,          // 依頼・中止の送信中
  config: null,         // arbitrage_settings.config (無ければ null)
  seenSettingsDate: null,   // 前回読んだ設定の購入予定日 (設定側で変わったときだけ入力欄に反映する)
  worker: null,         // arbitrage_worker_state の行 (無ければ null)
  requests: [],         // 直近の依頼 (新しい順)
  missingTable: false,  // db/arbitrage_research.sql が未実行
});

/**
 * リサーチ画面を組み立てる。open() で読み込みとポーリングを始め、close() で止める。
 * onEditSettings: 「設定を編集」/ onShowResults(runId): 「結果を見る」/ onSettingsChanged: 設定の購入予定日を書き換えた後
 * onRunFinished: 表示中に依頼が終了した後 (実行一覧の読み直し用)
 */
export function createResearch({ supabase, $root, onEditSettings, onShowResults, onSettingsChanged, onRunFinished }) {
  const $ = (name) => $root.querySelector(`[data-research="${name}"]`);
  const $status = $('status');
  const $body = $('body');

  let state = emptyState();
  let ui = null;          // フォームと各表示欄の要素
  let generation = 0;     // タブ移動・ログアウトの後に届いた古い応答を捨てるための番号
  let refreshSeq = 0;     // 再読込が重なったとき、最後に始めたものだけを表示に使う
  let timer = null;

  function setStatus(text, kind = 'info') {
    const color = { info: 'text-slate-500', ok: 'text-emerald-700', warn: 'text-amber-700', error: 'text-red-600' }[kind];
    $status.textContent = text;
    $status.className = `text-sm pb-3 ${color}`;
  }

  function setRunMessage(text, kind = 'info') {
    if (!ui) return;
    const color = { info: 'text-slate-500', ok: 'text-emerald-700', warn: 'text-amber-700', error: 'text-red-600' }[kind];
    ui.runMessage.textContent = text;
    ui.runMessage.className = `text-sm ${color}`;
  }

  const latest = () => state.requests[0] ?? null;
  const activeRequest = () => state.requests.find((r) => isActiveStatus(r.status)) ?? null;
  const settingsDate = () => (isObject(state.config) && isValidDate(state.config.purchase_date) ? state.config.purchase_date : null);

  // ----- フォーム -----
  function card(title, ...children) {
    return h('section', { class: 'bg-white rounded-lg shadow p-4 mb-4' },
      h('h2', { class: 'font-semibold text-slate-700' }, title), children);
  }

  function field(label, name, attrs = {}, help = null) {
    return h('label', { class: 'block text-sm' },
      h('span', { class: 'text-slate-500' }, label),
      h('input', { type: 'text', autocomplete: 'off', ...attrs, class: INPUT, dataset: { field: name } }),
      help && h('span', { class: 'block text-xs text-slate-400 mt-1' }, help),
      h('span', { class: 'hidden text-xs text-red-600 mt-1', dataset: { error: name }, id: `research-err-${name}` }));
  }

  function radio(value, label) {
    return h('label', { class: 'flex items-center gap-2 text-sm' },
      h('input', { type: 'radio', name: 'research-page-scope', value, class: 'h-4 w-4', dataset: { field: 'page_scope' } }),
      h('span', { class: 'text-slate-700' }, label));
  }

  function buildForm(form) {
    const region = (name, cls = '') => h('div', { class: cls, dataset: { region: name } });
    const $form = card('条件',
      h('div', { class: 'grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mt-3' },
        field('購入予定日', 'purchase_date', { type: 'date' }, '変更して実行すると、設定の購入予定日も同じ日付になります。'),
        field('対象の買取金額 下限 (円)', 'min_buyback', { inputmode: 'numeric', placeholder: '指定なし' }),
        field('対象の買取金額 上限 (円)', 'max_buyback', { inputmode: 'numeric', placeholder: '指定なし' }),
        field('載せる利益の下限 (円)', 'min_profit', { type: 'number', step: '1', min: String(MIN_PROFIT_RANGE[0]), max: String(MIN_PROFIT_RANGE[1]) },
          '1 = 黒字のみ。-1000 なら 1,000 円までの赤字も載せる')),
      h('div', { class: 'mt-4 border border-slate-200 rounded-lg p-3' },
        h('div', { class: 'flex flex-wrap items-center gap-2' },
          h('span', { class: 'text-sm font-semibold text-slate-600' }, 'この日に使われるポイント・クーポン'),
          h('span', { class: 'flex-1' }),
          h('button', { type: 'button', class: BTN_OUTLINE, dataset: { action: 'edit-settings' } }, '設定を編集')),
        region('active', 'mt-2')),
      h('div', { class: 'grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mt-4' },
        h('fieldset', { class: 'sm:col-span-2' },
          h('legend', { class: 'text-sm text-slate-500' }, '確定判定の範囲'),
          h('div', { class: 'mt-2 space-y-2' },
            radio('reachable', '届きそうな候補だけ (速い)'),
            radio('all', '全候補 (約 3〜4 倍の時間)')),
          h('span', { class: 'hidden text-xs text-red-600 mt-1', dataset: { error: 'page_scope' } })),
        field('届きそうと見なす余地 (%)', 'reach_slack', { type: 'number', step: '0.5', min: '0', max: '20' },
          '広げるほど確定判定の件数が増えます (予測時間には反映されません)'),
        field('終了時刻の上限 (任意)', 'deadline', { type: 'time' }, '次に来るその時刻で打ち切ります。空欄 = 最後まで')));

    const $run = h('button', { type: 'button', class: BTN_PRIMARY, dataset: { action: 'run' } }, 'リサーチ実行');
    const $runMessage = h('span', { class: 'text-sm text-slate-500', role: 'status' });
    $body.replaceChildren(
      $form,
      card('所要時間の予測', region('estimate', 'mt-2')),
      card('実行',
        region('worker', 'mt-2 text-sm'),
        h('div', { class: 'mt-3 flex flex-wrap items-center gap-3' }, $run, $runMessage)),
      card('実行状況', region('current', 'mt-2')),
      card('履歴 (直近 5 件)', region('history', 'mt-2')));

    const $region = (name) => $body.querySelector(`[data-region="${name}"]`);
    ui = {
      run: $run, runMessage: $runMessage,
      active: $region('active'), estimate: $region('estimate'), worker: $region('worker'),
      current: $region('current'), history: $region('history'),
    };
    writeForm(form);
  }

  const $field = (name) => $body.querySelector(`[data-field="${name}"]`);

  function writeForm(form) {
    for (const name of ['purchase_date', 'min_buyback', 'max_buyback', 'min_profit', 'reach_slack']) $field(name).value = form[name];
    $field('deadline').value = form.deadline_time;
    for (const $radio of $body.querySelectorAll('[data-field="page_scope"]')) $radio.checked = $radio.value === form.page_scope;
    syncScope();
  }

  function readForm() {
    const value = (name) => $field(name).value;
    return {
      purchase_date: value('purchase_date'), min_buyback: value('min_buyback'), max_buyback: value('max_buyback'),
      min_profit: value('min_profit'), reach_slack: value('reach_slack'), deadline_time: value('deadline'),
      page_scope: $body.querySelector('[data-field="page_scope"]:checked')?.value ?? '',
    };
  }

  // 範囲が「全候補」のとき、余地は使わないので無効化する
  function syncScope() {
    $field('reach_slack').disabled = readForm().page_scope === 'all';
  }

  function showErrors(errors) {
    const byPath = new Map([...errors].reverse().map((e) => [e.path, e.message]));
    for (const $error of $body.querySelectorAll('[data-error]')) {
      const message = byPath.get($error.dataset.error);
      $error.textContent = message ?? '';
      $error.classList.toggle('hidden', !message);
      const $input = $field($error.dataset.error);
      if ($input && $input.type !== 'radio') {
        $input.classList.toggle('border-red-500', Boolean(message));
        $input.classList.toggle('border-slate-300', !message);
        if (message) $input.setAttribute('aria-invalid', 'true'); else $input.removeAttribute('aria-invalid');
      }
    }
  }

  // ----- 表示 -----
  function listRow({ name, value, note }) {
    return h('li', { class: 'flex flex-wrap gap-x-3 py-0.5' },
      h('span', { class: 'text-slate-700' }, name),
      h('span', { class: 'font-medium text-slate-700 whitespace-nowrap' }, value),
      note && h('span', { class: 'text-xs text-slate-500 self-center' }, note));
  }

  function listBlock(title, rows, empty) {
    return h('div', { class: 'mt-2' },
      h('div', { class: 'text-xs font-semibold text-slate-500' }, `${title}（${rows.length} 件）`),
      rows.length > 0
        ? h('ul', { class: 'text-sm mt-1' }, rows.map(listRow))
        : h('p', { class: 'text-sm text-slate-400 mt-1' }, empty));
  }

  function renderActive() {
    const date = readForm().purchase_date;
    if (!isObject(state.config)) {
      ui.active.replaceChildren(h('p', { class: 'text-sm text-amber-700' },
        '設定がまだ保存されていません。「設定を編集」から設定を保存すると、リサーチを実行できます。'));
      return;
    }
    if (!isValidDate(date)) {
      ui.active.replaceChildren(h('p', { class: 'text-sm text-slate-400' }, '購入予定日を選ぶと、その日に有効なものを表示します。'));
      return;
    }
    const active = activeOnDate(state.config, date);
    ui.active.replaceChildren(
      date < todayIso() && h('p', { class: 'text-sm text-amber-700' }, '購入予定日が過去の日付です。'),
      listBlock(`${date} のキャンペーン`, active.campaigns.map(describePoint), 'この日が対象日のキャンペーンはありません'),
      listBlock('使える手持ちクーポン', active.coupons.map(describeCoupon), 'この日に有効な手持ちクーポンはありません'),
      listBlock('毎日付くポイント', active.common.map(describePoint), '登録なし'),
      h('p', { class: 'text-xs text-slate-400 mt-2' },
        'ストアポイントとボーナスストアPlus (+4% / +9%) は、商品ページと店舗リストから自動で読みます。'));
  }

  function currentEstimate(form, now) {
    const { params } = buildParams(form, now);
    const valid = (v) => (isInt(v) && v >= 0 ? v : null);
    const estimate = estimateDuration({
      histogram: state.worker?.buyback_histogram, rates: state.worker?.rates,
      minBuyback: valid(params.min_buyback), maxBuyback: valid(params.max_buyback), pageScope: params.page_scope,
    });
    return { estimate, deadline: params.deadline };
  }

  function stat(label, value, sub = null) {
    return h('div', { class: 'bg-slate-50 rounded-lg p-3' },
      h('div', { class: 'text-xs text-slate-500' }, label),
      h('div', { class: 'text-base font-semibold text-slate-700 mt-0.5' }, value),
      sub && h('div', { class: 'text-xs text-slate-500 mt-0.5' }, sub));
  }

  function renderEstimate() {
    const now = new Date();
    const { estimate, deadline } = currentEstimate(readForm(), now);
    if (!estimate) {
      ui.estimate.replaceChildren(
        h('div', { class: 'grid gap-3 grid-cols-2 lg:grid-cols-5' },
          stat('対象 JAN 数', '不明'), stat('API 段階', '不明'), stat('確定判定', '不明'), stat('合計', '不明'), stat('完了予定', '不明')),
        h('p', { class: 'text-sm text-amber-700 mt-3' }, '待ち受けを一度起動すると予測できます。'));
      return;
    }
    const finish = new Date(now.getTime() + estimate.totalSeconds * 1000);
    const check = checkDeadline(estimate, deadline, now);
    const deadlineText = deadline ? formatDateTime(deadline) : '';
    let $warning = null;
    if (check.level === 'api') {
      $warning = h('p', { class: 'mt-3 text-sm bg-red-50 border border-red-200 text-red-700 rounded-lg p-3', role: 'alert' },
        `終了時刻の上限 (${deadlineText}) までに API 段階が終わらない見込みです。確定判定に進めず、結果がほとんど残りません。`
        + '買取金額の範囲を狭めるか、終了時刻を遅くしてください。');
    } else if (check.level === 'pages') {
      $warning = h('p', { class: 'mt-3 text-sm bg-amber-50 border border-amber-200 text-amber-800 rounded-lg p-3', role: 'alert' },
        `終了時刻の上限 (${deadlineText}) が完了予定より早いため、確定判定の途中で打ち切られます`
        + `（約 ${num(Math.round(estimate.checks))} 件のうち約 ${num(check.checksPossible)} 件まで）。`
        + '見込みの高い順に確認するので、上位の候補は残ります。');
    }
    const asOf = formatDateTime(state.worker?.buyback_histogram?.as_of);
    ui.estimate.replaceChildren(
      h('div', { class: 'grid gap-3 grid-cols-2 lg:grid-cols-5' },
        stat('対象 JAN 数', `約 ${num(Math.round(estimate.jans))} 件`),
        stat('API 段階', formatDuration(estimate.apiSeconds)),
        stat('確定判定', formatDuration(estimate.checkSeconds), `約 ${num(Math.round(estimate.checks))} 件`),
        stat('合計', formatDuration(estimate.totalSeconds)),
        stat('完了予定', formatDateTime(finish.toISOString()), '今始めた場合')),
      $warning,
      h('p', { class: 'text-xs text-slate-400 mt-3' },
        `${asOf ? `買取価格データの基準: ${asOf}。` : ''}過去の実行の速さから計算した目安です。`));
  }

  function renderWorker() {
    if (isWorkerAlive(state.worker?.heartbeat_at)) {
      ui.worker.replaceChildren(h('p', { class: 'text-emerald-700 font-medium' }, '待ち受け: 動作中'));
      return;
    }
    const last = formatDateTime(state.worker?.heartbeat_at);
    ui.worker.replaceChildren(
      h('p', { class: 'text-amber-700 font-medium' }, `待ち受け: 停止中${last ? `（最後の応答 ${last}）` : ''}`),
      h('p', { class: 'text-slate-500 mt-1' }, '依頼は出せます。この PC のプロジェクトフォルダで次のコマンドを実行して待ち受けを起動すると開始します。'),
      h('code', { class: 'block mt-1 px-3 py-2 rounded-lg bg-slate-100 text-slate-800 text-sm select-all overflow-x-auto whitespace-nowrap' }, WORKER_COMMAND));
  }

  function renderRunButton() {
    const active = activeRequest();
    ui.run.disabled = state.busy || state.missingTable || Boolean(active) || !isObject(state.config);
    ui.run.title = active ? 'すでに実行中の依頼があります' : '';
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

  const badge = (status) => h('span', {
    class: `text-xs px-2 py-0.5 rounded-full whitespace-nowrap ${STATUS_COLORS[status] || STATUS_COLORS.queued}`,
  }, STATUS_LABELS[status] || str(status));

  function renderCurrent() {
    const r = latest();
    if (state.missingTable) {
      ui.current.replaceChildren(h('p', { class: 'text-sm text-red-600' }, `${SQL_FILE} を Supabase で実行してください。`));
      return;
    }
    if (!r) {
      ui.current.replaceChildren(h('p', { class: 'text-sm text-slate-400' }, 'まだ依頼はありません。'));
      return;
    }
    const p = isObject(r.progress) ? r.progress : {};
    const active = isActiveStatus(r.status);
    const alive = isWorkerAlive(state.worker?.heartbeat_at);
    const lines = [];
    if (r.status === 'queued') {
      lines.push(alive ? '待ち受けが依頼を受け取るのを待っています。' : '待ち受けを起動すると開始します。');
    }
    if (r.status === 'running' && !alive) {
      lines.push('待ち受けからの応答が途絶えています。PC の待ち受けが動いているか確認してください。');
    }
    if (active && r.cancel_requested) lines.push('中止を依頼済みです。確認済みの分を保存して止まります。');

    const info = [
      ['段階', r.status === 'running' ? (STAGE_LABELS[p.stage] || '準備中') : null],
      ['候補数', p.candidates != null ? `${num(p.candidates)} 件` : null],
      ['結果', p.results != null ? `${num(p.results)} 件` : null],
      ['完了予測', r.status === 'running' && p.eta ? formatDateTime(p.eta) : null],
      ['最終更新', formatDateTime(p.updated_at || r.updated_at) || null],
      ['実行名', p.run_name ? str(p.run_name) : null],
    ].filter(([, value]) => value);

    ui.current.replaceChildren(
      h('div', { class: 'flex flex-wrap items-center gap-2' },
        badge(r.status),
        h('span', { class: 'text-sm text-slate-500' }, `依頼 ${formatDateTime(r.created_at)}`),
        h('span', { class: 'flex-1' }),
        active && !r.cancel_requested && h('button', {
          type: 'button', disabled: state.busy,
          class: 'px-3 py-2 rounded-lg border border-red-600 text-red-600 hover:bg-red-50 disabled:opacity-50 text-sm font-medium',
          dataset: { action: 'cancel', id: String(r.id) },
        }, '中止'),
        canShowResults(r) && h('button', {
          type: 'button', class: BTN_OUTLINE, dataset: { action: 'show-results', runId: String(r.run_id) },
        }, '結果を見る')),
      h('p', { class: 'text-xs text-slate-500 mt-1' }, summarizeParams(r.params)),
      active && !r.cancel_requested && h('p', { class: 'text-xs text-slate-400 mt-1' }, '中止しても、確認済みの分は保存されます。'),
      lines.map((text) => h('p', { class: 'text-sm text-amber-700 mt-2' }, text)),
      (r.status === 'running' || p.jan_total != null) && h('div', { class: 'grid gap-3 sm:grid-cols-2 mt-3' },
        bar('API 段階 (JAN)', p.jan_done, p.jan_total),
        bar('確定判定 (候補)', p.pages_done, p.pages_total)),
      info.length > 0 && h('dl', { class: 'grid gap-x-4 gap-y-1 grid-cols-2 lg:grid-cols-3 text-sm mt-3' },
        info.map(([label, value]) => h('div', { class: 'flex gap-2 min-w-0' },
          h('dt', { class: 'text-slate-500 whitespace-nowrap' }, label),
          h('dd', { class: 'text-slate-700 break-all' }, value)))),
      p.note && h('p', { class: 'text-sm text-slate-600 mt-2 break-words' }, str(p.note)),
      r.message && h('p', {
        class: `text-sm mt-2 break-words ${r.status === 'failed' ? 'text-red-600' : 'text-slate-600'}`,
      }, str(r.message)));
  }

  function renderHistory() {
    if (state.requests.length === 0) {
      ui.history.replaceChildren(h('p', { class: 'text-sm text-slate-400' }, '履歴はありません。'));
      return;
    }
    ui.history.replaceChildren(h('ul', { class: 'divide-y divide-slate-100 text-sm' },
      state.requests.slice(0, HISTORY_LIMIT).map((r) => h('li', { class: 'py-2 flex flex-wrap items-center gap-x-3 gap-y-1' },
        h('span', { class: 'text-slate-500 whitespace-nowrap' }, formatDateTime(r.created_at)),
        badge(r.status),
        h('span', { class: 'text-slate-700 whitespace-nowrap' },
          isObject(r.progress) && r.progress.results != null ? `結果 ${num(r.progress.results)} 件` : '結果 —'),
        h('span', { class: 'text-xs text-slate-500 basis-full sm:basis-auto' }, summarizeParams(r.params))))));
  }

  function renderData() {
    if (!ui) return;
    renderActive();
    renderEstimate();
    renderWorker();
    renderRunButton();
    renderCurrent();
    renderHistory();
  }

  // ----- 読み込み・ポーリング -----
  function schedule() {
    clearTimeout(timer);
    if (!state.open) return;
    timer = setTimeout(() => refresh(), activeRequest() ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  }

  /** withSettings: 設定も読み直す (タブを開いたとき)。ポーリングでは依頼と待ち受けの状態だけ読む */
  async function refresh({ withSettings = false } = {}) {
    const token = generation;
    const seq = ++refreshSeq;
    clearTimeout(timer);
    const [settingsRes, workerRes, requestsRes] = await Promise.all([
      withSettings
        ? supabase.from('arbitrage_settings').select('config').eq('singleton', true).maybeSingle()
        : Promise.resolve(null),
      supabase.from('arbitrage_worker_state').select('heartbeat_at,buyback_histogram,rates,updated_at').eq('singleton', true).maybeSingle(),
      supabase.from('arbitrage_research_requests').select(REQUEST_COLUMNS).order('id', { ascending: false }).limit(HISTORY_LIMIT),
    ]);
    if (token !== generation || seq !== refreshSeq) return;

    if (settingsRes?.error) {
      // 設定が読めないと購入予定日もキャンペーンも出せないので、フォームは出さない
      console.error('load arbitrage_settings failed', settingsRes.error);
      if (!state.loaded) {
        setStatus('設定を読み込めませんでした。時間をおいてもう一度お試しください（このアカウントに権限が無い場合も読み込めません）。', 'error');
        $body.replaceChildren(h('button', { type: 'button', class: BTN_PRIMARY, dataset: { action: 'reload' } }, 'もう一度読み込む'));
        return;
      }
    }
    const failed = [workerRes.error, requestsRes.error].filter(Boolean);
    const missingTable = failed.some(isMissingTableError);
    if (workerRes.error) console.error('load arbitrage_worker_state failed', workerRes.error);
    if (requestsRes.error) console.error('load arbitrage_research_requests failed', requestsRes.error);

    const finished = !requestsRes.error && hasNewlyFinished(state.requests, requestsRes.data);
    // 読めなかったものは前回の値を残す (一時的な通信エラーで表示を消さない)
    state = {
      ...state,
      config: settingsRes && !settingsRes.error ? (settingsRes.data?.config ?? null) : state.config,
      worker: workerRes.error ? state.worker : (workerRes.data ?? null),
      requests: requestsRes.error ? state.requests : (requestsRes.data ?? []),
      missingTable,
    };
    if (missingTable) setStatus(`リサーチ用のテーブルがまだありません。${SQL_FILE} を Supabase で実行してください。`, 'error');
    else if (failed.length > 0 || settingsRes?.error) setStatus('最新の状態を読み込めませんでした。しばらくすると自動で読み直します。', 'warn');
    else setStatus('');

    if (!state.loaded) {
      // 初回: 前回の依頼の条件を初期値にする (購入予定日は設定の値を優先)
      buildForm(paramsToForm(latest()?.params, settingsDate() ?? todayIso()));
      state = { ...state, loaded: true, seenSettingsDate: settingsDate() };
    } else if (withSettings && settingsRes && !settingsRes.error) {
      // タブに戻ってきたとき: 設定側で購入予定日が変わっていたら合わせて知らせる。
      // 変わっていなければ入力欄は触らない (この画面で入力した日付を黙って戻さない)
      const date = purchaseDateToApply(state.seenSettingsDate, settingsDate(), $field('purchase_date').value);
      if (date) {
        $field('purchase_date').value = date;
        setRunMessage(`購入予定日を設定の値（${date}）に合わせました。`, 'warn');
      }
      state = { ...state, seenSettingsDate: settingsDate() };
    }
    renderData();
    if (finished) onRunFinished?.();
    schedule();
  }

  // ----- 依頼・中止 -----
  /** 設定を読み直し、purchase_date だけ差し替えて保存する (他のキーは触らない)。成功なら true */
  async function updatePurchaseDate(date) {
    const read = await supabase.from('arbitrage_settings').select('config').eq('singleton', true).maybeSingle();
    if (read.error || !isObject(read.data?.config)) {
      console.error('reload arbitrage_settings failed', read.error ?? 'no settings row');
      return false;
    }
    const config = { ...read.data.config, purchase_date: date };
    const write = await supabase.from('arbitrage_settings').update({ config }).eq('singleton', true).select('updated_at');
    if (write.error || (write.data ?? []).length === 0) {
      console.error('update purchase_date failed', write.error ?? 'no row updated');
      return false;
    }
    state = { ...state, config, seenSettingsDate: date };
    onSettingsChanged?.();
    return true;
  }

  function confirmText(params, now) {
    const { estimate } = currentEstimate(readForm(), now);
    const lines = ['この条件でリサーチを実行しますか？', '', summarizeParams(params)];
    if (estimate) {
      const finish = new Date(now.getTime() + estimate.totalSeconds * 1000);
      lines.push(`対象 約 ${num(Math.round(estimate.jans))} JAN / 所要時間の予測 ${formatDuration(estimate.totalSeconds)}`
        + `（完了予定 ${formatDateTime(finish.toISOString())}）`);
    } else {
      lines.push('所要時間は予測できません（待ち受けを一度起動すると予測できます）。');
    }
    if (params.purchase_date !== settingsDate()) lines.push(`設定の購入予定日も ${params.purchase_date} に変更します。`);
    if (!isWorkerAlive(state.worker?.heartbeat_at)) lines.push('待ち受けが停止中です。待ち受けを起動すると開始します。');
    return lines.join('\n');
  }

  async function run() {
    if (state.busy || ui.run.disabled) return;
    const first = buildParams(readForm());
    showErrors(first.errors);
    if (first.errors.length > 0) {
      setRunMessage(`入力内容に ${first.errors.length} 件の誤りがあるため、依頼していません。`, 'error');
      $field(first.errors[0].path)?.focus();
      return;
    }
    if (!confirm(confirmText(first.params, new Date()))) return;
    // 確認の間に時間が経っているので、終了時刻を決め直す
    const { params, errors } = buildParams(readForm());
    if (errors.length > 0) { showErrors(errors); return; }

    const token = generation;
    state = { ...state, busy: true };
    renderRunButton();
    setRunMessage('依頼を送信中…');
    try {
      if (params.purchase_date !== settingsDate()) {
        const ok = await updatePurchaseDate(params.purchase_date);
        if (token !== generation) return;
        if (!ok) {
          setRunMessage('設定の購入予定日を更新できなかったため、依頼していません。通信状態を確認して、もう一度お試しください。', 'error');
          return;
        }
      }
      // status などは DB の既定値に任せる (列単位の権限で params しか書けない)
      const { error } = await supabase.from('arbitrage_research_requests').insert({ params });
      if (token !== generation) return;
      if (error) {
        console.error('insert arbitrage_research_requests failed', error);
        if (error.code === '23505') setRunMessage('すでに実行中の依頼があります。', 'error');
        else if (isMissingTableError(error)) setRunMessage(`${SQL_FILE} を Supabase で実行してください。`, 'error');
        else setRunMessage('依頼を送信できませんでした。通信状態とログイン状態を確認して、もう一度お試しください。', 'error');
      } else {
        setRunMessage(isWorkerAlive(state.worker?.heartbeat_at)
          ? '依頼しました。まもなく開始します。' : '依頼しました。待ち受けを起動すると開始します。', 'ok');
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
    if (!confirm('実行中のリサーチを中止しますか？\n確認済みの分は保存されます。')) return;
    const token = generation;
    state = { ...state, busy: true };
    renderData();
    try {
      const { data, error } = await supabase.from('arbitrage_research_requests')
        .update({ cancel_requested: true }).eq('id', id).in('status', ACTIVE_STATUSES).select('id');
      if (token !== generation) return;
      if (error) {
        console.error('cancel request failed', error);
        setRunMessage('中止を依頼できませんでした。通信状態を確認して、もう一度お試しください。', 'error');
      } else if ((data ?? []).length === 0) {
        setRunMessage('この依頼はすでに終了しています。', 'warn');
      } else {
        setRunMessage('中止を依頼しました。確認済みの分を保存して止まります。', 'ok');
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
    const name = e.target.dataset?.field;
    if (!name || !state.loaded) return;
    if (name === 'page_scope') syncScope();
    if (name === 'purchase_date') renderActive();
    renderEstimate();
  });

  $body.addEventListener('click', (e) => {
    const $btn = e.target.closest('button[data-action]');
    if (!$btn) return;
    const { action } = $btn.dataset;
    if (action === 'reload') {
      setStatus('読み込み中…');
      refresh({ withSettings: true });
    } else if (!state.loaded) {
      return;
    } else if (action === 'edit-settings') {
      onEditSettings?.();
    } else if (action === 'run') {
      run();
    } else if (action === 'cancel') {
      cancel(Number($btn.dataset.id));
    } else if (action === 'show-results') {
      onShowResults?.(Number($btn.dataset.runId));
    }
  });

  function stop() {
    generation += 1;
    clearTimeout(timer);
    timer = null;
  }

  return {
    /** リサーチタブを開いたとき。設定・依頼・待ち受けの状態を読み、ポーリングを始める */
    open() {
      if (state.open) return;
      state = { ...state, open: true, busy: false };
      if (!state.loaded) setStatus('読み込み中…');
      refresh({ withSettings: true });
    },
    /** タブを離れたとき。ポーリングを止める (入力内容は残す) */
    close() {
      if (!state.open) return;
      stop();
      state = { ...state, open: false, busy: false };
    },
    /** ログアウト時。ポーリングを止め、読み込んだ内容と入力を捨てる */
    discard() {
      stop();
      state = emptyState();
      ui = null;
      $body.replaceChildren();
      setStatus('');
    },
  };
}
