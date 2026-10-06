/* RAT — plain HTML renderers (no framework). */
import { fmt, shortSha } from './api.js';

/* ------------------------------------------------------------------ */
/* Column definitions                                                  */
/* ------------------------------------------------------------------ */
export const COLS = {
  files: [
    { key: 'path', label: 'File' },
    { key: 'added', label: 'Added', num: true },
    { key: 'removed', label: 'Removed', num: true },
    { key: 'growth', label: 'Growth', num: true },
    { key: 'churn', label: 'Churn', num: true },
    { key: 'modifications', label: 'Mods', num: true, title: 'Commits in the selection that touched this file' },
    { key: 'modificationFrequency', label: 'Mod freq', num: true, title: 'Modifications / |S|' },
    { key: 'churnRate', label: 'Churn rate', num: true, title: 'Churn / |S|' },
  ],
  dirs: [
    { key: 'path', label: 'Directory' },
    { key: 'added', label: 'Added', num: true },
    { key: 'removed', label: 'Removed', num: true },
    { key: 'growth', label: 'Growth', num: true },
    { key: 'churn', label: 'Churn', num: true },
    { key: 'modifications', label: 'Mods', num: true, title: 'Commits in the selection that touched this directory' },
    { key: 'modificationFrequency', label: 'Mod freq', num: true, title: 'Modifications / |S|' },
    { key: 'churnRate', label: 'Churn rate', num: true, title: 'Churn / |S|' },
  ],
  authors: [
    { key: 'name', label: 'Author' },
    { key: 'commits', label: 'Commits', num: true },
    { key: 'added', label: 'Added', num: true },
    { key: 'removed', label: 'Removed', num: true },
    { key: 'growth', label: 'Growth', num: true },
    { key: 'churn', label: 'Churn', num: true },
    { key: 'modifications', label: 'Mods', num: true, title: 'Commits by this author that touched the scope' },
    { key: 'ownership', label: 'Ownership', num: true, title: 'Author churn / total churn (0 when total churn is 0)' },
  ],
};

/* ------------------------------------------------------------------ */
/* Summary cards                                                       */
/* ------------------------------------------------------------------ */
export function metricCards(r) {
  const m = r.metrics;
  const sel = r.selection;
  const modsSub = sel.commits ? `${fmt.int(m.modifications)} of ${fmt.int(sel.commits)} commits in |S|` : 'no commits selected';
  return [
    card('c-added', 'Added lines', fmt.int(m.added), 'lines inserted'),
    card('c-removed', 'Removed lines', fmt.int(m.removed), 'lines deleted'),
    card('c-growth', 'Growth', fmt.signed(m.growth), 'added − removed'),
    card('c-churn', 'Churn', fmt.int(m.churn), 'added + removed'),
    card('c-mods', 'Modifications', fmt.int(m.modifications), modsSub),
    card('c-freq', 'Modification freq.', sel.commits ? fmt.pct(m.modificationFrequency) : '—', 'modifications / |S|'),
    card('c-rate', 'Churn rate', sel.commits ? fmt.rate(m.churnRate) : '—', 'churn / |S| · lines per commit'),
    card('c-commits', 'Commits |S|', fmt.int(sel.commits), `of ${fmt.int(sel.totalCommits)} non-merge commits`),
  ].join('');
}

function card(cls, label, value, sub) {
  return `<div class="card ${cls}">
    <div class="card-label">${fmt.esc(label)}</div>
    <div class="card-value">${fmt.esc(value)}</div>
    <div class="card-sub">${fmt.esc(sub)}</div>
  </div>`;
}

/* ------------------------------------------------------------------ */
/* Tables                                                              */
/* ------------------------------------------------------------------ */
export function tableHTML(kind, rows, sort, query) {
  const cols = COLS[kind];
  const head = cols.map((c) => {
    const sorted = sort.col === c.key;
    const cls = sorted ? `sorted${sort.dir === 1 ? ' asc' : ''}` : '';
    const title = c.title ? ` title="${fmt.esc(c.title)}"` : '';
    return `<th data-col="${c.key}" class="${cls}"${title}>${fmt.esc(c.label)}</th>`;
  }).join('');

  let body;
  if (!rows.length) {
    const msg = query
      ? `No rows match “${fmt.esc(query)}”.`
      : 'No objects with activity in the current selection.';
    body = `<tr><td colspan="${cols.length}" class="empty-row">${msg}</td></tr>`;
  } else {
    body = rows.map((r) => rowHTML(kind, r)).join('');
  }
  return `<table class="rat"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function rowHTML(kind, r) {
  if (kind === 'files') {
    const flags = [
      r.binary ? '<span class="p-flag">binary</span>' : '',
      !r.atTip ? '<span class="p-flag">deleted</span>' : '',
    ].join('');
    return `<tr class="clickable" data-path="${fmt.esc(r.path)}" data-ptype="file" title="Click to scope the dashboard to this file">
      <td class="path-cell" title="${fmt.esc(r.path)}">${fmt.esc(r.path)}${flags}</td>
      <td class="num pos">${fmt.int(r.added)}</td>
      <td class="num neg">${fmt.int(r.removed)}</td>
      ${growthCell(r.growth)}
      <td class="num">${fmt.int(r.churn)}</td>
      <td class="num">${fmt.int(r.modifications)}</td>
      <td class="num">${fmt.pct(r.modificationFrequency)}</td>
      <td class="num">${fmt.rate(r.churnRate)}</td>
    </tr>`;
  }
  if (kind === 'dirs') {
    return `<tr class="clickable" data-path="${fmt.esc(r.path)}" data-ptype="dir" title="Click to scope the dashboard to this directory">
      <td class="path-cell" title="${fmt.esc(r.path)}"><span class="p-kind">▸</span>${fmt.esc(r.path)}</td>
      <td class="num pos">${fmt.int(r.added)}</td>
      <td class="num neg">${fmt.int(r.removed)}</td>
      ${growthCell(r.growth)}
      <td class="num">${fmt.int(r.churn)}</td>
      <td class="num">${fmt.int(r.modifications)}</td>
      <td class="num">${fmt.pct(r.modificationFrequency)}</td>
      <td class="num">${fmt.rate(r.churnRate)}</td>
    </tr>`;
  }
  // authors
  const flags = [
    r.mailmapped ? '<span class="p-flag">mailmap</span>' : '',
    r.merged ? `<span class="p-flag">merged ×${r.aliases ? r.aliases.length : 1}</span>` : '',
  ].join('');
  const aliasTitle = (r.aliases || []).map((a) => `${a.name} <${a.email}> (${a.commits})`).join('\n');
  return `<tr data-aid="${r.id}" title="Click to toggle this author as a filter\n${fmt.esc(aliasTitle)}">
    <td class="path-cell" title="${fmt.esc(r.email)}">${fmt.esc(r.name)} <span class="hint">${fmt.esc(r.email)}</span>${flags}</td>
    <td class="num">${fmt.int(r.commits)}</td>
    <td class="num pos">${fmt.int(r.added)}</td>
    <td class="num neg">${fmt.int(r.removed)}</td>
    ${growthCell(r.growth)}
    <td class="num">${fmt.int(r.churn)}</td>
    <td class="num">${fmt.int(r.modifications)}</td>
    <td class="num"><span class="own-bar"><span style="width:${(r.ownership * 100).toFixed(1)}%"></span></span>${fmt.pct(r.ownership)}</td>
  </tr>`;
}

function growthCell(v) {
  const cls = v > 0 ? 'pos' : v < 0 ? 'neg' : '';
  return `<td class="num ${cls}">${fmt.signed(v)}</td>`;
}

export function tableMetaText(kind, result, query, shownRows) {
  if (!result) return '';
  const t = result.tables || {};
  const q = query ? ` · “${query}” matches ${shownRows}` : '';
  if (kind === 'files') {
    const s = t.files || {};
    return `${fmt.int(s.inScope || 0)} files in scope · ${fmt.int(s.withActivity || 0)} with activity in the selection · showing ${fmt.int(shownRows)}${q}`;
  }
  if (kind === 'dirs') {
    const s = t.directories || {};
    return `${fmt.int(s.inScope || 0)} directories in scope · ${fmt.int(s.withActivity || 0)} with activity · showing ${fmt.int(shownRows)}${q}`;
  }
  const total = fmt.int(result.selection ? result.selection.authorFilter || 0 : 0);
  return `showing ${fmt.int(shownRows)} authors${q}${total ? ` · filtered to ${total} selected` : ''}`;
}

/* ------------------------------------------------------------------ */
/* Sidebar author list                                                 */
/* ------------------------------------------------------------------ */
export function authorListHTML(payload, selectedIds, query, maxShow = 400) {
  const sel = new Set(selectedIds);
  const q = (query || '').trim().toLowerCase();
  let authors = payload.authors || [];
  if (q) {
    authors = authors.filter((a) =>
      a.name.toLowerCase().includes(q) || a.email.toLowerCase().includes(q));
  }
  const shown = authors.slice(0, maxShow);
  if (!shown.length) {
    return `<div class="ml-empty">No authors match.</div>`;
  }
  return shown.map((a) => {
    const badges = [
      a.mailmapped ? '<span class="ml-badge badge-mailmap" title="Rewritten by .mailmap">mailmap</span>' : '',
      a.merged ? '<span class="ml-badge badge-merged" title="Manually merged identity">merged</span>' : '',
    ].join('');
    return `<label class="ml-row${sel.has(a.id) ? ' selected' : ''}" data-aid="${a.id}" title="${fmt.esc(a.email)}">
      <input type="checkbox" ${sel.has(a.id) ? 'checked' : ''}>
      <span class="ml-main">
        <span class="ml-name">${fmt.esc(a.name)}</span>
        <span class="ml-sub">${fmt.esc(a.email)} · ${fmt.int(a.commits)} commits</span>
      </span>
      ${badges}<span class="ml-badge">${fmt.int(a.commits)}</span>
    </label>`;
  }).join('');
}

export function authorHintText(payload, selectedIds, hidden = 0) {
  if (!payload) return '';
  const parts = [`${fmt.int(payload.authors.length)} authors`];
  if (payload.mailmapPresent) {
    parts.push(payload.mailmapMapped ? `.mailmap rewrites ${payload.mailmapMapped} identities` : '.mailmap present');
  } else {
    parts.push('no .mailmap — merge identities manually');
  }
  if (payload.merges && payload.merges.length) parts.push(`${payload.merges.length} merge group(s)`);
  if (selectedIds.length) parts.push(`${selectedIds.length} selected as filter`);
  if (hidden) parts.push(`showing first ${fmt.int(Math.max(0, payload.authors.length - hidden))}`);
  return parts.join(' · ');
}

/* ------------------------------------------------------------------ */
/* Path scope search results                                           */
/* ------------------------------------------------------------------ */
export function pathResultsHTML(payload) {
  const rows = [];
  rows.push(`<div class="ml-row" data-ptype="repo" title="Remove the file/directory scope">
    <span class="ml-main"><span class="ml-name">Whole repository</span>
    <span class="ml-sub">repository-wide metrics</span></span>
  </div>`);
  for (const d of (payload.dirs || []).slice(0, 30)) {
    rows.push(`<div class="ml-row" data-path="${fmt.esc(d.path)}" data-ptype="dir" title="${fmt.esc(d.path)}">
      <span class="ml-main"><span class="ml-name"><span class="p-kind">▸</span>${fmt.esc(d.path)}</span></span>
      <span class="ml-badge">dir</span>
    </div>`);
  }
  for (const f of (payload.files || []).slice(0, 40)) {
    rows.push(`<div class="ml-row" data-path="${fmt.esc(f.path)}" data-ptype="file" title="${fmt.esc(f.path)}">
      <span class="ml-main"><span class="ml-name">${fmt.esc(f.path)}</span></span>
      <span class="ml-badge">file</span>
    </div>`);
  }
  return rows.join('');
}

/* ------------------------------------------------------------------ */
/* Drill panel (ownership)                                             */
/* ------------------------------------------------------------------ */
export function drillAuthorsHTML(rows, max = 12) {
  const shown = rows.slice(0, max);
  if (!shown.length) return `<div class="hint" style="padding:10px">No authors contributed to this object in the selection.</div>`;
  const body = shown.map((a) => `<tr>
    <td class="path-cell" title="${fmt.esc(a.email)}">${fmt.esc(a.name)}</td>
    <td class="num">${fmt.int(a.churn)}</td>
    <td class="num">${fmt.int(a.modifications)}</td>
    <td class="num"><span class="own-bar"><span style="width:${(a.ownership * 100).toFixed(1)}%"></span></span>${fmt.pct(a.ownership)}</td>
  </tr>`).join('');
  return `<table class="rat">
    <thead><tr><th>Author</th><th>Churn</th><th>Mods</th><th>Ownership</th></tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

/* ------------------------------------------------------------------ */
/* Overview repo cards                                                 */
/* ------------------------------------------------------------------ */
export function repoCardHTML(row, activeId) {
  const meta = row.repo;
  const totals = row.totals || null;
  const stats = meta.stats || null;
  const active = meta.id === activeId;
  const busy = ['queued', 'cloning', 'extracting', 'parsing', 'loading'].includes(meta.status);
  const err = meta.status === 'error';

  const dot = busy ? 'dot-busy' : err ? 'dot-error' : 'dot-ready';
  const statusText = err
    ? (meta.error || 'Failed')
    : busy
      ? `${meta.message || meta.status}${meta.progress != null ? ` — ${Math.round(meta.progress)}%` : ''}`
      : 'Ready';

  const num = (v) => (v === null || v === undefined ? '—' : fmt.int(v));
  const statCells = [
    ['Commits', num((totals && totals.commits) ?? (stats && stats.commits))],
    ['Authors', num((totals && totals.authors) ?? (stats && stats.authors))],
    ['Files', num((totals && totals.files) ?? (stats && stats.paths))],
    ['Added', num(totals && totals.added)],
    ['Removed', num(totals && totals.removed)],
    ['Churn', num(totals && totals.churn)],
  ].map(([label, v]) => `<div class="rs"><b>${v}</b><span>${label}</span></div>`).join('');

  const progress = busy && meta.progress != null
    ? `<div class="progress-track"><span style="width:${Math.max(2, Math.min(100, meta.progress))}%"></span></div>`
    : '';

  const refSha = stats && stats.refSha ? `<span class="mono">${shortSha(stats.refSha)}</span> · ` : '';
  const updated = meta.updatedAt ? `updated ${fmt.timeAgo(meta.updatedAt)}` : '';

  return `<div class="repo-card${active ? ' active' : ''}" data-id="${fmt.esc(meta.id)}">
    <h3><span class="status-dot ${dot}"></span>${fmt.esc(meta.name)}${active ? ' <span class="hint">· open</span>' : ''}</h3>
    <div class="repo-src" title="${fmt.esc(meta.source || '')}">${fmt.esc(meta.sourceType === 'zip' ? `zip: ${meta.source}` : meta.source || '')}</div>
    <div class="hint">${fmt.esc(statusText)}</div>
    ${progress}
    <div class="repo-stats">${statCells}</div>
    ${row.totalsError ? `<div class="banner error">${fmt.esc(row.totalsError)}</div>` : ''}
    <div class="hint">${refSha}${updated}</div>
    <div class="repo-actions">
      <button class="btn btn-primary btn-sm" data-act="open" ${meta.status !== 'ready' ? 'disabled' : ''}>Open dashboard</button>
      <button class="btn btn-danger btn-sm" data-act="delete">Delete</button>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ */
/* Metric reference (help modal)                                       */
/* ------------------------------------------------------------------ */
export function helpHTML() {
  const f = (s) => `<span class="formula">${s}</span>`;
  return `<div class="metrics-doc">
    <h4>Commit sets</h4>
    <p class="hint" style="margin:0 0 6px">All metrics are computed over a set of commits <b>S</b> — the non-merge commits
    reachable from the selected reference <i>h</i> (typically a branch or tag), optionally restricted to a time
    window (committer date, <i>a ≤ date &lt; b</i>) or a manually selected commit list. Merges are excluded.<br>
    Renames are detected at the 50% similarity threshold: a pure rename does not count as added/removed lines, and
    further modifications are attributed to the new path. Deleted files count as removals on their old path.
    Binary files are not measured (git's definition).</p>

    <h4>File metrics</h4>
    <dl>
      <dt>Added lines</dt><dd>lines inserted into the file by the commits in <b>S</b>.</dd>
      <dt>Removed lines</dt><dd>lines deleted from the file by the commits in <b>S</b>.</dd>
      <dt>Growth</dt><dd>${f('added − removed')}</dd>
      <dt>Churn</dt><dd>${f('added + removed')}</dd>
    </dl>

    <h4>Directory metrics</h4>
    <p class="hint" style="margin:0 0 6px">Each metric of a directory is the sum of the same metric over its immediate
    children (which expands recursively to every file below it).</p>

    <h4>Repository metrics</h4>
    <p class="hint" style="margin:0 0 6px">All four metrics of the repository are the directory metrics of the root directory.</p>

    <h4>Commit set metrics</h4>
    <dl>
      <dt>Modifications(h, o)</dt><dd>number of commits in <b>S</b> that changed the object <i>o</i> (churn &gt; 0).</dd>
      <dt>Modification frequency</dt><dd>${f('modifications / |S|')}</dd>
      <dt>Churn rate</dt><dd>${f('churn / |S|')}</dd>
    </dl>

    <h4>Author metrics</h4>
    <dl>
      <dt>Author modifications(a)</dt><dd>commits in <b>S</b> authored by <i>a</i> with churn &gt; 0 on the object.</dd>
      <dt>Author churn(a)</dt><dd>churn generated by commits authored by <i>a</i>.</dd>
      <dt>Author ownership(a)</dt><dd>${f('author churn / total churn')} — 0 when total churn is 0.</dd>
    </dl>

    <h4>Author identities</h4>
    <p class="hint" style="margin:0">Identities are first rewritten through the repository's <code>.mailmap</code>
    (exactly as <code>git check-mailmap</code> does). Repositories without a mailmap, or remaining
    duplicates, can be merged manually with the <i>Merge…</i> button in the Authors panel —
    commit counts and ownership are recomputed immediately.</p>
  </div>`;
}
