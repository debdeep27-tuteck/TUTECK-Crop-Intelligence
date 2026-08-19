// ═══════════════════════════════════════════════════════════
// CropAI Enterprise — Live Alerts Feed Controller
// ═══════════════════════════════════════════════════════════

const ALERTS_DATA = [
  {
    id: 1,
    category: 'Weather',
    title: 'High Temperature Alert',
    desc: 'Temperature expected to rise above 40°C in the next 3 days across eastern districts. Increase irrigation cycle.',
    location: 'Jaipur, Rajasthan',
    time: '10 min ago',
    priority: 'High',
    read: false
  },
  {
    id: 2,
    category: 'Pest',
    title: 'Pest Outbreak Risk',
    desc: 'High risk of aphid infestation in Wheat crops. Surveillance teams deployed for ground inspection.',
    location: 'Alwar, Rajasthan',
    time: '45 min ago',
    priority: 'High',
    read: false
  },
  {
    id: 3,
    category: 'Advisory',
    title: 'Irrigation Advisory',
    desc: 'Optimal time for irrigation based on current soil moisture and evapotranspiration rates.',
    location: 'Kota, Rajasthan',
    time: '2 hrs ago',
    priority: 'Medium',
    read: false
  },
  {
    id: 4,
    category: 'Disease',
    title: 'Disease Monitoring Advisory',
    desc: 'Regular monitoring recommended for early signs of yellow leaf rust on Rabi crops.',
    location: 'Udaipur, Rajasthan',
    time: '4 hrs ago',
    priority: 'Medium',
    read: false
  },
  {
    id: 5,
    category: 'System',
    title: 'Data Sync Completed',
    desc: 'All meteorological sensor telemetry and satellite parcel data synchronized successfully.',
    location: 'Central Gateway',
    time: '6 hrs ago',
    priority: 'Low',
    read: false
  }
];

let activeCategory = 'all';
let activePriority = 'all';

function renderAlertFeed() {
  const container = document.getElementById('alert-feed-container');
  if (!container) return;

  const filtered = ALERTS_DATA.filter(a => {
    const matchCat = activeCategory === 'all' || a.category.toLowerCase() === activeCategory.toLowerCase();
    const matchPri = activePriority === 'all' || a.priority.toLowerCase() === activePriority.toLowerCase();
    return matchCat && matchPri;
  });

  if (!filtered.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-title">No active alerts for selected filter</div></div>';
    return;
  }

  container.innerHTML = filtered.map(a => {
    const pClass = a.priority.toLowerCase();
    const badge = a.priority === 'High' ? '<span class="badge badge-danger">High Priority</span>' : a.priority === 'Medium' ? '<span class="badge badge-warning">Medium Priority</span>' : '<span class="badge badge-info">Info</span>';
    return `
      <div class="alert-row-card ${pClass}">
        <div class="alert-main">
          <div class="alert-title">
            <span>${a.title}</span>
            ${badge}
          </div>
          <div class="alert-desc">${a.desc}</div>
          <div class="alert-meta">
            <span>📍 ${a.location}</span>
            <span>⏱ ${a.time}</span>
            <span>📂 ${a.category}</span>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

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

function markAllAsRead() {
  ALERTS_DATA.forEach(a => a.read = true);
  alert('All alerts marked as read.');
  renderAlertFeed();
}

// Init
const distFilter = document.getElementById('district-filter');
if (distFilter) {
  distFilter.innerHTML = '<option value="all">All Districts</option><option value="Jaipur">Jaipur</option><option value="Alwar">Alwar</option><option value="Kota">Kota</option><option value="Udaipur">Udaipur</option>';
}
renderAlertFeed();