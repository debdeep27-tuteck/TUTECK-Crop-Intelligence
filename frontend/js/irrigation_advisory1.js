// ── State detection ───────────────────────────────────────────────────────────
const STATE = new URLSearchParams(location.search).get('state')
           || localStorage.getItem('cropai_state')
           || 'tripura';

const API_BASE = "/api/irrigation";

// Sync area bigha ↔ ha (1 pucca bigha = 1/3.95 ha in Tripura)
const BIGHA_TO_HA = 1 / 3.95;  // ≈ 0.2532
document.getElementById("area_ha").addEventListener("input", e => {
  document.getElementById("area_bigha").value = +(e.target.value / BIGHA_TO_HA).toFixed(1);
});
document.getElementById("area_bigha").addEventListener("input", e => {
  document.getElementById("area_ha").value = +(e.target.value * BIGHA_TO_HA).toFixed(2);
});

// Default sowing date to 30 days ago
const d = new Date(); d.setDate(d.getDate() - 30);
document.getElementById("sowing_date").value = d.toISOString().split("T")[0];

// Default last rain date to 3 days ago
const r = new Date(); r.setDate(r.getDate() - 3);
document.getElementById("last_rain_date").value = r.toISOString().split("T")[0];

// "Last significant rainfall" can never be in the future — cap the picker
// at today so users can't select an impossible date in the first place.
const _todayISO = new Date().toISOString().split("T")[0];
document.getElementById("last_rain_date").max = _todayISO;

async function runAdvise() {
  const btn = document.getElementById("run-btn");
  const icon = document.getElementById("btn-icon");
  btn.classList.add("loading");
  icon.innerHTML = `<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>`;
  document.getElementById("loading-overlay").classList.add("active");

  const district      = document.getElementById("district").value;
  const crop          = document.getElementById("crop").value;
  const sowing        = document.getElementById("sowing_date").value;
  const soil          = document.getElementById("soil_type").value;
  const irr           = document.getElementById("irrigation_method").value;
  const area          = parseFloat(document.getElementById("area_ha").value) || 1;
  const soil_feel     = document.getElementById("soil_feel").value;
  let last_rain       = document.getElementById("last_rain_date").value || null;
  const warnEl        = document.getElementById("last-rain-warning");

  if (last_rain && last_rain > _todayISO) {
    warnEl.style.display = "block";
    last_rain = null; // don't send an impossible date to the backend
  } else {
    warnEl.style.display = "none";
  }

  try {
    const resp = await fetch(`${API_BASE}/advise`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        district, crop, sowing_date: sowing, soil_type: soil,
        irrigation_method: irr, area_ha: area,
        soil_feel, last_rain_date: last_rain,
        state: STATE
      })
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    renderResults(data);
  } catch (err) {
    document.getElementById("empty-state").style.display = "none";
    document.getElementById("results-wrap").style.display = "block";
    document.getElementById("results-wrap").innerHTML =
      `<div class="error-box">⚠️ Could not connect to backend. Make sure <code>irrigation_backend.py</code> is running on port 5001.<br><br><small>${err.message}</small></div>`;
  } finally {
    btn.classList.remove("loading");
    icon.innerHTML = `<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/><polyline points="12 6 12 12 16 14"/>`;
    document.getElementById("loading-overlay").classList.remove("active");
  }
}

function urgencyIcon(u) {
  return { critical:"🚨", warning:"⚠️", caution:"🟡", good:"✅", info:"💧" }[u] || "💧";
}
function urgencyLabel(u) {
  return { critical:"IRRIGATE NOW — CRITICAL", warning:"IRRIGATE TODAY", caution:"DELAY — RAIN COMING", good:"NO IRRIGATION NEEDED", info:"MONITOR" }[u] || "MONITOR";
}
function pillClass(u) {
  return `sched-pill pill-${u}`;
}
function rowUrgencyClass(u) {
  return `sched-row ${u}`;
}
function moistureColor(pct) {
  if (pct < 30) return "#b91c1c";
  if (pct < 50) return "#b45309";
  if (pct < 70) return "#2d7a3a";
  return "#2563a8";
}
function weatherIcon(rain, prob) {
  if (rain > 20) return "🌧️";
  if (rain > 5)  return "🌦️";
  if (prob > 60) return "⛅";
  return "☀️";
}

function renderResults(d) {
  document.getElementById("empty-state").style.display = "none";
  const wrap = document.getElementById("results-wrap");
  wrap.style.display = "block";

  const s   = d.summary;
  const sch = d.schedule.slice(0, 7);
  const wx  = d.weather_forecast.slice(0, 7);

  // Banner
  const bannerClass = s.next_urgency;
  const bannerIcon  = urgencyIcon(s.next_urgency);
  const bannerTitle = urgencyLabel(s.next_urgency);

  // Stats
  const kcPct = Math.round(d.crop_kc / 1.25 * 100);
  const needLabel = d.crop_kc >= 1.1 ? "HIGH" : d.crop_kc >= 0.8 ? "MEDIUM" : "LOW";
  const needClass = d.crop_kc >= 1.1 ? "high" : d.crop_kc >= 0.8 ? "medium" : "low";

  // Moisture SVG chart
  const moisData   = sch.map(r => r.moisture_pct);
  const critLine   = 60; // representative
  const chartW     = 520, chartH = 120;
  const padL = 30, padR = 10, padT = 10, padB = 20;
  const pw = chartW - padL - padR, ph = chartH - padT - padB;
  const xs = moisData.map((_, i) => padL + (i / (moisData.length - 1)) * pw);
  const ys = moisData.map(v => padT + ph - (v / 100) * ph);
  const pathD = xs.map((x, i) => `${i===0?"M":"L"}${x},${ys[i]}`).join(" ");
  const areaD = pathD + ` L${xs[xs.length-1]},${padT+ph} L${xs[0]},${padT+ph} Z`;
  const critY  = padT + ph - (critLine / 100) * ph;

  const svgChart = `
    <svg class="moisture-chart" viewBox="0 0 ${chartW} ${chartH}">
      <defs>
        <linearGradient id="moisGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#2d7a3a" stop-opacity=".3"/>
          <stop offset="100%" stop-color="#2d7a3a" stop-opacity=".03"/>
        </linearGradient>
      </defs>
      <line x1="${padL}" y1="${critY}" x2="${chartW-padR}" y2="${critY}"
            stroke="#b91c1c" stroke-width="1" stroke-dasharray="4,3" opacity=".5"/>
      <text x="${padL-4}" y="${critY+4}" font-size="8" fill="#b91c1c" text-anchor="end" font-family="Space Mono,monospace">${critLine}%</text>
      <path d="${areaD}" fill="url(#moisGrad)"/>
      <path d="${pathD}" fill="none" stroke="#2d7a3a" stroke-width="2" stroke-linejoin="round"/>
      ${xs.map((x, i) => `
        <circle cx="${x}" cy="${ys[i]}" r="4" fill="${moistureColor(moisData[i])}" stroke="white" stroke-width="1.5"/>
        <text x="${x}" y="${padT+ph+14}" font-size="8" fill="#4a6b4a" text-anchor="middle" font-family="Space Mono,monospace">${sch[i].day_label.split(",")[0].trim().slice(0,3)}</text>
      `).join("")}
      <text x="${padL-4}" y="${padT+4}" font-size="8" fill="#4a6b4a" text-anchor="end" font-family="Space Mono,monospace">100%</text>
      <text x="${padL-4}" y="${padT+ph+4}" font-size="8" fill="#4a6b4a" text-anchor="end" font-family="Space Mono,monospace">0%</text>
    </svg>`;

  // Schedule rows
  const schedRows = sch.map(r => `
    <div class="${rowUrgencyClass(r.urgency)}">
      <div class="sched-date">${r.day_label}</div>
      <div class="sched-advice">${r.advice}</div>
      <div class="m-bar-wrap">
        <div class="m-bar-track">
          <div class="m-bar-fill" style="width:${r.moisture_pct}%;background:${moistureColor(r.moisture_pct)}"></div>
        </div>
        <span style="font-family:var(--mono);font-size:11px;font-weight:700;color:${moistureColor(r.moisture_pct)};min-width:36px;text-align:right">${r.moisture_pct}%</span>
      </div>
      <div class="sched-metric">
        <span class="val">${r.rainfall_mm > 0 ? r.rainfall_mm+"mm" : "—"}</span>
        <span class="lbl">RAIN</span>
      </div>
      <div class="sched-metric">
        ${r.irrigation_mm > 0
          ? `<span class="val" style="color:var(--sky)">${r.irrigation_mm}mm</span><span class="lbl">IRRIGATE</span>`
          : `<span class="${pillClass(r.urgency)}" style="font-size:9px">${urgencyIcon(r.urgency)} ${r.urgency.toUpperCase()}</span>`}
      </div>
    </div>`).join("");

  // Weather strip
  const wxDays = wx.map(w => `
    <div class="ws-day">
      <div class="ws-day-label">${new Date(w.date).toLocaleDateString("en-IN",{weekday:"short"})}</div>
      <div class="ws-icon">${weatherIcon(w.rainfall, w.rain_prob)}</div>
      <div class="ws-rain">${w.rainfall > 0 ? w.rainfall.toFixed(1)+"mm" : "—"}</div>
      <div class="ws-temp">${w.temp_min.toFixed(0)}–${w.temp_max.toFixed(0)}°C</div>
      <div class="ws-prob">${w.rain_prob.toFixed(0)}% rain</div>
    </div>`).join("");

  const irrEvColor = s.irrigation_events_7d > 3 ? "red" : s.irrigation_events_7d > 1 ? "amber" : "green";

  wrap.innerHTML = `
    <div class="advice-banner ${bannerClass}">
      <div class="banner-icon">${bannerIcon}</div>
      <div class="banner-content">
        <div class="banner-label">TODAY'S RECOMMENDATION · ${d.district} · ${d.crop}</div>
        <div class="banner-title">${bannerTitle}</div>
        <div class="banner-sub">${s.next_advice}</div>
      </div>
    </div>

    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-label">Current Stage</div>
        <div class="stat-val" style="font-size:16px;font-weight:700">${d.current_stage}</div>
        <div class="stat-sub">${d.days_in_field} days in field · ${d.days_to_next_stage}d to next stage</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Irrigation This Week</div>
        <div class="stat-val ${irrEvColor}">${s.irrigation_events_7d}</div>
        <div class="stat-sub">events · ${s.total_irrigation_mm} mm total · ${s.total_irrigation_m3} m³</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Expected Rainfall (7d)</div>
        <div class="stat-val blue">${s.total_rainfall_7d_mm}</div>
        <div class="stat-sub">mm forecast</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Daily ET₀</div>
        <div class="stat-val amber">${s.avg_et0_mm_day}</div>
        <div class="stat-sub">mm/day water loss · Kc = ${d.crop_kc}</div>
      </div>
    </div>

    <div class="mid-row">
      <div class="chart-card">
        <div class="card-title">📊 7-Day Soil Moisture Forecast (% of field capacity)</div>
        <div class="chart-area">${svgChart}</div>
        <div style="font-size:11px;color:var(--ink3);margin-top:10px;display:flex;align-items:center;gap:6px">
          <span style="display:inline-block;width:20px;height:2px;background:#b91c1c;border-top:1px dashed #b91c1c"></span>
          Red dashed line = irrigation trigger threshold
        </div>
      </div>

      <div class="stage-card">
        <div class="card-title">🌱 Crop Growth Stage</div>
        <div class="stage-name">${d.current_stage}</div>
        <div class="stage-days">${d.crop} · Day ${d.days_in_field} · ${d.soil_type}</div>
        <div class="stage-info-row">
          <span class="stage-info-label">Crop coefficient (Kc)</span>
          <span class="stage-info-val">${d.crop_kc}</span>
        </div>
        <div class="kc-bar-track"><div class="kc-bar-fill" style="width:${kcPct}%"></div></div>
        <div class="stage-info-row">
          <span class="stage-info-label">Est. soil moisture</span>
          <span class="stage-info-val" style="color:${moistureColor(d.estimated_moisture_pct)}">${d.estimated_moisture_pct}%</span>
        </div>
        <div class="stage-info-row">
          <span class="stage-info-label">Field capacity</span>
          <span class="stage-info-val">${d.field_capacity_pct}%</span>
        </div>
        <div class="stage-info-row">
          <span class="stage-info-label">Avail. water (TAW)</span>
          <span class="stage-info-val">${d.taw_mm} mm</span>
        </div>
        <div class="stage-info-row">
          <span class="stage-info-label">Method</span>
          <span class="stage-info-val">${d.irrigation_method}</span>
        </div>
        <div class="water-need-chips" style="margin-top:14px">
          <span class="wnc ${needClass}">💧 ${needLabel} WATER NEED</span>
          ${d.days_to_next_stage < 5 ? `<span class="wnc critical">⚡ STAGE CHANGE SOON</span>` : ""}
        </div>
      </div>
    </div>

    <div class="schedule-title">📅 7-Day Irrigation Schedule</div>
    <div class="schedule-grid">${schedRows}</div>

    <div class="weather-strip" style="margin-top:24px">
      <div class="ws-header">🌤 7-Day Weather Forecast · ${d.district}</div>
      <div class="ws-grid">${wxDays}</div>
    </div>
  `;
}