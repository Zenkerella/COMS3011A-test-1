/* RAT — dependency-free SVG chart renderers.
 *
 * Every renderer takes its container element; charts re-render automatically
 * when the window is resized (single debounced listener, registry of last
 * render args per container).
 */
import { fmt } from './api.js';

const NS = 'http://www.w3.org/2000/svg';

export const PALETTE = [
  '#38bdf8', '#a78bfa', '#34d399', '#fbbf24', '#f87171',
  '#22d3ee', '#f472b6', '#84cc16', '#fb923c', '#60a5fa',
];
const OTHER_COLOR = '#475569';

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function emptyNote(node, text) {
  const div = document.createElement('div');
  div.className = 'hint';
  div.style.padding = '18px 10px';
  div.textContent = text;
  node.appendChild(div);
}

/* ---------------- shared tooltip ---------------- */
let tooltipEl = null;

function tooltip() {
  if (!tooltipEl) {
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'tooltip';
    tooltipEl.hidden = true;
    document.body.appendChild(tooltipEl);
  }
  return tooltipEl;
}

export function showTooltip(html, x, y) {
  const el = tooltip();
  el.innerHTML = html;
  el.hidden = false;
  const pad = 14;
  let left = x + pad;
  let top = y + pad;
  if (left + el.offsetWidth > window.innerWidth - 8) left = x - el.offsetWidth - pad;
  if (top + el.offsetHeight > window.innerHeight - 8) top = y - el.offsetHeight - pad;
  el.style.left = `${Math.max(4, left)}px`;
  el.style.top = `${Math.max(4, top)}px`;
}

export function hideTooltip() {
  if (tooltipEl) tooltipEl.hidden = true;
}

function tipRows(rows) {
  return rows
    .map(([k, v]) => `<div class="tt-row"><span>${fmt.esc(k)}</span><span>${fmt.esc(v)}</span></div>`)
    .join('');
}

/* ---------------- resize-aware redraw registry ---------------- */
const registry = new Map();   // node -> { render, args }
let resizeTimer = null;

function remember(node, render, args) {
  registry.set(node, { render, args });
}

/** Drop the resize-redraw entry for a container (e.g. when its content is reset). */
export function forget(node) {
  registry.delete(node);
}

window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    for (const { render, args } of registry.values()) {
      const node = args[0];
      if (node && node.isConnected) render(...args);
    }
  }, 150);
});

/* ================================================================== */
/* Activity over time: diverging added/removed bars + growth line     */
/* ================================================================== */
export function renderActivity(node, series) {
  clear(node);
  hideTooltip();
  remember(node, renderActivity, [node, series]);
  if (!series || !series.length) {
    emptyNote(node, 'No commits in the current selection.');
    return;
  }

  const W = Math.max(380, node.clientWidth || 640);
  const H = 248;
  const m = { l: 46, r: 52, t: 12, b: 22 };
  const plotW = W - m.l - m.r;
  const plotH = H - m.t - m.b;
  const midY = m.t + plotH / 2;
  const halfH = plotH / 2 - 6;
  const n = series.length;

  let maxP = 1;
  let cMax = 0;
  let cMin = 0;
  for (const s of series) {
    maxP = Math.max(maxP, s.added, s.removed);
    cMax = Math.max(cMax, s.cumulativeGrowth);
    cMin = Math.min(cMin, s.cumulativeGrowth);
  }
  if (cMax === cMin) { cMax += 1; cMin -= 1; }

  const step = plotW / n;
  const barW = Math.max(1.5, Math.min(30, step * 0.62));
  const xc = (i) => m.l + step * (i + 0.5);
  const yUp = (v) => midY - (v / maxP) * halfH;
  const yDown = (v) => midY + (v / maxP) * halfH;
  const yC = (v) => m.t + ((cMax - v) / (cMax - cMin)) * plotH;

  const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` });

  /* horizontal grid (primary scale, both directions) */
  const gGrid = svgEl('g');
  for (const frac of [0.5, 1]) {
    for (const dir of [-1, 1]) {
      const y = midY + dir * frac * halfH;
      gGrid.appendChild(svgEl('line', { x1: m.l, x2: m.l + plotW, y1: y, y2: y, class: 'grid-line' }));
      const label = svgEl('text', {
        x: m.l - 6, y: y + 3, 'text-anchor': 'end', class: 'axis-label',
      });
      label.textContent = fmt.compact(maxP * frac);
      gGrid.appendChild(label);
    }
  }
  svg.appendChild(gGrid);

  /* secondary axis (cumulative growth) */
  const gSec = svgEl('g');
  for (const [val, y] of [[cMax, yC(cMax)], [cMin, yC(cMin)]]) {
    const label = svgEl('text', {
      x: m.l + plotW + 6, y: y + 3, 'text-anchor': 'start', class: 'axis-label',
    });
    label.textContent = fmt.compact(val);
    gSec.appendChild(label);
  }
  if (cMin < 0 && cMax > 0) {
    gSec.appendChild(svgEl('line', {
      x1: m.l, x2: m.l + plotW, y1: yC(0), y2: yC(0),
      class: 'grid-line', 'stroke-dasharray': '2 3',
    }));
  }
  svg.appendChild(gSec);

  /* baseline */
  svg.appendChild(svgEl('line', {
    x1: m.l, x2: m.l + plotW, y1: midY, y2: midY,
    stroke: '#2c3d50', 'stroke-width': 1,
  }));

  /* bars */
  const gBars = svgEl('g');
  for (let i = 0; i < n; i++) {
    const s = series[i];
    const x = xc(i) - barW / 2;
    if (s.added > 0) {
      gBars.appendChild(svgEl('rect', {
        x, y: yUp(s.added), width: barW, height: midY - yUp(s.added),
        class: 'bar-added', rx: Math.min(2, barW / 3),
      }));
    }
    if (s.removed > 0) {
      gBars.appendChild(svgEl('rect', {
        x, y: midY, width: barW, height: yDown(s.removed) - midY,
        class: 'bar-removed', rx: Math.min(2, barW / 3),
      }));
    }
  }
  svg.appendChild(gBars);

  /* cumulative growth line */
  if (n > 1) {
    const pts = series.map((s, i) => `${xc(i).toFixed(1)},${yC(s.cumulativeGrowth).toFixed(1)}`);
    svg.appendChild(svgEl('path', { d: `M${pts.join(' L')}`, class: 'growth-line' }));
  } else {
    svg.appendChild(svgEl('circle', {
      cx: xc(0), cy: yC(series[0].cumulativeGrowth), r: 3, fill: 'var(--violet)',
    }));
  }

  /* time ticks */
  const span = series[n - 1].t - series[0].t;
  const gTicks = svgEl('g');
  const tickIdx = [];
  const want = Math.min(6, n);
  for (let j = 0; j < want; j++) {
    const i = want === 1 ? 0 : Math.round((j * (n - 1)) / (want - 1));
    if (!tickIdx.includes(i)) tickIdx.push(i);
  }
  for (const i of tickIdx) {
    const label = svgEl('text', {
      x: xc(i), y: H - 6, 'text-anchor': 'middle', class: 'axis-label',
    });
    label.textContent = fmt.axisDate(series[i].t, span);
    gTicks.appendChild(label);
  }
  svg.appendChild(gTicks);

  /* hover layer */
  const hoverLine = svgEl('line', {
    y1: m.t, y2: m.t + plotH, class: 'hover-line', opacity: 0,
  });
  svg.appendChild(hoverLine);
  const overlay = svgEl('rect', {
    x: m.l, y: m.t, width: plotW, height: plotH, fill: 'transparent',
  });
  svg.appendChild(overlay);

  overlay.addEventListener('mousemove', (e) => {
    const rect = svg.getBoundingClientRect();
    const px = (e.clientX - rect.left) * (W / rect.width);
    let i = Math.floor((px - m.l) / step);
    i = Math.max(0, Math.min(n - 1, i));
    hoverLine.setAttribute('x1', xc(i));
    hoverLine.setAttribute('x2', xc(i));
    hoverLine.setAttribute('opacity', 1);
    const s = series[i];
    showTooltip(
      `<div class="tt-title">Bucket ending ${fmt.date(s.t)}</div>${tipRows([
        ['Commits', fmt.int(s.commits)],
        ['Added', fmt.int(s.added)],
        ['Removed', fmt.int(s.removed)],
        ['Growth', fmt.signed(s.growth)],
        ['Cumulative', fmt.int(s.cumulativeGrowth)],
      ])}`,
      e.clientX, e.clientY,
    );
  });
  overlay.addEventListener('mouseleave', () => {
    hoverLine.setAttribute('opacity', 0);
    hideTooltip();
  });

  node.appendChild(svg);
}

/* ================================================================== */
/* Horizontal bar list (top objects by churn)                         */
/* ================================================================== */
export function renderBars(node, items, onPick) {
  clear(node);
  hideTooltip();
  remember(node, renderBars, [node, items, onPick]);
  if (!items || !items.length) {
    emptyNote(node, 'Nothing with activity in the current selection.');
    return;
  }

  const W = Math.max(280, node.clientWidth || 380);
  const rowH = 30;
  const H = items.length * rowH + 12;
  const labelW = Math.min(190, Math.max(90, W * 0.4));
  const valueW = 58;
  const plotW = Math.max(30, W - labelW - valueW - 10);
  const maxV = Math.max(1, ...items.map((it) => it.value));

  const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` });

  items.forEach((it, i) => {
    const y = 6 + i * rowH;
    const g = svgEl('g', { class: 'bar-row' });
    g.appendChild(svgEl('rect', { x: 0, y: y - 3, width: W, height: rowH - 2, fill: 'transparent' }));

    const maxChars = Math.floor(labelW / 6.1);
    const label = svgEl('text', { x: 0, y: y + 13, class: 'bar-label' });
    label.textContent = fmt.trunc(it.label, maxChars);
    const title = svgEl('title');
    title.textContent = it.label;
    label.appendChild(title);
    g.appendChild(label);

    const w = Math.max(2, (it.value / maxV) * (plotW - 6));
    g.appendChild(svgEl('rect', {
      x: labelW, y: y + 3, width: w, height: 13, rx: 3, class: 'barh',
    }));

    const value = svgEl('text', {
      x: W - 2, y: y + 13, 'text-anchor': 'end', class: 'bar-value',
    });
    value.textContent = fmt.int(it.value);
    g.appendChild(value);

    if (onPick && it.item) {
      g.addEventListener('click', () => onPick(it.item, it));
      g.addEventListener('mouseenter', (e) => {
        const extra = it.sub ? tipRows([[it.sub[0], it.sub[1]]]) : '';
        showTooltip(`<div class="tt-title">${fmt.esc(it.label)}</div>${tipRows([['Churn', fmt.int(it.value)]])}${extra}`, e.clientX, e.clientY);
      });
      g.addEventListener('mousemove', (e) => {
        const extra = it.sub ? tipRows([[it.sub[0], it.sub[1]]]) : '';
        showTooltip(`<div class="tt-title">${fmt.esc(it.label)}</div>${tipRows([['Churn', fmt.int(it.value)]])}${extra}`, e.clientX, e.clientY);
      });
      g.addEventListener('mouseleave', hideTooltip);
    }
    svg.appendChild(g);
  });

  node.appendChild(svg);
}

/* ================================================================== */
/* Donut (author ownership share)                                     */
/* ================================================================== */
export function renderDonut(node, slices, center, sub) {
  clear(node);
  hideTooltip();
  remember(node, renderDonut, [node, slices, center, sub]);
  const total = (slices || []).reduce((acc, s) => acc + s.value, 0);
  if (!total) {
    emptyNote(node, 'No churn on this object in the selection.');
    return;
  }

  const W = Math.max(220, Math.min(node.clientWidth || 300, 340));
  const H = 246;
  const cx = W / 2;
  const cy = H / 2;
  const r = Math.min(W, H) / 2 - 16;
  const inner = r * 0.62;

  const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` });
  const polar = (rad, ang) => [cx + rad * Math.cos(ang), cy + rad * Math.sin(ang)];

  if (slices.length === 1 || slices[0].value >= total - 1e-9) {
    svg.appendChild(svgEl('circle', { cx, cy, r, fill: slices[0].color }));
    svg.appendChild(svgEl('circle', { cx, cy, r: inner, fill: '#111823' }));
  } else {
    let a0 = -Math.PI / 2;
    for (const s of slices) {
      const a1 = a0 + (s.value / total) * Math.PI * 2;
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const [x0o, y0o] = polar(r, a0);
      const [x1o, y1o] = polar(r, a1);
      const [x1i, y1i] = polar(inner, a1);
      const [x0i, y0i] = polar(inner, a0);
      const d = `M${x0o.toFixed(2)},${y0o.toFixed(2)} ` +
        `A${r},${r} 0 ${large} 1 ${x1o.toFixed(2)},${y1o.toFixed(2)} ` +
        `L${x1i.toFixed(2)},${y1i.toFixed(2)} ` +
        `A${inner},${inner} 0 ${large} 0 ${x0i.toFixed(2)},${y0i.toFixed(2)} Z`;
      const seg = svgEl('path', { d, fill: s.color, class: 'donut-seg' });
      seg.addEventListener('mouseenter', (e) => {
        showTooltip(
          `<div class="tt-title">${fmt.esc(s.name)}</div>${tipRows([
            ['Churn', fmt.int(s.value)],
            ['Ownership', fmt.pct(s.value / total)],
          ])}`,
          e.clientX, e.clientY,
        );
      });
      seg.addEventListener('mousemove', (e) => {
        showTooltip(
          `<div class="tt-title">${fmt.esc(s.name)}</div>${tipRows([
            ['Churn', fmt.int(s.value)],
            ['Ownership', fmt.pct(s.value / total)],
          ])}`,
          e.clientX, e.clientY,
        );
      });
      seg.addEventListener('mouseleave', hideTooltip);
      svg.appendChild(seg);
      a0 = a1;
    }
  }

  const centerLabel = svgEl('text', {
    x: cx, y: cy + 2, 'text-anchor': 'middle', class: 'center-label',
  });
  centerLabel.textContent = center;
  svg.appendChild(centerLabel);
  const centerSub = svgEl('text', {
    x: cx, y: cy + 17, 'text-anchor': 'middle', class: 'center-sub',
  });
  centerSub.textContent = sub;
  svg.appendChild(centerSub);

  node.appendChild(svg);
}

export { OTHER_COLOR };
