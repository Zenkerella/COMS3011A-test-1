/* RAT — HTTP client + shared formatting utilities. */

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(method, url, { json, signal } = {}) {
  const init = { method, headers: {}, signal };
  if (json !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(json);
  }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (err) {
    if (err.name === 'AbortError') throw new ApiError('Request timed out.', 0);
    throw new ApiError(`Network error: ${err.message}`, 0);
  }
  let payload = null;
  const text = await resp.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { /* non-JSON body */ }
  }
  if (!resp.ok) {
    const msg = (payload && (payload.error || payload.message)) || text || `${resp.status} ${resp.statusText}`;
    throw new ApiError(String(msg).trim(), resp.status);
  }
  return payload;
}

const enc = encodeURIComponent;

function qs(params) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === null || v === undefined || v === '') continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export const api = {
  config: () => request('GET', '/api/config'),
  repos: () => request('GET', '/api/repos'),
  repo: (id, ref) => request('GET', `/api/repos/${enc(id)}${qs({ ref })}`),
  clone: (url, name) => request('POST', '/api/repos/clone', { json: { url, name } }),
  deleteRepo: (id) => request('DELETE', `/api/repos/${enc(id)}`),
  overview: (signal) => request('GET', '/api/overview', { signal }),
  prepare: (id, ref) => request('POST', `/api/repos/${enc(id)}/prepare`, { json: { ref } }),
  authors: (id, ref) => request('GET', `/api/repos/${enc(id)}/authors${qs({ ref })}`),
  mergeAuthors: (id, ref, groups) =>
    request('POST', `/api/repos/${enc(id)}/authors/merge`, { json: { ref, groups } }),
  commits: (id, params) => request('GET', `/api/repos/${enc(id)}/commits${qs(params)}`),
  paths: (id, params) => request('GET', `/api/repos/${enc(id)}/paths${qs(params)}`),
  metrics: (id, body) => request('POST', `/api/repos/${enc(id)}/metrics`, { json: body }),
};

/** Multipart upload with progress reporting (fetch() cannot track upload progress). */
export function uploadZip(file, name, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/repos/upload');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload);
      } else {
        const msg = (payload && (payload.error || payload.message)) || xhr.statusText || 'Upload failed';
        reject(new ApiError(msg, xhr.status));
      }
    };
    xhr.onerror = () => reject(new ApiError('Network error while uploading.', 0));
    const fd = new FormData();
    fd.append('file', file);
    if (name) fd.append('name', name);
    xhr.send(fd);
  });
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */

const INT = new Intl.NumberFormat('en-US');
const ESC_RE = /[&<>"']/g;
const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const pad2 = (n) => String(n).padStart(2, '0');

export const fmt = {
  int: (n) => INT.format(Math.round(Number(n) || 0)),
  signed: (n) => {
    const v = Math.round(Number(n) || 0);
    return (v > 0 ? '+' : '') + INT.format(v);
  },
  /** Compact axis / donut label: 1.2k, 3.4M … */
  compact: (n) => {
    n = Number(n) || 0;
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1) + 'B';
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M';
    if (a >= 1e4) return (n / 1e3).toFixed(0) + 'k';
    if (a >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(Math.round(n));
  },
  pct: (v, digits = 1) => `${((Number(v) || 0) * 100).toFixed(digits)}%`,
  /** lines-per-commit style rates: 0.9, 60.4, 1,203 */
  rate: (v) => {
    v = Number(v) || 0;
    if (Math.abs(v) >= 1000) return INT.format(Math.round(v));
    return v.toFixed(Math.abs(v) >= 100 ? 0 : 1);
  },
  /** Accepts epoch seconds (number) or ISO string. */
  date: (v) => {
    const d = toDate(v);
    return d ? `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` : '—';
  },
  dateTime: (v) => {
    const d = toDate(v);
    if (!d) return '—';
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  },
  axisDate: (ts, span) => {
    const d = new Date(ts * 1000);
    if (span >= 3 * 365 * 86400) return String(d.getFullYear());
    if (span >= 200 * 86400) return d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  },
  timeAgo: (ts) => {
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  },
  bytes: (n) => {
    n = Number(n) || 0;
    if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GiB`;
    if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(0)} MiB`;
    if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(0)} KiB`;
    return `${n} B`;
  },
  esc: (s) => String(s ?? '').replace(ESC_RE, (c) => ESC_MAP[c]),
  trunc: (s, maxChars) => (s.length > maxChars ? `${s.slice(0, Math.max(1, maxChars - 1))}…` : s),
  /** epoch seconds -> value for <input type="datetime-local"> (local time) */
  toLocalInput: (ts) => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  },
  /** datetime-local value -> epoch seconds (or null) */
  fromLocalInput: (v) => {
    if (!v) return null;
    const ms = new Date(v).getTime();
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
  },
};

function toDate(v) {
  if (v === null || v === undefined || v === '') return null;
  const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function debounce(fn, ms = 150) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

export function shortSha(sha) {
  return (sha || '').slice(0, 8);
}
