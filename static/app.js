'use strict';

const MM_PER_IN = 25.4;
const $ = (id) => document.getElementById(id);

/* One colour per stop measurement, so equal-width track cuts are visibly a set.
   Hue only -- the line colour still says which saw.

   A fixed palette wrapped around, which is worse than no colour at all: two
   widths that are nearly the same -- 7-3/8" and 7-3/4" -- came out identical and
   read as one run. So the wheel is walked by the golden angle instead, which
   never repeats within a job, and lightness cycles through three steps so even
   far-apart hues stay apart. Sorting by width puts near-equal widths at adjacent
   indices, and the golden angle throws adjacent indices to opposite sides of the
   wheel -- exactly where the confusion was. */
const GOLDEN_ANGLE = 137.508;
let STOP_INDEX = null;         // rounded width (tenths of a mm) -> position
function stopIndex() {
  if (STOP_INDEX) return STOP_INDEX;
  STOP_INDEX = new Map();
  if (!RESULT || !RESULT.stop_plan) return STOP_INDEX;
  [...new Set(RESULT.stop_plan.map(r => Math.round(r.width_mm * 10)))]
    .sort((a, b) => a - b)
    .forEach((width, i) => STOP_INDEX.set(width, i));
  return STOP_INDEX;
}
function stopColour(offset) {
  const i = stopIndex().get(Math.round(offset * 10));
  if (i === undefined) return 'hsl(0 0% 60%)';
  const hue = (i * GOLDEN_ANGLE) % 360;
  const light = [42, 55, 32][i % 3];
  const sat = [64, 52, 72][i % 3];
  return `hsl(${hue.toFixed(1)} ${sat}% ${light}%)`;
}

const SAW_COLOUR = {
  miter: '#1f8a4c',   // chop saw: the easy cuts
  track: '#c0392b',   // track saw
};
const BADGE_PX = 9;       // cut-number radius in screen px once zoomed in
const BADGE_MAX_MM = 33;  // but never larger than this on the sheet itself
const GAP = 260;        // mm of space between sheets on the canvas
const TITLE_H = 170;    // mm reserved above each sheet for its heading
const GROUP_HUES = [28, 205, 140, 330, 265];

let JOB = null;
let PANELS = [];
let OVERRIDES = {};
let RESULT = null;
let runToken = 0;       // guards against a stale run overwriting a newer one
let bomMode = localStorage.getItem('ply.bomMode') || 'pieces';

const CRITERIA = [
  ['stopchanges', 'Track saw stop changes'],
  ['trackcuts', 'Track saw cuts'],
  ['staged', 'Saw changes'],
  ['mitercuts', 'Mitre saw cuts'],
  ['offcut', 'Largest offcut'],
  ['grouping', 'Cuts to sort by cabinet'],
];
const DEFAULT_PRIORITIES = ['stopchanges', 'trackcuts', 'staged',
                            'mitercuts', 'offcut', 'grouping'];
let PRIORITIES = DEFAULT_PRIORITIES.slice();
try {
  const saved = JSON.parse(localStorage.getItem('ply.priorities') || 'null');
  if (Array.isArray(saved) && saved.length === DEFAULT_PRIORITIES.length) PRIORITIES = saved;
} catch (_) { /* keep the default */ }

function renderPriorities() {
  const names = Object.fromEntries(CRITERIA);
  $('priorities').innerHTML = PRIORITIES.map((key, i) => `<li>
      <span class="name">${names[key] || key}</span>
      <button data-i="${i}" data-d="-1" ${i === 0 ? 'disabled' : ''} title="Move up">\u25b2</button>
      <button data-i="${i}" data-d="1" ${i === PRIORITIES.length - 1 ? 'disabled' : ''}
              title="Move down">\u25bc</button>
    </li>`).join('');
  $('priorities').querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      const i = +b.dataset.i, j = i + (+b.dataset.d);
      if (j < 0 || j >= PRIORITIES.length) return;
      [PRIORITIES[i], PRIORITIES[j]] = [PRIORITIES[j], PRIORITIES[i]];
      localStorage.setItem('ply.priorities', JSON.stringify(PRIORITIES));
      renderPriorities();
      solve();
    };
  });
}

// Sheet prices, CAD. Only these two are known -- any other thickness starts
// blank and is left out of the total until you fill it in, rather than being
// quietly costed at a number nobody supplied.
const PRICE_DEFAULTS = { '1/4"': null, '3/8"': null, '1/2"': 48.98,
                         '5/8"': null, '3/4"': 68.88, '1"': null };
let PRICES = {};
try { PRICES = JSON.parse(localStorage.getItem('ply.prices') || '{}'); } catch (_) { PRICES = {}; }

const money = new Intl.NumberFormat('en-CA', { style: 'currency', currency: 'CAD' });
const priceKey = (g) => g.nominal || g.thickness_mm.toFixed(2) + 'mm';

function priceFor(g) {
  const key = priceKey(g);
  // An explicit null means "cleared" and is honoured; only an absent key falls
  // back to the default, so emptying the box actually empties it.
  if (key in PRICES) return PRICES[key];
  const fallback = PRICE_DEFAULTS[key];
  return fallback === undefined ? null : fallback;
}

function setPrice(key, value) {
  PRICES[key] = (value === null || Number.isNaN(value)) ? null : value;
  try { localStorage.setItem('ply.prices', JSON.stringify(PRICES)); } catch (_) { /* ignore */ }
}

/* ================= units ================= */

const unitMode = () => $('units').value;

function fmt(mm) {
  if (unitMode() === 'mm') return mm.toFixed(1) + ' mm';
  const denom = 32;
  const total = Math.round((mm / MM_PER_IN) * denom);
  const whole = Math.floor(total / denom);
  let num = total - whole * denom;
  if (num === 0) return whole + '"';
  let d = denom;
  const gcd = (a, b) => (b ? gcd(b, a % b) : a);
  const k = gcd(num, d);
  num /= k; d /= k;
  return (whole ? whole + '-' : '') + num + '/' + d + '"';
}

const inToMm = (v) => (parseFloat(v) || 0) * MM_PER_IN;

/* `hidden` is an HTMLElement property -- SVGElement does not implement it, so
   `svg.hidden = false` sets a meaningless JS expando and leaves the attribute
   (and its display:none) in place. Always toggle the attribute itself. */
const isHidden = (el) => el.hasAttribute('hidden');
const setHidden = (el, on) => {
  if (on) el.setAttribute('hidden', ''); else el.removeAttribute('hidden');
};
const escapeHtml = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ================= loading / progress ================= */

/* `full` covers the viewer (nothing to look at yet); otherwise a compact card
   sits at the bottom so the search animation stays visible behind it. */
function showLoading({ title, detail = '', progress = null, full = false }) {
  const el = $('loading');
  el.className = 'loading ' + (full ? 'full' : 'compact');
  setHidden(el, false);
  $('loadTitle').textContent = title;
  $('loadDetail').textContent = detail;
  const bar = el.querySelector('.bar');
  bar.classList.toggle('indeterminate', progress === null);
  $('loadBar').style.width = progress === null
    ? '' : Math.max(0, Math.min(100, progress * 100)).toFixed(1) + '%';
}

function hideLoading() { setHidden($('loading'), true); }

const nf = new Intl.NumberFormat();

/* ================= status ================= */

function setStatus(text, live) {
  const el = $('status');
  el.textContent = text || '';
  el.classList.toggle('live', !!live);
}

function showError(e) {
  $('warnings').innerHTML = '<div class="warn err">' +
    escapeHtml(String((e && e.message) || e)) + '</div>';
}

/* ================= loading a file ================= */

async function loadSample() {
  await withRun(async () => {
    setStatus('Reading sample…', true);
    showLoading({ title: 'Reading STEP file…', full: true,
                  detail: 'Extracting parts and measuring thicknesses' });
    const r = await fetch('/api/sample', { method: 'POST' });
    if (!r.ok) throw new Error((await r.json()).detail || 'could not load sample');
    adoptUpload(await r.json());
  });
  await previewFloor();
}

async function uploadFile(file) {
  await withRun(async () => {
    setStatus('Reading ' + file.name + '…', true);
    showLoading({ title: 'Reading ' + file.name, full: true,
                  detail: 'Extracting parts and measuring thicknesses' });
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || 'upload failed');
    adoptUpload(await r.json());
  });
  await previewFloor();
}

/* Wraps an async step so the status indicator can never be left stuck on. */
let PREVIEW = false;    // showing the floor-only layout, awaiting a real run
let previewing = false;

async function withRun(fn) {
  const token = ++runToken;
  try {
    await fn();
  } catch (e) {
    showError(e);
    hideLoading();
  } finally {
    if (token === runToken) setStatus('');
  }
}

function adoptUpload(data) {
  settled = false;
  JOB = data.job_id;
  PANELS = data.panels;
  OVERRIDES = {};
  RESULT = null;
  STOP_INDEX = null;
  view.fitted = false;
  $('source').textContent = data.source + ' — ' + PANELS.length + ' parts';
  $('solve').disabled = false;
  setHidden($('empty'), true);
  setHidden($('canvas'), false);
  setHidden($('viewerBar'), false);
  renderParts();
  renderWarnings(data.warnings);
}

/* Opening a file runs only far enough to establish the plywood floor. A full
   ranked search before the user has said what matters wastes their time and
   ours -- and the answer would change the moment they touched the ranking. */
async function previewFloor() {
  PREVIEW = true;
  await solve({ floorOnly: true });
  PREVIEW = false;
}

/* ================= solving ================= */

function currentParams() {
  return {
    kerf_mm: inToMm($('kerf').value),
    sheet_width_mm: inToMm($('sheetW').value),
    sheet_length_mm: inToMm($('sheetL').value),
    edge_trim_mm: inToMm($('trim').value),
    min_offcut_mm: inToMm($('minOff').value),
    effort: $('effort').value,
    priorities: PRIORITIES.slice(),
    continuous_grain: $('grainRun').checked,
    max_sheets: $('maxSheets').value === '' ? null : +$('maxSheets').value,
    floor_only: false,
    // Keep searching after the first answer lands; the stream stays open and
    // pushes a better layout if it finds one.
    background_seconds: $('keepGoing').checked ? 600 : 0,
    units: unitMode(),
  };
}

async function solve(runOpts) {
  if (!JOB) return;
  const floorOnly = !!(runOpts && runOpts.floorOnly);
  const token = ++runToken;
  settled = false;
  previewing = floorOnly;
  const params = currentParams();
  params.floor_only = floorOnly;
  if (floorOnly) params.background_seconds = 0;
  const body = JSON.stringify({ job_id: JOB, params, overrides: OVERRIDES });
  const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body };
  $('warnings').innerHTML = '';

  try {
    const firstRun = !RESULT;
    if (!$('animate').checked) {
      setStatus('Optimising…', true);
      showLoading({ title: 'Optimising layout…', full: firstRun,
                    detail: 'Searching for the best arrangement' });
      const r = await fetch('/api/layout', opts);
      if (!r.ok) throw new Error((await r.json()).detail || 'layout failed');
      if (token !== runToken) return;
      RESULT = await r.json();
      STOP_INDEX = null;
      hideLoading();
      renderAll();
      return;
    }

    setStatus('Searching…', true);
    showLoading({ title: 'Optimising layout…', full: firstRun,
                  detail: 'Starting search' });
    const r = await fetch('/api/layout/stream', opts);
    if (!r.ok) throw new Error((await r.json()).detail || 'layout failed');

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (token !== runToken) { reader.cancel(); return; }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        handleFrame(JSON.parse(line), token);
      }
    }
  } catch (e) {
    if (token === runToken) showError(e);
  } finally {
    // Always clears, even if the run threw or was superseded mid-stream.
    if (token === runToken) { setStatus(''); hideLoading(); }
  }
}

let bestSoFar = null;
let settled = false;   // a full result is on screen; further work is refinement
let VIEWING = 0;       // 0 = the chosen layout, 1.. = a runner-up

/* The layout currently on screen. Runner-ups carry their own sheets and stats,
   so browsing them never disturbs the chosen result. */
function viewed() {
  const alts = (RESULT && RESULT.alternatives) || [];
  if (VIEWING > 0 && alts[VIEWING - 1]) {
    const alt = alts[VIEWING - 1];
    return { sheets: alt.sheets, stats: alt.stats };
  }
  return { sheets: RESULT.sheets, stats: RESULT.stats };
}

function handleFrame(frame, token) {
  if (token !== runToken) return;
  if (frame.type === 'error') {
    showError(new Error(frame.detail));
    hideLoading();
    return;
  }

  if (frame.type === 'progress') {
    // Only some frames carry a new drawing; the rest are telemetry saying the
    // search is still running on the same best layout.
    if (frame.sheets && !settled) {
      bestSoFar = frame.stats.sheets;
      renderCanvas(frame.sheets, frame.stats);
      renderStats(frame.stats, null);
      // Once something is on screen, stop covering it up.
      $('loading').className = 'loading compact';
    }
    if (settled) {
      // Background refinement: a quiet status line, no loading card.
      setStatus('Still improving in the background…', false);
      return;
    }
    const s = frame.search || {};
    setStatus(bestSoFar !== null
      ? 'Searching… best so far: ' + bestSoFar + ' sheet(s)' : 'Searching…', true);
    showLoading({
      title: s.group_count > 1
        ? `Optimising ${s.group}  (${s.group_index} of ${s.group_count})`
        : 'Optimising layout…',
      detail: [
        s.attempts !== undefined ? nf.format(s.attempts) + ' layouts tried' : null,
        bestSoFar !== null ? 'best so far: ' + bestSoFar + ' sheets' : null,
        s.elapsed !== undefined ? s.elapsed.toFixed(1) + 's' : null,
      ].filter(Boolean).join('  ·  '),
      progress: s.budget ? Math.min(0.99, s.elapsed / s.budget) : null,
      full: $('loading').classList.contains('full'),
    });
    return;
  }

  bestSoFar = null;
  VIEWING = 0;
  RESULT = frame.payload;
  STOP_INDEX = null;
  canvas.classList.toggle('preview', previewing);
  setHidden($('previewNote'), !previewing);
  if (RESULT.sheet_floor) {
    const box = $('maxSheets');
    // The area bound is what no layout can beat; the floor is the best we have
    // actually packed. Let the box reach the bound so a tighter target can be
    // asked for -- but default to what is known achievable.
    const bound = RESULT.sheet_bound || RESULT.sheet_floor;
    box.min = bound;
    if (box.value === '' || +box.value < bound) box.value = RESULT.sheet_floor;
    $('sheetFloor').textContent = bound < RESULT.sheet_floor
      ? `best ${RESULT.sheet_floor} · bound ${bound}`
      : `floor ${RESULT.sheet_floor}`;
  }
  settled = true;
  hideLoading();
  renderAll();
  setStatus(frame.refined ? 'Found a better layout'
    : ($('keepGoing').checked ? 'Still improving in the background…' : ''), !!frame.refined);
}

/* ================= parts table ================= */

function effective(p) {
  const o = OVERRIDES[p.id] || {};
  return {
    included: o.included !== undefined ? o.included : p.included,
    grain_locked: o.grain_locked !== undefined ? o.grain_locked : p.grain_locked,
    grain_match: o.grain_match !== undefined ? o.grain_match : p.grain_match,
    label: o.label || p.label,
  };
}

const setOverride = (id, patch) => { OVERRIDES[id] = Object.assign({}, OVERRIDES[id], patch); };

function renderParts() {
  $('partCount').textContent = PANELS.length
    ? '(' + PANELS.filter(p => effective(p).included).length + ' of ' + PANELS.length + ')' : '';
  const box = $('parts');
  box.innerHTML = '';
  PANELS.forEach(p => {
    const st = effective(p);
    const row = document.createElement('div');
    row.className = 'part' + (st.included ? '' : ' excluded');

    const inc = document.createElement('input');
    inc.type = 'checkbox'; inc.checked = st.included;
    inc.title = 'Include in layout';
    inc.onchange = () => { setOverride(p.id, { included: inc.checked }); renderParts(); solve(); };

    const lock = document.createElement('input');
    lock.type = 'checkbox'; lock.checked = st.grain_locked;
    lock.title = 'Lock grain direction (prevent rotating)';
    lock.onchange = () => { setOverride(p.id, { grain_locked: lock.checked }); solve(); };

    const info = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'pname';
    name.textContent = st.label;
    name.title = p.path.join(' / ') + ' — click to rename';
    name.contentEditable = 'true';
    name.spellcheck = false;
    name.onblur = () => {
      const v = name.textContent.trim();
      if (v !== st.label) { setOverride(p.id, { label: v }); solve(); }
    };
    name.onkeydown = (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); name.blur(); } };

    const dims = document.createElement('div');
    dims.className = 'pdims';
    dims.textContent = fmt(p.thickness_mm) + ' · ' + fmt(p.width_mm) + ' × ' + fmt(p.length_mm);
    info.append(name, dims);
    if (p.reject_reason) {
      const why = document.createElement('div');
      why.className = 'reason'; why.textContent = p.reject_reason;
      info.append(why);
    }
    // Which faces form a cabinet's continuous grain run. Pre-ticked by name and
    // size; correct it here when the guess is wrong.
    const grain = document.createElement('input');
    grain.type = 'checkbox';
    grain.checked = st.grain_match;
    grain.title = 'Part of this cabinet\u2019s continuous grain run';
    grain.className = 'grainbox';
    grain.onchange = () => {
      setOverride(p.id, { grain_match: grain.checked });
      if ($('grainRun').checked) solve();
    };

    row.append(inc, lock, grain, info);
    box.append(row);
  });
}

/* ================= canvas: zoom + pan ================= */

const view = { x: 0, y: 0, w: 1000, h: 1000, content: null, fitted: false, adjusted: false };
const canvas = $('canvas');

function applyView() {
  const r = canvas.getBoundingClientRect();
  if (!r.width || !r.height) return;
  // Keep the viewBox aspect equal to the element's so screen->world stays 1:1.
  view.h = view.w * (r.height / r.width);
  canvas.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
  rescaleBadges(r);
  const pct = view.content ? Math.round((view.content.w / view.w) * 100) : 100;
  $('zoomLabel').textContent = pct + '%';
}

/* Cut badges are unit-sized in the SVG so their scale can be set per view.
   Zoomed in they hold a constant screen size instead of swelling over the part
   labels; zoomed out the cap takes over, so they stay proportional to the sheet
   rather than burying a whole fit-to-window layout under giant discs. */
function rescaleBadges(rect) {
  const box = rect || canvas.getBoundingClientRect();
  if (!box.width) return;
  const k = Math.min(BADGE_PX * (view.w / box.width), BADGE_MAX_MM);
  canvas.querySelectorAll('.cutbadge').forEach(g => {
    g.setAttribute('transform',
      `translate(${g.dataset.cx},${g.dataset.cy}) scale(${k})`);
  });
  // Stop markers follow the same rule, a little smaller so they read as
  // annotation rather than competing with the cut numbers.
  const d = Math.min(BADGE_PX * 0.62 * (view.w / box.width), BADGE_MAX_MM * 0.62);
  canvas.querySelectorAll('.stopdot').forEach(g => {
    g.setAttribute('transform',
      `translate(${g.dataset.cx},${g.dataset.cy}) scale(${d})`);
  });
}

function fitView() {
  const c = view.content;
  if (!c) return;
  const r = canvas.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const pad = 1.06;
  // Widen the view if the content is proportionally taller than the panel.
  const byWidth = c.w * pad;
  const byHeight = (c.h * pad) * (r.width / r.height);
  view.w = Math.max(byWidth, byHeight);
  view.h = view.w * (r.height / r.width);
  view.x = c.x + c.w / 2 - view.w / 2;
  view.y = c.y + c.h / 2 - view.h / 2;
  view.adjusted = false;
  view.fitted = true;
  applyView();
}

function zoomAt(factor, clientX, clientY) {
  const r = canvas.getBoundingClientRect();
  const px = r.width ? (clientX - r.left) / r.width : 0.5;
  const py = r.height ? (clientY - r.top) / r.height : 0.5;
  const ax = view.x + px * view.w;
  const ay = view.y + py * view.h;

  const limit = view.content ? view.content.w : 5000;
  const next = Math.min(Math.max(view.w * factor, limit / 60), limit * 8);
  if (next === view.w) return;
  view.w = next;
  view.h = view.w * (r.height / r.width);
  view.x = ax - px * view.w;
  view.y = ay - py * view.h;
  view.adjusted = true;
  applyView();
}

canvas.addEventListener('wheel', (e) => {
  if (isHidden(canvas)) return;
  e.preventDefault();       // stop the page zooming/scrolling instead
  // Trackpad pinch arrives as ctrlKey+wheel; treat both as zoom.
  const step = e.ctrlKey ? 0.010 : 0.0022;
  zoomAt(Math.exp(e.deltaY * step), e.clientX, e.clientY);
}, { passive: false });

let panning = null;
canvas.addEventListener('pointerdown', (e) => {
  if (isHidden(canvas) || e.button !== 0) return;
  panning = { id: e.pointerId, x: e.clientX, y: e.clientY };
  canvas.setPointerCapture(e.pointerId);
  canvas.classList.add('panning');
});
canvas.addEventListener('pointermove', (e) => {
  if (!panning || e.pointerId !== panning.id) return;
  const r = canvas.getBoundingClientRect();
  view.x -= (e.clientX - panning.x) * (view.w / r.width);
  view.y -= (e.clientY - panning.y) * (view.h / r.height);
  panning.x = e.clientX; panning.y = e.clientY;
  view.adjusted = true;
  applyView();
});
const endPan = (e) => {
  if (!panning || e.pointerId !== panning.id) return;
  canvas.releasePointerCapture(panning.id);
  canvas.classList.remove('panning');
  panning = null;
};
canvas.addEventListener('pointerup', endPan);
canvas.addEventListener('pointercancel', endPan);

function centreZoom(f) {
  const r = canvas.getBoundingClientRect();
  zoomAt(f, r.left + r.width / 2, r.top + r.height / 2);
}
$('zoomIn').onclick = () => centreZoom(1 / 1.3);
$('zoomOut').onclick = () => centreZoom(1.3);
$('fit').onclick = fitView;

new ResizeObserver(() => { if (!isHidden(canvas)) (view.adjusted ? applyView() : fitView()); })
  .observe($('viewer'));

/* ================= canvas: drawing ================= */

function fitText(label, w, h, maxFont) {
  const words = label.split(/\s*\/\s*|\s+/).filter(Boolean);
  for (let fs = maxFont; fs >= 13; fs -= 2) {
    const charW = fs * 0.56, lineH = fs * 1.18;
    const maxChars = Math.floor((w - fs * 0.5) / charW);
    if (maxChars < 4) continue;
    const lines = [];
    let cur = '';
    for (const word of words) {
      const candidate = cur ? cur + ' ' + word : word;
      if (candidate.length <= maxChars) cur = candidate;
      else { if (cur) lines.push(cur); cur = word; }
    }
    if (cur) lines.push(cur);
    if (lines.some(l => l.length > maxChars)) continue;
    if (lines.length * lineH <= h - fs * 0.4) return { fs, lines, lineH };
  }
  return null;
}

function partLabelSvg(pl, cx, cy) {
  const pad = 10;
  let fit = fitText(pl.label, pl.w - pad, pl.h - pad, 46);
  let rotated = false;
  if (!fit) { fit = fitText(pl.label, pl.h - pad, pl.w - pad, 46); rotated = !!fit; }
  if (!fit) return '';

  const along = rotated ? pl.w : pl.h;
  const showDims = (fit.lines.length + 1) * fit.lineH <= along - pad;
  const lines = showDims ? fit.lines.concat([fmt(pl.w) + ' x ' + fmt(pl.h)]) : fit.lines;
  const top = cy - (lines.length * fit.lineH) / 2 + fit.lineH * 0.78;

  const spans = lines.map((ln, i) => {
    const isDim = showDims && i === lines.length - 1;
    return `<tspan x="${cx}" y="${(top + i * fit.lineH).toFixed(1)}"
      font-size="${isDim ? (fit.fs * 0.82).toFixed(1) : fit.fs}"
      fill="${isDim ? '#5f564b' : '#241f19'}"
      font-weight="${isDim ? 400 : 600}">${escapeHtml(ln)}</tspan>`;
  }).join('');
  return `<text text-anchor="middle"${rotated ? ` transform="rotate(-90 ${cx} ${cy})"` : ''}
    >${spans}</text>`;
}

const groupOf = (id, groups) => (groups || []).find(g => g.id === id);
const hueFor = (id, groups) => {
  const i = (groups || []).findIndex(g => g.id === id);
  return GROUP_HUES[(i < 0 ? 0 : i) % GROUP_HUES.length];
};

function renderCanvas(sheets, stats, groups) {
  groups = groups || (RESULT && RESULT.groups) || inferGroups(sheets);
  const showCuts = $('showCuts').checked;
  const showOutlines = $('showOutlines').checked;
  const byId = Object.fromEntries(PANELS.map(p => [p.id, p]));

  if (!sheets.length) {
    canvas.innerHTML = '';
    view.content = null;
    return;
  }

  const W = sheets[0].width_mm, L = sheets[0].length_mm;
  // Wrap into a grid rather than one long row -- twelve sheets side by side fit
  // to a strip too small to read. Sheets are ~twice as tall as wide, so favour
  // columns over rows to keep the whole block roughly square.
  const cols = Math.max(1, Math.ceil(Math.sqrt(sheets.length * 2)));
  const cellW = W + GAP, cellH = L + TITLE_H + GAP;

  const body = sheets.map((sheet, i) => {
    const g = groupOf(sheet.group_id, groups);
    const hue = hueFor(sheet.group_id, groups);

    const parts = sheet.placements.map((pl, n) => {
      const panel = byId[pl.panel_id];
      const y = L - pl.y - pl.h;      // sheet origin is bottom-left, SVG's is top-left
      const fill = `hsl(${hue} 40% ${80 - (n % 3) * 6}%)`;
      let outline = '';
      if (showOutlines && panel && panel.outline) {
        const pts = panel.outline.map(([u, v]) => {
          const sx = pl.rotated ? pl.x + v : pl.x + u;
          const sy = pl.rotated ? pl.y + u : pl.y + v;
          return sx.toFixed(1) + ',' + (L - sy).toFixed(1);
        }).join(' ');
        outline = `<polygon points="${pts}" fill="none" stroke="hsl(${hue} 65% 26%)"
                    stroke-width="1.2" stroke-dasharray="5 4" opacity=".8"
                    vector-effect="non-scaling-stroke"/>`;
      }
      return `<g><rect x="${pl.x}" y="${y}" width="${pl.w}" height="${pl.h}"
          fill="${fill}" stroke="hsl(${hue} 45% 30%)" stroke-width="1.2"
          vector-effect="non-scaling-stroke"/>${outline}
        ${partLabelSvg({ ...pl, y }, pl.x + pl.w / 2, y + pl.h / 2)}</g>`;
    }).join('');

    const cuts = !showCuts ? '' : sheet.cuts.map(c => {
      const y1 = L - c.y1, y2 = L - c.y2;
      const f = 0.18 + 0.64 * ((c.index * 0.37) % 1);   // spread badges along the cut
      const mx = c.x1 + (c.x2 - c.x1) * f, my = y1 + (y2 - y1) * f;
      // The badge is drawn at unit size and scaled in applyView(), so it stays
      // the same size on screen instead of swelling over the labels as you zoom.
      // Colour says which saw: green you chop at home, red means setting the
      // track back up, blue is a full-length rip the store can do for you.
      // Isolating cuts stay solid and heavier so they still stand out.
      const col = SAW_COLOUR[c.saw] || SAW_COLOUR.track;
      const dash = c.separates ? '' : ' stroke-dasharray="7 5"';
      // Colour a tick by the stop measurement, so cuts you can make without
      // touching the saw carry the same marker.
      const stopHue = c.saw === 'track' ? stopColour(c.offset_mm) : null;
      const wide = c.separates ? 3 : 2.2;
      return `<g><line x1="${c.x1}" y1="${y1}" x2="${c.x2}" y2="${y2}"
          stroke="${col}" stroke-width="${wide}"${dash}
          data-off="${c.saw === 'track' ? c.offset_mm.toFixed(1) : ''}"
          data-base="${wide}" vector-effect="non-scaling-stroke"/>
        ${stopHue === null ? '' : `<g class="stopdot" data-cx="${c.x1}" data-cy="${y1}">
          <circle r="1" fill="${stopHue}" stroke="#fff" stroke-width="0.22"/></g>`}
        <g class="cutbadge" data-cx="${mx}" data-cy="${my}">
          <circle r="1" fill="${col}" stroke="#fff" stroke-width="0.17"/>
          <text y="0.37" text-anchor="middle" font-size="1.15"
                font-weight="700" fill="#fff">${c.index}</text>
        </g></g>`;
    }).join('');

    const pct = Math.round(100 * sheet.used_area_mm2 / (W * L));
    const title = `Sheet ${i + 1}${g && g.nominal ? ' — ' + g.nominal : ''}`;
    const sub = `${pct}% used` + (sheet.largest_offcut
      ? ` · offcut ${fmt(sheet.largest_offcut[0])} × ${fmt(sheet.largest_offcut[1])}` : '');

    return `<g transform="translate(${(i % cols) * cellW},${Math.floor(i / cols) * cellH})">
      <text x="0" y="${-TITLE_H + 62}" font-size="62" font-weight="650"
            fill="#1c1a17">${escapeHtml(title)}</text>
      <text x="0" y="${-TITLE_H + 132}" font-size="46" fill="#6f6a61">${escapeHtml(sub)}</text>
      <rect x="0" y="0" width="${W}" height="${L}" fill="#efe9df"
            stroke="#8b8175" stroke-width="6"/>
      ${parts}${cuts}</g>`;
  }).join('');

  canvas.innerHTML = body;
  const rows = Math.ceil(sheets.length / cols);
  view.content = {
    x: 0, y: -TITLE_H,
    w: Math.min(sheets.length, cols) * cellW - GAP,
    h: rows * cellH - GAP,
  };
  if (!view.adjusted || !view.fitted) fitView(); else applyView();
}

/* Progress frames carry no group list; derive enough to colour them consistently. */
function inferGroups(sheets) {
  const ids = [];
  sheets.forEach(s => { if (!ids.includes(s.group_id)) ids.push(s.group_id); });
  return ids.map(id => ({ id, nominal: null }));
}

/* ================= tables ================= */

function renderAll() {
  if (!RESULT) return;
  const view = viewed();
  renderWarnings(RESULT.warnings);
  renderReport(RESULT.report);
  $('prove').disabled = false;
  renderCandidates();
  renderStats(view.stats, RESULT.groups);
  renderCanvas(view.sheets, view.stats, RESULT.groups);
  renderCutList();
  renderStopPlan();
  renderBom();
  renderParts();
}

/* Runner-up layouts, with the numbers that distinguish them. A candidate that
   scores worse on the ranking may still be the one that suits the shop. */
function renderCandidates() {
  const alts = (RESULT && RESULT.alternatives) || [];
  const host = $('candidates');
  if (!alts.length) { setHidden(host, true); return; }
  setHidden(host, false);

  // `sheets` is not a ranked criterion any more, so it is absent from the
  // report -- take it from the stats or the chip reads "NaN sheets".
  const best = Object.fromEntries((RESULT.report || []).map(c => [c.key, c.value]));
  best.sheets = RESULT.stats.sheets;
  const entries = [{ label: 'Chosen', values: best }].concat(
    alts.map(a => ({ label: a.label, values: a.values || {} })));

  host.innerHTML = entries.map((e, i) => {
    const v = e.values;
    // Show what actually separates the candidates, or two of them read alike.
    const bits = [`${v.sheets ?? '?'} sheets`,
                  `${Math.round(v.trackcuts)} track`,
                  `${Math.round(v.stopchanges)} stops`,
                  `${Math.round(v.staged)} swaps`];
    return `<button class="cand${i === VIEWING ? ' on' : ''}" data-i="${i}">
      <b>${escapeHtml(e.label)}</b><span>${bits.join(' · ')}</span></button>`;
  }).join('');

  host.querySelectorAll('.cand').forEach(b => {
    b.onclick = () => {
      VIEWING = +b.dataset.i;
      view.adjusted = false;      // refit: a runner-up may use a different count
      renderAll();
    };
  });
}

function renderWarnings(list) {
  $('warnings').innerHTML = (list || []).map(w =>
    '<div class="warn">' + escapeHtml(w) + '</div>').join('');
}

function renderReport(report) {
  if (!report || !report.length) return;
  const fmtVal = (c) => c.key === 'offcut'
    ? fmt(Math.sqrt(Math.max(0, -c.value)))          // side of an equivalent square
    : (+c.value).toLocaleString();
  $('report').innerHTML = '<table><tbody>' + report.map(c => {
    const badge = c.bound === null || c.bound === undefined ? ''
      : c.optimal ? '<span class="badge ok">optimal</span>'
      : `<span class="badge gap">floor ${(+c.bound).toLocaleString()}</span>`;
    return `<tr><td>#${c.rank}</td><td>${escapeHtml(c.label)}</td>
      <td class="v">${escapeHtml(fmtVal(c))}</td><td>${badge}</td></tr>`;
  }).join('') + '</tbody></table>';
}

function renderStats(stats, groups) {
  const byGroup = (groups || []).map(g => {
    const n = stats.sheets_by_group ? (stats.sheets_by_group[g.id] || 0) : 0;
    return `<div>${g.nominal || ''} (${g.thickness_mm.toFixed(2)} mm): <b>${n}</b> sheet(s)</div>`;
  }).join('');

  const lines = [`<div>Total sheets: <b>${stats.sheets}</b></div>`, byGroup];
  if (stats.yield_pct !== undefined) lines.push(`<div>Material used: <b>${stats.yield_pct}%</b></div>`);
  if (stats.total_cuts !== undefined) {
    // Break the total down by which saw does the work.
    const by = Object.fromEntries((RESULT && RESULT.report || [])
      .map(c => [c.key, c.value]));
    const track = by.trackcuts, miter = by.mitercuts;
    let detail = '';
    if (track !== undefined && miter !== undefined) {
      const swaps = by.staged, stops = by.stopchanges;
      detail = `<div class="cutsplit">`
        + `<span>${miter} mitre</span>`
        + `<span class="heavy">${track} track saw</span>`
        + (swaps !== undefined ? `<span>${swaps} saw change${swaps === 1 ? '' : 's'}</span>` : '')
        + (stops !== undefined ? `<span>${stops} stop change${stops === 1 ? '' : 's'}</span>` : '')
        + `</div>`;
    }
    lines.push(`<div>Cuts: <b>${stats.total_cuts}</b></div>${detail}`);
  }
  if (stats.separating_cuts) {
    lines.push(`<div>Cuts to sort by cabinet: <b>${stats.separating_cuts}</b>`
      + (stats.total_cuts ? ` of ${stats.total_cuts}` : '') + `</div>`);
  }
  if (stats.single_cabinet_sheets !== undefined && stats.sheets) {
    const n = stats.single_cabinet_sheets;
    lines.push(`<div>Sheets with one cabinet: <b>${n}</b> of ${stats.sheets}</div>`);
  }
  $('stats').innerHTML = lines.join('');
}

/* Rebuilt in the browser rather than used as sent, so switching units is
   instant instead of costing a full re-solve. */
function cutSentence(c) {
  const verb = c.axis === 'V' ? 'Rip' : 'Crosscut';
  return `${verb} piece ${c.piece_label} (${fmt(c.piece_w)} x ${fmt(c.piece_h)}) ` +
    `at ${fmt(c.offset_mm)} from the ${c.from_edge} edge` +
    (c.produces && c.produces.length ? ` -> ${c.produces.join(' + ')}` : '');
}

function renderCutList() {
  const sep = viewed().stats.separating_cuts;
  $('cutCount').textContent = viewed().stats.total_cuts
    ? '(' + viewed().stats.total_cuts + ' cuts'
      + (sep ? ', ' + sep + ' isolating' : '') + ')' : '';
  const rows = [];
  if (sep) {
    rows.push(`<tr><td colspan="2" class="seplead">Make the
      <span class="septag">isolating</span> cuts first — after those
      ${sep} cuts, every piece belongs to a single cabinet.</td></tr>`);
  }
  viewed().sheets.forEach((sheet, i) => {
    const g = groupOf(sheet.group_id, RESULT.groups);
    // A sheet whose first track cut repeats the last one of the sheet before it
    // is picked up with the stop already set -- worth saying, because it is the
    // one saving that is invisible on the drawing.
    const opens = sheet.cuts.find(c => c.saw === 'track');
    const prior = i > 0 ? viewed().sheets[i - 1].cuts
      .filter(c => c.saw === 'track').slice(-1)[0] : null;
    const carries = opens && prior
      && Math.abs(opens.offset_mm - prior.offset_mm) < 0.15;
    rows.push(`<tr class="grp"><td colspan="2">Sheet ${i + 1}${g && g.nominal ? ' — ' + g.nominal : ''}
      (${sheet.cuts.length} cuts)${carries
        ? ` <span class="septag">stop already at ${escapeHtml(fmt(opens.offset_mm))}</span>` : ''}</td></tr>`);
    sheet.cuts.forEach(c => rows.push(
      `<tr><td class="num"><span class="cutno${c.separates ? ' sep' : ''}"
             >${c.index}</span></td>
           <td>${escapeHtml(cutSentence(c))}${c.separates
             ? ' <span class="septag">isolates a cabinet</span>' : ''}</td></tr>`));
  });
  $('cutlist').innerHTML = rows.length
    ? '<table><tbody>' + rows.join('') + '</tbody></table>'
    : '<p class="muted small">No cuts.</p>';
}

/* Every run of track cuts that shares a stop, in cutting order. Hovering a row
   lights up the cuts it covers, so you can see what one stop setting buys. */
function renderStopPlan() {
  const runs = (RESULT && RESULT.stop_plan) || [];
  const host = $('stopplan');
  if (!runs.length) { host.innerHTML = '<p class="muted small">No track cuts.</p>'; return; }

  const carried = runs.filter(r => (r.sheets || []).length > 1).length;
  const lead = carried
    ? `<tr><td colspan="4" class="seplead">${carried} of these
        ${carried === 1 ? 'setting carries' : 'settings carry'} straight into the
        next sheet — the saw is not touched at the changeover.</td></tr>`
    : '';
  host.innerHTML = '<table><thead><tr><th>#</th><th class="num">Stop</th>'
    + '<th class="num">Cuts</th><th class="num">Sheet</th></tr></thead><tbody>'
    + lead
    + runs.map(r => {
        const on = r.sheets || [];
        const span = on.length > 1 ? `${on[0]}–${on[on.length - 1]}` : (on[0] || '');
        return `<tr class="stoprow" data-w="${r.width_mm}">
        <td class="num"><span class="swatch" style="background:${stopColour(r.width_mm)}"></span>${r.index}</td>
        <td class="num"><b>${escapeHtml(fmt(r.width_mm))}</b></td>
        <td class="num">${r.count}</td>
        <td class="num${on.length > 1 ? ' carried' : ''}">${span}</td></tr>`;
      }).join('')
    + '</tbody></table>';

  host.querySelectorAll('.stoprow').forEach(row => {
    row.onmouseenter = () => highlightStop(+row.dataset.w);
    row.onmouseleave = () => highlightStop(null);
  });
}

function highlightStop(width) {
  canvas.querySelectorAll('line[data-off]').forEach(line => {
    const on = width !== null && Math.abs(+line.dataset.off - width) < 0.15;
    line.setAttribute('stroke-width', on ? 4 : (line.dataset.base || 1.5));
    line.setAttribute('opacity', width === null || on ? 1 : 0.25);
  });
}

function renderBom() {
  setSeg();
  if (bomMode === 'sheets') renderSheetBom(); else renderPieceBom();
}

function setSeg() {
  $('bomPieces').classList.toggle('on', bomMode === 'pieces');
  $('bomSheets').classList.toggle('on', bomMode === 'sheets');
}

function renderPieceBom() {
  const rows = [];
  RESULT.groups.forEach(g => {
    const mine = RESULT.bom.filter(b => b.group_id === g.id);
    if (!mine.length) return;
    const total = mine.reduce((a, b) => a + b.qty, 0);
    const sheets = RESULT.stats.sheets_by_group[g.id] || 0;
    rows.push(`<tr class="grp"><td colspan="4">${g.nominal || ''} —
      ${g.thickness_mm.toFixed(2)} mm — ${total} pieces, ${sheets} sheet(s)</td></tr>`);
    mine.forEach(b => rows.push(
      `<tr><td class="num">${b.qty}×</td><td class="num">${escapeHtml(fmt(b.width_mm))}</td>
           <td class="num">${escapeHtml(fmt(b.length_mm))}</td>
           <td class="muted">${escapeHtml(b.labels.join(', '))}</td></tr>`));
  });
  $('bom').innerHTML = rows.length
    ? '<table><thead><tr><th>Qty</th><th>Width</th><th>Length</th><th>Parts</th></tr></thead>'
      + '<tbody>' + rows.join('') + '</tbody></table>'
    : '<p class="muted small">Nothing to cut.</p>';
}

/* What to buy, and what it costs. Prices are per sheet, in CAD. */
function renderSheetBom() {
  const rows = [];
  let total = 0;
  let unpriced = 0;

  RESULT.groups.forEach(g => {
    const sheets = RESULT.stats.sheets_by_group[g.id] || 0;
    if (!sheets) return;
    const price = priceFor(g);
    const subtotal = price === null ? null : price * sheets;
    if (subtotal === null) unpriced++; else total += subtotal;

    rows.push(`<tr>
      <td class="stock"><b>${g.nominal || '—'}</b>
          <span class="muted">${g.thickness_mm.toFixed(2)}&nbsp;mm</span></td>
      <td class="num">${sheets}</td>
      <td class="num"><input class="price" type="number" min="0" step="0.01"
            data-key="${escapeHtml(priceKey(g))}" placeholder="—"
            value="${price === null ? '' : price}"></td>
      <td class="num">${subtotal === null ? '—' : money.format(subtotal)}</td></tr>`);
  });

  if (!rows.length) {
    $('bom').innerHTML = '<p class="muted small">Nothing to buy.</p>';
    return;
  }

  rows.push(`<tr class="total">
    <td>Total</td>
    <td class="num">${RESULT.stats.sheets}</td>
    <td class="num muted">${RESULT.stats.total_cuts} cuts</td>
    <td class="num">${money.format(total)}</td></tr>`);

  $('bom').innerHTML =
    '<table><thead><tr><th>Stock</th><th class="num">Sheets</th>'
    + '<th class="num">$ / sheet</th><th class="num">Subtotal</th></tr></thead><tbody>'
    + rows.join('') + '</tbody></table>'
    + (unpriced
        ? `<div class="costnote">${unpriced} stock(s) have no price set and are left
           out of the total. Type a price to include them.</div>`
        : '');

  $('bom').querySelectorAll('.price').forEach(input => {
    input.onchange = () => {
      const v = input.value.trim();
      setPrice(input.dataset.key, v === '' ? null : parseFloat(v));
      renderSheetBom();          // costs are arithmetic -- no need to re-solve
    };
  });
}

$('bomPieces').onclick = () => { bomMode = 'pieces'; localStorage.setItem('ply.bomMode', bomMode); if (RESULT) renderBom(); else setSeg(); };
$('bomSheets').onclick = () => { bomMode = 'sheets'; localStorage.setItem('ply.bomMode', bomMode); if (RESULT) renderBom(); else setSeg(); };
setSeg();
renderPriorities();

/* ================= draggable splitters ================= */

function makeSplitter(el, opts) {
  /* Re-clamp a restored size against the window it is being restored into.
     A value dragged small on a laptop was being honoured verbatim on a big
     screen, which is how the cut list ended up 72px tall with its content --
     and the plywood total -- hidden below the fold. */
  const applySaved = () => {
    const saved = localStorage.getItem(opts.key);
    if (!saved || !/^\d+(\.\d+)?px$/.test(saved.trim())) {
      if (saved) localStorage.removeItem(opts.key);
      return;
    }
    const box = opts.container.getBoundingClientRect();
    const span = opts.axis === 'x' ? box.width : box.height;
    if (!span) return;
    const floor = Math.max(opts.min, span * (opts.minFrac || 0));
    const value = Math.min(Math.max(parseFloat(saved), floor), opts.max(box));
    opts.target.style.setProperty(opts.varName, Math.round(value) + 'px');
  };
  applySaved();
  new ResizeObserver(applySaved).observe(opts.container);

  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    // Capture is a nicety, not a requirement -- it throws for synthetic pointers.
    try { el.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    el.classList.add('dragging');
    document.body.style.userSelect = 'none';

    // Listen on the window: a 6px handle is easy to outrun with the pointer.
    const move = (ev) => {
      const box = opts.container.getBoundingClientRect();
      const raw = opts.axis === 'x' ? ev.clientX - box.left : box.bottom - ev.clientY;
      const value = Math.round(Math.min(Math.max(raw, opts.min), opts.max(box)));
      opts.target.style.setProperty(opts.varName, value + 'px');
    };
    const up = () => {
      el.classList.remove('dragging');
      document.body.style.userSelect = '';
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      localStorage.setItem(opts.key,
        opts.target.style.getPropertyValue(opts.varName).trim());
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  });
}

makeSplitter($('vsplit'), {
  key: 'ply.sidebar', varName: '--sidebar', target: $('app'), container: $('app'),
  axis: 'x', min: 210, max: (box) => Math.min(620, box.width - 320),
});
makeSplitter($('hsplit'), {
  key: 'ply.bottom', varName: '--bottom', target: $('main'), container: $('main'),
  // A quarter of the height at minimum: three panes of tables need the room.
  axis: 'y', min: 150, minFrac: 0.24, max: (box) => box.height - 200,
});
makeSplitter($('bsplit'), {
  key: 'ply.cutcol', varName: '--cutcol', target: $('bottom'), container: $('bottom'),
  axis: 'x', min: 220, max: (box) => box.width * 0.5,
});
makeSplitter($('ssplit'), {
  key: 'ply.stopcol', varName: '--stopcol', target: $('bottom'), container: $('bottom'),
  axis: 'x', min: 200, max: (box) => box.width * 0.75,
});

/* ================= wiring ================= */

['sheetW', 'sheetL', 'kerf', 'trim', 'minOff', 'effort', 'keepGoing',
  'grainRun', 'maxSheets'].forEach(id => $(id).addEventListener('change', solve));
$('units').addEventListener('change', () => { if (RESULT) renderAll(); renderParts(); });
$('showCuts').addEventListener('change', () => RESULT && renderAll());
$('showOutlines').addEventListener('change', () => RESULT && renderAll());
$('solve').addEventListener('click', () => solve());

$('prove').addEventListener('click', async () => {
  if (!JOB) return;
  const btn = $('prove');
  btn.disabled = true;
  $('proof').innerHTML = '<span class="muted">Searching exhaustively\u2026</span>';
  try {
    const r = await fetch('/api/prove', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: JOB, params: currentParams(), overrides: OVERRIDES }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || 'proof failed');
    const p = await r.json();
    const better = p.sheets.filter(s => s.status === 'a better arrangement exists');
    $('proof').innerHTML =
      `<div><b class="ok">${p.proven_sheets}</b> of ${p.total_sheets} sheets proven to use
        the fewest possible cuts.</div>` +
      (better.length
        ? `<div><b>${better.length}</b> sheet(s) can be improved: ` +
          better.map(s => `#${s.sheet} (${s.current}\u2192${s.best})`).join(', ') + '</div>'
        : '') +
      `<div>${p.total_sheets - p.proven_sheets - better.length} sheet(s) too large to
        finish \u2014 no claim made about those.</div>`;
  } catch (e) {
    $('proof').innerHTML = '<span class="muted">' + escapeHtml(String(e.message || e)) + '</span>';
  } finally {
    btn.disabled = false;
  }
});
$('pick').onclick = $('pick2').onclick = () => $('file').click();
$('file').addEventListener('change', (e) => {
  if (e.target.files[0]) uploadFile(e.target.files[0]);
  e.target.value = '';
});
$('sample').addEventListener('click', loadSample);

let dragDepth = 0;
window.addEventListener('dragenter', (e) => {
  e.preventDefault(); dragDepth++; setHidden($('dropmask'), false);
});
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('dragleave', () => {
  if (--dragDepth <= 0) { dragDepth = 0; setHidden($('dropmask'), true); }
});
window.addEventListener('drop', (e) => {
  e.preventDefault(); dragDepth = 0; setHidden($('dropmask'), true);
  const f = e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

// Offer the sample only if one exists. Nothing loads until the user asks.
fetch('/api/sample').then(r => r.json()).then(info => {
  if (info.available) {
    const b = $('sample');
    setHidden(b, false);
    b.textContent = 'Load sample: ' + info.name;
  }
}).catch(() => {});
