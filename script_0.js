
    function showToast(msg, isError = false) {
      const box = document.getElementById("toastBox");
      const toast = document.createElement("div");
      toast.className = `toast ${isError ? 'toast-error' : 'toast-success'}`;
      toast.innerHTML = isError
        ? `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> <span>${msg}</span>`
        : `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> <span>${msg}</span>`;

      box.appendChild(toast);
      setTimeout(() => {
        toast.style.transition = 'opacity 0.25s ease, transform 0.25s ease';
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(50px)';
        setTimeout(() => toast.remove(), 250);
      }, 3500);
    }

    const SESSION_KEY = "cropai_session";

    function getSession() {
      try {
        const raw = localStorage.getItem(SESSION_KEY);
        return raw ? JSON.parse(raw) : null;
      } catch {
        return null;
      }
    }

    const session = getSession();

    function authHeaders() {
      return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (session ? session.token : "")
      };
    }

    let activeTab = 'storages';
    let currentStorages = [];
    let filteredStorages = [];
    let storagePage = 1;
    const storagePageSize = 10;

    let currentPermissions = {};
    let allManagedPages = [];
    let editingStorageId = null;
    let currentYieldConfig = [];
    let currentCropFeatures = [];
    let currentDistricts = [];
    let editingCropName = null;

    // â”€â”€ INITIAL PERMISSION GATE â”€â”€
    async function checkPermissionAndInit() {
      if (!session || !session.token) {
        showDenied();
        return;
      }
      try {
        const res = await fetch("/api/auth/me", { headers: authHeaders() });
        if (!res.ok) {
          showDenied();
          return;
        }
        const data = await res.json();
        const pages = (data.permissions && data.permissions.pages) || [];
        const role = String(data.role || session.role || '').toLowerCase().trim();
        const userState = String(data.state || session.state || '').toLowerCase().trim();

        if (role === 'admin' || pages.includes('/master-data-config')) {
          document.getElementById('mainContainer').style.display = 'flex';
          loadColdStorages();
          loadRolePermissions();
          loadYieldConfig();
          loadCreditScoreAccounts();

          const allowedStates = ["rajasthan", "tripura", "meghalaya"];
          const stateSelect = document.getElementById('cropFeatureState');
          const userAllowedStates = userState ? allowedStates.filter(s => s === userState) : allowedStates;

          if (userAllowedStates.length <= 1 && userAllowedStates[0]) {
            selectCropState(userAllowedStates[0]);
            document.getElementById('statePillContainer').style.display = 'none';
          } else {
            selectCropState('rajasthan');
          }
        } else {
          showDenied();
        }
      } catch (err) {
        showDenied();
      }
    }

    function showDenied() {
      document.getElementById('mainContainer').style.display = 'none';
      document.getElementById('deniedContainer').style.display = 'flex';
    }

    // â”€â”€ TAB SWITCHING â”€â”€
    function switchTab(tab) {
      activeTab = tab;
      const isStorages = tab === 'storages';
      const isRoles = tab === 'roles';
      const isYield = tab === 'yield';
      const isCropFeatures = tab === 'crop-features';
      const isCreditScore = tab === 'credit-score';

      document.getElementById('tabBtnStorages').classList.toggle('active', isStorages);
      document.getElementById('tabBtnRoles').classList.toggle('active', isRoles);
      document.getElementById('tabBtnYield').classList.toggle('active', isYield);
      document.getElementById('tabBtnCropFeatures').classList.toggle('active', isCropFeatures);
      if (document.getElementById('tabBtnCreditScore')) document.getElementById('tabBtnCreditScore').classList.toggle('active', isCreditScore);

      document.getElementById('tabContentStorages').style.display = isStorages ? 'flex' : 'none';
      document.getElementById('tabContentRoles').style.display = isRoles ? 'flex' : 'none';
      document.getElementById('tabContentYield').style.display = isYield ? 'flex' : 'none';
      document.getElementById('tabContentCropFeatures').style.display = isCropFeatures ? 'flex' : 'none';
      if (document.getElementById('tabContentCreditScore')) document.getElementById('tabContentCreditScore').style.display = isCreditScore ? 'flex' : 'none';

      if (isCropFeatures) {
        loadCropFeatures();
        loadDistricts();
      }
      if (isCreditScore) {
        loadCreditScoreAccounts();
      }
    }

    function refreshCurrentTab() {
      if (activeTab === 'storages') loadColdStorages();
      else if (activeTab === 'roles') loadRolePermissions();
      else if (activeTab === 'yield') loadYieldConfig();
      else if (activeTab === 'crop-features') { loadCropFeatures(); loadDistricts(); }
      else if (activeTab === 'credit-score') loadCreditScoreAccounts();
      showToast("Data refreshed.");
    }

    // â”€â”€ COLD STORAGES TAB â”€â”€
    async function loadColdStorages() {
      try {
        const res = await fetch("/api/master-data/cold-storages", { headers: authHeaders() });
        if (!res.ok) throw new Error("Failed to load cold storages");
        const data = await res.json();
        currentStorages = data || [];

        // Update KPIs
        document.getElementById('kpi-total-storages').textContent = currentStorages.length;
        document.getElementById('tabBadgeStorages').textContent = currentStorages.length;
        
        // Count distinct states
        const stateSet = new Set(currentStorages.map(s => (s.state || '').toLowerCase()).filter(Boolean));
        document.getElementById('kpi-storages-sub').textContent = `${stateSet.size} active states registered`;

        const totalCap = currentStorages.reduce((sum, item) => sum + (Number(item.capacity_mt) || 0), 0);
        document.getElementById('kpi-total-capacity').textContent = totalCap.toLocaleString();

        onStorageFilterChange();
      } catch (err) {
        document.getElementById('storagesBody').innerHTML = `<tr><td colspan="8"><div class="empty-state" style="color:var(--danger)"><div class="empty-state-title">Failed to load cold storages.</div><div>${escapeHtml(err.message)}</div></div></td></tr>`;
      }
    }

    function onStorageFilterChange() {
      const q = (document.getElementById('storageSearchInput').value || '').trim().toLowerCase();
      const state = document.getElementById('storageStateFilter').value;
      const status = document.getElementById('storageStatusFilter').value;

      filteredStorages = currentStorages.filter(item => {
        if (state !== 'all' && (item.state || '').toLowerCase() !== state) return false;
        if (status !== 'all' && (item.status || 'active').toLowerCase() !== status) return false;
        if (q) {
          const matchName = (item.name || '').toLowerCase().includes(q);
          const matchDist = (item.district || '').toLowerCase().includes(q);
          const matchId = String(item.id).includes(q);
          if (!matchName && !matchDist && !matchId) return false;
        }
        return true;
      });

      storagePage = 1;
      renderStoragesTable();
    }

    function resetStorageFilters() {
      document.getElementById('storageSearchInput').value = '';
      document.getElementById('storageStateFilter').value = 'all';
      document.getElementById('storageStatusFilter').value = 'all';
      onStorageFilterChange();
    }

    function renderStoragesTable() {
      const tbody = document.getElementById('storagesBody');
      const countBadge = document.getElementById('storageCountBadge');
      const total = filteredStorages.length;
      
      countBadge.innerHTML = `Showing <strong>${total}</strong> of ${currentStorages.length} facilities`;

      if (!total) {
        tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg><div class="empty-state-title">No matching cold storage facilities found</div><div>Try adjusting your filters or search keywords.</div></div></td></tr>`;
        renderStoragePagination(0);
        return;
      }

      const totalPages = Math.ceil(total / storagePageSize);
      if (storagePage > totalPages) storagePage = totalPages;

      const startIdx = (storagePage - 1) * storagePageSize;
      const pagedItems = filteredStorages.slice(startIdx, startIdx + storagePageSize);

      tbody.innerHTML = pagedItems.map(s => {
        const isActive = (s.status || 'active').toLowerCase() === 'active';
        const lat = s.latitude !== null && s.latitude !== undefined ? Number(s.latitude).toFixed(4) : null;
        const lon = s.longitude !== null && s.longitude !== undefined ? Number(s.longitude).toFixed(4) : null;
        
        let gpsHtml = `<span style="color:#94A3B8;">â€”</span>`;
        if (lat && lon) {
          const mapUrl = `https://maps.google.com/?q=${lat},${lon}`;
          gpsHtml = `<a href="${mapUrl}" target="_blank" rel="noopener" class="gps-link" title="Open in Google Maps">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
            ${lat}, ${lon}
          </a>`;
        }

        const cap = Number(s.capacity_mt) || 0;
        const stateRaw = s.state || '';
        const stateName = stateRaw.charAt(0).toUpperCase() + stateRaw.slice(1);
        const subLoc = [s.block, s.village].filter(Boolean).join(', ');

        return `
          <tr>
            <td><span style="font-family:var(--font-mono);font-size:12px;font-weight:600;color:var(--text-muted);">#${s.id}</span></td>
            <td>
              <div class="facility-cell">
                <div class="facility-avatar">
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M9 21v-6h6v6"/></svg>
                </div>
                <div class="facility-details">
                  <div class="facility-name">${escapeHtml(s.name)}</div>
                  ${subLoc ? `<div class="facility-location-sub">${escapeHtml(subLoc)}</div>` : ''}
                </div>
              </div>
            </td>
            <td><span class="badge-state">${escapeHtml(stateName || 'â€”')}</span></td>
            <td><strong style="color:var(--text-sub);">${escapeHtml(s.district || 'â€”')}</strong></td>
            <td class="col-num">${cap.toLocaleString()}</td>
            <td class="col-center">${gpsHtml}</td>
            <td class="col-center">
              <span class="badge badge-dot ${isActive ? 'badge-active' : 'badge-inactive'}">${isActive ? 'Active' : 'Inactive'}</span>
            </td>
            <td class="col-center">
              <div style="display:inline-flex;gap:6px;">
                <button class="btn-icon" title="Edit facility" onclick="openStorageModal(${s.id})">
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="Delete facility" onclick="deleteStorage(${s.id})">
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
                </button>
              </div>
            </td>
          </tr>
        `;
      }).join('');

      renderStoragePagination(totalPages);
    }

    function renderStoragePagination(totalPages) {
      const info = document.getElementById('storagePaginationInfo');
      const controls = document.getElementById('storagePaginationControls');

      if (totalPages <= 1) {
        info.textContent = `Showing 1â€“${filteredStorages.length} of ${filteredStorages.length}`;
        controls.innerHTML = '';
        return;
      }

      const start = (storagePage - 1) * storagePageSize + 1;
      const end = Math.min(storagePage * storagePageSize, filteredStorages.length);
      info.textContent = `Showing ${start}â€“${end} of ${filteredStorages.length}`;

      let btns = `
        <button class="page-btn" onclick="goToStoragePage(${storagePage - 1})" ${storagePage === 1 ? 'disabled' : ''} title="Previous page">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
      `;

      for (let p = 1; p <= totalPages; p++) {
        if (p === 1 || p === totalPages || (p >= storagePage - 1 && p <= storagePage + 1)) {
          btns += `<button class="page-btn ${p === storagePage ? 'active' : ''}" onclick="goToStoragePage(${p})">${p}</button>`;
        } else if (p === storagePage - 2 || p === storagePage + 2) {
          btns += `<span style="padding:0 4px;color:var(--text-muted);">â€¦</span>`;
        }
      }

      btns += `
        <button class="page-btn" onclick="goToStoragePage(${storagePage + 1})" ${storagePage === totalPages ? 'disabled' : ''} title="Next page">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
        </button>
      `;

      controls.innerHTML = btns;
    }

    function goToStoragePage(p) {
      storagePage = p;
      renderStoragesTable();
    }

    function exportColdStoragesCsv() {
      if (!currentStorages.length) {
        showToast("No data available to export.", true);
        return;
      }
      const headers = ["ID", "Name", "State", "District", "Block", "Village", "Capacity_MT", "Status", "Latitude", "Longitude"];
      const rows = currentStorages.map(s => [
        s.id,
        `"${(s.name || '').replace(/"/g, '""')}"`,
        `"${(s.state || '').replace(/"/g, '""')}"`,
        `"${(s.district || '').replace(/"/g, '""')}"`,
        `"${(s.block || '').replace(/"/g, '""')}"`,
        `"${(s.village || '').replace(/"/g, '""')}"`,
        s.capacity_mt || 0,
        s.status || 'active',
        s.latitude || '',
        s.longitude || ''
      ]);
      const csvContent = "data:text/csv;charset=utf-8," + [headers.join(','), ...rows.map(e => e.join(','))].join('\n');
      const encodedUri = encodeURI(csvContent);
      const link = document.createElement("a");
      link.setAttribute("href", encodedUri);
      link.setAttribute("download", `cold_storages_master_${new Date().toISOString().slice(0,10)}.csv`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      showToast("CSV export generated successfully.");
    }

    function openStorageModal(id = null) {
      editingStorageId = id;
      const modalTitle = document.getElementById('storageModalTitle');

      if (id) {
        modalTitle.textContent = "Edit Cold Storage Facility";
        const item = currentStorages.find(s => s.id === id);
        if (item) {
          document.getElementById('modalStorageName').value = item.name || '';
          document.getElementById('modalStorageState').value = (item.state || 'tripura').toLowerCase();
          document.getElementById('modalStorageDistrict').value = item.district || '';
          document.getElementById('modalStorageBlock').value = item.block || '';
          document.getElementById('modalStorageVillage').value = item.village || '';
          document.getElementById('modalStorageCapacity').value = item.capacity_mt || 0;
          document.getElementById('modalStorageStatus').value = item.status || 'active';
          document.getElementById('modalStorageLat').value = item.latitude !== null && item.latitude !== undefined ? item.latitude : '';
          document.getElementById('modalStorageLon').value = item.longitude !== null && item.longitude !== undefined ? item.longitude : '';
        }
      } else {
        modalTitle.textContent = "Add Cold Storage Facility";
        document.getElementById('modalStorageName').value = '';
        document.getElementById('modalStorageState').value = 'rajasthan';
        document.getElementById('modalStorageDistrict').value = '';
        document.getElementById('modalStorageBlock').value = '';
        document.getElementById('modalStorageVillage').value = '';
        document.getElementById('modalStorageCapacity').value = '';
        document.getElementById('modalStorageStatus').value = 'active';
        document.getElementById('modalStorageLat').value = '';
        document.getElementById('modalStorageLon').value = '';
      }

      document.getElementById('storageModalOverlay').classList.add('show');
    }

    function closeStorageModal() {
      document.getElementById('storageModalOverlay').classList.remove('show');
      editingStorageId = null;
    }

    async function saveStorageModal() {
      const name = document.getElementById('modalStorageName').value.trim();
      const state = document.getElementById('modalStorageState').value.trim();
      const district = document.getElementById('modalStorageDistrict').value.trim();
      const block = document.getElementById('modalStorageBlock').value.trim();
      const village = document.getElementById('modalStorageVillage').value.trim();
      const capacity_mt = parseInt(document.getElementById('modalStorageCapacity').value) || 0;
      const status = document.getElementById('modalStorageStatus').value;
      const latRaw = document.getElementById('modalStorageLat').value;
      const lonRaw = document.getElementById('modalStorageLon').value;

      if (!name || !district) {
        showToast("Facility name and district are required.", true);
        return;
      }

      const payload = {
        name, state, district, block, village, capacity_mt, status,
        latitude: latRaw ? parseFloat(latRaw) : null,
        longitude: lonRaw ? parseFloat(lonRaw) : null,
      };

      try {
        let res;
        if (editingStorageId) {
          res = await fetch(`/api/master-data/cold-storages/${editingStorageId}`, {
            method: "PATCH",
            headers: authHeaders(),
            body: JSON.stringify(payload)
          });
        } else {
          res = await fetch("/api/master-data/cold-storages", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify(payload)
          });
        }

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Save failed");

        showToast(editingStorageId ? "Facility updated successfully." : "Facility registered successfully.");
        closeStorageModal();
        loadColdStorages();
      } catch (err) {
        showToast(err.message || "Failed to save facility.", true);
      }
    }

    async function deleteStorage(id) {
      if (!confirm("Are you sure you want to delete this cold storage facility? This action cannot be undone.")) return;

      try {
        const res = await fetch(`/api/master-data/cold-storages/${id}`, {
          method: "DELETE",
          headers: authHeaders()
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Delete failed");

        showToast("Facility removed.");
        loadColdStorages();
      } catch (err) {
        showToast(err.message || "Failed to delete facility.", true);
      }
    }

    // â”€â”€ ROLE PERMISSIONS TAB â”€â”€
    async function loadRolePermissions() {
      try {
        const res = await fetch("/api/master-data/role-permissions", { headers: authHeaders() });
        if (!res.ok) throw new Error("Failed to load role permissions");
        const data = await res.json();

        currentPermissions = data.permissions || {};
        allManagedPages = data.all_pages || [];

        // Update KPIs
        const roleKeys = Object.keys(currentPermissions);
        document.getElementById('kpi-total-roles').textContent = roleKeys.length;
        document.getElementById('tabBadgeRoles').textContent = roleKeys.length;
        document.getElementById('kpi-total-pages').textContent = allManagedPages.length;
        document.getElementById('rolesCountBadge').innerHTML = `<strong>${roleKeys.length}</strong> system roles configured`;

        renderRolePermissionsTable();
      } catch (err) {
        document.getElementById('rolesBody').innerHTML = `<tr><td colspan="10"><div class="empty-state" style="color:var(--danger)"><div class="empty-state-title">Failed to load permissions.</div><div>${escapeHtml(err.message)}</div></div></td></tr>`;
      }
    }

    function formatPageLabel(path) {
      const clean = path.replace('/', '').replace(/-/g, ' ');
      return clean.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    }

    function renderRolePermissionsTable() {
      const head = document.getElementById('rolesHead');
      const tbody = document.getElementById('rolesBody');
      const filter = (document.getElementById('roleSearchInput')?.value || '').trim().toLowerCase();

      // Table Header
      head.innerHTML = `
        <tr>
          <th style="width:16%;">Role</th>
          <th class="col-center" style="width:9%;">CRUD Access</th>
          ${allManagedPages.map(p => `<th class="matrix-page-head" title="${p}">${formatPageLabel(p)}</th>`).join('')}
          <th class="col-center" style="width:9%;">Action</th>
        </tr>
      `;

      let roles = Object.keys(currentPermissions).sort();
      if (filter) {
        roles = roles.filter(r => r.toLowerCase().includes(filter));
      }

      if (!roles.length) {
        tbody.innerHTML = `<tr><td colspan="${allManagedPages.length + 3}"><div class="empty-state"><div class="empty-state-title">No matching role permissions configured</div></div></td></tr>`;
        return;
      }

      tbody.innerHTML = roles.map(role => {
        const perm = currentPermissions[role] || { pages: [], crud: false };
        const pagesList = perm.pages || [];

        const pageChecks = allManagedPages.map(p => `
          <td class="col-center">
            <input type="checkbox" class="matrix-custom-check"
              data-role="${role}"
              data-page="${p}"
              ${pagesList.includes(p) ? 'checked' : ''}>
          </td>
        `).join('');

        return `
          <tr id="role-row-${role}">
            <td>
              <div style="display:flex;flex-direction:column;gap:4px;">
                <span class="badge badge-role">${role.replace(/_/g, ' ')}</span>
                <div style="display:flex;gap:4px;">
                  <button type="button" class="role-quick-btn" onclick="toggleAllRolePages('${role}', true)">All</button>
                  <button type="button" class="role-quick-btn" onclick="toggleAllRolePages('${role}', false)">None</button>
                </div>
              </div>
            </td>
            <td class="col-center">
              <label class="toggle-switch">
                <input type="checkbox" id="crud-check-${role}" ${perm.crud ? 'checked' : ''}>
                <span class="toggle-slider"></span>
              </label>
            </td>
            ${pageChecks}
            <td class="col-center">
              <button class="btn btn-secondary btn-sm" id="btn-save-role-${role}" onclick="saveRoleRow('${role}')">
                Save
              </button>
            </td>
          </tr>
        `;
      }).join('');
    }

    function toggleAllRolePages(role, selectAll) {
      const row = document.getElementById(`role-row-${role}`);
      if (!row) return;
      const checkboxes = row.querySelectorAll(`input[type="checkbox"][data-role="${role}"]`);
      checkboxes.forEach(cb => cb.checked = selectAll);
    }

    async function saveRoleRow(role) {
      const row = document.getElementById(`role-row-${role}`);
      if (!row) return;

      const btn = document.getElementById(`btn-save-role-${role}`);
      if (btn) { btn.disabled = true; btn.textContent = 'Savingâ€¦'; }

      const checkedBoxes = row.querySelectorAll(`input[type="checkbox"][data-role="${role}"]:checked`);
      const selectedPages = Array.from(checkedBoxes).map(cb => cb.dataset.page);
      const crudChecked = document.getElementById(`crud-check-${role}`).checked;

      try {
        const res = await fetch(`/api/master-data/role-permissions/${role}`, {
          method: "PUT",
          headers: authHeaders(),
          body: JSON.stringify({
            pages: selectedPages,
            crud: crudChecked
          })
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Failed to update role permissions");

        currentPermissions[role] = { pages: data.pages, crud: data.crud };
        showToast(`Permissions updated for role: ${role.replace(/_/g, ' ')}.`);
      } catch (err) {
        showToast(err.message || "Failed to save permissions.", true);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
      }
    }

    // â”€â”€ YIELD DETECT CONFIG TAB â”€â”€
    async function loadYieldConfig() {
      try {
        const res = await fetch("/api/master-data/yield-config", { headers: authHeaders() });
        if (!res.ok) throw new Error("Failed to load yield config");
        const data = await res.json();
        currentYieldConfig = data || [];
        document.getElementById('tabBadgeYield').textContent = currentYieldConfig.length;
        document.getElementById('yieldCountBadge').innerHTML = `<strong>${currentYieldConfig.length}</strong> parameters loaded`;
        renderYieldConfigTable();
      } catch (err) {
        document.getElementById('yieldConfigBody').innerHTML = `<tr><td colspan="6"><div class="empty-state" style="color:var(--danger)"><div class="empty-state-title">Failed to load yield engine configuration.</div><div>${escapeHtml(err.message)}</div></div></td></tr>`;
      }
    }

    function renderYieldConfigTable() {
      const tbody = document.getElementById('yieldConfigBody');
      const filter = (document.getElementById('yieldSearchInput')?.value || '').trim().toLowerCase();

      let items = currentYieldConfig;
      if (filter) {
        items = items.filter(c => (c.key || '').toLowerCase().includes(filter) || (c.description || '').toLowerCase().includes(filter) || (c.category || '').toLowerCase().includes(filter));
      }

      if (!items.length) {
        tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state"><div class="empty-state-title">No matching yield parameters found</div></div></td></tr>`;
        return;
      }

      tbody.innerHTML = items.map(cfg => `
        <tr>
          <td><span class="param-key">${escapeHtml(cfg.key)}</span></td>
          <td><input class="param-input" id="yield-val-${escapeHtml(cfg.key)}" value="${escapeHtml(cfg.value)}"></td>
          <td><span class="badge-category cat-default">${escapeHtml(cfg.category || 'General')}</span></td>
          <td style="font-size:12.5px;color:var(--text-sub);">${escapeHtml(cfg.description || 'System inference parameter.')}</td>
          <td style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono);">${cfg.last_updated ? new Date(cfg.last_updated).toLocaleString() : 'â€”'}</td>
          <td class="col-center">
            <button class="btn btn-secondary btn-sm" id="btn-save-yield-${escapeHtml(cfg.key)}" onclick="saveYieldConfig('${escapeHtml(cfg.key)}')">
              Save
            </button>
          </td>
        </tr>
      `).join('');
    }

    async function saveYieldConfig(key) {
      const valueInput = document.getElementById(`yield-val-${key}`);
      if (!valueInput) return;
      const value = valueInput.value;
      const row = currentYieldConfig.find(c => c.key === key);

      const btn = document.getElementById(`btn-save-yield-${key}`);
      if (btn) { btn.disabled = true; btn.textContent = 'Savingâ€¦'; }

      try {
        const res = await fetch(`/api/master-data/yield-config/${encodeURIComponent(key)}`, {
          method: "PUT",
          headers: authHeaders(),
          body: JSON.stringify({
            value,
            category: row ? row.category : 'general',
            description: row ? row.description : ''
          })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Save failed");

        showToast(`Parameter "${key}" updated.`);
        loadYieldConfig();
      } catch (err) {
        showToast(err.message || "Failed to save yield config.", true);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
      }
    }

    // â”€â”€ CROP FEATURES & DISTRICTS TAB â”€â”€
    function selectCropState(st) {
      document.getElementById('cropFeatureState').value = st;
      document.getElementById('pillStateRajasthan').classList.toggle('active', st === 'rajasthan');
      document.getElementById('pillStateTripura').classList.toggle('active', st === 'tripura');
      document.getElementById('pillStateMeghalaya').classList.toggle('active', st === 'meghalaya');
      onCropFeatureStateChange();
    }

    function onCropFeatureStateChange() {
      currentCropFeatures = [];
      currentDistricts = [];
      editingCropName = null;
      loadCropFeatures();
      loadDistricts();
    }

    function getCategoryBadgeClass(cat) {
      const c = (cat || '').toLowerCase();
      if (c.includes('cereal')) return 'cat-cereal';
      if (c.includes('pulse')) return 'cat-pulse';
      if (c.includes('oilseed')) return 'cat-oilseed';
      if (c.includes('vegetable')) return 'cat-vegetable';
      if (c.includes('fruit')) return 'cat-fruit';
      if (c.includes('spice')) return 'cat-spice';
      if (c.includes('fiber') || c.includes('fibre')) return 'cat-fiber';
      if (c.includes('cash')) return 'cat-cash';
      return 'cat-default';
    }

    function renderWaterNeedHtml(val) {
      const v = parseInt(val) || 2;
      let drops = '';
      let label = 'Medium';
      if (v === 1) {
        drops = '<span class="water-drop">ðŸ’§</span><span class="water-drop-dim">ðŸ’§ðŸ’§</span>';
        label = 'Low';
      } else if (v === 3) {
        drops = '<span class="water-drop">ðŸ’§ðŸ’§ðŸ’§</span>';
        label = 'High';
      } else {
        drops = '<span class="water-drop">ðŸ’§ðŸ’§</span><span class="water-drop-dim">ðŸ’§</span>';
        label = 'Medium';
      }
      return `<div class="water-meter">${drops} <span>${label}</span></div>`;
    }

    async function loadCropFeatures() {
      const state = document.getElementById('cropFeatureState').value;
      try {
        const res = await fetch(`/api/master-data/crop-features?state=${encodeURIComponent(state)}`, { headers: authHeaders() });
        if (!res.ok) {
          const data = await res.json();
          throw new Error(data.error || "Failed to load crop features");
        }
        const data = await res.json();
        currentCropFeatures = data.crops || [];
        document.getElementById('tabBadgeCrops').textContent = currentCropFeatures.length;
        document.getElementById('cropCountBadge').innerHTML = `<strong>${currentCropFeatures.length}</strong> crops registered`;
        renderCropFeaturesTable(currentCropFeatures);
      } catch (err) {
        document.getElementById('cropFeaturesBody').innerHTML = `<tr><td colspan="5"><div class="empty-state" style="color:var(--danger)"><div class="empty-state-title">Failed to load crop features</div><div>${escapeHtml(err.message)}</div></div></td></tr>`;
      }
    }

    function renderCropFeaturesTable(items) {
      const tbody = document.getElementById('cropFeaturesBody');
      const filter = (document.getElementById('cropSearchInput')?.value || '').trim().toLowerCase();

      let list = items;
      if (filter) {
        list = list.filter(c => (c.crop || '').toLowerCase().includes(filter) || (c.Crop_Category || '').toLowerCase().includes(filter));
      }

      if (!list.length) {
        tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><div class="empty-state-title">No crop profiles registered for this state</div></div></td></tr>`;
        return;
      }

      tbody.innerHTML = list.map(c => `
        <tr>
          <td><strong style="color:var(--text-main);font-size:13.5px;">${escapeHtml(c.crop)}</strong></td>
          <td><span class="badge-category ${getCategoryBadgeClass(c.Crop_Category)}">${escapeHtml(c.Crop_Category || 'â€”')}</span></td>
          <td>${renderWaterNeedHtml(c.Crop_Water_Need)}</td>
          <td class="col-num">${c.Crop_Duration_Days ? `${c.Crop_Duration_Days} days` : 'â€”'}</td>
          <td class="col-center">
            <div style="display:inline-flex;gap:6px;">
              <button class="btn-icon" title="Edit crop" onclick="openCropModal('${escapeHtml(c.crop)}')">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
              </button>
              <button class="btn-icon danger" title="Delete crop" onclick="deleteCrop('${escapeHtml(c.crop)}')">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
              </button>
            </div>
          </td>
        </tr>
      `).join('');
    }

    function openCropModal(cropName = null) {
      editingCropName = cropName;
      const modalTitle = document.getElementById('cropModalTitle');
      const nameInput = document.getElementById('modalCropName');
      const catSelect = document.getElementById('modalCropCategory');
      const waterSelect = document.getElementById('modalCropWaterNeed');
      const durInput = document.getElementById('modalCropDuration');

      if (cropName) {
        modalTitle.textContent = "Edit Crop Profile";
        const item = currentCropFeatures.find(c => c.crop === cropName);
        if (item) {
          nameInput.value = item.crop || '';
          nameInput.disabled = true;
          catSelect.value = item.Crop_Category || '';
          waterSelect.value = item.Crop_Water_Need ?? "2";
          durInput.value = item.Crop_Duration_Days ?? '';
        }
      } else {
        modalTitle.textContent = "Add Crop Profile";
        nameInput.value = '';
        nameInput.disabled = false;
        catSelect.value = '';
        waterSelect.value = "2";
        durInput.value = '';
      }

      document.getElementById('cropModalOverlay').classList.add('show');
    }

    function closeCropModal() {
      document.getElementById('cropModalOverlay').classList.remove('show');
      editingCropName = null;
    }

    async function saveCropModal() {
      const state = document.getElementById('cropFeatureState').value;
      const cropName = document.getElementById('modalCropName').value.trim();
      const category = document.getElementById('modalCropCategory').value;
      const waterNeed = parseInt(document.getElementById('modalCropWaterNeed').value) || 2;
      const duration = parseInt(document.getElementById('modalCropDuration').value) || 0;

      if (!cropName) {
        showToast("Crop name is required.", true);
        return;
      }
      if (!category) {
        showToast("Crop category is required.", true);
        return;
      }
      if (duration <= 0) {
        showToast("Crop duration must be a positive integer.", true);
        return;
      }

      const payload = {
        state,
        crop: cropName,
        Crop_Category: category,
        Crop_Water_Need: waterNeed,
        Crop_Duration_Days: duration,
      };

      try {
        let res;
        if (editingCropName) {
          res = await fetch(`/api/master-data/crop-features/${encodeURIComponent(editingCropName)}`, {
            method: "PATCH",
            headers: authHeaders(),
            body: JSON.stringify(payload)
          });
        } else {
          res = await fetch("/api/master-data/crop-features", {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify(payload)
          });
        }

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Save failed");

        showToast(editingCropName ? "Crop features updated." : "Crop registered successfully.");
        closeCropModal();
        loadCropFeatures();
      } catch (err) {
        showToast(err.message || "Failed to save crop.", true);
      }
    }

    async function deleteCrop(cropName) {
      if (!confirm(`Are you sure you want to delete crop "${cropName}"? This will remove all associated rows from state dataset and cannot be undone.`)) return;

      const state = document.getElementById('cropFeatureState').value;
      try {
        const res = await fetch(`/api/master-data/crop-features/${encodeURIComponent(cropName)}`, {
          method: "DELETE",
          headers: authHeaders(),
          body: JSON.stringify({ state })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Delete failed");

        showToast(`Crop "${cropName}" deleted.`);
        loadCropFeatures();
      } catch (err) {
        showToast(err.message || "Failed to delete crop.", true);
      }
    }

    async function loadDistricts() {
      const state = document.getElementById('cropFeatureState').value;
      try {
        const res = await fetch(`/api/master-data/districts?state=${encodeURIComponent(state)}`, { headers: authHeaders() });
        if (!res.ok) {
          const data = await res.json();
          throw new Error(data.error || "Failed to load districts");
        }
        const data = await res.json();
        currentDistricts = data.districts || [];
        document.getElementById('districtCountBadge').innerHTML = `<strong>${currentDistricts.length}</strong> districts`;
        renderDistrictsTable(currentDistricts);
      } catch (err) {
        document.getElementById('districtsListContainer').innerHTML = `<div class="empty-state" style="color:var(--danger)"><div class="empty-state-title">Failed to load districts</div><div>${escapeHtml(err.message)}</div></div>`;
      }
    }

    function renderDistrictsTable(items) {
      const container = document.getElementById('districtsListContainer');
      const filter = (document.getElementById('districtSearchInput')?.value || '').trim().toLowerCase();

      let list = items;
      if (filter) {
        list = list.filter(d => d.toLowerCase().includes(filter));
      }

      if (!list.length) {
        container.innerHTML = `<div class="empty-state"><div class="empty-state-title">No districts found for this state</div></div>`;
        return;
      }

      container.innerHTML = list.map(d => `
        <div class="district-item">
          <div class="district-name">${escapeHtml(d)}</div>
          <button class="btn-icon danger" title="Delete district" onclick="deleteDistrict('${escapeHtml(d)}')">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
          </button>
        </div>
      `).join('');
    }

    function openDistrictModal() {
      document.getElementById('modalDistrictName').value = '';
      document.getElementById('districtModalTitle').textContent = `Add District (${document.getElementById('cropFeatureState').value.toUpperCase()})`;
      document.getElementById('districtModalOverlay').classList.add('show');
    }

    function closeDistrictModal() {
      document.getElementById('districtModalOverlay').classList.remove('show');
    }

    async function saveDistrictModal() {
      const state = document.getElementById('cropFeatureState').value;
      const districtName = document.getElementById('modalDistrictName').value.trim();

      if (!districtName) {
        showToast("District name is required.", true);
        return;
      }

      try {
        const res = await fetch("/api/master-data/districts", {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ state, district: districtName })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Save failed");

        showToast(`District "${districtName}" enrolled.`);
        closeDistrictModal();
        loadDistricts();
      } catch (err) {
        showToast(err.message || "Failed to save district.", true);
      }
    }

    async function deleteDistrict(districtName) {
      if (!confirm(`Are you sure you want to remove district "${districtName}"? This will modify state datasets.`)) return;

      const state = document.getElementById('cropFeatureState').value;
      try {
        const res = await fetch(`/api/master-data/districts/${encodeURIComponent(districtName)}`, {
          method: "DELETE",
          headers: authHeaders(),
          body: JSON.stringify({ state })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Delete failed");

        showToast(`District "${districtName}" removed.`);
        loadDistricts();
      } catch (err) {
        showToast(err.message || "Failed to delete district.", true);
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str).replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[m]));
    }


    // ── AGRI CREDIT SCORE TAB ──────────────────────────────────────────────────
    let currentCreditScoreAccounts = [];
    let filteredCreditScoreAccounts = [];
    let creditScorePage = 1;
    const creditScorePageSize = 10;
    let editingCsFarmerId = null;

    async function loadCreditScoreAccounts() {
      try {
        const res = await fetch("/api/credit-score/admin/farmers", { headers: authHeaders() });
        if (!res.ok) {
          const d = await res.json();
          throw new Error(d.error || "Failed to load credit score accounts");
        }
        const data = await res.json();
        currentCreditScoreAccounts = data.farmers || [];
        document.getElementById('tabBadgeCreditScore').textContent = currentCreditScoreAccounts.length;
        onCreditScoreFilterChange();
      } catch (err) {
        document.getElementById('creditScoreBody').innerHTML = `<tr><td colspan="10"><div class="empty-state" style="color:var(--danger)"><div class="empty-state-title">Failed to load credit accounts.</div><div>${escapeHtml(err.message)}</div></div></td></tr>`;
      }
    }

    function onCreditScoreFilterChange() {
      const q = (document.getElementById('creditScoreSearchInput').value || '').trim().toLowerCase();
      const state = document.getElementById('creditScoreStateFilter').value;
      const repayment = document.getElementById('creditScoreRepaymentFilter').value;

      filteredCreditScoreAccounts = currentCreditScoreAccounts.filter(f => {
        if (state !== 'all' && (f.state || '').toLowerCase() !== state.toLowerCase()) return false;
        if (repayment !== 'all' && f.repayment_status !== repayment) return false;
        if (q) {
          const matchId   = (f.farmer_id || '').toLowerCase().includes(q);
          const matchName = (f.farmer_name || '').toLowerCase().includes(q);
          const matchEmail= (f.email || '').toLowerCase().includes(q);
          if (!matchId && !matchName && !matchEmail) return false;
        }
        return true;
      });

      creditScorePage = 1;
      renderCreditScoreTable();
    }

    function resetCreditScoreFilters() {
      document.getElementById('creditScoreSearchInput').value = '';
      document.getElementById('creditScoreStateFilter').value = 'all';
      document.getElementById('creditScoreRepaymentFilter').value = 'all';
      onCreditScoreFilterChange();
    }

    function _csGradeColor(grade) {
      if (grade === 'AAA' || grade === 'AA') return '#047857';
      if (grade === 'A') return '#0284C7';
      if (grade === 'BBB') return '#D97706';
      return '#DC2626';
    }

    function _csScoreBg(score) {
      if (score >= 80) return '#ECFDF5';
      if (score >= 65) return '#E0F2FE';
      if (score >= 50) return '#FEF3C7';
      if (score >= 35) return '#FEF3C7';
      return '#FEE2E2';
    }

    function _repaymentBadge(status) {
      if (status === 'Repaid on time') return `<span class="badge badge-active badge-dot">${escapeHtml(status)}</span>`;
      if (status === 'Defaulted') return `<span class="badge badge-inactive badge-dot">${escapeHtml(status)}</span>`;
      return `<span class="badge" style="background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;">${escapeHtml(status || '—')}</span>`;
    }

    function renderCreditScoreTable() {
      const tbody = document.getElementById('creditScoreBody');
      const total = filteredCreditScoreAccounts.length;
      document.getElementById('creditScoreCountBadge').innerHTML = `Showing <strong>${total}</strong> of ${currentCreditScoreAccounts.length} accounts`;

      if (!total) {
        tbody.innerHTML = `<tr><td colspan="10"><div class="empty-state"><div class="empty-state-title">No matching farmer accounts found</div><div>Try adjusting your filters or search keywords.</div></div></td></tr>`;
        renderCreditScorePagination(0);
        return;
      }

      const totalPages = Math.ceil(total / creditScorePageSize);
      if (creditScorePage > totalPages) creditScorePage = totalPages;

      const startIdx = (creditScorePage - 1) * creditScorePageSize;
      const paged = filteredCreditScoreAccounts.slice(startIdx, startIdx + creditScorePageSize);

      tbody.innerHTML = paged.map(f => {
        const score = f.credit_score ?? '—';
        const grade = f.rating_grade || '—';
        const gradeColor = _csGradeColor(grade);
        const scoreBg = typeof score === 'number' ? _csScoreBg(score) : '#F1F5F9';
        const stateName = (f.state || '').charAt(0).toUpperCase() + (f.state || '').slice(1);

        return `
          <tr>
            <td><span style="font-family:var(--font-mono);font-size:12px;font-weight:700;color:var(--primary);">${escapeHtml(f.farmer_id)}</span></td>
            <td>
              <div class="facility-cell">
                <div class="facility-avatar" style="background:#EDE9FE;color:#7C3AED;font-size:13px;font-weight:800;">
                  ${escapeHtml((f.farmer_name || '?').charAt(0).toUpperCase())}
                </div>
                <div class="facility-details">
                  <div class="facility-name">${escapeHtml(f.farmer_name || '—')}</div>
                  <div class="facility-location-sub">${escapeHtml(f.email || '—')}</div>
                </div>
              </div>
            </td>
            <td>
              <div style="display:flex;flex-direction:column;gap:3px;">
                <span class="badge-state">${escapeHtml(stateName || '—')}</span>
                <span style="font-size:11.5px;color:var(--text-muted);">${escapeHtml(f.district || '—')}</span>
              </div>
            </td>
            <td style="font-family:var(--font-mono);font-weight:600;color:var(--text-sub);">${Number(f.land_acres || 0).toFixed(2)}</td>
            <td style="font-size:12.5px;color:var(--text-sub);">${escapeHtml(f.crop || '—')}</td>
            <td class="col-num">&#8377;${Number(f.past_loan_amount || 0).toLocaleString()}</td>
            <td class="col-num">${Number(f.past_yield_quintals || 0).toFixed(1)}</td>
            <td>${_repaymentBadge(f.repayment_status)}</td>
            <td class="col-center">
              <div style="display:inline-flex;flex-direction:column;align-items:center;gap:2px;background:${scoreBg};border-radius:8px;padding:4px 10px;min-width:52px;">
                <span style="font-size:15px;font-weight:800;color:${gradeColor};">${score}</span>
                <span style="font-size:10px;font-weight:700;color:${gradeColor};">${grade}</span>
              </div>
            </td>
            <td class="col-center">
              <button class="btn-icon" title="Edit account" onclick="openCreditScoreModal('${escapeHtml(f.farmer_id)}')">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
              </button>
            </td>
          </tr>
        `;
      }).join('');

      renderCreditScorePagination(totalPages);
    }

    function renderCreditScorePagination(totalPages) {
      const info = document.getElementById('creditScorePaginationInfo');
      const controls = document.getElementById('creditScorePaginationControls');
      if (totalPages <= 1) {
        info.textContent = `Showing 1–${filteredCreditScoreAccounts.length} of ${filteredCreditScoreAccounts.length}`;
        controls.innerHTML = '';
        return;
      }
      const start = (creditScorePage - 1) * creditScorePageSize + 1;
      const end = Math.min(creditScorePage * creditScorePageSize, filteredCreditScoreAccounts.length);
      info.textContent = `Showing ${start}–${end} of ${filteredCreditScoreAccounts.length}`;

      let btns = `<button class="page-btn" onclick="goToCsPage(${creditScorePage - 1})" ${creditScorePage === 1 ? 'disabled' : ''}>
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
      </button>`;
      for (let p = 1; p <= totalPages; p++) {
        if (p === 1 || p === totalPages || (p >= creditScorePage - 1 && p <= creditScorePage + 1)) {
          btns += `<button class="page-btn ${p === creditScorePage ? 'active' : ''}" onclick="goToCsPage(${p})">${p}</button>`;
        } else if (p === creditScorePage - 2 || p === creditScorePage + 2) {
          btns += `<span style="padding:0 4px;color:var(--text-muted);">…</span>`;
        }
      }
      btns += `<button class="page-btn" onclick="goToCsPage(${creditScorePage + 1})" ${creditScorePage === totalPages ? 'disabled' : ''}>
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
      </button>`;
      controls.innerHTML = btns;
    }

    function goToCsPage(p) { creditScorePage = p; renderCreditScoreTable(); }

    // ── Client-side score calculator (mirrors backend logic) ──
    function _clientCalcScore(landAcres, pastLoan, pastYield) {
      const land = Math.max(0.1, parseFloat(landAcres) || 1.0);
      const loan = Math.max(0.0, parseFloat(pastLoan) || 0.0);
      const yld  = Math.max(0.0, parseFloat(pastYield) || 0.0);

      const landScore  = Math.min(land / 10.0, 1.0) * 30.0;
      let loanScore = 0;
      if (loan > 0) { loanScore = loan > (land * 50000) ? 10.0 : 20.0; }
      const yieldScore = Math.min(yld / 50.0, 1.0) * 50.0;
      const total = Math.max(0, Math.min(100, landScore + loanScore + yieldScore));

      let grade, risk, multiplier;
      if (total >= 80)      { grade = 'AAA'; risk = 'Very Low Risk';   multiplier = 1.5; }
      else if (total >= 65) { grade = 'AA';  risk = 'Low Risk';        multiplier = 1.2; }
      else if (total >= 50) { grade = 'A';   risk = 'Moderate Risk';   multiplier = 1.0; }
      else if (total >= 35) { grade = 'BBB'; risk = 'Elevated Risk';   multiplier = 0.7; }
      else                  { grade = 'C';   risk = 'High Risk';       multiplier = 0.4; }

      let maxLoan = Math.round(((land * 50000 + yld * 5000) * multiplier) / 5000) * 5000;
      maxLoan = Math.max(25000, Math.min(2000000, maxLoan));

      return {
        score: Math.round(total * 10) / 10,
        grade, risk, maxLoan,
        landScore: Math.round(landScore * 10) / 10,
        loanScore: Math.round(loanScore * 10) / 10,
        yieldScore: Math.round(yieldScore * 10) / 10,
      };
    }

    function updateCsPreview() {
      const land = document.getElementById('csModalLandAcres').value;
      const loan = document.getElementById('csModalLoanAmount').value;
      const yld  = document.getElementById('csModalYieldQtl').value;
      const result = _clientCalcScore(land, loan, yld);

      document.getElementById('csPreviewScore').textContent = result.score;
      document.getElementById('csPreviewGrade').textContent = result.grade;
      document.getElementById('csPreviewGrade').style.color = _csGradeColor(result.grade);
      document.getElementById('csPreviewRisk').textContent  = result.risk;
      document.getElementById('csPreviewLoan').textContent  = `\u20B9${result.maxLoan.toLocaleString()}`;
      document.getElementById('csBreakLand').textContent  = result.landScore;
      document.getElementById('csBreakLoan').textContent  = result.loanScore;
      document.getElementById('csBreakYield').textContent = result.yieldScore;
    }

    function openCreditScoreModal(farmerId) {
      editingCsFarmerId = farmerId;
      const f = currentCreditScoreAccounts.find(a => a.farmer_id === farmerId);
      if (!f) { showToast("Farmer record not found.", true); return; }

      document.getElementById('csModalFarmerBadge').textContent = `ID: ${f.farmer_id}`;
      document.getElementById('csModalFarmerName').textContent  = f.farmer_name || '—';
      document.getElementById('csModalEmail').textContent       = f.email || '—';

      const state = (f.state || 'Rajasthan');
      const stateTitle = state.charAt(0).toUpperCase() + state.slice(1).toLowerCase();
      document.getElementById('csModalState').value       = stateTitle;
      document.getElementById('csModalDistrict').value   = f.district || '';
      document.getElementById('csModalLandAcres').value  = f.land_acres || '';
      document.getElementById('csModalCrop').value       = f.crop || '';
      document.getElementById('csModalLoanAmount').value = f.past_loan_amount || '';
      document.getElementById('csModalYieldQtl').value   = f.past_yield_quintals || '';
      document.getElementById('csModalRepayment').value  = f.repayment_status || 'Repaid on time';

      updateCsPreview();
      document.getElementById('creditScoreModalOverlay').classList.add('show');
    }

    function closeCreditScoreModal() {
      document.getElementById('creditScoreModalOverlay').classList.remove('show');
      editingCsFarmerId = null;
    }

    async function saveCreditScoreModal() {
      const landAcres        = parseFloat(document.getElementById('csModalLandAcres').value);
      const pastLoanAmount   = parseFloat(document.getElementById('csModalLoanAmount').value);
      const pastYieldQtl     = parseFloat(document.getElementById('csModalYieldQtl').value);
      const state            = document.getElementById('csModalState').value.trim();
      const district         = document.getElementById('csModalDistrict').value.trim();
      const crop             = document.getElementById('csModalCrop').value.trim();
      const repaymentStatus  = document.getElementById('csModalRepayment').value;

      if (!state || !district)            { showToast("State and district are required.", true); return; }
      if (!crop)                          { showToast("Primary crop is required.", true); return; }
      if (isNaN(landAcres) || landAcres <= 0)         { showToast("Land acres must be greater than 0.", true); return; }
      if (isNaN(pastLoanAmount) || pastLoanAmount < 0) { showToast("Past loan amount must be 0 or more.", true); return; }
      if (isNaN(pastYieldQtl) || pastYieldQtl < 0)    { showToast("Past yield must be 0 or more.", true); return; }

      const payload = {
        state, district, crop,
        land_acres: landAcres,
        past_loan_amount: pastLoanAmount,
        past_yield_quintals: pastYieldQtl,
        repayment_status: repaymentStatus,
      };

      try {
        const res = await fetch(`/api/credit-score/admin/farmers/${encodeURIComponent(editingCsFarmerId)}`, {
          method: "PATCH",
          headers: authHeaders(),
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Save failed");

        showToast(`Account ${editingCsFarmerId} updated successfully.`);
        closeCreditScoreModal();
        loadCreditScoreAccounts();
      } catch (err) {
        showToast(err.message || "Failed to save account.", true);
      }
    }
    // Initialization
    window.addEventListener('DOMContentLoaded', checkPermissionAndInit);
  