
// ── State detection ───────────────────────────────────────────────────────────

const STATE = new URLSearchParams(location.search).get('state')
           || localStorage.getItem('cropai_state')
           || 'tripura';

const STATE_NAME = STATE.charAt(0).toUpperCase() + STATE.slice(1);

const API_BASE = '/api/disease';

let imageB64  = null;
let imageType = 'image/jpeg';


// ── BASIC HTML ESCAPE ─────────────────────────────────────────────────────────

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}


// ── INIT ─────────────────────────────────────────────────────────────────────

document.getElementById('state-sub').textContent = `AI · ${STATE_NAME}`;


// Load supported crops for dropdown
async function loadCrops() {
  try {
    const res = await fetch(`${API_BASE}/supported_crops?state=${encodeURIComponent(STATE)}`, {
      cache: 'no-store'
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = await res.json();
    const sel = document.getElementById('crop-hint');

    // Avoid duplicate options if script reloads
    while (sel.options.length > 1) {
      sel.remove(1);
    }

    (data.crops || []).forEach(crop => {
      const option = document.createElement('option');
      option.value = crop;
      option.textContent = crop;
      sel.appendChild(option);
    });

  } catch (err) {
    console.warn('Could not load supported crops:', err);
  }
}

loadCrops();


// Health check
async function checkBackend() {
  const pill = document.getElementById('disease-status');

  try {
    const res = await fetch(`${API_BASE}/health?state=${encodeURIComponent(STATE)}`, {
      cache: 'no-store'
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = await res.json();

    if (data.groq_key_set) {
      pill.textContent = 'AI ONLINE';
      pill.classList.add('online');
      pill.style.color = '';
    } else {
      pill.textContent = 'GROQ KEY MISSING';
      pill.classList.remove('online');
      pill.style.color = 'var(--amber)';
    }

  } catch (err) {
    console.warn('Disease backend health check failed:', err);
    pill.textContent = 'BACKEND OFFLINE';
    pill.classList.remove('online');
    pill.style.color = '';
  }
}

checkBackend();
setInterval(checkBackend, 20000);


// ── FILE HANDLING ─────────────────────────────────────────────────────────────

const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});

dropZone.addEventListener('dragleave', () => {
  dropZone.classList.remove('drag-over');
});

dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');

  const file = e.dataTransfer.files[0];
  if (file) handleFile(file);
});

fileInput.addEventListener('change', e => {
  if (e.target.files[0]) handleFile(e.target.files[0]);
});

function handleFile(file) {
  if (!file.type.startsWith('image/')) {
    alert('Please upload an image file.');
    return;
  }

  if (file.size > 10 * 1024 * 1024) {
    alert('Image too large — max 10MB.');
    return;
  }

  imageType = file.type || 'image/jpeg';

  const reader = new FileReader();

  reader.onload = e => {
    const dataUrl = e.target.result;
    imageB64 = dataUrl.split(',')[1];

    document.getElementById('preview-img').src = dataUrl;
    document.getElementById('preview-wrap').style.display = 'block';
    dropZone.style.display = 'none';
    document.getElementById('run-btn').disabled = false;
  };

  reader.readAsDataURL(file);
}

function clearImage() {
  imageB64 = null;
  imageType = 'image/jpeg';

  document.getElementById('preview-wrap').style.display = 'none';
  dropZone.style.display = 'block';

  document.getElementById('run-btn').disabled = true;
  fileInput.value = '';

  document.getElementById('empty-state').style.display = 'flex';
  document.getElementById('loading-state').style.display = 'none';
  document.getElementById('results').style.display = 'none';
}


// ── RUN DETECTION ─────────────────────────────────────────────────────────────

async function runDetection() {
  if (!imageB64) return;

  const btn = document.getElementById('run-btn');
  const notes = document.getElementById('extra-notes').value.trim();
  const hint = document.getElementById('crop-hint').value;

  btn.disabled = true;
  btn.textContent = '⏳ ANALYSING…';

  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('results').style.display = 'none';
  document.getElementById('loading-state').style.display = 'flex';

  try {
    const body = {
      state: STATE,
      image_base64: imageB64,
      media_type: imageType,
      crop_hint: hint || '',
      notes: notes || ''
    };

    const res = await fetch(`${API_BASE}/detect`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(body)
    });

    let data;
    try {
      data = await res.json();
    } catch {
      data = {};
    }

    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }

    renderResults(data);

  } catch (err) {
    document.getElementById('loading-state').style.display = 'none';

    const results = document.getElementById('results');
    results.style.display = 'block';
    results.innerHTML = `
      <div class="error-box">⚠️ ${esc(err.message)}</div>
    `;

  } finally {
    btn.disabled = false;
    btn.textContent = '🔬 ANALYSE IMAGE';
  }
}


// ── RENDER RESULTS ────────────────────────────────────────────────────────────

function renderResults(d) {
  document.getElementById('loading-state').style.display = 'none';

  const status = (d.health_status || '').toLowerCase();

  let bannerCls = 'unclear';
  let bannerIcon = '🔍';

  if (status === 'healthy') {
    bannerCls = 'healthy';
    bannerIcon = '✅';
  } else if (
    status.includes('disease') ||
    status.includes('pest') ||
    status.includes('deficien') ||
    status.includes('multiple')
  ) {
    bannerCls = 'diseased';
    bannerIcon = '⚠️';
  } else if (status === 'unclear') {
    bannerCls = 'unclear';
    bannerIcon = '❓';
  }

  const urgency = d.urgency || 'None';
  const urgencyKey = urgency
    .replace(/\s+/g, '')
    .replace('Within3days', '3days')
    .replace('Withinaweek', 'week')
    .replace('Monitoringonly', 'Monitor');

  const yieldImp = d.yield_impact_estimate || '—';
  const yieldGood = yieldImp.toLowerCase().includes('negligible') || status === 'healthy';

  const diseases = d.diseases || [];
  const treatments = d.treatments || [];
  const actions = d.immediate_actions || [];
  const prevents = d.preventive_measures || [];

  const resultsEl = document.getElementById('results');
  resultsEl.style.display = 'block';

  resultsEl.innerHTML = `
    <div class="health-banner ${esc(bannerCls)}">
      <div class="banner-icon">${bannerIcon}</div>

      <div>
        <div class="banner-crop">${esc(d.crop_detected || 'Unknown crop')}</div>
        <div class="banner-status ${esc(bannerCls)}">${esc(d.health_status || '—')}</div>
        <div class="banner-conf">AI confidence: ${esc(d.confidence ?? '—')}%</div>
      </div>

      <div class="urgency-tag urgency-${esc(urgencyKey)}">${esc(urgency)}</div>
    </div>

    <div class="results-grid">

      <div class="card ${diseases.length === 0 ? 'full' : ''}">
        <div class="card-label">Diseases / Issues Detected</div>

        ${
          diseases.length === 0
            ? '<div style="color:var(--green);font-size:13px">✅ No diseases detected — crop appears healthy.</div>'
            : diseases.map(dis => `
              <div class="disease-item">
                <div class="disease-name">
                  ${esc(dis.name)}
                  <span class="severity-badge severity-${esc(dis.severity)}">${esc(dis.severity)}</span>
                </div>

                <div class="disease-conf">Confidence: ${esc(dis.confidence ?? '—')}%</div>
                <div class="disease-symptoms">${esc(dis.symptoms_observed || '')}</div>

                ${
                  (dis.affected_parts || []).length
                    ? `
                      <div class="disease-parts">
                        ${(dis.affected_parts || []).map(p => `<span class="part-tag">${esc(p)}</span>`).join('')}
                      </div>
                    `
                    : ''
                }
              </div>
            `).join('')
        }
      </div>

      ${
        diseases.length > 0
          ? `
            <div class="card">
              <div class="card-label">Yield Impact (if untreated)</div>
              <div class="yield-impact ${yieldGood ? 'good' : ''}">${esc(yieldImp)}</div>
            </div>
          `
          : ''
      }

      ${
        actions.length
          ? `
            <div class="card">
              <div class="card-label">Immediate Actions</div>
              <ul class="action-list">
                ${actions.map(a => `<li>${esc(a)}</li>`).join('')}
              </ul>
            </div>
          `
          : ''
      }

      ${
        treatments.length
          ? `
            <div class="card ${actions.length === 0 ? 'full' : ''}">
              <div class="card-label">Recommended Treatments</div>

              ${treatments.map(t => `
                <div class="treatment-item t-${esc(t.type)}">
                  <div class="treatment-type">${esc(t.type)}</div>
                  <div class="treatment-name">${esc(t.product_or_method)}</div>
                  <div class="treatment-detail">
                    ${esc(t.dosage_or_details || '')}
                    ${t.timing ? ' · ' + esc(t.timing) : ''}
                  </div>
                </div>
              `).join('')}
            </div>
          `
          : ''
      }

      ${
        prevents.length
          ? `
            <div class="card full">
              <div class="card-label">Preventive Measures</div>
              <ul class="prevent-list">
                ${prevents.map(p => `<li>${esc(p)}</li>`).join('')}
              </ul>
            </div>
          `
          : ''
      }

      ${
        d.additional_notes
          ? `
            <div class="card full">
              <div class="card-label">Additional Observations</div>
              <div class="notes-box">${esc(d.additional_notes)}</div>
            </div>
          `
          : ''
      }

    </div>

    <div style="font-size:10px;color:var(--text3);font-family:var(--mono);margin-top:8px">
      Model: ${esc(d.model_used || '—')} · Provider: ${esc(d.provider || '—')} · State: ${esc(d.state || STATE)}
    </div>
  `;
}
