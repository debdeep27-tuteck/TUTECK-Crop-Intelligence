// ── Session / role scoping ──────────────────────────────────────────────────
function getSession() {
  try { return JSON.parse(localStorage.getItem('cropai_session')); } catch { return null; }
}
const SESSION = getSession();
const IS_DISTRICT_ADMIN = !!(SESSION && (SESSION.role || '').toLowerCase() === 'district_admin');
const IS_STATE_ADMIN = !!(SESSION && (SESSION.role || '').toLowerCase() === 'state_admin');
const IS_STATE_LOCKED = IS_DISTRICT_ADMIN || IS_STATE_ADMIN;
const ASSIGNED_DISTRICT = IS_DISTRICT_ADMIN ? (SESSION.district || '') : null;

// ── State detection ───────────────────────────────────────────────────────────
// district_admin/state_admin accounts are pinned to their assigned state —
// ignore any ?state= override or stale localStorage pick for them.
const STATE = (IS_STATE_LOCKED && SESSION.state)
  ? SESSION.state.toLowerCase()
  : (new URLSearchParams(location.search).get('state')
     || localStorage.getItem('cropai_state')
     || 'tripura');

// ── State ─────────────────────────────────────────────────────────────────────
let ALL_DATA = [];
let FILTERED = [];
let SORT_COL = 'anomaly';
let SORT_ASC = true;
let THRESHOLD = -20;
let META = {};

// ── Load predictions.json ─────────────────────────────────────────────────────
async function loadData() {
  startLoading();
  try {
    const resp = await fetch(`/predictions.json?state=${STATE}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const json = await resp.json();

    ALL_DATA = json.predictions || [];

    // district_admin only ever sees their assigned district's rows — never
    // other districts in the state, and no way to switch via the filter.
    if (IS_DISTRICT_ADMIN && ASSIGNED_DISTRICT) {
      ALL_DATA = ALL_DATA.filter(r => r.district === ASSIGNED_DISTRICT);
    }

    META = {
      generated_at: json.generated_at,
      run_date: json.run_date,
      model_version: json.model_version,
    };

    // Populate filter dropdowns
    const districts = [...new Set(ALL_DATA.map(r => r.district))].sort();
    const crops     = [...new Set(ALL_DATA.map(r => r.crop))].sort();
    const fDist = document.getElementById('f-district');
    const fCrop = document.getElementById('f-crop');
    districts.forEach(d => { const o = document.createElement('option'); o.value = d; o.textContent = d; fDist.appendChild(o); });
    crops.forEach(c => { const o = document.createElement('option'); o.value = c; o.textContent = c; fCrop.appendChild(o); });

    // Lock the district filter to their assigned district — no "All
    // districts" escape hatch, no picking a different one.
    if (IS_DISTRICT_ADMIN && ASSIGNED_DISTRICT) {
      fDist.value = ASSIGNED_DISTRICT;
      fDist.disabled = true;
    }

    // Header
    const genAt = new Date(META.generated_at);
    document.getElementById('gen-time').textContent = `Generated ${genAt.toLocaleDateString('en-IN')} ${genAt.toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit'})}`;
    document.getElementById('model-status').textContent = 'LIVE · REAL PREDICTIONS';

    FILTERED = [...ALL_DATA];
    renderTable();
    stopLoading();
  } catch (err) {
    stopLoading();
    showError(err);
  }
}

function showError(err) {
  document.getElementById('tbody').innerHTML = `
    <tr><td colspan="9">
      <div class="error-box">
        ⚠ Could not load predictions.json<br><br>
        ${err.message}<br><br>
        Make sure you have run: <strong>python generate_alerts.py</strong><br>
        predictions.json not found
      </div>
    </td></tr>`;
  document.getElementById('model-status').textContent = 'NO DATA';
  document.getElementById('gen-time').textContent = 'Run generate_alerts.py first';
}

// ── Loading bar ───────────────────────────────────────────────────────────────
let _lv = 0, _li;
function startLoading() {
  _lv = 0;
  const p = document.getElementById('lprog');
  p.style.width = '0%';
  _li = setInterval(() => { _lv = Math.min(_lv + 8, 88); p.style.width = _lv + '%'; }, 80);
}
function stopLoading() {
  clearInterval(_li);
  const p = document.getElementById('lprog');
  p.style.width = '100%';
  setTimeout(() => { p.style.width = '0%'; }, 400);
}

// ── Render ────────────────────────────────────────────────────────────────────
function anomalyColor(a) {
  if (a === null || a === undefined) return 'var(--text3)';
  if (a <= -30) return 'var(--red-bright)';
  if (a <= -20) return 'var(--amber)';
  if (a < 0)   return 'var(--text2)';
  return 'var(--green)';
}

function badgeClass(s) { return s === 'critical' ? 'b-crit' : s === 'watch' ? 'b-watch' : 'b-norm'; }
function badgeLabel(s) { return s === 'critical' ? '▲ CRITICAL' : s === 'watch' ? '◆ WATCH' : '● NORMAL'; }

function applyThreshold(rows) {
  return rows.map(r => {
    const status = (r.anomaly === null || r.anomaly === undefined) ? 'normal'
                 : r.anomaly <= (THRESHOLD - 10) ? 'critical'
                 : r.anomaly <= THRESHOLD ? 'watch' : 'normal';
    return { ...r, status };
  });
}

function renderTable() {
  // Apply threshold
  let rows = applyThreshold(FILTERED);

  // Sort
  rows.sort((a, b) => {
    const va = a[SORT_COL], vb = b[SORT_COL];
    if (typeof va === 'string') return SORT_ASC ? va.localeCompare(vb) : vb.localeCompare(va);
    return SORT_ASC ? va - vb : vb - va;
  });

  // Summary
  const crit  = rows.filter(r => r.status === 'critical').length;
  const watch = rows.filter(r => r.status === 'watch').length;
  const dists = new Set(rows.filter(r => r.status !== 'normal').map(r => r.district)).size;
  document.getElementById('s-crit').textContent  = crit;
  document.getElementById('s-watch').textContent = watch;
  document.getElementById('s-dist').textContent  = dists;
  document.getElementById('s-total').textContent = rows.length;
  document.getElementById('s-total-sub').textContent = `of ${ALL_DATA.length} total combos`;

  if (!rows.length) {
    document.getElementById('tbody').innerHTML = `<tr><td colspan="9" class="empty">
      <div class="empty-icon">◎</div><div>No results match current filters</div>
    </td></tr>`;
    return;
  }

  window._rows = rows;
  document.getElementById('tbody').innerHTML = rows.map((r, i) => {
    const hasAnom = r.anomaly !== null && r.anomaly !== undefined;
    const barW = hasAnom ? Math.min(100, Math.abs(r.anomaly) / 65 * 100) : 0;
    const barC = !hasAnom ? 'var(--text3)' : r.anomaly <= -30 ? 'var(--red)' : r.anomaly <= -20 ? 'var(--amber)' : r.anomaly < 0 ? 'var(--text3)' : 'var(--green)';
    const anomTxt = hasAnom ? `${r.anomaly > 0 ? '+' : ''}${r.anomaly.toFixed(1)}%` : 'N/A';
    const isAlert = r.status !== 'normal';
    return `<tr class="${isAlert ? 'alert-row' : ''}" onclick="showDetail(${i})">
      <td style="color:var(--text);font-weight:${isAlert?500:400}">${r.district}</td>
      <td>${r.crop}</td>
      <td style="font-family:var(--mono);font-size:11px">${r.season}</td>
      <td style="font-family:var(--mono)">${r.predicted.toFixed(2)}</td>
      <td style="font-family:var(--mono);color:var(--text3)">${r.normal.toFixed(2)}</td>
      <td>
        <div class="anom-cell">
          <div class="anom-track"><div class="anom-fill" style="width:${barW}%;background:${barC}"></div></div>
          <div class="anom-val" style="color:${anomalyColor(r.anomaly)}">${anomTxt}</div>
        </div>
      </td>
      <td><span class="badge ${badgeClass(r.status)}">${badgeLabel(r.status)}</span></td>
      <td class="wx-src">${r.weather_year || '—'}</td>
      <td style="font-family:var(--mono);font-size:10px;color:var(--text3)">→</td>
    </tr>`;
  }).join('');
}

function applyFilters() {
  const d = document.getElementById('f-district').value;
  const s = document.getElementById('f-season').value;
  const st = document.getElementById('f-status').value;
  const c = document.getElementById('f-crop').value;

  FILTERED = ALL_DATA.filter(r => {
    if (d !== 'all' && r.district !== d) return false;
    if (s !== 'all' && r.season !== s) return false;
    if (c !== 'all' && r.crop !== c) return false;
    if (st !== 'all') {
      const computed = r.anomaly <= (THRESHOLD - 10) ? 'critical' : r.anomaly <= THRESHOLD ? 'watch' : 'normal';
      if (computed !== st) return false;
    }
    return true;
  });
  renderTable();
}

function sortBy(col) {
  if (SORT_COL === col) SORT_ASC = !SORT_ASC;
  else { SORT_COL = col; SORT_ASC = true; }
  renderTable();
}

function onThreshold(v) {
  THRESHOLD = -parseInt(v);
  document.getElementById('thr-label').textContent = `−${v}%`;
  renderTable();
}

// ── Detail modal ──────────────────────────────────────────────────────────────
function getActions(r) {
  if (r.status === 'critical') {
    const acts = [
      { text: 'Initiate emergency import procurement for this crop', cls: '' },
      { text: 'Activate PMFBY crop insurance claims in this district', cls: '' },
    ];
    if (r.weather && r.weather.rain_total < 800)
      acts.push({ text: 'Issue irrigation emergency order — rainfall deficit detected', cls: '' });
    acts.push({ text: 'Alert district agriculture officer immediately', cls: '' });
    return acts;
  }
  if (r.status === 'watch') return [
    { text: 'Monitor weekly — prepare contingency import plan', cls: 'amb' },
    { text: 'Alert district agriculture officer for field verification', cls: 'amb' },
  ];
  return [
    { text: 'No intervention required — yield within normal range', cls: 'grn' },
    { text: 'Continue standard seasonal monitoring', cls: 'grn' },
  ];
}

function showDetail(i) {
  const r = window._rows[i];
  document.getElementById('d-subtitle').textContent = `${r.district} · ${r.season} · Weather year ${r.weather_year || '—'}`;
  document.getElementById('d-title').textContent = r.crop;

  const wx = r.weather || {};
  const actions = getActions(r);

  document.getElementById('d-body').innerHTML = `
    <div class="d-grid">
      <div>
        <div class="d-metric-label">Predicted yield</div>
        <div class="d-metric-val" style="color:${anomalyColor(r.anomaly)}">${r.predicted.toFixed(2)} <span style="font-size:13px;color:var(--text3)">t/ha</span></div>
      </div>
      <div>
        <div class="d-metric-label">Normal yield (5yr avg)</div>
        <div class="d-metric-val" style="color:var(--text2)">${r.normal.toFixed(2)} <span style="font-size:13px;color:var(--text3)">t/ha</span></div>
      </div>
      <div>
        <div class="d-metric-label">Yield anomaly</div>
        <div class="d-metric-val" style="color:${anomalyColor(r.anomaly)}">${(r.anomaly === null || r.anomaly === undefined) ? 'N/A' : `${r.anomaly > 0 ? '+' : ''}${r.anomaly.toFixed(1)}%`}</div>
      </div>
      <div>
        <div class="d-metric-label">Alert status</div>
        <div style="margin-top:4px"><span class="badge ${badgeClass(r.status)}" style="font-size:11px">${badgeLabel(r.status)}</span></div>
      </div>
    </div>

    <div>
      <div class="d-metric-label" style="margin-bottom:10px">Actual weather used by model (${r.weather_year || '—'})</div>
      <div class="wx-row"><span class="wx-lbl">Total rainfall</span><span class="wx-val">${wx.rain_total ?? '—'} mm</span></div>
      <div class="wx-row"><span class="wx-lbl">Rainy days (&gt;1mm)</span><span class="wx-val">${wx.rain_days ?? '—'} days</span></div>
      <div class="wx-row"><span class="wx-lbl">Mean temperature</span><span class="wx-val">${wx.temp_mean ?? '—'} °C</span></div>
      <div class="wx-row"><span class="wx-lbl">Evapotranspiration</span><span class="wx-val">${wx.et0_total ?? '—'} mm</span></div>
      <div class="wx-row"><span class="wx-lbl">Wind speed (max)</span><span class="wx-val">${wx.wind_mean ?? '—'} km/h</span></div>
      <div class="wx-row"><span class="wx-lbl">Solar radiation</span><span class="wx-val">${wx.solarrad_total ?? '—'} MJ/m²</span></div>
    </div>

    <div>
      <div class="d-metric-label" style="margin-bottom:10px">Recommended actions</div>
      <div style="display:flex;flex-direction:column;gap:7px">
        ${actions.map(a => `<div class="rec-action ${a.cls}">${a.text}</div>`).join('')}
      </div>
    </div>
  `;

  document.getElementById('overlay').classList.add('open');
}

function closeDetail(e) {
  if (e.target === document.getElementById('overlay'))
    document.getElementById('overlay').classList.remove('open');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
loadData();