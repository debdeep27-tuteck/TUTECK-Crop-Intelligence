// ═══════════════════════════════════════════════════════════
// CropAI Enterprise — Live Alerts Feed Controller
// ═══════════════════════════════════════════════════════════

// ── Read-state persistence (localStorage) ─────────────────
const READ_STORE_KEY = 'cropai_alerts_read';

function getReadSet() {
  try { return new Set(JSON.parse(localStorage.getItem(READ_STORE_KEY) || '[]')); }
  catch { return new Set(); }
}

function saveReadSet(set) {
  try { localStorage.setItem(READ_STORE_KEY, JSON.stringify([...set])); }
  catch {}
}

function alertKey(a) { return a.category + '|' + a.title; }

// ── Alert data (live from /predictions.json) ───────────────
let ALERTS_DATA = [];
let activeCategory = 'all';
let activePriority = 'all';

function getState() {
  try {
    const s = JSON.parse(localStorage.getItem('cropai_session'));
    if (s && s.state) return s.state.toLowerCase();
  } catch {}
  return new URLSearchParams(location.search).get('state')
    || localStorage.getItem('cropai_state')
    || 'rajasthan';
}

async function loadAlertsFromPredictions() {
  const container = document.getElementById('alert-feed-container');
  if (container) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-title">Loading live alerts\u2026</div></div>';
  }
  try {
    const state = getState();
    const res = await fetch('/predictions.json?state=' + encodeURIComponent(state),
                            { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const preds = data.predictions || [];
    const readSet = getReadSet();

    ALERTS_DATA = preds
      .filter(r => r.status === 'critical' || r.status === 'watch')
      .sort((a, b) => {
        if (a.status !== b.status) return a.status === 'critical' ? -1 : 1;
        return (a.anomaly || 0) - (b.anomaly || 0);
      })
      .map((r, i) => {
        const label = (r.status === 'critical' ? 'Critical' : 'Watch') + ': ' + r.crop;
        return {
          id: i + 1,
          category: 'Yield',
          title: label,
          desc: r.district + ' \u00b7 ' + r.season + ' season \u2014 yield anomaly '
            + (r.anomaly > 0 ? '+' : '') + (r.anomaly || 0).toFixed(1) + '% vs historical normal.',
          location: r.district,
          time: (r.weather_year || 'Current') + ' forecast \u00b7 XGBoost',
          priority: r.status === 'critical' ? 'High' : 'Medium',
          status: r.status,
        };
      });

    // Populate district filter from live data
    const distFilter = document.getElementById('district-filter');
    if (distFilter) {
      const dists = [...new Set(preds.map(r => r.district).filter(Boolean))].sort();
      distFilter.innerHTML = '<option value="all">All Districts</option>'
        + dists.map(d => '<option value="' + d + '">' + d + '</option>').join('');
    }

    // Update page subtitle
    const sub = document.getElementById('alert-subtitle');
    if (sub && data.summary) {
      const sm = data.summary;
      const st = state.charAt(0).toUpperCase() + state.slice(1);
      sub.textContent = 'Live XGBoost predictions \u00b7 ' + (sm.critical || 0) + ' critical \u00b7 '
        + (sm.watch || 0) + ' watch \u00b7 ' + (sm.normal || 0) + ' normal \u2014 ' + st;
    }

  } catch (err) {
    ALERTS_DATA = [];
    if (container) {
      container.innerHTML = '<div class="empty-state"><div class="empty-state-title">\u26a0 Could not load live alerts</div>'
        + '<div style="color:#888;font-size:13px;margin-top:8px;">' + err.message + '</div></div>';
    }
    return;
  }

  renderAlertFeed();
}

// ── Render ─────────────────────────────────────────────────
function renderAlertFeed() {
  const container = document.getElementById('alert-feed-container');
  if (!container) return;

  const readSet = getReadSet();
  const distEl  = document.getElementById('district-filter');
  const selDist = distEl ? distEl.value : 'all';

  const filtered = ALERTS_DATA.filter(a => {
    const matchCat  = activeCategory === 'all' || a.category.toLowerCase() === activeCategory.toLowerCase();
    const matchPri  = activePriority === 'all' || a.priority.toLowerCase() === activePriority.toLowerCase();
    const matchDist = selDist === 'all' || a.location === selDist;
    return matchCat && matchPri && matchDist;
  });

  if (!filtered.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-title">No active alerts for selected filter</div></div>';
    return;
  }

  container.innerHTML = filtered.map(a => {
    const pClass    = a.priority.toLowerCase();
    const isRead    = readSet.has(alertKey(a));
    const readStyle = isRead ? 'opacity:0.45;filter:grayscale(0.4);' : '';
    const readMark  = isRead ? '<span style="font-size:11px;color:#10B981;margin-left:6px;">\u2713 Read</span>' : '';
    const badge = a.priority === 'High'
      ? '<span class="badge badge-danger">High Priority</span>'
      : a.priority === 'Medium'
        ? '<span class="badge badge-warning">Medium Priority</span>'
        : '<span class="badge badge-info">Info</span>';

    return '<div class="alert-row-card ' + pClass + '" style="' + readStyle + '">'
      + '<div class="alert-main">'
      + '<div class="alert-title"><span>' + a.title + '</span>' + badge + readMark + '</div>'
      + '<div class="alert-desc">' + a.desc + '</div>'
      + '<div class="alert-meta">'
      + '<span>\ud83d\udccd ' + a.location + '</span>'
      + '<span>\u23f1 ' + a.time + '</span>'
      + '<span>\ud83d\udcc2 ' + a.category + '</span>'
      + '</div></div></div>';
  }).join('');
}

// ── Filters ────────────────────────────────────────────────
function filterCategory(cat, btn) {
  activeCategory = cat;
  document.querySelectorAll('.cat-tab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  renderAlertFeed();
}

function applyPriorityFilter() {
  activePriority = document.getElementById('priority-filter').value;
  renderAlertFeed();
}

// ── Mark all as read ───────────────────────────────────────
function markAllAsRead() {
  if (!ALERTS_DATA.length) {
    showToast('\u26a0 No alerts to mark as read', '#c9922a');
    return;
  }
  const readSet = getReadSet();
  ALERTS_DATA.forEach(a => readSet.add(alertKey(a)));
  saveReadSet(readSet);
  renderAlertFeed();
  showToast('\u2713 All alerts marked as read');
}

// ── Toast helper ───────────────────────────────────────────
function showToast(msg, color) {
  let toast = document.getElementById('_alert_toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = '_alert_toast';
    Object.assign(toast.style, {
      position: 'fixed', bottom: '24px', right: '24px',
      color: '#fff', padding: '10px 20px', borderRadius: '8px',
      fontSize: '13px', fontWeight: '600',
      boxShadow: '0 4px 16px rgba(0,0,0,.25)',
      zIndex: '9999', opacity: '0',
      transition: 'opacity .25s ease', pointerEvents: 'none',
    });
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.background = color || '#10B981';
  toast.style.opacity = '1';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toast.style.opacity = '0'; }, 2500);
}

// ── Init ───────────────────────────────────────────────────
loadAlertsFromPredictions();