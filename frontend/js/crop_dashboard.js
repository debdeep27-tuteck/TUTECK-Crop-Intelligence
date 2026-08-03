// ═══════════════════════════════════════════════════════════
// DATA
// ═══════════════════════════════════════════════════════════
// ── live caches ──────────────────────────────────────────────
// ── API / state config ─────────────────────────────────────────
// The dashboard is served by the gateway on :8085. All crop APIs must go
// through /api/crop so the gateway can route Tripura -> :5000 and
// Meghalaya -> :5002.
const STATE = (
  new URLSearchParams(window.location.search).get('state') ||
  localStorage.getItem('cropai_state') ||
  'tripura'
).toLowerCase().trim();

const BACKEND = '/api/crop';
let LAST_API_ERROR = '';

function apiUrl(path, params = {}) {
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  const url = new URL(`${BACKEND}${cleanPath}`, window.location.origin);
  url.searchParams.set('state', STATE);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  });
  return url.toString();
}

async function fetchJson(path, { timeout = 8000, params = {}, options = {} } = {}) {
  const url = apiUrl(path, params);
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(timeout), ...options });
    if (!resp.ok) {
      const body = await resp.text().catch(() => '');
      LAST_API_ERROR = `${resp.status} ${resp.statusText} → ${url}${body ? ' :: ' + body.slice(0, 250) : ''}`;
      console.warn('[Crop Dashboard API]', LAST_API_ERROR);
      return null;
    }
    return await resp.json();
  } catch (err) {
    LAST_API_ERROR = `${url} :: ${err.message || err}`;
    console.warn('[Crop Dashboard API]', LAST_API_ERROR);
    return null;
  }
}


let STATS = null;   // /stats
let MODEL_INFO = null;   // /model_info
// TREND_DATA declared below near buildTrends
let PROFILES = null;   // /profiles

// Populated dynamically after stats load
let yieldTable = {};
let crop_stats_local = {
  'Rice': 0.9, 'Jute': 8.8, 'Maize': 1.6, 'Wheat': 2.0, 'Sugarcane': 55, 'Groundnut': 1.3,
  'Arhar/Tur': 0.75, 'Moong(Green Gram)': 0.65, 'Urad': 0.68, 'Cotton(lint)': 1.4,
  'Masoor': 0.8, 'Mesta': 8.5, 'Sesamum': 0.6, 'Rapeseed &Mustard': 0.83,
};  // overwritten by /profiles on load

async function fetchStats() {
  if (STATS && STATS._v === 2) return STATS;
  try {
    const data = await fetchJson('/stats', { timeout: 10000, params: { v: 2 } });
    if (data) {
      STATS = data;
      STATS._v = 2;
      // build yieldTable & crop_stats_local from real data
      yieldTable = STATS.crop_season || {};
      return STATS;
    }
  } catch { }
  return null;
}

async function fetchModelInfo() {
  if (MODEL_INFO) return MODEL_INFO;
  try {
    const data = await fetchJson('/model_info', { timeout: 8000 });
    if (data) { MODEL_INFO = data; return MODEL_INFO; }
  } catch { }
  return null;
}

async function fetchProfiles() {
  if (PROFILES) return PROFILES;
  try {
    const data = await fetchJson('/profiles', { timeout: 8000 });
    if (data) {
      PROFILES = data;
      // Build crop_stats_local: crop → avg_yield
      Object.entries(PROFILES).forEach(([crop, p]) => {
        crop_stats_local[crop] = p.avg_yield || 1.0;
      });
      return PROFILES;
    }
  } catch { }
  return null;
}

async function fetchValidCrops() {
  try {
    const d = await fetchJson('/valid_crops', { timeout: 5000 });
    if (d) { return d.valid_crops || []; }
  } catch { }
  return [];
}

async function populateCropSelectors() {
  const crops = await fetchValidCrops();
  if (!crops.length) return;
  ['cye-crop', 'trendCropSel', 'p-crop'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = crops.map(c => `<option value="${c}">${c}</option>`).join('');
  });
}

function topN(obj, n) {
  const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, n);
  return { keys: entries.map(e => e[0]), vals: entries.map(e => +e[1]) };
}

// ═══════════════════════════════════════════════════════════
// CHART HELPERS
// ═══════════════════════════════════════════════════════════
const CHARTS = {};

// Fixed pixel heights per chart id. Chart.js's "responsive" mode resizes the
// canvas to match its *parent container's* box — if that container has no
// explicit height (as with plain grid/flex cards), the canvas and its
// container can end up growing each other in a feedback loop, which is why
// charts were rendering oversized. Wrapping every canvas in a `.chart-frame`
// with an explicit height (below) fixes this permanently.
const CHART_HEIGHTS = {
  cropFreqChart: 260,
  seasonPieChart: 260,
  yieldByCropChart: 190,
  yieldBySeasonChart: 190,
  pestImpactChart: 190,
  pestCropChart: 190,
  cyeRainChart: 240,
  cyeFertChart: 240,
  cyeMatrixChart: 240,
  soilChart: 260,
  irrigChart: 260,
  fertCropChart: 260,
  overallTrendChart: 190,
  decadeCompChart: 260,
  top5TrendChart: 190,
  singleCropTrend: 240,
  featPieChart: 260,
  modelR2Chart: 190,
  modelMapeChart: 190,
  modelRmseChart: 190,
  modelMaeChart: 190,
  histAvgChart: 260,
  compareChart: 260,
  alertDistChart: 260,
  alertAnomalyChart: 260,
};
const DEFAULT_CHART_HEIGHT = 190;

function mkChart(id, cfg) {
  if (CHARTS[id]) CHARTS[id].destroy();
  const canvas = document.getElementById(id);
  if (!canvas) return null;

  // Ensure the canvas lives inside a height-constrained frame so Chart.js
  // sizes itself against a stable box instead of an auto-height container.
  let frame = canvas.parentElement;
  if (!frame || !frame.classList.contains('chart-frame')) {
    frame = document.createElement('div');
    frame.className = 'chart-frame';
    canvas.parentNode.insertBefore(frame, canvas);
    frame.appendChild(canvas);
  }

  frame.style.height = (CHART_HEIGHTS[id] || DEFAULT_CHART_HEIGHT) + 'px';

  cfg.options = { ...(cfg.options || {}), responsive: true, maintainAspectRatio: false };

  CHARTS[id] = new Chart(canvas, cfg);
  return CHARTS[id];
}

const baseScales = {
  x: { grid: { color: '#e0ddd5' }, ticks: { color: '#1a1a18', font: { size: 9, family: "'DM Mono'" } } },
  y: { grid: { color: '#e0ddd5' }, ticks: { color: '#1a1a18', font: { size: 9, family: "'DM Mono'" } } }
};
const gOpts = (extra = {}) => ({
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 700, easing: 'easeOutQuart' },
  plugins: { legend: { display: false }, ...(extra.plugins || {}) },
  scales: baseScales, ...extra
});

const PALETTE = ['#4a7c59', '#2980b9', '#c9922a', '#c0392b', '#a78bfa', '#f97316', '#34d399', '#60a5fa', '#fde68a', '#f472b6'];

// ═══════════════════════════════════════════════════════════
// NAVIGATION
// ═══════════════════════════════════════════════════════════
const BUILT = { overview: false };

function showPage(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.i-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (el) el.classList.add('active');
  if (!BUILT[name]) { buildCharts(name); BUILT[name] = true; }
}

function buildCharts(name) {
  if (name === 'overview') buildOverview();
  if (name === 'eda') buildEDA();
  if (name === 'conditional') buildConditional();
  if (name === 'weather') buildWeather();
  if (name === 'trends') buildTrends();
  if (name === 'models') buildModels();
  if (name === 'predict') buildPredict();
  if (name === 'alerts') buildAlerts();
}

// ═══════════════════════════════════════════════════════════
// OVERVIEW
// ═══════════════════════════════════════════════════════════
async function buildOverview() {
  const [s, td] = await Promise.all([fetchStats(), loadTrendData()]);

  if (s) {
    const sm = s.summary;
    // Stat strip
    const gainLabel = {
      tripura: '19-Yr Gain',
      meghalaya: '15-Yr Gain',
      rajasthan: '26-Yr Gain'
    }[STATE] || 'Period Gain';
    const gainImprovement = {
      tripura: '19-Yr Improvement',
      meghalaya: '15-Yr Improvement',
      rajasthan: '26-Yr Improvement'
    }[STATE] || 'Period Gain';
    const gainPeriod = {
      tripura: '2004→2022',
      meghalaya: '2008→2022',
      rajasthan: '1997→2022'
    }[STATE] || '';
    document.querySelector('#page-overview .stat-strip').innerHTML = `
      <div class="sc"><div class="sc-lbl">Records</div><div class="sc-val g">${sm.n_records.toLocaleString()}</div><div class="sc-sub">District×Year×Crop</div></div>
      <div class="sc"><div class="sc-lbl">Crops</div><div class="sc-val b">${sm.n_crops}</div><div class="sc-sub">${sm.n_seasons} seasons · ${sm.n_districts} districts</div></div>
      <div class="sc"><div class="sc-lbl">Avg Yield</div><div class="sc-val a">${parseFloat(sm.avg_yield).toFixed(2)}</div><div class="sc-sub">Tonne/Ha median</div></div>
      <div class="sc"><div class="sc-lbl">Avg Rainfall</div><div class="sc-val b">${sm.avg_rainfall ?? '—'}</div><div class="sc-sub">mm per season</div></div>
      <div class="sc"><div class="sc-lbl">Avg Temp</div><div class="sc-val a">${sm.avg_temp != null ? sm.avg_temp + '°C' : '—'}</div><div class="sc-sub">seasonal mean</div></div>
      <div class="sc"><div class="sc-lbl">Model R²</div><div class="sc-val g">0.988</div><div class="sc-sub">XGBoost best</div></div>
      <div class="sc">
  <div class="sc-lbl">${gainLabel}</div>
  <div class="sc-val g">${td ? computeOverallGain(td) : '—'}</div>
  <div class="sc-sub">${gainPeriod}</div>
</div>
    `;
    // Insight badges — derived from real data
    const bestYield = Object.entries(s.crop_yield_med).sort((a, b) => b[1] - a[1])[0];
    const bestSeason = Object.entries(s.season_yields).sort((a, b) => b[1] - a[1])[0];
    const bestSoil = Object.entries(s.soil_yield).sort((a, b) => b[1] - a[1])[0];
    const bestIrr = Object.entries(s.irr_yield).sort((a, b) => b[1] - a[1])[0];
    const hasPestData = s.pest_yield && ('Low' in s.pest_yield) && ('High' in s.pest_yield);
    const pestLow = hasPestData ? s.pest_yield['Low'] : null;
    const pestHigh = hasPestData ? s.pest_yield['High'] : null;
    const pestImpact = (hasPestData && pestHigh > 0) ? Math.round((pestLow - pestHigh) / pestHigh * 100) : null;
    const gain19 = td ? computeOverallGain(td) : '—';
    document.querySelector('#page-overview .insight-row').innerHTML = `
      <div class="ib"><div class="ib-icon">🏆</div><div><div class="ib-lbl">Best Yielding</div><div class="ib-val">${bestYield ? bestYield[0] : '—'}</div><div class="ib-sub">${bestYield ? '~' + bestYield[1].toFixed(1) + ' T/Ha median' : ''}</div></div></div>
      <div class="ib"><div class="ib-icon">🗓️</div><div><div class="ib-lbl">Best Season</div><div class="ib-val">${bestSeason ? bestSeason[0] : '—'}</div><div class="ib-sub">Highest median yield</div></div></div>
      <div class="ib"><div class="ib-icon">🪱</div><div><div class="ib-lbl">Best Soil</div><div class="ib-val">${bestSoil ? bestSoil[0] : '—'}</div><div class="ib-sub">${bestSoil ? bestSoil[1].toFixed(2) + ' T/Ha' : ''}</div></div></div>
      <div class="ib"><div class="ib-icon">💧</div><div><div class="ib-lbl">Best Irrigation</div><div class="ib-val">${bestIrr ? bestIrr[0] : '—'}</div><div class="ib-sub">${bestIrr ? bestIrr[1].toFixed(2) + ' T/Ha' : ''}</div></div></div>
      <div class="ib"><div class="ib-icon">🐛</div><div><div class="ib-lbl">Pest Impact</div><div class="ib-val">${pestImpact === null ? '—' : (pestImpact === 0 ? '0%' : '−' + pestImpact + '%')}</div><div class="ib-sub">High vs low incidence</div></div></div>
      <div class="ib"><div class="ib-icon">📈</div><div><div class="ib-lbl">Yield Trend</div><div class="ib-val">${gain19}</div><div class="ib-sub">${gainImprovement}</div></div></div>
    `;

    // Crop frequency chart — top 12
    const cf = topN(s.crop_freq, 10);
    mkChart('cropFreqChart', {
      type: 'bar',
      data: {
        labels: cf.keys, datasets: [{
          label: 'Records', data: cf.vals,
          backgroundColor: PALETTE.map(c => c + 'bb'), borderColor: PALETTE, borderWidth: 1,
          borderRadius: 5, borderSkipped: false
        }]
      },
      options: {
        ...gOpts(), scales: {
          x: {
            ...baseScales.x, ticks: {
              ...baseScales.x.ticks,
              color: '#1a1a18',
              font: {
                size: 11,
                weight: '600'
              },
              maxRotation: 45,
              minRotation: 45
            }
          }, y: baseScales.y
        }
      }
    });

    // Season pie — counts
    const seasons = Object.keys(s.season_counts);
    mkChart('seasonPieChart', {
      type: 'doughnut',
      data: {
        labels: seasons, datasets: [{
          data: seasons.map(k => s.season_counts[k]),
          backgroundColor: PALETTE.map(c => c + 'cc'), borderColor: PALETTE, borderWidth: 1
        }]
      },
      options: {
        responsive: true, cutout: '55%',
        plugins: { legend: { position: 'right', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } }
      }
    });
  } else {
    // Backend offline — show placeholder
    document.querySelector('#page-overview .stat-strip').innerHTML =
      `<div class="sc" style="grid-column:1/-1;text-align:center;padding:20px"><div class="sc-lbl" style="color:var(--amber)">⚠ Crop API unavailable — check gateway :8085 and crop backend :5000/:5002</div><div class="sc-sub" style="margin-top:8px;word-break:break-all">${LAST_API_ERROR || 'No API response'}</div></div>`;
  }
}

function computeOverallGain(td) {
  if (!td || !td.overall || td.overall.length < 2) return '—';
  const first = td.overall[0], last = td.overall[td.overall.length - 1];
  const pct = ((last - first) / first * 100).toFixed(1);
  return (pct >= 0 ? '+' : '') + pct + '%';
}

// ═══════════════════════════════════════════════════════════
// EDA
// ═══════════════════════════════════════════════════════════
async function buildEDA() {
  const s = await fetchStats();
  if (!s) { document.getElementById('page-eda').innerHTML += '<div class="tip" style="color:var(--amber)">⚠ Crop API unavailable — check gateway :8085 and crop backend :5000/:5002.</div>'; return; }

  const top12yields = topN(s.crop_yield_med, 12);
  mkChart('yieldByCropChart', {
    type: 'bar',
    data: {
      labels: top12yields.keys, datasets: [{
        label: 'Median Yield', data: top12yields.vals,
        backgroundColor: top12yields.vals.map(v => v > 3 ? 'rgba(34,197,94,0.7)' : 'rgba(56,189,248,0.7)'),
        borderColor: top12yields.vals.map(v => v > 3 ? '#4a7c59' : '#2980b9'),
        borderWidth: 1, borderRadius: 5, borderSkipped: false
      }]
    },
    options: {
      ...gOpts(), scales: {
        x: { ...baseScales.x, ticks: { ...baseScales.x.ticks, maxRotation: 35 } },
        y: { ...baseScales.y, type: 'logarithmic', title: { display: true, text: 'T/Ha (log)', color: '#f7f713', font: { size: 9 } } }
      }
    }
  });

  const seasons = Object.keys(s.season_yields);
  mkChart('yieldBySeasonChart', {
    type: 'bar',
    data: {
      labels: seasons, datasets: [{
        label: 'Yield', data: seasons.map(k => s.season_yields[k]),
        backgroundColor: PALETTE.map(c => c + 'bb'), borderColor: PALETTE, borderWidth: 1, borderRadius: 5, borderSkipped: false
      }]
    },
    options: {
      ...gOpts(), scales: {
        x: baseScales.x,
        y: { ...baseScales.y, type: 'logarithmic', title: { display: true, text: 'T/Ha (log)', color: '#a8a89a', font: { size: 9 } } }
      }
    }
  });

  const py = s.pest_yield;
  mkChart('pestImpactChart', {
    type: 'bar',
    data: {
      labels: ['🟢 Low Pest', '🟡 Medium Pest', '🔴 High Pest'],
      datasets: [{
        label: 'Yield', data: [py.Low || 0, py.Medium || 0, py.High || 0],
        backgroundColor: ['rgba(34,197,94,0.7)', 'rgba(251,191,36,0.7)', 'rgba(248,113,113,0.7)'],
        borderColor: ['#4a7c59', '#c9922a', '#c0392b'], borderWidth: 1, borderRadius: 6, borderSkipped: false
      }]
    },
    options: {
      ...gOpts(), scales: {
        x: baseScales.x,
        y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } }
      }
    }
  });

  // Stacked pest mix — top 8 crops by frequency, real distribution from backend
  const top8crops = topN(s.crop_freq, 8).keys;
  const pestDist = s.pest_crop_dist || {};
  mkChart('pestCropChart', {
    type: 'bar',
    data: {
      labels: top8crops, datasets: [
        { label: 'Low 🟢', data: top8crops.map(c => pestDist[c]?.Low ?? 33), backgroundColor: 'rgba(34,197,94,0.75)', borderRadius: 0 },
        { label: 'Medium 🟡', data: top8crops.map(c => pestDist[c]?.Medium ?? 34), backgroundColor: 'rgba(251,191,36,0.75)', borderRadius: 0 },
        { label: 'High 🔴', data: top8crops.map(c => pestDist[c]?.High ?? 33), backgroundColor: 'rgba(248,113,113,0.75)', borderRadius: 0 },
      ]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: true, position: 'bottom', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } },
      scales: { x: { ...baseScales.x, stacked: true }, y: { ...baseScales.y, stacked: true, max: 100 } }
    }
  });

  buildCropSeasonHeatmap(s.crop_season);
}

function buildCropSeasonHeatmap(cropSeasonData) {
  if (!cropSeasonData) { document.getElementById('cropSeasonHeatmap').innerHTML = '<div class="empty-state"><div class="ei">⚠</div>No data — backend offline</div>'; return; }
  const seasons = ['Kharif', 'Rabi', 'Whole Year', 'Summer', 'Autumn', 'Winter'];
  const crops = Object.keys(cropSeasonData);
  let html = '<table class="hm-table"><thead><tr><th>Crop</th>';
  seasons.forEach(s => html += `<th>${s}</th>`);
  html += '</tr></thead><tbody>';
  crops.forEach(crop => {
    const rowVals = seasons.map(s => cropSeasonData[crop][s]).filter(v => v != null);
    const rowMin = Math.min(...rowVals);
    const rowMax = Math.max(...rowVals);
    const rowRange = rowMax - rowMin || 1;
    html += `<tr><td style="text-align:left;font-weight:600;color:var(--text);padding-right:16px;font-size:11px;font-family:var(--body)">${crop}</td>`;
    seasons.forEach(s => {
      const val = cropSeasonData[crop][s];
      if (val == null) { html += `<td style="background:rgba(28,43,28,0.3);color:#1c2b1c">—</td>`; return; }
      const n = (val - rowMin) / rowRange;
      const g = Math.round(40 + n * 180);
      const bg = `rgba(0,${g},30,${0.12 + n * 0.72})`;
      const isBest = n > 0.95;
      const textColor = n > 0.45 ? '#d4e8d4' : '#7a9e7a';
      const border = isBest ? `border:1px solid rgba(34,197,94,0.5);` : '';
      html += `<td style="background:${bg};color:${textColor};${border}">${val.toFixed(1)}</td>`;
    });
    html += '</tr>';
  });
  html += '</tbody></table>';
  document.getElementById('cropSeasonHeatmap').innerHTML = html;
}

// ═══════════════════════════════════════════════════════════
// CONDITIONAL YIELD EXPLORER
// ═══════════════════════════════════════════════════════════
function buildConditional() { doUpdateConditional(); }

function calcYield(crop, pest, rain, temp, fert, irr, soil) {
  // Local fallback only — used when backend offline
  // crop_stats_local is populated from /profiles on startup
  const base = crop_stats_local[crop] || 1.5;
  const pm = pest === 'Low' ? 1.06 : pest === 'Medium' ? 1.01 : 0.90;
  const im = irr === 'Drip' ? 1.10 : irr === 'Canal' ? 1.02 : 0.96;
  const sm = soil === 'Red Laterite' ? 0.97 : 1.0;
  const fm = fert < 50 ? 0.88 : fert < 100 ? 0.95 : fert < 180 ? 1.04 : fert < 260 ? 1.0 : 0.93;
  const rm = rain < 100 ? 0.88 : rain < 200 ? 0.96 : rain < 350 ? 1.05 : rain < 500 ? 1.02 : 0.93;
  const tm = temp < 22 ? 0.94 : temp < 25 ? 0.99 : temp < 28 ? 1.03 : temp < 31 ? 1.0 : 0.93;
  return Math.max(0.2, base * pm * im * sm * fm * rm * tm);
}

async function fetchPrediction(crop, district, pest, rain, raindays, et0, temp, fert, irr, soil) {
  try {
    const r = await fetch(apiUrl('/predict'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        crop, district, state: STATE,
        Pest_Disease_Incidence: pest, Irrigation_Type: irr, Soil_Type: soil,
        Fertilizer_kg_per_ha: fert, weather_rain_total: rain, weather_rain_days: raindays,
        weather_et0_total: et0, weather_temp_mean: temp, weather_wind_mean: 12,
        weather_solarrad_total: 2200, 'Area (Hectare)': 500
      }),
      signal: AbortSignal.timeout(5000)
    });
    if (r.ok) {
      const d = await r.json();
      backendOnline = true;
      return d.yield;
    }
  } catch { }
  return null;
}

let cyeTimer = null;
let CYE_SCATTER = {};  // per-crop scatter cache from /stats/crop_scatter
async function fetchCropScatter(crop) {
  if (CYE_SCATTER[crop]) return CYE_SCATTER[crop];
  try {
    const r = await fetch(apiUrl('/stats/crop_scatter', { crop: crop }), { signal: AbortSignal.timeout(6000) });
    if (r.ok) { CYE_SCATTER[crop] = await r.json(); return CYE_SCATTER[crop]; }
  } catch { }
  return null;
}
function updateConditional() { clearTimeout(cyeTimer); cyeTimer = setTimeout(doUpdateConditional, 400); }

// ── LOESS smoother (tricubic kernel) for scatter trendlines ──
function loess(pts, bandwidth = 0.5) {
  if (!pts || pts.length < 3) return [];
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const n = xs.length;
  const k = Math.max(3, Math.round(bandwidth * n));
  return xs.map((x0, i) => {
    const dists = xs.map(xi => Math.abs(xi - x0)).sort((a, b) => a - b);
    const h = dists[Math.min(k - 1, n - 1)] || 1;
    let sw = 0, swx = 0, swy = 0, swxx = 0, swxy = 0;
    xs.forEach((xi, j) => {
      const u = Math.abs(xi - x0) / h;
      if (u >= 1) return;
      const w = Math.pow(1 - Math.pow(u, 3), 3);
      sw += w; swx += w * xi; swy += w * ys[j]; swxx += w * xi * xi; swxy += w * xi * ys[j];
    });
    const det = sw * swxx - swx * swx || 1;
    const b = (sw * swxy - swx * swy) / det;
    const a = (swy - b * swx) / sw;
    return { x: x0, y: a + b * x0 };
  });
}

async function doUpdateConditional() {
  const crop = document.getElementById('cye-crop').value;
  const district = document.getElementById('cye-district').value;
  const pest = document.getElementById('cye-pest').value;
  const temp = parseFloat(document.getElementById('cye-temp').value);
  const irr = document.getElementById('cye-irr').value;
  const soil = document.getElementById('cye-soil').value;
  const et0 = Math.round(temp * 30);

  // Fetch per-crop historical scatter FIRST so we can clamp sliders before predicting
  const cropScat = await fetchCropScatter(crop);

  // ── helper: derive axis bounds + clamp slider from scatter data ──
  // (defined here so it's available for pre-clamp before prediction too)
  function dataAxisBounds(pts, sliderEl, valDisplayEl, unit) {
    if (!pts || pts.length < 4) return null;
    const xs = pts.map(p => p.x).sort((a, b) => a - b);
    const ys = pts.map(p => p.y).sort((a, b) => a - b);
    const xPct = (pct) => xs[Math.max(0, Math.floor(xs.length * pct))];
    const yPct = (pct) => ys[Math.max(0, Math.floor(ys.length * pct))];
    // X: use 2nd–98th percentile of real data, with small padding
    const xLo = xPct(0.02), xHi = xPct(0.98);
    const xSpan = xHi - xLo || 1;
    const xPad = xSpan * 0.08;
    const xMin = Math.max(0, Math.floor(xLo - xPad));
    const xMax = Math.ceil(xHi + xPad);
    // Y: use 3rd–97th percentile
    const yLo = yPct(0.03), yHi = yPct(0.97);
    const ySpan = yHi - yLo || yLo * 0.2 || 0.1;
    const yPad = ySpan * 0.3;
    const yMin = Math.max(0, parseFloat((yLo - yPad).toFixed(3)));
    const yMax = parseFloat((yHi + yPad).toFixed(3));
    // Clamp + snap slider to data range
    if (sliderEl) {
      sliderEl.min = xMin;
      sliderEl.max = xMax;
      const cur = parseFloat(sliderEl.value);
      if (cur < xMin) { sliderEl.value = xMin; if (valDisplayEl) valDisplayEl.textContent = xMin + unit; }
      else if (cur > xMax) { sliderEl.value = xMax; if (valDisplayEl) valDisplayEl.textContent = xMax + unit; }
    }
    return { xMin, xMax, yMin, yMax };
  }

  // ── Pre-clamp sliders to data range BEFORE running prediction ──
  const rawRainScat = (cropScat?.rain_scatter || []).filter(p => p.x > 0 && p.x <= 3000 && p.y > 0);
  rawRainScat.sort((a, b) => a.x - b.x);
  const rawFertScat0 = (cropScat?.fert_scatter || []).filter(p => p.x >= 0 && p.x <= 800 && p.y > 0);
  rawFertScat0.sort((a, b) => a.x - b.x);

  const rainSlider = document.getElementById('cye-rain');
  const rainValDisp = document.getElementById('cye-rain-v');
  dataAxisBounds(rawRainScat, rainSlider, rainValDisp, 'mm');  // clamp only

  const fertSlider = document.getElementById('cye-fert');
  const fertValDisp = document.getElementById('cye-fert-v');
  dataAxisBounds(rawFertScat0, fertSlider, fertValDisp, 'kg');  // clamp only

  // Read slider values AFTER clamping
  const rain = parseFloat(rainSlider.value);
  const fert = parseFloat(fertSlider.value);
  const raindays = Math.round(rain / 10);

  // Central prediction using clamped rain/fert
  let pred = await fetchPrediction(crop, district, pest, rain, raindays, et0, temp, fert, irr, soil);
  const usedModel = pred !== null;
  if (!usedModel) pred = calcYield(crop, pest, rain, temp, fert, irr, soil);

  const histAvg = crop_stats_local[crop] || pred;
  const diff = ((pred - histAvg) / histAvg * 100).toFixed(1);
  document.getElementById('cye-result').textContent = pred.toFixed(2);
  const vsEl = document.getElementById('cye-vs');
  vsEl.style.display = 'inline-block';
  if (diff > 0) { vsEl.textContent = `+${diff}% vs hist avg${usedModel ? ' · model' : ' · est.'}`; vsEl.style.background = 'rgba(34,197,94,0.1)'; vsEl.style.color = '#4a7c59'; vsEl.style.border = '1px solid rgba(34,197,94,0.25)'; }
  else { vsEl.textContent = `${diff}% vs hist avg${usedModel ? ' · model' : ' · est.'}`; vsEl.style.background = 'rgba(248,113,113,0.1)'; vsEl.style.color = '#c0392b'; vsEl.style.border = '1px solid rgba(248,113,113,0.25)'; }
  const srcBadge = document.getElementById('cye-source-badge');
  if (srcBadge) { srcBadge.textContent = usedModel ? '✓ Model prediction' : '⚠ Local simulation (backend offline)'; srcBadge.style.color = usedModel ? '#4a7c59' : '#c9922a'; }

  // ── RAIN CHART ──
  // (rawRainScat already computed above)
  const rainClamped = rain;

  // Recompute bounds (for axis extents — slider already clamped above)
  const rainBounds = dataAxisBounds(rawRainScat, null, null, 'mm');
  let rainLoess, yMin, yMax, rainXMin, rainXMax;
  if (rawRainScat.length >= 4 && rainBounds) {
    rainLoess = loess(rawRainScat, 0.5);
    // Y range: expand to include pred dot if it's in-range
    yMin = rainBounds.yMin;
    yMax = rainBounds.yMax;
    rainXMin = rainBounds.xMin;
    rainXMax = rainBounds.xMax;
  } else {
    // Fallback: sweep model predictions across a sensible default range
    const rainPts = [50, 80, 100, 130, 150, 175, 200, 230, 260, 300, 350, 400, 450, 500];
    const rainYields = usedModel
      ? await Promise.all(rainPts.map(r =>
        fetchPrediction(crop, district, pest, r, Math.round(r / 10), Math.round(temp * 30), temp, fert, irr, soil)
          .then(v => v ?? calcYield(crop, pest, r, temp, fert, irr, soil))))
      : rainPts.map(r => calcYield(crop, pest, r, temp, fert, irr, soil));
    const sweepPts = rainPts.map((r, i) => ({ x: r, y: rainYields[i] }));
    rainLoess = loess(sweepPts, 0.4);
    const sweepYMin = Math.min(...rainYields), sweepYMax = Math.max(...rainYields);
    const yPad = (sweepYMax - sweepYMin) * 0.5 || pred * 0.15 || 0.1;
    yMin = Math.max(0, sweepYMin - yPad);
    yMax = sweepYMax + yPad;
    rainXMin = 50; rainXMax = 500;
  }
  // Expand Y axis to always show current prediction dot
  if (pred < yMin) yMin = Math.max(0, pred * 0.9);
  if (pred > yMax) yMax = pred * 1.1;

  const rainDatasets = [
    ...(rawRainScat.length ? [{
      label: `${crop} historical`, data: rawRainScat, type: 'scatter',
      pointRadius: 2.5, pointBackgroundColor: 'rgba(56,189,248,0.28)', pointBorderColor: 'transparent',
      pointBorderWidth: 0, showLine: false, order: 3
    }] : []),
    {
      label: 'Historical trend', data: rainLoess, type: 'line',
      borderColor: '#2980b9', backgroundColor: 'rgba(56,189,248,0.08)', fill: true,
      tension: 0.35, pointRadius: 0, borderWidth: 2.5, order: 2
    },
    {
      label: 'Current', data: [{ x: rainClamped, y: pred }], type: 'scatter',
      pointRadius: 8, pointBackgroundColor: '#4a7c59', pointBorderColor: '#fff',
      pointBorderWidth: 2, showLine: false, order: 1
    },
  ];
  mkChart('cyeRainChart', {
    type: 'scatter',
    data: { datasets: rainDatasets },
    options: {
      responsive: true, animation: { duration: 300 },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: c => c.dataset.label === 'Current'
              ? `Current (model): ${Number(c.raw.y).toFixed(3)} T/Ha`
              : `${Number(c.raw?.y ?? c.raw).toFixed(3)} T/Ha`
          }
        }
      },
      scales: {
        x: {
          ...baseScales.x, type: 'linear', min: rainXMin, max: rainXMax,
          title: { display: true, text: 'Rainfall (mm)', color: '#a8a89a', font: { size: 9 } }
        },
        y: {
          ...baseScales.y, min: parseFloat(yMin.toFixed(3)), max: parseFloat(yMax.toFixed(3)),
          title: { display: true, text: 'Yield (T/Ha)', color: '#a8a89a', font: { size: 9 } }
        }
      }
    }
  });

  // ── FERT CHART ──
  const rawFertScat = rawFertScat0;  // already computed above
  const fertBounds = dataAxisBounds(rawFertScat, null, null, 'kg');
  const fertClamped = fert;  // already clamped above

  let fertLoess, fyMin, fyMax, fertXMin, fertXMax;
  if (rawFertScat.length >= 4 && fertBounds) {
    fertLoess = loess(rawFertScat, 0.5);
    fyMin = fertBounds.yMin;
    fyMax = fertBounds.yMax;
    fertXMin = fertBounds.xMin;
    fertXMax = fertBounds.xMax;
  } else {
    const fertPts = [0, 20, 40, 60, 80, 100, 120, 150, 180, 220, 260, 300, 350, 400];
    const fertYields = usedModel
      ? await Promise.all(fertPts.map(f =>
        fetchPrediction(crop, district, pest, rain, raindays, et0, temp, f, irr, soil)
          .then(v => v ?? calcYield(crop, pest, rain, temp, f, irr, soil))))
      : fertPts.map(f => calcYield(crop, pest, rain, temp, f, irr, soil));
    const sweepPts = fertPts.map((f, i) => ({ x: f, y: fertYields[i] }));
    fertLoess = loess(sweepPts, 0.4);
    const fSweepYMin = Math.min(...fertYields), fSweepYMax = Math.max(...fertYields);
    const fyPad = (fSweepYMax - fSweepYMin) * 0.5 || pred * 0.15 || 0.1;
    fyMin = Math.max(0, fSweepYMin - fyPad);
    fyMax = fSweepYMax + fyPad;
    fertXMin = 0; fertXMax = 400;
  }
  if (pred < fyMin) fyMin = Math.max(0, pred * 0.9);
  if (pred > fyMax) fyMax = pred * 1.1;

  const fertDatasets = [
    ...(rawFertScat.length ? [{
      label: `${crop} historical`, data: rawFertScat, type: 'scatter',
      pointRadius: 2.5, pointBackgroundColor: 'rgba(249,115,22,0.28)', pointBorderColor: 'transparent',
      pointBorderWidth: 0, showLine: false, order: 3
    }] : []),
    {
      label: 'Historical trend', data: fertLoess, type: 'line',
      borderColor: '#c9922a', backgroundColor: 'rgba(251,191,36,0.08)', fill: true,
      tension: 0.35, pointRadius: 0, borderWidth: 2.5, order: 2
    },
    {
      label: 'Current', data: [{ x: fertClamped, y: pred }], type: 'scatter',
      pointRadius: 8, pointBackgroundColor: '#4a7c59', pointBorderColor: '#fff',
      pointBorderWidth: 2, showLine: false, order: 1
    },
  ];
  mkChart('cyeFertChart', {
    type: 'scatter',
    data: { datasets: fertDatasets },
    options: {
      responsive: true, animation: { duration: 300 },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: c => c.dataset.label === 'Current'
              ? `Current (model): ${Number(c.raw.y).toFixed(3)} T/Ha`
              : `${Number(c.raw?.y ?? c.raw).toFixed(3)} T/Ha`
          }
        }
      },
      scales: {
        x: {
          ...baseScales.x, type: 'linear', min: fertXMin, max: fertXMax,
          title: { display: true, text: 'Fertilizer (kg/ha)', color: '#a8a89a', font: { size: 9 } }
        },
        y: {
          ...baseScales.y, min: parseFloat(fyMin.toFixed(3)), max: parseFloat(fyMax.toFixed(3)),
          title: { display: true, text: 'Yield (T/Ha)', color: '#a8a89a', font: { size: 9 } }
        }
      }
    }
  });

  // Pest × Irrigation matrix — local formula
  const pests = ['Low', 'Medium', 'High'];
  const irrs = ['Drip', 'Canal', 'Rainfed'];
  mkChart('cyeMatrixChart', {
    type: 'bar',
    data: {
      labels: pests, datasets: irrs.map((ir, i) => ({
        label: ir, data: pests.map(p => calcYield(crop, p, rain, temp, fert, ir, soil)),
        backgroundColor: ['rgba(34,197,94,0.7)', 'rgba(56,189,248,0.7)', 'rgba(251,191,36,0.7)'][i],
        borderColor: ['#4a7c59', '#2980b9', '#c9922a'][i], borderWidth: 1, borderRadius: 4, borderSkipped: false
      }))
    },
    options: {
      responsive: true, animation: { duration: 400 },
      plugins: { legend: { display: true, position: 'top', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } },
      scales: {
        x: { ...baseScales.x, title: { display: true, text: 'Pest Level', color: '#a8a89a', font: { size: 9 } } },
        y: { ...baseScales.y, title: { display: true, text: 'Yield (T/Ha)', color: '#a8a89a', font: { size: 9 } } }
      }
    }
  });
}

// ═══════════════════════════════════════════════════════════
// WEATHER
// ═══════════════════════════════════════════════════════════
// Build a scatter + LOESS trendline chart on a given canvas
function withOpacity(rgba, a) {
  // rgba(r,g,b,1) → rgba(r,g,b,a)
  return rgba.replace(/,\s*[\d.]+\s*\)$/, `,${a})`);
}

function mkScatterOnly(canvasId, pts, color, xLabel) {
  // Pure scatter — no trendline. Mixed-crop data makes LOESS misleading here.
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (!pts || !pts.length) {
    if (CHARTS[canvasId]) CHARTS[canvasId].destroy();
    const c2d = ctx.getContext('2d');
    c2d.clearRect(0, 0, ctx.width, ctx.height);
    c2d.fillStyle = '#a8a89a'; c2d.font = '11px DM Mono';
    c2d.fillText('No data — restart backend to load scatter data', 16, 60);
    return;
  }
  const sorted = [...pts].sort((a, b) => a.x - b.x);
  const n = sorted.length;

  // X: trim top 3% / bottom 2% sparse tail so one outlier crop does not stretch the axis
  const xSorted = sorted.map(p => p.x).slice().sort((a, b) => a - b);
  const xMin = xSorted[Math.floor(n * 0.02)];
  const xMax = xSorted[Math.floor(n * 0.97)];

  // Y: clamp to P3-P97 to keep scale sensible across all crops
  const ys = sorted.map(p => p.y).slice().sort((a, b) => a - b);
  const yLo = ys[Math.floor(n * 0.03)];
  const yHi = ys[Math.floor(n * 0.97)];
  const yPad = (yHi - yLo) * 0.12 || 0.3;
  const yMin = Math.max(0, parseFloat((yLo - yPad).toFixed(2)));
  const yMax = parseFloat((yHi + yPad).toFixed(2));

  // Keep only points inside the trimmed window
  const trimmed = sorted.filter(p => p.x >= xMin && p.x <= xMax && p.y >= yMin && p.y <= yMax);
  const trend = loess(trimmed, 0.35);
  const TREND_COLORS = {
    rainfallChart: '#0808cf',   // amber
    tempChart: '#981004',       // green
    et0Chart: '#0d0352',        // purple
    fertYieldChart: '#8f5c04'   // blue
  };

  const trendColor = TREND_COLORS[canvasId] || '#c9922a';
  mkChart(canvasId, {
    type: 'scatter',
    data: {
      datasets: [
        {
          label: 'Data',
          data: trimmed,
          pointRadius: 2.5,
          pointBackgroundColor: withOpacity(color, 0.25),
          pointBorderColor: 'transparent',
          pointBorderWidth: 0,
          showLine: false,
        },
        {
          label: 'Mean Trend',
          data: trend,
          type: 'line',
          borderColor: trendColor,
          backgroundColor: 'transparent',
          borderWidth: 3,
          pointRadius: 1,
          tension: 0.35,
        }
      ]
    },
    options: {
      responsive: true, animation: { duration: 400 },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: c => {
              if (c.dataset.label === 'Mean Trend') {
                return `Mean Yield: ${Number(c.raw.y).toFixed(2)} T/Ha`;
              }

              return `${xLabel}: ${Number(c.raw.x).toFixed(2)} · Yield: ${Number(c.raw.y).toFixed(2)} T/Ha`;
            }
          }
        }
      },
      scales: {
        x: {
          ...baseScales.x, type: 'linear', min: xMin, max: xMax,
          title: { display: true, text: xLabel, color: '#a8a89a', font: { size: 9 } }
        },
        y: {
          ...baseScales.y, min: yMin, max: yMax,
          title: { display: true, text: 'Yield (T/Ha)', color: '#a8a89a', font: { size: 9 } }
        }
      }
    }
  });
}

async function buildWeather() {
  const s = await fetchStats();
  if (!s) return;

  // Scatter + LOESS for rain, temp, ET0, fert — full data, no binning
  mkScatterOnly('rainfallChart', s.rain_scatter, 'rgba(56,189,248,1)', 'Rainfall (mm)');
  mkScatterOnly('tempChart', s.temp_scatter, 'rgba(248,113,113,1)', 'Temperature (°C)');
  mkScatterOnly('et0Chart', s.et0_scatter, 'rgba(96,165,250,1)', 'ET₀ (mm)');
  mkScatterOnly('fertYieldChart', s.fert_scatter, 'rgba(249,115,22,1)', 'Fertilizer (kg/ha)');

  // Fix tooltips per chart (re-label x axis text in tooltip)
  // Soil and irrigation stay as bar charts
  const soilLabels = Object.keys(s.soil_yield);
  const soilVals = soilLabels.map(k => s.soil_yield[k]);
  mkChart('soilChart', {
    type: 'bar', data: {
      labels: soilLabels, datasets: [{
        label: 'Yield', data: soilVals,
        backgroundColor: soilVals.map((_, i) => i === 0 ? 'rgba(251,191,36,0.7)' : 'rgba(34,197,94,0.7)'),
        borderColor: soilVals.map((_, i) => i === 0 ? '#c9922a' : '#4a7c59'), borderWidth: 1, borderRadius: 8, borderSkipped: false
      }]
    },
    options: { ...gOpts(), scales: { x: baseScales.x, y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } } } }
  });

  const irrLabels = Object.keys(s.irr_yield);
  const irrVals = irrLabels.map(k => s.irr_yield[k]);
  mkChart('irrigChart', {
    type: 'bar', data: {
      labels: irrLabels, datasets: [{
        label: 'Yield', data: irrVals,
        backgroundColor: ['rgba(56,189,248,0.7)', 'rgba(34,197,94,0.7)', 'rgba(251,191,36,0.7)'].slice(0, irrLabels.length),
        borderColor: ['#2980b9', '#4a7c59', '#c9922a'].slice(0, irrLabels.length), borderWidth: 1, borderRadius: 8, borderSkipped: false
      }]
    },
    options: { ...gOpts(), scales: { x: baseScales.x, y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } } } }
  });

  // Fertilizer usage by crop — top 12 (this stays bar)
  const fertTop = topN(s.fert_usage, 12);
  mkChart('fertCropChart', {
    type: 'bar', data: {
      labels: fertTop.keys, datasets: [{
        label: 'Fert', data: fertTop.vals,
        backgroundColor: fertTop.vals.map(v => v > 180 ? 'rgba(248,113,113,0.65)' : 'rgba(34,197,94,0.65)'),
        borderColor: fertTop.vals.map(v => v > 180 ? '#c0392b' : '#4a7c59'), borderWidth: 1, borderRadius: 4, borderSkipped: false
      }]
    },
    options: { ...gOpts(), scales: { x: { ...baseScales.x, ticks: { ...baseScales.x.ticks, maxRotation: 45 } }, y: { ...baseScales.y, title: { display: true, text: 'kg/Ha', color: '#a8a89a', font: { size: 9 } } } } }
  });

  // Soil × Irrigation heatmap from real data
  const sxi = s.soil_x_irr || {};
  const allSoils = Object.keys(sxi);
  const allIrrs = [...new Set(allSoils.flatMap(s2 => Object.keys(sxi[s2])))];
  const allVals = allSoils.flatMap(s2 => allIrrs.map(ir => sxi[s2][ir] || 0));
  const mn = Math.min(...allVals), mx = Math.max(...allVals);
  let h = '<table class="hm-table" style="margin:auto"><thead><tr><th></th>' + allIrrs.map(ir => `<th>${ir}</th>`).join('') + '</tr></thead><tbody>';
  allSoils.forEach(s2 => {
    h += `<tr><th style="text-align:left;padding:6px 14px;color:var(--text);font-family:var(--body)">${s2}</th>`;
    allIrrs.forEach(ir => {
      const v = sxi[s2][ir] || 0, n = (v - mn) / (mx - mn || 1);
      h += `<td style="background:rgba(74,124,89,${0.1 + n * 0.5});color:#1a1a18;font-family:var(--mono);font-size:11px">${v.toFixed(2)}</td>`;
    });
    h += '</tr>';
  });
  h += '</tbody></table>';
  document.getElementById('soilIrrHeatmap').innerHTML = h;
}

// ═══════════════════════════════════════════════════════════
// TRENDS
// ═══════════════════════════════════════════════════════════
// ── TRENDS STATE ──
let TREND_DATA = null; // cached from /crop_trends

async function loadTrendData() {
  if (TREND_DATA) return TREND_DATA;
  try {
    const data = await fetchJson('/crop_trends', { timeout: 8000 });
    if (data) { TREND_DATA = data; return TREND_DATA; }
  } catch { }
  return null;
}

async function buildTrends() {
  ['overallTrendChart', 'singleCropTrend', 'decadeCompChart', 'top5TrendChart'].forEach(id => {
    const ctx = document.getElementById(id);
    if (ctx) { const c = ctx.getContext('2d'); c.fillStyle = '#3d5c3d'; c.font = '12px DM Mono'; c.fillText('Loading real data…', 20, 60); }
  });

  const data = await loadTrendData();

  if (!data) {
    buildTrendsFallback();
    return;
  }

  const years = data.years;

  // ── Update stat strip from real data ──
  const overall = data.overall || [];
  if (overall.length >= 2) {
    const early5 = overall.slice(0, 5).reduce((a, b) => a + b, 0) / Math.min(5, overall.length);
    const recent5 = overall.slice(-5).reduce((a, b) => a + b, 0) / Math.min(5, overall.length);
    const gain = ((recent5 - early5) / early5 * 100).toFixed(1);
    const bestYearIdx = overall.indexOf(Math.max(...overall));
    const bestYear = years[bestYearIdx] || '—';
    document.querySelector('#page-trends .stat-strip').innerHTML = `
      <div class="sc"><div class="sc-lbl">2004–2008 Avg</div><div class="sc-val a">${early5.toFixed(2)}</div><div class="sc-sub">T/Ha median</div></div>
      <div class="sc"><div class="sc-lbl">2019–2022 Avg</div><div class="sc-val g">${recent5.toFixed(2)}</div><div class="sc-sub">T/Ha median</div></div>
      <div class="sc"><div class="sc-lbl">19-Year Gain</div><div class="sc-val g">${gain >= 0 ? '+' : ''}${gain}%</div><div class="sc-sub">overall improvement</div></div>
      <div class="sc"><div class="sc-lbl">Best Year</div><div class="sc-val g">${bestYear}</div><div class="sc-sub">highest median yield</div></div>
    `;
  }

  // Overall trend
  const trendSlope = (data.overall[data.overall.length - 1] - data.overall[0]) / (years.length - 1);
  mkChart('overallTrendChart', {
    type: 'line',
    data: {
      labels: years, datasets: [
        {
          label: 'Median Yield (real data)', data: data.overall,
          borderColor: '#4a7c59', backgroundColor: 'rgba(34,197,94,0.06)',
          fill: true, tension: 0.4, pointRadius: 3, borderWidth: 2, pointBackgroundColor: '#4a7c59'
        },
        {
          label: 'Trend', data: years.map((_, i) => round2(data.overall[0] + trendSlope * i)),
          borderColor: '#2980b9', borderDash: [4, 3], pointRadius: 0, borderWidth: 1.5, tension: 0
        }
      ]
    },
    options: {
      responsive: true, animation: { duration: 700 },
      plugins: { legend: { display: true, position: 'top', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } },
      scales: { x: baseScales.x, y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } } }
    }
  });

  // Default crop selector to first available
  const trendSel = document.getElementById('trendCropSel');
  const defaultCrop = trendSel?.value || 'Rice';
  buildSingleCropChart(defaultCrop, data);

  const decadeCrops = ['Rice', 'Jute', 'Maize', 'Wheat', 'Groundnut', 'Arhar/Tur'].filter(c => data.decade[c]);
  const earlyVals = decadeCrops.map(c => data.decade[c]?.early || 0);
  const recentVals = decadeCrops.map(c => data.decade[c]?.recent || 0);
  mkChart('decadeCompChart', {
    type: 'bar',
    data: {
      labels: decadeCrops, datasets: [
        { label: '2004–2013', data: earlyVals, backgroundColor: 'rgba(90,114,90,0.6)', borderColor: '#3d5c3d', borderWidth: 1, borderRadius: 4, borderSkipped: false },
        { label: '2014–2022', data: recentVals, backgroundColor: 'rgba(34,197,94,0.65)', borderColor: '#4a7c59', borderWidth: 1, borderRadius: 4, borderSkipped: false }
      ]
    },
    options: {
      responsive: true, animation: { duration: 700 },
      plugins: { legend: { display: true, position: 'top', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } },
      scales: { x: baseScales.x, y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } } }
    }
  });

  const t5crops = ['Rice', 'Maize', 'Wheat', 'Groundnut', 'Arhar/Tur'].filter(c => data.crops[c]);
  const t5colors = ['#4a7c59', '#2980b9', '#a78bfa', '#c9922a', '#c0392b'];
  const xs5 = years.map((_, i) => i);
  mkChart('top5TrendChart', {
    type: 'line',
    data: {
      labels: years, datasets: t5crops.map((c, i) => ({
        label: c,
        data: loessTrend(xs5, data.crops[c], 0.5).map(v => round2(v)),
        borderColor: t5colors[i], backgroundColor: 'transparent',
        tension: 0.3, pointRadius: 2, borderWidth: 1.8, pointBackgroundColor: t5colors[i]
      }))
    },
    options: {
      responsive: true, animation: { duration: 700 },
      plugins: {
        legend: { display: true, position: 'top', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } },
        tooltip: { callbacks: { title: items => 'Year: ' + items[0].label, label: item => item.dataset.label + ': ' + item.raw + ' T/Ha (smoothed)' } }
      },
      scales: { x: baseScales.x, y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } } }
    }
  });
}

function round2(v) { return Math.round(v * 100) / 100; }

// ── LOESS smoother for trend arrays (xs/ys arrays, not {x,y} objects) ──────
// bandwidth: fraction of points to use for each local regression (0–1)
function loessTrend(xs, ys, bandwidth) {
  const n = xs.length;
  const bw = Math.max(3, Math.floor(bandwidth * n));
  const smoothed = new Array(n);
  for (let i = 0; i < n; i++) {
    // Find bw nearest neighbors by |x - xs[i]|
    const dists = xs.map((x, j) => ({ j, d: Math.abs(x - xs[i]) }));
    dists.sort((a, b) => a.d - b.d);
    const nbrs = dists.slice(0, bw);
    const maxD = nbrs[nbrs.length - 1].d || 1;
    // Tricube weights
    let sw = 0, swx = 0, swy = 0, swxx = 0, swxy = 0;
    for (const { j, d } of nbrs) {
      const u = d / maxD;
      const w = Math.pow(1 - u * u * u, 3);
      sw += w; swx += w * xs[j]; swy += w * ys[j];
      swxx += w * xs[j] * xs[j]; swxy += w * xs[j] * ys[j];
    }
    const det = sw * swxx - swx * swx;
    if (Math.abs(det) < 1e-10) {
      smoothed[i] = swy / sw;
    } else {
      const b = (sw * swxy - swx * swy) / det;
      const a = (swy - b * swx) / sw;
      smoothed[i] = a + b * xs[i];
    }
  }
  return smoothed;
}

function buildSingleCropChart(crop, data) {
  const td = data || TREND_DATA;
  if (td && td.crops && td.crops[crop]) {
    const years = td.years;
    const yields = td.crops[crop];
    const xs = years.map((_, i) => i);
    const loessLine = loessTrend(xs, yields, 0.5).map(v => round2(v));
    mkChart('singleCropTrend', {
      type: 'line',
      data: {
        labels: years, datasets: [
          {
            label: crop + ' (actual)', data: yields,
            borderColor: '#2980b9', backgroundColor: 'rgba(56,189,248,0.06)', fill: true,
            tension: 0.4, pointRadius: 3, borderWidth: 2, pointBackgroundColor: '#2980b9'
          },
          {
            label: 'LOESS trend', data: loessLine,
            borderColor: '#c9922a', borderDash: [4, 3], pointRadius: 0, borderWidth: 1.8, tension: 0.3,
            backgroundColor: 'transparent'
          }
        ]
      },
      options: {
        responsive: true, animation: { duration: 500 },
        plugins: { legend: { display: true, position: 'top', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } },
        scales: { x: baseScales.x, y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } } }
      }
    });
    return;
  }
  // No data available
  const ctx = document.getElementById('singleCropTrend');
  if (ctx) { const c = ctx.getContext('2d'); c.clearRect(0, 0, ctx.width, ctx.height); c.fillStyle = '#a8a89a'; c.font = '11px DM Mono'; c.fillText('No trend data for ' + crop + ' (backend offline)', 16, 60); }
}

function buildTrendsFallback() {
  // Backend offline — show a clear message on all trend charts
  ['overallTrendChart', 'singleCropTrend', 'decadeCompChart', 'top5TrendChart'].forEach(id => {
    const ctx = document.getElementById(id);
    if (!ctx) return;
    const c = ctx.getContext('2d');
    c.clearRect(0, 0, ctx.width, ctx.height);
    c.fillStyle = '#a8a89a';
    c.font = '12px DM Mono';
    c.fillText('⚠ Crop API unavailable — check gateway :8085 and crop backend :5000/:5002', 16, 60);
  });
  document.querySelector('#page-trends .stat-strip').innerHTML =
    '<div class="sc" style="grid-column:1/-1;text-align:center;padding:20px"><div class="sc-lbl" style="color:var(--amber)">⚠ Crop API unavailable — trend statistics unavailable</div></div>';
}

// ═══════════════════════════════════════════════════════════
// MODELS
// ═══════════════════════════════════════════════════════════

// ── Hardcoded fallback metrics (from actual run output) ──────────────────
// These are replaced by /model_info when backend is online.
const MODEL_METRICS_FALLBACK = {
  XGBoost: { test_all: { r2: 0.987, rmse: 0.498, mape: 9.11, mae: 0.242 }, test_core: { r2: 0.987, rmse: 0.498, mape: 9.11, mae: 0.242 }, future_all: { r2: 0.975, rmse: 0.558, mape: 10.2, mae: 0.271 }, future_core: { r2: 0.975, rmse: 0.558, mape: 10.2, mae: 0.271 } },
  RandomForest: { test_all: { r2: 0.978, rmse: 0.612, mape: 11.4, mae: 0.308 }, test_core: { r2: 0.978, rmse: 0.612, mape: 11.4, mae: 0.308 }, future_all: { r2: 0.961, rmse: 0.693, mape: 13.1, mae: 0.341 }, future_core: { r2: 0.961, rmse: 0.693, mape: 13.1, mae: 0.341 } },
  GradientBoosting: { test_all: { r2: 0.982, rmse: 0.558, mape: 10.3, mae: 0.274 }, test_core: { r2: 0.982, rmse: 0.558, mape: 10.3, mae: 0.274 }, future_all: { r2: 0.968, rmse: 0.628, mape: 11.7, mae: 0.298 }, future_core: { r2: 0.968, rmse: 0.628, mape: 11.7, mae: 0.298 } },
  Ridge: { test_all: { r2: 0.921, rmse: 1.12, mape: 18.6, mae: 0.621 }, test_core: { r2: 0.921, rmse: 1.12, mape: 18.6, mae: 0.621 }, future_all: { r2: 0.908, rmse: 1.19, mape: 20.1, mae: 0.658 }, future_core: { r2: 0.908, rmse: 1.19, mape: 20.1, mae: 0.658 } },
  SVR: { test_all: { r2: 0.943, rmse: 0.951, mape: 15.8, mae: 0.531 }, test_core: { r2: 0.943, rmse: 0.951, mape: 15.8, mae: 0.531 }, future_all: { r2: 0.931, rmse: 1.01, mape: 17.2, mae: 0.572 }, future_core: { r2: 0.931, rmse: 1.01, mape: 17.2, mae: 0.572 } },
};

const MODEL_COLORS = {
  XGBoost: '#4a7c59',
  RandomForest: '#2980b9',
  GradientBoosting: '#a78bfa',
  Ridge: '#c9922a',
  SVR: '#c0392b',
};
const MODEL_LABELS = {
  XGBoost: 'XGBoost', RandomForest: 'Random Forest', GradientBoosting: 'Grad. Boosting', Ridge: 'Ridge', SVR: 'SVR'
};

let ACTIVE_MODEL = 'XGBoost';
let ACTIVE_SPLIT = 'test'; // 'test' | 'future'
let MODEL_METRICS = null; // loaded from backend or fallback

async function buildModels() {
  // Try fetching full model_metrics from /model_info
  const mi = await fetchModelInfo();
  if (mi && mi.model_metrics) {
    MODEL_METRICS = mi.model_metrics;
  } else {
    MODEL_METRICS = MODEL_METRICS_FALLBACK;
  }

  // Build comparison charts
  buildModelComparisonCharts();
  renderModelMetricStrip();
  renderModelMetricsTable();

  // Feature importance (XGBoost only, from /model_info)
  if (mi && mi.feat_importances) {
    const featEntries = Object.entries(mi.feat_importances).slice(0, 12);
    const maxImp = featEntries[0]?.[1] || 1;
    const cont = document.getElementById('featImportanceBars');
    cont.innerHTML = '';
    featEntries.forEach(([name, val], i) => {
      const pct = (val * 100).toFixed(1), w = (val / maxImp * 100).toFixed(1);
      cont.innerHTML += `<div class="feat-row"><div class="feat-name">${name}</div><div class="feat-bar-wrap"><div class="feat-bar" style="width:${w}%;background:${PALETTE[i % PALETTE.length]};box-shadow:0 0 5px ${PALETTE[i % PALETTE.length]}44"></div></div><div class="feat-pct">${pct}%</div></div>`;
    });
    mkChart('featPieChart', {
      type: 'doughnut',
      data: {
        labels: [...featEntries.slice(0, 6).map(([n]) => n), 'Others'],
        datasets: [{
          data: [...featEntries.slice(0, 6).map(([, v]) => v), featEntries.slice(6).reduce((a, [, v]) => a + v, 0)],
          backgroundColor: [...PALETTE.slice(0, 6), '#b8d4bf'], borderColor: [...PALETTE.slice(0, 6), '#e0ddd5'], borderWidth: 1
        }]
      },
      options: {
        responsive: true, cutout: '55%',
        plugins: { legend: { position: 'right', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } }
      }
    });

    if (mi.correlations) {
      const corrEntries = Object.entries(mi.correlations).sort((a, b) => b[1] - a[1]);
      const cb = document.getElementById('corrBars');
      cb.innerHTML = '';
      const maxAbsCorr = Math.max(...corrEntries.map(([, v]) => Math.abs(v))) || 1;
      corrEntries.forEach(([name, val]) => {
        const pos = val >= 0, w = (Math.abs(val) / maxAbsCorr * 46).toFixed(1);
        cb.innerHTML += `<div class="corr-row"><div class="corr-name">${name}</div><div class="corr-track"><div class="corr-center"></div>${pos ? `<div class="corr-pos" style="width:${w}%;background:#22c55e;box-shadow:0 0 4px #4a7c5944"></div>` : `<div class="corr-neg" style="width:${w}%;background:#f87171;box-shadow:0 0 4px #c0392b44"></div>`}</div><div class="corr-val" style="color:${pos ? '#4a7c59' : '#c0392b'}">${val >= 0 ? '+' : ''}${val.toFixed(2)}</div></div>`;
      });
      document.getElementById('corrChart').style.display = 'none';
    }
  } else {
    document.getElementById('featImportanceBars').innerHTML = '<div style="font-family:var(--mono);font-size:10px;color:var(--text3);padding:10px">Feature importance requires backend · run crop_yield_with_weather.py</div>';
  }
}

function selectModel(name, btn) {
  ACTIVE_MODEL = name;
  document.querySelectorAll('#modelTabRow .fr-btn').forEach(b => {
    b.style.background = ''; b.style.borderColor = '';
  });
  btn.style.background = `rgba(74,124,89,0.18)`;
  btn.style.borderColor = `rgba(74,124,89,0.5)`;
  renderModelMetricStrip();
}

function setSplit(split) {
  ACTIVE_SPLIT = split;
  document.getElementById('split-test').style.background = split === 'test' ? 'var(--s3)' : 'transparent';
  document.getElementById('split-test').style.color = split === 'test' ? 'var(--leaf)' : 'var(--text3)';
  document.getElementById('split-future').style.background = split === 'future' ? 'var(--s3)' : 'transparent';
  document.getElementById('split-future').style.color = split === 'future' ? 'var(--leaf)' : 'var(--text3)';
  document.getElementById('ms-split').textContent = split === 'test' ? 'Test Set' : 'Holdout';
  renderModelMetricStrip();
}

function renderModelMetricStrip() {
  if (!MODEL_METRICS) return;
  const splitKey = ACTIVE_SPLIT === 'test' ? 'test_core' : 'future_core';
  const m = MODEL_METRICS[ACTIVE_MODEL]?.[splitKey];
  if (!m) return;
  document.getElementById('ms-r2').textContent = m.r2.toFixed(3);
  document.getElementById('ms-rmse').textContent = m.rmse.toFixed(3);
  document.getElementById('ms-mape').textContent = m.mape.toFixed(2) + '%';
  document.getElementById('ms-mae').textContent = m.mae.toFixed(3);
  // Colour R² green if good, amber if mediocre
  document.getElementById('ms-r2').style.color = m.r2 > 0.95 ? 'var(--leaf)' : m.r2 > 0.90 ? 'var(--amber)' : 'var(--red)';
}

function buildModelComparisonCharts() {
  if (!MODEL_METRICS) return;
  const names = Object.keys(MODEL_METRICS);
  const labels = names.map(n => MODEL_LABELS[n] || n);
  const colors = names.map(n => MODEL_COLORS[n] || '#888');
  const splitKey = 'test_core';

  const r2s = names.map(n => MODEL_METRICS[n]?.[splitKey]?.r2 ?? 0);
  const mapes = names.map(n => MODEL_METRICS[n]?.[splitKey]?.mape ?? 0);
  const rmses = names.map(n => MODEL_METRICS[n]?.[splitKey]?.rmse ?? 0);
  const maes = names.map(n => MODEL_METRICS[n]?.[splitKey]?.mae ?? 0);

  const barBase = (data, colorArr, label) => ({
    type: 'bar',
    data: { labels, datasets: [{ label, data, backgroundColor: colorArr.map(c => c + '99'), borderColor: colorArr, borderWidth: 1.5, borderRadius: 5, borderSkipped: false }] },
    options: {
      ...gOpts(), indexAxis: 'y',
      scales: { x: { ...baseScales.x }, y: { ...baseScales.y, ticks: { ...baseScales.y.ticks, font: { size: 10 } } } }
    }
  });

  mkChart('modelR2Chart', barBase(r2s, colors, 'R² Score'));
  mkChart('modelMapeChart', barBase(mapes, colors, 'MAPE %'));
  mkChart('modelRmseChart', barBase(rmses, colors, 'RMSE T/Ha'));
  mkChart('modelMaeChart', barBase(maes, colors, 'MAE T/Ha'));
}

function renderModelMetricsTable() {
  if (!MODEL_METRICS) return;
  const names = Object.keys(MODEL_METRICS);
  const best = {}; // find best (lowest mape test_core) for highlighting
  ['rmse', 'mape', 'r2', 'mae'].forEach(k => {
    if (k === 'r2') best[k] = Math.max(...names.map(n => MODEL_METRICS[n]?.test_core?.[k] ?? -99));
    else best[k] = Math.min(...names.map(n => MODEL_METRICS[n]?.test_core?.[k] ?? 99));
  });

  let h = `<table style="width:100%;border-collapse:separate;border-spacing:0;font-size:11px">
    <thead><tr>
      <th style="font-family:var(--mono);font-size:9px;letter-spacing:0.1em;color:var(--text3);text-align:left;padding:8px 12px;border-bottom:1px solid var(--border)">MODEL</th>
      <th style="font-family:var(--mono);font-size:9px;letter-spacing:0.1em;color:var(--text3);text-align:center;padding:8px 12px;border-bottom:1px solid var(--border)" colspan="4">── TEST SET (Yrs 15–17, Core Crops) ──</th>
      <th style="font-family:var(--mono);font-size:9px;letter-spacing:0.1em;color:var(--text3);text-align:center;padding:8px 12px;border-bottom:1px solid var(--border)" colspan="4">── FUTURE HOLDOUT (Yr 18, Core Crops) ──</th>
    </tr><tr>
      <th style="font-family:var(--mono);font-size:9px;color:var(--text3);text-align:left;padding:4px 12px 10px;border-bottom:2px solid var(--border)"></th>
      ${['R²', 'RMSE', 'MAPE', 'MAE', 'R²', 'RMSE', 'MAPE', 'MAE'].map(l => `<th style="font-family:var(--mono);font-size:9px;color:var(--text3);text-align:center;padding:4px 12px 10px;border-bottom:2px solid var(--border)">${l}</th>`).join('')}
    </tr></thead><tbody>`;

  names.forEach((name, i) => {
    const tc = MODEL_METRICS[name]?.test_core || {};
    const fc = MODEL_METRICS[name]?.future_core || {};
    const bg = i % 2 === 0 ? 'var(--s1)' : 'var(--s2)';
    const nameColor = MODEL_COLORS[name] || 'var(--text)';

    const cell = (val, metric, isGood) => {
      const isBest = isGood ? val === best[metric] : val === best[metric];
      const style = isBest
        ? `style="text-align:center;padding:10px 12px;font-family:var(--mono);font-weight:700;color:var(--leaf);background:rgba(74,124,89,0.08)"`
        : `style="text-align:center;padding:10px 12px;font-family:var(--mono);color:var(--text2)"`;
      const txt = metric === 'r2' ? val.toFixed(3) : metric === 'mape' ? val.toFixed(2) + '%' : val.toFixed(3);
      return `<td ${style}>${txt}${isBest ? ' ★' : ''}</td>`;
    };

    h += `<tr style="background:${bg}">
      <td style="padding:10px 12px;font-family:var(--mono);font-size:11px;color:${nameColor};font-weight:600;border-left:3px solid ${nameColor}">${MODEL_LABELS[name] || name}</td>
      ${cell(tc.r2 ?? 0, 'r2', true)}${cell(tc.rmse ?? 0, 'rmse', false)}${cell(tc.mape ?? 0, 'mape', false)}${cell(tc.mae ?? 0, 'mae', false)}
      ${cell(fc.r2 ?? 0, 'r2', true)}${cell(fc.rmse ?? 0, 'rmse', false)}${cell(fc.mape ?? 0, 'mape', false)}${cell(fc.mae ?? 0, 'mae', false)}
    </tr>`;
  });

  h += '</tbody></table>';
  document.getElementById('modelMetricsTable').innerHTML = h;
}

// ═══════════════════════════════════════════════════════════
// PREDICT
// ═══════════════════════════════════════════════════════════
let backendOnline = false;

function buildPredict() {
  checkBackend();
  updateHistChart();
}

async function checkBackend() {
  try {
    const r = await fetch(apiUrl('/health'), { signal: AbortSignal.timeout(3000) });
    backendOnline = r.ok;
  } catch { backendOnline = false; }
  const b = document.getElementById('pred-backend-badge');
  const gs = document.getElementById('backend-status');
  if (backendOnline) {
    b.textContent = 'BACKEND: ONLINE · XGBoost'; b.style.color = '#4a7c59'; b.style.borderColor = 'rgba(34,197,94,0.3)'; b.style.background = 'rgba(34,197,94,0.06)';
    if (gs) { gs.textContent = 'BACKEND ONLINE'; gs.style.color = '#4a7c59'; }
  } else {
    b.textContent = 'BACKEND: OFFLINE · local sim'; b.style.color = '#c9922a'; b.style.borderColor = 'rgba(251,191,36,0.25)';
    if (gs) { gs.textContent = 'BACKEND OFFLINE'; }
  }
}

function updateHistChart() {
  const crop = document.getElementById('p-crop')?.value || 'Rice';
  const td = TREND_DATA;
  if (td && td.crops && td.crops[crop]) {
    mkChart('histAvgChart', {
      type: 'line',
      data: {
        labels: td.years, datasets: [{
          label: crop, data: td.crops[crop],
          borderColor: '#2980b9', backgroundColor: 'rgba(56,189,248,0.06)', fill: true, tension: 0.4,
          pointRadius: 2, borderWidth: 1.5, pointBackgroundColor: '#2980b9'
        }]
      },
      options: {
        responsive: true, plugins: { legend: { display: false } },
        scales: {
          x: { ...baseScales.x, ticks: { ...baseScales.x.ticks, font: { size: 8 } } },
          y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } }
        }
      }
    });
  } else {
    // Try loading trend data then retry
    loadTrendData().then(data => {
      if (data && data.crops[crop]) {
        mkChart('histAvgChart', {
          type: 'line',
          data: {
            labels: data.years, datasets: [{
              label: crop, data: data.crops[crop],
              borderColor: '#2980b9', backgroundColor: 'rgba(56,189,248,0.06)', fill: true, tension: 0.4,
              pointRadius: 2, borderWidth: 1.5, pointBackgroundColor: '#2980b9'
            }]
          },
          options: {
            responsive: true, plugins: { legend: { display: false } },
            scales: {
              x: { ...baseScales.x, ticks: { ...baseScales.x.ticks, font: { size: 8 } } },
              y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } }
            }
          }
        });
      }
    });
  }
}

async function runPrediction() {
  const crop = document.getElementById('p-crop').value;
  const season = document.getElementById('p-season').value;
  const pest = document.getElementById('p-pest').value;
  const irr = document.getElementById('p-irr').value;
  const soil = document.getElementById('p-soil').value;
  const fert = parseFloat(document.getElementById('p-fert').value) || 120;
  const rain = parseFloat(document.getElementById('p-rain').value) || 220;
  const raindays = parseFloat(document.getElementById('p-raindays').value) || 85;
  const et0 = parseFloat(document.getElementById('p-et0').value) || 820;
  const temp = parseFloat(document.getElementById('p-temp').value) || 25;
  const district = document.getElementById('p-district').value;

  let pred, source = 'local';

  if (backendOnline) {
    try {
      const res = await fetch(apiUrl('/predict'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          crop, district, state: STATE, Season: season, Pest_Disease_Incidence: pest,
          Irrigation_Type: irr, Soil_Type: soil, Fertilizer_kg_per_ha: fert,
          weather_rain_total: rain, weather_rain_days: raindays, weather_et0_total: et0,
          weather_temp_mean: temp, 'Area (Hectare)': 500
        }),
        signal: AbortSignal.timeout(8000)
      });
      if (res.ok) { const d = await res.json(); pred = d.yield; source = 'model'; }
    } catch { }
  }

  if (!pred) {
    // Local simulation fallback using live crop_stats_local (or hardcoded multipliers)
    pred = calcYield(crop, pest, rain, temp, fert, irr, soil);
    const distM = { Dhalai: 0.97, Gomati: 1.02, Khowai: 1.0, 'North tripura': 0.98, Sepahijala: 1.03, 'South tripura': 1.01, Unakoti: 0.99, 'West tripura': 1.02 }[district] || 1.0;
    pred *= distM;
  }
  pred = Math.max(0.2, Math.round(pred * 1000) / 1000);

  document.getElementById('resultValue').textContent = pred.toFixed(3);
  document.getElementById('resultUnit').textContent = `T/Ha · ${crop} · ${season} · ${district} · ${source === 'model' ? 'XGBoost' : 'Local Sim'}`;
  document.getElementById('resultConf').style.display = 'inline-block';
  document.getElementById('resultBox').classList.add('lit');

  // histAvg: prefer live yieldTable from backend stats, else crop_stats_local
  const histAvg = (yieldTable[crop] || {})[season] || crop_stats_local[crop] || pred;
  const diff = ((pred - histAvg) / histAvg * 100).toFixed(1);
  const adviceBox = document.getElementById('adviceBox');
  adviceBox.style.display = 'block';
  const trend = diff > 0 ? `<span style="color:#22c55e">+${diff}% above</span>` : `<span style="color:#f87171">${diff}% below</span>`;
  const advices = [];
  if (pest === 'High') advices.push('🐛 <strong>Reduce pest pressure</strong> — switching to low incidence management could raise yield ~15–18%');
  if (irr === 'Rainfed') advices.push('💧 <strong>Consider drip irrigation</strong> — typically adds 10–14% yield vs rainfed');
  if (soil === 'Red Laterite') advices.push('🪱 <strong>Consider soil amendments</strong> for Red Laterite to improve nutrient retention');
  if (fert < 80) advices.push(`🧪 <strong>Increase fertilizer</strong> to 100–150 kg/ha — current ${fert} kg/ha may limit yield`);
  if (fert > 250) advices.push(`🧪 <strong>Reduce fertilizer</strong> — ${fert} kg/ha is above optimal range, wastes cost and risks soil`);
  if (rain < 150) advices.push('🌧️ <strong>Low rainfall area</strong> — irrigation is especially critical');
  if (!advices.length) advices.push('✅ Conditions look well-optimised for this crop-season combination');
  document.getElementById('adviceText').innerHTML = `<p style="margin-bottom:8px;color:var(--text2)">Predicted yield is ${trend} the historical average for ${crop} in ${season}.</p>` + advices.map(a => `<p style="margin:5px 0;color:var(--text2)">→ ${a}</p>`).join('');

  mkChart('compareChart', {
    type: 'bar',
    data: {
      labels: ['Historical Avg', 'Predicted'], datasets: [{
        label: 'T/Ha',
        data: [histAvg, pred],
        backgroundColor: ['rgba(61,92,61,0.6)', 'rgba(34,197,94,0.7)'],
        borderColor: ['#3d5c3d', '#4a7c59'], borderWidth: 1, borderRadius: 6, borderSkipped: false
      }]
    },
    options: {
      ...gOpts(), plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => `${c.raw.toFixed(3)} T/Ha` } } },
      scales: { x: baseScales.x, y: { ...baseScales.y, title: { display: true, text: 'T/Ha', color: '#a8a89a', font: { size: 9 } } } }
    }
  });

  updateHistChart();
}

// ═══════════════════════════════════════════════════════════
// ALERTS — loads predictions.json
// ═══════════════════════════════════════════════════════════
let ALL_ALERTS = [], FILTERED_ALERTS = [], ALERT_SORT = 'anomaly', ALERT_ASC = true;

async function buildAlerts() {
  try {
    const resp = await fetch(`/predictions.json?state=${encodeURIComponent(STATE)}`);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const json = await resp.json();
    ALL_ALERTS = json.predictions || [];
    FILTERED_ALERTS = [...ALL_ALERTS];

    const genAt = new Date(json.generated_at || '');
    document.getElementById('alert-gen-time').textContent = `Generated ${genAt.toLocaleDateString('en-IN')} ${genAt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}`;
    const alStatusEl = document.getElementById('alerts-status');
    if (alStatusEl) {
      alStatusEl.textContent = `ALERTS: ${ALL_ALERTS.filter(r => r.status !== 'normal').length} FLAGGED`;
      alStatusEl.style.color = '#c0392b';
    }

    const dists = [...new Set(ALL_ALERTS.map(r => r.district))].sort();
    const crops = [...new Set(ALL_ALERTS.map(r => r.crop))].sort();
    const fd = document.getElementById('al-f-district'), fc = document.getElementById('al-f-crop');
    dists.forEach(d => { const o = document.createElement('option'); o.value = d; o.textContent = d; fd.appendChild(o); });
    crops.forEach(c => { const o = document.createElement('option'); o.value = c; o.textContent = c; fc.appendChild(o); });

    renderAlertTable();
    buildAlertCharts();
  } catch (e) {
    document.getElementById('alert-tbody').innerHTML = `<tr><td colspan="8"><div class="empty-state"><div class="ei">⚠</div>Could not load predictions.json<br>Run <strong>python generate_alerts.py</strong> first.<br><small style="color:var(--text3)">${e.message}</small></div></td></tr>`;
    const alStatusElErr = document.getElementById('alerts-status');
    if (alStatusElErr) { alStatusElErr.textContent = 'ALERTS: NO DATA'; }
  }
}

function renderAlertTable() {
  let rows = [...FILTERED_ALERTS];
  rows.sort((a, b) => { const va = a[ALERT_SORT], vb = b[ALERT_SORT]; return typeof va === 'string' ? (ALERT_ASC ? va.localeCompare(vb) : vb.localeCompare(va)) : (ALERT_ASC ? va - vb : vb - va); });

  const crit = rows.filter(r => r.status === 'critical').length;
  const watch = rows.filter(r => r.status === 'watch').length;
  const dists = new Set(rows.filter(r => r.status !== 'normal').map(r => r.district)).size;
  document.getElementById('al-crit').textContent = crit;
  document.getElementById('al-watch').textContent = watch;
  document.getElementById('al-dist').textContent = dists;
  document.getElementById('al-total').textContent = rows.length;

  if (!rows.length) { document.getElementById('alert-tbody').innerHTML = '<tr><td colspan="8"><div class="empty-state"><div class="ei">◎</div>No results match filters</div></td></tr>'; return; }

  const ac = a => a <= -30 ? 'var(--red)' : a <= -20 ? 'var(--amber)' : a < 0 ? 'var(--text3)' : 'var(--leaf)';
  document.getElementById('alert-tbody').innerHTML = rows.map(r => {
    const hasAnom = r.anomaly !== null && r.anomaly !== undefined;
    const bw = hasAnom ? Math.min(100, Math.abs(r.anomaly) / 65 * 100) : 0;
    const bc = hasAnom ? (r.anomaly <= -30 ? 'var(--red)' : r.anomaly <= -20 ? 'var(--amber)' : r.anomaly < 0 ? 'var(--text3)' : 'var(--leaf)') : 'var(--text3)';
    const anomTxt = hasAnom ? `${r.anomaly > 0 ? '+' : ''}${r.anomaly.toFixed(1)}%` : 'N/A';
    const anomColor = hasAnom ? ac(r.anomaly) : 'var(--text3)';
    const badge = r.status === 'critical' ? '<span class="badge b-crit">▲ CRITICAL</span>' : r.status === 'watch' ? '<span class="badge b-watch">◆ WATCH</span>' : '<span class="badge b-norm">● NORMAL</span>';
    return `<tr><td style="color:var(--text);font-weight:${r.status !== 'normal' ? 500 : 400}">${r.district}</td><td>${r.crop}</td><td style="font-family:var(--mono);font-size:10px">${r.season}</td><td style="font-family:var(--mono)">${r.predicted.toFixed(2)}</td><td style="font-family:var(--mono);color:var(--text3)">${r.normal.toFixed(2)}</td><td><div class="anom-cell"><div class="anom-track"><div class="anom-fill" style="width:${bw}%;background:${bc}"></div></div><div class="anom-val" style="color:${anomColor}">${anomTxt}</div></div></td><td>${badge}</td><td style="font-family:var(--mono);font-size:10px;color:var(--text3)">${r.weather_year || '—'}</td></tr>`;
  }).join('');
}

function applyAlertFilters() {
  const d = document.getElementById('al-f-district').value;
  const s = document.getElementById('al-f-season').value;
  const st = document.getElementById('al-f-status').value;
  const c = document.getElementById('al-f-crop').value;
  FILTERED_ALERTS = ALL_ALERTS.filter(r => {
    if (d !== 'all' && r.district !== d) return false;
    if (s !== 'all' && r.season !== s) return false;
    if (c !== 'all' && r.crop !== c) return false;
    if (st !== 'all' && r.status !== st) return false;
    return true;
  });
  renderAlertTable();
}

function sortAlerts(col) {
  if (ALERT_SORT === col) ALERT_ASC = !ALERT_ASC; else { ALERT_SORT = col; ALERT_ASC = true; }
  renderAlertTable();
}

function buildAlertCharts() {
  // Alerts by district
  const dists = [...new Set(ALL_ALERTS.map(r => r.district))].sort();
  const critCounts = dists.map(d => ALL_ALERTS.filter(r => r.district === d && r.status === 'critical').length);
  const watchCounts = dists.map(d => ALL_ALERTS.filter(r => r.district === d && r.status === 'watch').length);
  mkChart('alertDistChart', {
    type: 'bar',
    data: {
      labels: dists, datasets: [
        { label: 'Critical', data: critCounts, backgroundColor: 'rgba(248,113,113,0.7)', borderColor: '#c0392b', borderWidth: 1, borderRadius: 4, borderSkipped: false },
        { label: 'Watch', data: watchCounts, backgroundColor: 'rgba(251,191,36,0.7)', borderColor: '#c9922a', borderWidth: 1, borderRadius: 4, borderSkipped: false }
      ]
    },
    options: {
      responsive: true, plugins: { legend: { display: true, position: 'top', labels: { boxWidth: 8, font: { size: 9 }, color: '#6b6b5e' } } },
      scales: { x: { ...baseScales.x, ticks: { ...baseScales.x.ticks, maxRotation: 30 } }, y: { ...baseScales.y, stacked: false } }
    }
  });

  // Anomaly distribution histogram
  const bins = [-60, -50, -40, -30, -20, -10, 0, 10, 20, 30];
  const labels = bins.slice(0, -1).map((b, i) => `${b} to ${bins[i + 1]}%`);
  const counts = bins.slice(0, -1).map((b, i) => ALL_ALERTS.filter(r => r.anomaly >= b && r.anomaly < bins[i + 1]).length);
  mkChart('alertAnomalyChart', {
    type: 'bar',
    data: {
      labels, datasets: [{
        label: 'Count', data: counts,
        backgroundColor: bins.slice(0, -1).map(b => b < -30 ? 'rgba(248,113,113,0.7)' : b < -20 ? 'rgba(251,191,36,0.7)' : b < 0 ? 'rgba(61,92,61,0.6)' : 'rgba(34,197,94,0.65)'),
        borderColor: bins.slice(0, -1).map(b => b < -30 ? '#c0392b' : b < -20 ? '#c9922a' : b < 0 ? '#3d5c3d' : '#4a7c59'),
        borderWidth: 1, borderRadius: 4, borderSkipped: false
      }]
    },
    options: { ...gOpts(), scales: { x: { ...baseScales.x, ticks: { ...baseScales.x.ticks, maxRotation: 35, font: { size: 8 } } }, y: { ...baseScales.y, title: { display: true, text: '# Predictions', color: '#a8a89a', font: { size: 9 } } } } }
  });
}


// ═══════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════
BUILT['overview'] = true;
// Kick off parallel pre-loads on page open
Promise.all([
  fetchProfiles(),
  loadTrendData(),
  populateCropSelectors(),
]).then(() => {
  populateSoilSelectors();
  buildOverview();
  checkBackend();
});