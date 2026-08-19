// ── Session / role scoping ──────────────────────────────────────────────────
function getSession() {
  try { return JSON.parse(localStorage.getItem('cropai_session')); } catch { return null; }
}
const SESSION = getSession();
const IS_DISTRICT_ADMIN = !!(SESSION && (SESSION.role || '').toLowerCase() === 'district_admin');
const IS_STATE_ADMIN = !!(SESSION && (SESSION.role || '').toLowerCase() === 'state_admin');
const IS_STATE_LOCKED = IS_DISTRICT_ADMIN || IS_STATE_ADMIN;

// ── State detection ───────────────────────────────────────────────────────────
// district_admin/state_admin accounts are pinned to their assigned state —
// ignore any ?state= override or stale localStorage pick for them.
const STATE = (IS_STATE_LOCKED && SESSION.state)
  ? SESSION.state.toLowerCase()
  : (new URLSearchParams(location.search).get('state')
     || localStorage.getItem('cropai_state')
     || 'tripura');

const BACKEND = '/api/crop';
let backendOnline = false;

// ── BACKEND STATUS ────────────────────────────────────────────────────────────
async function checkBackend() {
  try {
    const r = await fetch(BACKEND + `/health?state=${STATE}`, { signal: AbortSignal.timeout(800) });
    backendOnline = r.ok;
  } catch { backendOnline = false; }
  const dot = document.getElementById('status-dot');
  const lbl = document.getElementById('status-label');
  if (backendOnline) {
    dot.style.background = '#4a7c59';
    lbl.textContent = 'Model live · ' + BACKEND;
  } else {
    dot.style.background = '#c9922a';
    lbl.textContent = 'Backend offline — run python backend.py';
  }
}
checkBackend();
setInterval(checkBackend, 5000);

// ── HELPERS ───────────────────────────────────────────────────────────────────
function getFormVal(id, chipId) {
  const el = document.getElementById(id);
  if (el && el.value) return el.value;
  if (chipId) {
    return document.querySelector(`#${chipId} .chip.active`)?.dataset.val || '';
  }
  return '';
}

function getUserInputs() {
  return {
    // Weather — matches WEATHER_FEATURES in backend
    weather_rain_days:    parseFloat(document.getElementById('rain-days').value),
    weather_et0_total:    parseFloat(document.getElementById('et0').value),
    weather_temp_mean:    parseFloat(document.getElementById('temp').value),
    weather_rain_total:   parseFloat(document.getElementById('rain-total').value),
    weather_solarrad_total: parseFloat(document.getElementById('solar').value),
    weather_wind_mean:    parseFloat(document.getElementById('wind').value),
    // Farm
    Fertilizer_kg_per_ha:  parseFloat(document.getElementById('fert').value),
    'Area (Hectare)':      parseFloat(document.getElementById('area').value),
    Soil_Type:             getFormVal('soil-select', 'soil-chips'),
    Irrigation_Type:       getFormVal('irr-select', 'irr-chips'),
    Season:                getFormVal('season-select', 'season-chips'),
    Pest_Disease_Incidence: getFormVal('pest-select', 'pest-chips'),
  };
}

// ── ANOMALY DISPLAY ───────────────────────────────────────────────────────────
function anomalyTag(anomaly) {
  const sign = anomaly > 0 ? '+' : '';
  const str  = `${sign}${anomaly.toFixed(1)}% vs normal`;
  if (anomaly >= 0)    return `<span class="anomaly-val anomaly-pos">${str}</span>`;
  if (anomaly >= -20)  return `<span class="anomaly-val anomaly-neg-mild">${str}</span>`;
  return `<span class="anomaly-val anomaly-neg-bad">${str}</span>`;
}

// ── MAIN RECOMMENDER ──────────────────────────────────────────────────────────
async function runRecommender() {
  if (!backendOnline) {
    alert('Backend is offline.\n\nRun: python backend.py\n\nThen try again.');
    return;
  }

  const btn = document.querySelector('.run-btn');
  btn.classList.add('loading');
  btn.textContent = 'Running…';

  const user     = getUserInputs();
  const district = document.getElementById('district').value;

  try {
    const res = await fetch(BACKEND + '/recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...user, district, top_n: 7, state: STATE }),
      signal: AbortSignal.timeout(15000),
    });

    if (!res.ok) throw new Error(`Backend returned ${res.status}`);
    const data = await res.json();

    renderResults(data, district, user);
  } catch (err) {
    alert('Error: ' + err.message);
  } finally {
    btn.classList.remove('loading');
    btn.textContent = 'Get Recommendations';
  }
}

// ── RENDER ────────────────────────────────────────────────────────────────────
function renderResults(data, district, user) {
  document.getElementById('empty-state').style.display = 'none';
  const container = document.getElementById('results-container');
  container.style.display = 'block';

  document.getElementById('results-meta').textContent =
    `${district} · ${user.Season} · ${user.Soil_Type}`;

  const grid = document.getElementById('results-grid');
  grid.innerHTML = '';

  data.results.forEach((r, i) => {
    const isTop = i === 0;

    const yieldNum = r.predicted;
    const yieldStr = yieldNum >= 10
      ? yieldNum.toFixed(1) + ' T/ha'
      : yieldNum.toFixed(2) + ' T/ha';

    const sourceTag = r.source === 'model'
      ? '<span class="model-tag">XGBoost</span>'
      : '<span class="hist-tag">hist. avg</span>';

    let seasonClass, seasonLabel;
    if      (r.season_fit > 0.5) { seasonClass = 'season-good';    seasonLabel = '✓ ' + user.Season; }
    else if (r.season_fit > 0)   { seasonClass = 'season-partial';  seasonLabel = '~ partial fit'; }
    else                          { seasonClass = 'season-off';      seasonLabel = '✗ off-season'; }

    const card = document.createElement('div');
    card.className = 'crop-card' + (isTop ? ' rank-1' : '');
    card.innerHTML = `
      <div class="rank-badge">${i + 1}</div>
      <div class="crop-info">
        <div class="crop-name">
          ${r.crop}
          ${isTop ? '<span class="best-tag">Best match</span>' : ''}
          ${sourceTag}
        </div>
        <div class="suit-bar-wrap">
          <div class="suit-bar" style="width:0%"></div>
        </div>
        <div class="crop-tags">
          <span class="tag ${seasonClass}">${seasonLabel}</span>
          <span class="tag">${user.Soil_Type}</span>
          <span class="tag">${user.Irrigation_Type}</span>
        </div>
      </div>
      <div class="crop-right">
        <div class="suit-pct">${r.suit_pct}%</div>
        <div class="yield-val">${yieldStr}</div>
        ${anomalyTag(r.anomaly)}
      </div>`;

    grid.appendChild(card);

    requestAnimationFrame(() => {
      setTimeout(() => {
        card.querySelector('.suit-bar').style.width = r.suit_pct + '%';
      }, 60 + i * 50);
    });
  });
}

// ── SLIDER BINDINGS ───────────────────────────────────────────────────────────
const sliders = [
  ['rain-days',   'rain-daysv',  v => v + ' days'],
  ['et0',         'et0v',        v => v + ' mm'],
  ['temp',        'tempv',       v => parseFloat(v).toFixed(1) + ' °C'],
  ['rain-total',  'rain-totalv', v => v + ' mm'],
  ['solar',       'solarv',      v => v + ' MJ/m²'],
  ['wind',        'windv',       v => parseFloat(v).toFixed(1) + ' km/h'],
  ['fert',        'fertv',       v => v + ' kg/ha'],
  ['area',        'areav',       v => v + ' ha'],
];
sliders.forEach(([id, vid, fmt]) => {
  const el = document.getElementById(id);
  const vl = document.getElementById(vid);
  const upd = () => vl.textContent = fmt(el.value);
  el.addEventListener('input', upd);
  upd();
});

// ── CHIP GROUPS ───────────────────────────────────────────────────────────────
document.querySelectorAll('.chips').forEach(group => {
  group.querySelectorAll('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      group.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
    });
  });
});