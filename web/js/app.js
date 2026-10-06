/* RAT — application shell: state, wiring, filters, modals, rendering. */
import { api, ApiError, fmt, uploadZip, debounce, shortSha } from './api.js';
import {
  renderActivity, renderBars, renderDonut, forget, hideTooltip,
  PALETTE, OTHER_COLOR,
} from './charts.js';
import * as views from './views.js';

const $ = (id) => document.getElementById(id);
const ACTIVE_STATUSES = ['queued', 'cloning', 'extracting', 'parsing', 'loading'];

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */
const state = {
  view: 'dashboard',
  config: null,
  repos: [],
  repoId: null,
  detail: null,
  ref: null,
  authorsPayload: null,
  result: null,
  error: null,
  filters: { mode: 'all', from: null, to: null, shas: [], authorIds: [], path: null, pathType: null },
  table: {
    kind: 'files',
    query: '',
    sort: {
      files: { col: 'churn', dir: -1 },
      dirs: { col: 'churn', dir: -1 },
      authors: { col: 'churn', dir: -1 },
    },
  },
  querySeq: 0,
  pollTimer: null,
  prevStatuses: new Map(),
  pendingRef: null,
  overviewRows: null,
};

let loadingCount = 0;

/* ------------------------------------------------------------------ */
/* Small UI helpers                                                    */
/* ------------------------------------------------------------------ */
function showLoading(on) {
  loadingCount = Math.max(0, loadingCount + (on ? 1 : -1));
  $('loadingIndicator').hidden = loadingCount === 0;
}

function toast(msg, kind = 'info') {
  const host = $('toasts');
  const el = document.createElement('div');
  el.className = `toast${kind === 'error' ? ' error' : kind === 'success' ? ' success' : ''}`;
  el.textContent = msg;
  el.title = 'Click to dismiss';
  el.addEventListener('click', () => el.remove());
  host.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 7000 : 4200);
}

function errorToast(err) {
  toast(err && err.message ? err.message : String(err), 'error');
}

function openModal(html) {
  $('modalBox').innerHTML = html;
  $('modalRoot').hidden = false;
}

function closeModal() {
  $('modalRoot').hidden = true;
  $('modalBox').innerHTML = '';
  hideTooltip();
}

function setBanner(msg, kind = 'info') {
  const b = $('selectionWarn');
  if (!msg) {
    b.hidden = true;
    b.textContent = '';
    return;
  }
  b.className = `banner ${kind}`;
  b.textContent = msg;
  b.hidden = false;
}

function download(name, text) {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 500);
}

/* ------------------------------------------------------------------ */
/* Views / repositories                                                */
/* ------------------------------------------------------------------ */
function setView(view) {
  state.view = view;
  document.querySelectorAll('.tab[data-view]').forEach((t) =>
    t.classList.toggle('active', t.dataset.view === view));
  $('dashboardView').hidden = view !== 'dashboard';
  $('overviewView').hidden = view !== 'overview';
  if (view === 'overview') activateOverview();
  else updateStatusChip();
}

async function refreshRepos() {
  const data = await api.repos();
  state.repos = data.repos || [];
  state.prevStatuses = new Map(state.repos.map((r) => [r.id, r.status]));
  renderRepoSelect();
  updateStatusChip();
  if (state.view === 'overview') renderOverviewCards();
  return state.repos;
}

function renderRepoSelect() {
  const sel = $('repoSelect');
  const cur = state.repoId;
  sel.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = state.repos.length ? 'Select a repository…' : 'No repositories yet';
  placeholder.disabled = true;
  if (!cur) placeholder.selected = true;
  sel.appendChild(placeholder);
  for (const r of state.repos) {
    const o = document.createElement('option');
    o.value = r.id;
    const mark = r.status === 'ready' ? '' : r.status === 'error' ? ' ⚠' : ' ⏳';
    o.textContent = `${r.name}${mark}`;
    sel.appendChild(o);
  }
  if (cur) sel.value = cur;
}

function updateStatusChip() {
  const chip = $('repoStatusChip');
  const meta = state.repos.find((r) => r.id === state.repoId);
  if (!meta) { chip.hidden = true; return; }
  let inner = '';
  if (meta.status === 'error') {
    inner = `<span class="status-dot dot-error"></span><span class="chip-text">${fmt.esc(meta.error || 'Failed')}</span>`;
  } else if (ACTIVE_STATUSES.includes(meta.status)) {
    const pct = meta.progress != null ? Math.round(meta.progress) : null;
    inner = `<span class="status-dot dot-busy"></span><span class="chip-text">${fmt.esc(meta.message || meta.status)}</span>` +
      (pct != null ? `<span class="progress-track" style="width:64px;flex:0 0 auto"><span style="width:${pct}%"></span></span><span class="mono">${pct}%</span>` : '');
  } else if (state.pendingRef) {
    const job = ((meta.refJobs || {})[state.pendingRef]) || {};
    const busy = job.status !== 'error';
    const pct = busy && job.progress != null ? Math.round(job.progress) : null;
    inner = `<span class="status-dot ${busy ? 'dot-busy' : 'dot-error'}"></span>` +
      `<span class="chip-text">${fmt.esc(job.message || `Preparing ${state.pendingRef}`)}</span>` +
      (pct != null ? `<span class="mono">${pct}%</span>` : '');
  } else {
    chip.hidden = true;
    return;
  }
  chip.innerHTML = inner;
  chip.title = meta.error || meta.message || '';
  chip.hidden = false;
}

async function selectRepo(id) {
  // A reference belongs to one repository: when switching to another one,
  // fall back to its own default so the new stats load immediately.
  if (id !== state.repoId) state.ref = null;
  state.repoId = id;
  localStorage.setItem('rat.lastRepo', id);
  $('repoSelect').value = id;
  state.detail = null;
  state.result = null;
  state.authorsPayload = null;
  state.pendingRef = null;
  state.overviewRows = null;
  resetFiltersState();
  const meta = state.repos.find((r) => r.id === id);
  if (!meta) { showNoRepo(); return; }
  if (meta.status !== 'ready') {
    renderPreparing(meta);
    updateStatusChip();
    startPolling();
    return;
  }
  await loadRepoDetail();
}

function showNoRepo() {
  state.repoId = null;
  state.detail = null;
  state.result = null;
  $('emptyState').hidden = false;
  $('dashboardBody').hidden = true;
  $('refGroup').hidden = true;
  $('repoStatusChip').hidden = true;
  $('scopeTitle').innerHTML = '<span class="scope-kind">repository</span>';
  $('scopeMeta').textContent = '';
  $('scopeUpBtn').hidden = true;
}

async function loadRepoDetail() {
  showLoading(true);
  try {
    const { repo } = await api.repo(state.repoId, state.ref || undefined);
    if (repo.statsError && state.ref) {
      // The requested reference does not exist in this repository (possible
      // after a repository switch) — fall back to its default reference.
      state.ref = null;
      return await loadRepoDetail();
    }
    state.detail = repo;
    state.ref = repo.ref || state.ref || repo.defaultRef || 'HEAD';
    populateRefs();
    $('refGroup').hidden = false;
    if (!repo.headSha) {
      renderMessage('This repository has no commits.', 'info');
      return;
    }
    await loadAuthors();
    await runQuery();
    updateStatusChip();
  } catch (err) {
    errorToast(err);
    renderMessage(`Failed to load repository: ${err.message}`, 'error');
  } finally {
    showLoading(false);
  }
}

function populateRefs() {
  const detail = state.detail;
  const sel = $('refSelect');
  sel.innerHTML = '';
  const refs = detail.refs || [];
  const current = state.ref || detail.ref || detail.defaultRef || 'HEAD';
  let hasCurrent = false;
  const makeOpt = (parent, obj, label) => {
    const o = document.createElement('option');
    o.value = obj.name;
    o.textContent = label || obj.name;
    parent.appendChild(o);
    if (obj.name === current) hasCurrent = true;
  };
  const branches = refs.filter((r) => r.type !== 'tag');
  if (branches.length) {
    const g = document.createElement('optgroup');
    g.label = 'Branches';
    branches.forEach((r) => makeOpt(g, r));
    sel.appendChild(g);
  }
  const tags = refs.filter((r) => r.type === 'tag');
  if (tags.length) {
    const g = document.createElement('optgroup');
    g.label = 'Tags';
    tags.forEach((r) => makeOpt(g, r, `${r.name} (tag)`));
    sel.appendChild(g);
  }
  if (!hasCurrent) {
    const o = document.createElement('option');
    o.value = current;
    o.textContent = current;
    sel.insertBefore(o, sel.firstChild);
  }
  sel.value = current;
}

async function selectRef(ref) {
  if (!state.repoId || !ref || ref === state.ref) return;
  state.ref = ref;
  state.filters.authorIds = [];
  state.filters.path = null;
  state.filters.pathType = null;
  state.result = null;
  state.pendingRef = null;
  updatePathChip();
  $('refSelect').disabled = true;
  const meta = state.repos.find((r) => r.id === state.repoId);
  const job = meta && meta.refJobs ? meta.refJobs[ref] : null;
  try {
    if (job && job.status === 'ready') {
      await loadRepoDetail();
    } else {
      renderMessage(`Preparing reference “${ref}”…`, 'info');
      const resp = await api.prepare(state.repoId, ref);
      if (resp.job && resp.job.status === 'ready') {
        await loadRepoDetail();
      } else {
        state.pendingRef = ref;
        updateStatusChip();
        renderMessage(`Parsing the history of “${ref}” in the background — the dashboard will refresh when it is ready.`, 'info');
        startPolling();
      }
    }
  } catch (err) {
    errorToast(err);
    state.ref = state.detail ? (state.detail.ref || state.detail.defaultRef) : state.ref;
    populateRefs();
  } finally {
    $('refSelect').disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* Polling                                                             */
/* ------------------------------------------------------------------ */
function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(pollTick, 1200);
  pollTick();
}

function maybeStopPolling() {
  const active = state.repos.some((r) => ACTIVE_STATUSES.includes(r.status));
  if (!active && !state.pendingRef && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    updateStatusChip();
  }
}

async function pollTick() {
  let repos;
  try {
    repos = (await api.repos()).repos || [];
  } catch {
    return;
  }
  const prev = state.prevStatuses;
  state.repos = repos;
  state.prevStatuses = new Map(repos.map((r) => [r.id, r.status]));
  renderRepoSelect();
  updateStatusChip();
  if (state.view === 'overview') renderOverviewCards();

  const cur = repos.find((r) => r.id === state.repoId);
  if (cur) {
    const was = prev.get(cur.id);
    if (was && was !== cur.status) {
      if (cur.status === 'ready') {
        if (!state.detail) await selectRepo(cur.id);
      } else if (cur.status === 'error') {
        renderMessage(`Repository failed: ${cur.error || 'unknown error'}`, 'error');
        toast(`Repository failed: ${cur.error || 'unknown error'}`, 'error');
      } else {
        renderPreparing(cur);
      }
    }
    if (state.pendingRef) {
      const job = (cur.refJobs || {})[state.pendingRef] || null;
      if (job && job.status === 'ready') {
        state.pendingRef = null;
        await loadRepoDetail();
      } else if (job && job.status === 'error') {
        const failedRef = state.pendingRef;
        state.pendingRef = null;
        toast(`Failed to parse “${failedRef}”: ${job.error || 'error'}`, 'error');
        renderMessage(`Failed to parse “${failedRef}”: ${job.error || 'error'}`, 'error');
      }
    }
  }
  maybeStopPolling();
}

/* ------------------------------------------------------------------ */
/* Authors                                                             */
/* ------------------------------------------------------------------ */
async function loadAuthors() {
  try {
    state.authorsPayload = await api.authors(state.repoId, state.ref);
  } catch (err) {
    state.authorsPayload = null;
    toast(`Author identities unavailable: ${err.message}`, 'error');
  }
  renderAuthorList();
  updateAuthorHint();
}

function renderAuthorList() {
  const host = $('authorList');
  if (!state.authorsPayload) {
    host.innerHTML = '<div class="ml-empty">Loading authors…</div>';
    return;
  }
  host.innerHTML = views.authorListHTML(
    state.authorsPayload, state.filters.authorIds, $('authorSearch').value.trim());
}

function updateAuthorHint() {
  $('authorHint').textContent = state.authorsPayload
    ? views.authorHintText(state.authorsPayload, state.filters.authorIds)
    : '';
}

function toggleAuthor(aid) {
  const f = state.filters;
  const i = f.authorIds.indexOf(aid);
  if (i >= 0) f.authorIds.splice(i, 1);
  else f.authorIds.push(aid);
  renderAuthorList();
  updateAuthorHint();
  runQuerySoon();
}

/* ------------------------------------------------------------------ */
/* Path scope                                                          */
/* ------------------------------------------------------------------ */
async function loadPathResults(q) {
  if (!state.repoId || !state.detail) return;
  try {
    const payload = await api.paths(state.repoId, { ref: state.ref, q, limit: 60 });
    $('pathResults').innerHTML = views.pathResultsHTML(payload);
  } catch {
    /* scope search is best-effort */
  }
}

function setScope(path, type) {
  state.filters.path = path;
  state.filters.pathType = type;
  updatePathChip();
  $('pathResults').innerHTML = '';
  $('pathSearch').value = '';
  runQuery();
}

function clearScope() {
  state.filters.path = null;
  state.filters.pathType = null;
  updatePathChip();
  $('pathResults').innerHTML = '';
  $('pathSearch').value = '';
  runQuery();
}

function updatePathChip() {
  const f = state.filters;
  const chip = $('pathChip');
  if (!f.path) {
    chip.hidden = true;
    chip.innerHTML = '';
    $('scopeUpBtn').hidden = true;
    return;
  }
  chip.innerHTML = `<span class="chip-kind">${f.pathType === 'dir' ? 'dir' : 'file'}</span>` +
    `<span class="chip-path" title="${fmt.esc(f.path)}">${fmt.esc(f.path)}</span>` +
    `<span class="chip-x" title="Clear the scope">×</span>`;
  chip.hidden = false;
  $('scopeUpBtn').hidden = false;
}

function onPickObject(item) {
  setScope(item.path, item.type);
}

/* ------------------------------------------------------------------ */
/* Commit selection filters                                            */
/* ------------------------------------------------------------------ */
function setCommitMode(mode, { run = true } = {}) {
  state.filters.mode = mode;
  [...$('commitModeSeg').children].forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === mode));
  $('rangePane').hidden = mode !== 'range';
  $('listPane').hidden = mode !== 'list';
  updateSelectedInfo();
  if (run) runQuery();
}

function updateSelectedInfo() {
  const n = state.filters.shas.length;
  $('selectedCommitsInfo').textContent = n
    ? `${fmt.int(n)} commit${n === 1 ? '' : 's'} selected`
    : 'No commits selected';
}

function applyPreset(p) {
  const f = state.filters;
  if (p === 'clear') {
    f.from = null;
    f.to = null;
  } else {
    const days = { '90d': 90, '1y': 365, '3y': 1095 }[p] || 0;
    f.from = Math.floor(Date.now() / 1000) - days * 86400;
    f.to = null;
  }
  $('fromInput').value = fmt.toLocalInput(f.from);
  $('toInput').value = fmt.toLocalInput(f.to);
  if (f.mode !== 'range') setCommitMode('range', { run: false });
  runQuery();
}

function resetFiltersState() {
  const f = state.filters;
  f.mode = 'all';
  f.from = null;
  f.to = null;
  f.shas = [];
  f.authorIds = [];
  f.path = null;
  f.pathType = null;
  $('fromInput').value = '';
  $('toInput').value = '';
  setCommitMode('all', { run: false });
  $('authorSearch').value = '';
  renderAuthorList();
  updateAuthorHint();
  updatePathChip();
  $('pathResults').innerHTML = '';
  $('pathSearch').value = '';
  $('tableSearch').value = '';
  state.table.query = '';
  state.table.kind = 'files';
  [...$('tableTabs').children].forEach((b) =>
    b.classList.toggle('active', b.dataset.table === 'files'));
}

function resetFilters() {
  resetFiltersState();
  runQuery();
}

/* ------------------------------------------------------------------ */
/* Query + dashboard rendering                                         */
/* ------------------------------------------------------------------ */
function buildQueryBody() {
  const f = state.filters;
  return {
    ref: state.ref,
    commits: {
      mode: f.mode,
      from: f.mode === 'range' ? f.from : null,
      to: f.mode === 'range' ? f.to : null,
      shas: f.mode === 'list' ? f.shas : [],
    },
    authors: f.authorIds,
    path: f.path || null,
    pathType: f.pathType || null,
  };
}

async function runQuery() {
  if (!state.repoId || !state.detail || !state.detail.headSha) return;
  const meta = state.repos.find((r) => r.id === state.repoId);
  if (meta && meta.status !== 'ready') return;
  const seq = ++state.querySeq;
  showLoading(true);
  try {
    const res = await api.metrics(state.repoId, buildQueryBody());
    if (seq !== state.querySeq) return;
    state.result = res;
    renderDashboard();
  } catch (err) {
    if (seq === state.querySeq) errorToast(err);
  } finally {
    if (seq === state.querySeq) showLoading(false);
  }
}

const runQuerySoon = debounce(runQuery, 160);

function renderMessage(msg, kind) {
  $('emptyState').hidden = true;
  $('dashboardBody').hidden = false;
  setBanner(msg, kind);
  $('cards').innerHTML = '';
  for (const id of ['activityChart', 'topObjectsChart', 'ownershipDonut']) {
    const node = $(id);
    if (node) {
      forget(node);
      node.innerHTML = '';
    }
  }
  $('topObjectsChart').closest('.panel').hidden = false;
  $('drillPanel').hidden = true;
  $('tableWrap').innerHTML = '';
  $('tableMeta').textContent = '';
  $('elapsedBadge').hidden = true;
}

function renderPreparing(meta) {
  const pct = meta.progress != null ? ` ${Math.round(meta.progress)}%` : '';
  renderMessage(`${meta.message || meta.status}${pct}…`, 'info');
  $('scopeTitle').innerHTML = `<span class="scope-kind">repository</span>${fmt.esc(meta.name)}`;
  $('scopeMeta').textContent = meta.sourceType === 'zip' ? `zip: ${meta.source}` : (meta.source || '');
}

function updateWarn() {
  const f = state.filters;
  const sel = state.result ? state.result.selection : null;
  let msg = '';
  if (f.mode === 'range' && f.from != null && f.to != null && f.from >= f.to) {
    msg = 'The “From” time is not before “To” — no commit can match this range.';
  } else if (f.mode === 'list' && !f.shas.length) {
    msg = 'No commits selected — all metrics are zero. Use “Select commits…” to build the commit set.';
  } else if (sel && sel.commits === 0) {
    msg = 'No commits match the current filters — all metrics are zero.';
  }
  setBanner(msg, 'warn');
}

function renderDashboard() {
  const detail = state.detail;
  const r = state.result;
  if (!detail || !r) return;
  $('emptyState').hidden = true;
  $('dashboardBody').hidden = false;

  const sel = r.selection;
  const obj = r.object || { type: 'repo' };

  let kindLabel = 'repository';
  let name = detail.name || state.repoId;
  if (obj.type === 'file') { kindLabel = 'file'; name = obj.path; }
  else if (obj.type === 'dir') { kindLabel = 'directory'; name = obj.path; }
  $('scopeTitle').innerHTML = `<span class="scope-kind">${kindLabel}</span>${fmt.esc(name)}`;
  const first = sel.firstCommit ? sel.firstCommit.date : null;
  const last = sel.lastCommit ? sel.lastCommit.date : null;
  $('scopeMeta').textContent =
    `${fmt.int(sel.commits)} of ${fmt.int(sel.totalCommits)} commits · ref ${sel.ref}` +
    (first ? ` · ${fmt.date(first)} → ${fmt.date(last)}` : '') +
    (sel.authorFilter ? ` · ${sel.authorFilter} author filter(s)` : '');
  $('scopeUpBtn').hidden = !state.filters.path;
  if (r.elapsedMs != null) {
    $('elapsedBadge').textContent = `${r.elapsedMs} ms`;
    $('elapsedBadge').hidden = false;
  } else {
    $('elapsedBadge').hidden = true;
  }

  updateWarn();
  $('cards').innerHTML = views.metricCards(r);

  renderActivity($('activityChart'), r.series);

  const barPanel = $('topObjectsChart').closest('.panel');
  if (obj.type === 'file') {
    barPanel.hidden = true;
  } else {
    barPanel.hidden = false;
    if (obj.type === 'dir') {
      $('topObjectsTitle').textContent = 'Top files in scope by churn';
      const items = (r.files || []).slice(0, 8).map((f) => ({
        label: f.path, value: f.churn, item: { path: f.path, type: 'file' },
      }));
      renderBars($('topObjectsChart'), items, onPickObject);
    } else {
      $('topObjectsTitle').textContent = 'Top directories by churn';
      const items = (r.directories || []).slice(0, 8).map((d) => ({
        label: d.path, value: d.churn, item: { path: d.path, type: 'dir' },
      }));
      renderBars($('topObjectsChart'), items, onPickObject);
    }
  }

  const f = state.filters;
  if (f.path && r.authors && r.authors.length) {
    $('drillPanel').hidden = false;
    $('drillScopeName').textContent = f.path;
    const top = r.authors.slice(0, 8).map((a, i) => ({
      name: a.name, value: a.churn, color: PALETTE[i % PALETTE.length],
    }));
    const rest = r.authors.slice(8).reduce((acc, a) => acc + a.churn, 0);
    if (rest > 0) top.push({ name: `Other (${r.authors.length - 8} authors)`, value: rest, color: OTHER_COLOR });
    renderDonut($('ownershipDonut'), top, fmt.compact(r.metrics.churn), 'total churn');
    $('drillAuthorsTable').innerHTML = views.drillAuthorsHTML(r.authors);
  } else {
    $('drillPanel').hidden = true;
  }

  renderTables();
}

/* ------------------------------------------------------------------ */
/* Tables                                                              */
/* ------------------------------------------------------------------ */
function currentTableRows(kind) {
  const r = state.result;
  if (!r) return [];
  let rows;
  if (kind === 'files') rows = r.files || [];
  else if (kind === 'dirs') rows = r.directories || [];
  else rows = r.authors || [];
  const q = state.table.query.trim().toLowerCase();
  if (q) {
    rows = rows.filter((row) => (kind === 'authors'
      ? row.name.toLowerCase().includes(q) || row.email.toLowerCase().includes(q)
      : row.path.toLowerCase().includes(q)));
  }
  const { col, dir } = state.table.sort[kind];
  const copy = rows.slice();
  copy.sort((a, b) => {
    const va = a[col];
    const vb = b[col];
    if (typeof va === 'string' || typeof vb === 'string') {
      return dir * String(va).localeCompare(String(vb));
    }
    return dir * ((Number(va) || 0) - (Number(vb) || 0));
  });
  return copy;
}

function renderTables() {
  const kind = state.table.kind;
  const rows = currentTableRows(kind);
  $('tableWrap').innerHTML = views.tableHTML(kind, rows, state.table.sort[kind], state.table.query.trim());
  $('tableMeta').textContent = views.tableMetaText(kind, state.result, state.table.query.trim(), rows.length);
}

function exportCsv() {
  const kind = state.table.kind;
  const rows = currentTableRows(kind);
  if (!rows.length) {
    toast('Nothing to export for the current table.', 'error');
    return;
  }
  const cols = views.COLS[kind];
  const csvEsc = (v) => {
    const s = String(v ?? '');
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [cols.map((c) => csvEsc(c.label)).join(',')];
  for (const r of rows) {
    lines.push(cols.map((c) => {
      if (kind === 'authors' && c.key === 'name') return csvEsc(`${r.name} <${r.email}>`);
      let v = r[c.key];
      if (typeof v === 'number' && !Number.isInteger(v)) v = v.toFixed(6);
      return csvEsc(v);
    }).join(','));
  }
  download(`${state.repoId}-${kind}.csv`, lines.join('\r\n'));
  toast(`Exported ${fmt.int(rows.length)} rows to CSV.`, 'success');
}

/* ------------------------------------------------------------------ */
/* Commit picker modal                                                 */
/* ------------------------------------------------------------------ */
const picker = { shas: new Set(), q: '', authorId: '', offset: 0, limit: 100, total: 0, rows: [] };

function openCommitPicker() {
  if (!state.detail) return;
  picker.shas = new Set(state.filters.shas);
  picker.q = '';
  picker.authorId = '';
  picker.offset = 0;
  const authorOpts = state.authorsPayload
    ? state.authorsPayload.authors.map((a) =>
      `<option value="${a.id}">${fmt.esc(a.name)} (${fmt.int(a.commits)})</option>`).join('')
    : '';
  openModal(`
    <div class="modal-head"><h3>Select commits</h3><div class="spacer"></div><button class="modal-close" data-close>×</button></div>
    <div class="modal-body">
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input id="pkSearch" class="input input-sm" placeholder="Search message, hash or author…" style="flex:1">
        <select id="pkAuthor" class="select input-sm"><option value="">All authors</option>${authorOpts}</select>
      </div>
      <div class="hint" id="pkInfo" style="margin-bottom:6px"></div>
      <div id="pkList" style="border:1px solid var(--border-soft);border-radius:7px;max-height:44vh;overflow:auto"></div>
      <div style="display:flex;align-items:center;gap:8px;margin-top:10px">
        <button class="btn btn-sm" id="pkPrev">‹ Prev</button>
        <button class="btn btn-sm" id="pkNext">Next ›</button>
        <span class="hint" id="pkPage"></span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="pkAll">Select all matching</button>
        <button class="btn btn-sm" id="pkClear">Clear</button>
      </div>
    </div>
    <div class="modal-foot">
      <span class="hint" id="pkCount" style="margin-right:auto"></span>
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-primary" id="pkApply">Apply</button>
    </div>`);
  $('pkSearch').addEventListener('input', debounce(() => {
    picker.q = $('pkSearch').value.trim();
    picker.offset = 0;
    loadPickerPage();
  }, 250));
  $('pkAuthor').addEventListener('change', () => {
    picker.authorId = $('pkAuthor').value;
    picker.offset = 0;
    loadPickerPage();
  });
  $('pkPrev').addEventListener('click', () => {
    picker.offset = Math.max(0, picker.offset - picker.limit);
    loadPickerPage();
  });
  $('pkNext').addEventListener('click', () => {
    if (picker.offset + picker.limit < picker.total) {
      picker.offset += picker.limit;
      loadPickerPage();
    }
  });
  $('pkAll').addEventListener('click', selectAllMatching);
  $('pkClear').addEventListener('click', () => {
    picker.shas.clear();
    renderPickerRows();
  });
  $('pkApply').addEventListener('click', applyPicker);
  $('pkList').addEventListener('click', (e) => {
    const row = e.target.closest('.commit-row');
    if (row && row.dataset.sha) togglePickerSha(row.dataset.sha);
  });
  loadPickerPage();
}

async function loadPickerPage() {
  try {
    const resp = await api.commits(state.repoId, {
      ref: state.ref, offset: picker.offset, limit: picker.limit,
      q: picker.q, authors: picker.authorId,
    });
    picker.total = resp.total;
    picker.rows = resp.commits || [];
    renderPickerRows();
    if (!$('pkInfo')) return;
    const lo = resp.total ? picker.offset + 1 : 0;
    const hi = Math.min(picker.offset + picker.limit, resp.total);
    $('pkInfo').textContent = `${fmt.int(resp.total)} non-merge commits reachable from ${state.ref}`;
    $('pkPage').textContent = `${fmt.int(lo)}–${fmt.int(hi)} of ${fmt.int(resp.total)}`;
    $('pkPrev').disabled = picker.offset === 0;
    $('pkNext').disabled = picker.offset + picker.limit >= picker.total;
  } catch (err) {
    errorToast(err);
  }
}

function renderPickerRows() {
  const list = $('pkList');
  if (!list) return;
  list.innerHTML = picker.rows.map((c) => `
    <div class="commit-row${picker.shas.has(c.sha) ? ' selected' : ''}" data-sha="${c.sha}">
      <input type="checkbox" ${picker.shas.has(c.sha) ? 'checked' : ''}>
      <span class="commit-sha">${shortSha(c.sha)}</span>
      <span class="commit-subject" title="${fmt.esc(c.subject)}">${fmt.esc(c.subject)}</span>
      <span class="commit-meta">${fmt.esc(c.authorName)} · ${fmt.date(c.time)}</span>
    </div>`).join('') || '<div class="ml-empty">No commits match.</div>';
  updatePickerCount();
}

function updatePickerCount() {
  const count = $('pkCount');
  if (!count) return;
  count.textContent = `${fmt.int(picker.shas.size)} selected`;
  $('pkApply').textContent = `Apply (${fmt.int(picker.shas.size)})`;
}

function togglePickerSha(sha) {
  if (picker.shas.has(sha)) picker.shas.delete(sha);
  else picker.shas.add(sha);
  renderPickerRows();
}

async function selectAllMatching() {
  const cap = 20000;
  const total = Math.min(picker.total, cap);
  let offset = 0;
  let added = 0;
  $('pkAll').disabled = true;
  try {
    while (offset < total) {
      const resp = await api.commits(state.repoId, {
        ref: state.ref, offset, limit: 500, q: picker.q, authors: picker.authorId,
      });
      const rows = resp.commits || [];
      for (const c of rows) {
        if (!picker.shas.has(c.sha)) { picker.shas.add(c.sha); added++; }
      }
      if (!rows.length) break;
      offset += rows.length;
    }
    toast(`${fmt.int(added)} commits added to the selection.`, 'success');
    if (picker.total > cap) toast(`Selection capped at ${fmt.int(cap)} commits (API limit).`, 'error');
  } catch (err) {
    errorToast(err);
  } finally {
    const btn = $('pkAll');
    if (btn) btn.disabled = false;
    renderPickerRows();
  }
}

function applyPicker() {
  state.filters.shas = [...picker.shas];
  setCommitMode('list', { run: false });
  updateSelectedInfo();
  closeModal();
  runQuery();
}

/* ------------------------------------------------------------------ */
/* Merge authors modal                                                 */
/* ------------------------------------------------------------------ */
function openMergeModal() {
  const payload = state.authorsPayload;
  if (!payload) {
    toast('Author list is not loaded yet.', 'error');
    return;
  }
  openModal(`
    <div class="modal-head"><h3>Merge author identities</h3><div class="spacer"></div><button class="modal-close" data-close>×</button></div>
    <div class="modal-body">
      <p class="hint" style="margin-top:0">Select two or more identities that belong to the same developer. This is applied on top of
      <code>.mailmap</code> rewriting and persists for this repository + reference. Commit counts, churn and ownership are recomputed immediately.</p>
      <input id="mgSearch" class="input input-sm" placeholder="Search authors…" style="width:100%;margin-bottom:8px">
      <div id="mgList" class="multi-list" style="max-height:46vh"></div>
      ${payload.merges.length ? `<div class="hint" style="margin-top:8px">${payload.merges.length} manual merge group(s) currently active.</div>` : ''}
    </div>
    <div class="modal-foot">
      <button class="btn btn-sm" id="mgReset" ${payload.merges.length ? '' : 'disabled'}>Clear all manual merges</button>
      <div class="spacer"></div>
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-primary" id="mgApply">Merge selected</button>
    </div>`);
  renderMergeRows('');
  $('mgSearch').addEventListener('input', debounce(() => renderMergeRows($('mgSearch').value.trim()), 150));
  $('mgApply').addEventListener('click', applyMerge);
  if (payload.merges.length) $('mgReset').addEventListener('click', resetMerges);
}

function renderMergeRows(q) {
  const payload = state.authorsPayload;
  const list = $('mgList');
  if (!list || !payload) return;
  const ql = q.toLowerCase();
  const rows = payload.authors
    .map((a, i) => ({ a, i }))
    .filter(({ a }) => !ql || a.name.toLowerCase().includes(ql) || a.email.toLowerCase().includes(ql));
  list.innerHTML = rows.map(({ a, i }) => `
    <label class="ml-row" data-i="${i}">
      <input type="checkbox">
      <span class="ml-main">
        <span class="ml-name">${fmt.esc(a.name)}</span>
        <span class="ml-sub">${fmt.esc(a.email)} · ${fmt.int(a.commits)} commits · ${a.keys.length} identit${a.keys.length === 1 ? 'y' : 'ies'}</span>
      </span>
      ${a.mailmapped ? '<span class="ml-badge badge-mailmap">mailmap</span>' : ''}${a.merged ? '<span class="ml-badge badge-merged">merged</span>' : ''}
    </label>`).join('') || '<div class="ml-empty">No authors match.</div>';
}

async function applyMerge() {
  const payload = state.authorsPayload;
  const list = $('mgList');
  if (!list || !payload) return;
  const checked = [...list.querySelectorAll('input:checked')];
  if (checked.length < 2) {
    toast('Select at least two identities to merge.', 'error');
    return;
  }
  const selected = checked.map((input) => payload.authors[Number(input.closest('.ml-row').dataset.i)]);
  const union = [...new Set(selected.flatMap((a) => a.keys))];
  const existing = (payload.merges || []).filter((g) => !g.some((k) => union.includes(k)));
  const groups = [...existing, union];
  $('mgApply').disabled = true;
  try {
    const resp = await api.mergeAuthors(state.repoId, state.ref, groups);
    state.authorsPayload = resp;
    state.filters.authorIds = [];
    renderAuthorList();
    updateAuthorHint();
    await runQuery();
    toast(`Merged ${selected.length} identities.`, 'success');
    closeModal();
  } catch (err) {
    errorToast(err);
    const btn = $('mgApply');
    if (btn) btn.disabled = false;
  }
}

async function resetMerges() {
  try {
    const resp = await api.mergeAuthors(state.repoId, state.ref, []);
    state.authorsPayload = resp;
    state.filters.authorIds = [];
    renderAuthorList();
    updateAuthorHint();
    await runQuery();
    toast('Manual merges cleared.', 'success');
    closeModal();
  } catch (err) {
    errorToast(err);
  }
}

/* ------------------------------------------------------------------ */
/* Add repository modal                                                */
/* ------------------------------------------------------------------ */
function openAddRepoModal() {
  openModal(`
    <div class="modal-head"><h3>Add repository</h3><div class="spacer"></div><button class="modal-close" data-close>×</button></div>
    <div class="modal-body">
      <div class="tabrow" id="addTabs">
        <button data-addtab="clone" class="active">Clone from URL</button>
        <button data-addtab="zip">Upload zip archive</button>
      </div>
      <div id="addClonePane">
        <label class="field">Repository URL
          <input id="cloneUrl" class="input" placeholder="https://github.com/user/project.git" spellcheck="false"></label>
        <label class="field">Display name (optional)
          <input id="cloneName" class="input" placeholder="derived from the URL"></label>
        <p class="hint">The repository is deep-cloned (mirror) in the background. Progress is shown in the top bar and the Repositories view.</p>
      </div>
      <div id="addZipPane" hidden>
        <label class="field">Zip archive of the repository (must contain its <code>.git</code> directory)
          <input type="file" id="zipFile" class="input" accept=".zip,application/zip"></label>
        <label class="field">Display name (optional)
          <input id="zipName" class="input"></label>
        <div class="progress-track" id="zipProgress" hidden style="margin-bottom:8px"><span></span></div>
        <p class="hint" id="zipHint"></p>
      </div>
      <div class="banner error" id="addErr" hidden></div>
    </div>
    <div class="modal-foot">
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-primary" id="addSubmit">Add repository</button>
    </div>`);
  if (state.config) $('zipHint').textContent = `Maximum archive size: ${fmt.bytes(state.config.maxUploadBytes)}.`;
  $('addTabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-addtab]');
    if (!b) return;
    const tab = b.dataset.addtab;
    [...$('addTabs').children].forEach((x) => x.classList.toggle('active', x === b));
    $('addClonePane').hidden = tab !== 'clone';
    $('addZipPane').hidden = tab !== 'zip';
    $('addErr').hidden = true;
  });
  $('addSubmit').addEventListener('click', submitAddRepo);
  setTimeout(() => $('cloneUrl').focus(), 40);
}

async function submitAddRepo() {
  const err = $('addErr');
  err.hidden = true;
  const zipTab = !$('addZipPane').hidden;
  $('addSubmit').disabled = true;
  try {
    let resp;
    if (zipTab) {
      const file = $('zipFile').files[0];
      if (!file) throw new ApiError('Choose a .zip archive first.');
      const prog = $('zipProgress');
      prog.hidden = false;
      prog.firstElementChild.style.width = '0%';
      resp = await uploadZip(file, $('zipName').value.trim(), (p) => {
        prog.firstElementChild.style.width = `${Math.round(p * 100)}%`;
      });
    } else {
      const url = $('cloneUrl').value.trim();
      if (!url) throw new ApiError('Enter a repository URL.');
      resp = await api.clone(url, $('cloneName').value.trim() || null);
    }
    closeModal();
    toast(`Repository “${resp.repo.name}” added — ingestion is running in the background.`, 'success');
    await refreshRepos();
    await selectRepo(resp.repo.id);
    startPolling();
  } catch (e) {
    if ($('addErr')) {
      $('addErr').textContent = e.message;
      $('addErr').hidden = false;
    }
    const btn = $('addSubmit');
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* Delete + overview                                                   */
/* ------------------------------------------------------------------ */
function confirmDeleteRepo(id) {
  const meta = state.repos.find((r) => r.id === id);
  if (!meta) return;
  openModal(`
    <div class="modal-head"><h3>Delete repository</h3><div class="spacer"></div><button class="modal-close" data-close>×</button></div>
    <div class="modal-body">
      <p>Delete <b>${fmt.esc(meta.name)}</b>?</p>
      <p class="hint" style="font-family:var(--mono)">${fmt.esc(meta.source || '')}</p>
      <p class="hint">This removes the clone/extraction and every parsed cache from the server. It cannot be undone.</p>
    </div>
    <div class="modal-foot">
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-danger" id="delConfirm">Delete</button>
    </div>`);
  $('delConfirm').addEventListener('click', async () => {
    try {
      await api.deleteRepo(id);
      closeModal();
      toast(`Deleted “${meta.name}”.`, 'success');
      if (state.repoId === id) {
        state.repoId = null;
        state.detail = null;
        state.result = null;
        localStorage.removeItem('rat.lastRepo');
      }
      await refreshRepos();
      const next = state.repos.find((r) => r.status === 'ready') || state.repos[0];
      if (next) await selectRepo(next.id);
      else showNoRepo();
      if (state.view === 'overview') renderOverviewCards();
    } catch (err) {
      errorToast(err);
    }
  });
}

async function activateOverview() {
  renderOverviewCards();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 9000);
  try {
    const data = await api.overview(ctl.signal);
    state.overviewRows = new Map((data.repos || []).map((row) => [row.repo.id, row]));
  } catch {
    /* totals are a nice-to-have; registry stats are still displayed */
  } finally {
    clearTimeout(timer);
  }
  renderOverviewCards();
}

function renderOverviewCards() {
  const host = $('repoCards');
  if (!state.repos.length) {
    host.innerHTML = `<div class="empty-state">
      <div class="empty-icon">◧</div><h2>No repositories yet</h2>
      <p>Add one from a zip archive or clone it from a URL.</p>
      <button class="btn btn-primary" id="ovEmptyAdd">Add repository</button></div>`;
    const btn = $('ovEmptyAdd');
    if (btn) btn.addEventListener('click', openAddRepoModal);
    return;
  }
  host.innerHTML = state.repos.map((meta) => {
    const row = state.overviewRows ? state.overviewRows.get(meta.id) : null;
    return views.repoCardHTML(row || { repo: meta }, state.repoId);
  }).join('');
}

/* ------------------------------------------------------------------ */
/* Wiring + boot                                                       */
/* ------------------------------------------------------------------ */
function wire() {
  document.querySelectorAll('.tab[data-view]').forEach((t) =>
    t.addEventListener('click', () => setView(t.dataset.view)));
  $('helpBtn').addEventListener('click', () => openModal(`
    <div class="modal-head"><h3>Metric reference</h3><div class="spacer"></div><button class="modal-close" data-close>×</button></div>
    <div class="modal-body">${views.helpHTML()}</div>`));
  $('modalRoot').addEventListener('click', (e) => {
    if (e.target === $('modalRoot') || e.target.closest('[data-close]')) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('modalRoot').hidden) closeModal();
  });

  $('repoSelect').addEventListener('change', () => {
    const id = $('repoSelect').value;
    if (id && id !== state.repoId) selectRepo(id);
  });
  for (const id of ['addRepoBtn', 'addRepoBtn2', 'emptyAddBtn']) {
    $(id).addEventListener('click', openAddRepoModal);
  }
  $('refSelect').addEventListener('change', () => selectRef($('refSelect').value));

  $('commitModeSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mode]');
    if (b) setCommitMode(b.dataset.mode);
  });
  $('fromInput').addEventListener('change', () => {
    state.filters.from = fmt.fromLocalInput($('fromInput').value);
    runQuery();
  });
  $('toInput').addEventListener('change', () => {
    state.filters.to = fmt.fromLocalInput($('toInput').value);
    runQuery();
  });
  document.querySelectorAll('.presets button').forEach((b) =>
    b.addEventListener('click', () => applyPreset(b.dataset.preset)));
  $('pickCommitsBtn').addEventListener('click', openCommitPicker);

  $('authorSearch').addEventListener('input', debounce(renderAuthorList, 120));
  $('authorList').addEventListener('change', (e) => {
    const row = e.target.closest('.ml-row');
    if (row && row.dataset.aid) toggleAuthor(Number(row.dataset.aid));
  });
  $('mergeAuthorsBtn').addEventListener('click', openMergeModal);

  $('pathSearch').addEventListener('input', debounce(() => {
    loadPathResults($('pathSearch').value.trim());
  }, 200));
  $('pathSearch').addEventListener('focus', () => {
    if (!$('pathResults').innerHTML) loadPathResults($('pathSearch').value.trim());
  });
  $('pathResults').addEventListener('click', (e) => {
    const row = e.target.closest('.ml-row');
    if (!row) return;
    const t = row.dataset.ptype;
    if (t === 'repo') clearScope();
    else if (t && row.dataset.path) setScope(row.dataset.path, t);
  });
  $('pathChip').addEventListener('click', (e) => {
    if (e.target.closest('.chip-x')) clearScope();
  });
  $('scopeUpBtn').addEventListener('click', clearScope);
  $('resetFiltersBtn').addEventListener('click', resetFilters);

  $('tableTabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-table]');
    if (!b) return;
    state.table.kind = b.dataset.table;
    [...$('tableTabs').children].forEach((x) => x.classList.toggle('active', x === b));
    renderTables();
  });
  $('tableSearch').addEventListener('input', debounce(() => {
    state.table.query = $('tableSearch').value;
    renderTables();
  }, 150));
  $('tableWrap').addEventListener('click', (e) => {
    const th = e.target.closest('th[data-col]');
    if (th) {
      const kind = state.table.kind;
      const s = state.table.sort[kind];
      if (s.col === th.dataset.col) {
        s.dir = -s.dir;
      } else {
        s.col = th.dataset.col;
        s.dir = (th.dataset.col === 'path' || th.dataset.col === 'name') ? 1 : -1;
      }
      renderTables();
      return;
    }
    const tr = e.target.closest('tr.clickable');
    if (tr && tr.dataset.path) {
      setScope(tr.dataset.path, tr.dataset.ptype);
      return;
    }
    const atr = e.target.closest('tr[data-aid]');
    if (atr) toggleAuthor(Number(atr.dataset.aid));
  });
  $('exportCsvBtn').addEventListener('click', exportCsv);

  $('repoCards').addEventListener('click', (e) => {
    const card = e.target.closest('.repo-card');
    const act = e.target.closest('button[data-act]');
    if (!card || !act) return;
    const id = card.dataset.id;
    if (act.dataset.act === 'open') {
      setView('dashboard');
      selectRepo(id);
    } else {
      confirmDeleteRepo(id);
    }
  });
}

async function boot() {
  wire();
  try {
    state.config = await api.config();
  } catch {
    /* config is optional */
  }
  try {
    await refreshRepos();
  } catch (err) {
    toast(`Cannot reach the RAT server: ${err.message}`, 'error');
    showNoRepo();
    return;
  }
  const last = localStorage.getItem('rat.lastRepo');
  const pick = state.repos.find((r) => r.id === last)
    || state.repos.find((r) => r.status === 'ready')
    || state.repos[0];
  if (pick) await selectRepo(pick.id);
  else showNoRepo();
  if (state.repos.some((r) => ACTIVE_STATUSES.includes(r.status))) startPolling();
}

boot();
