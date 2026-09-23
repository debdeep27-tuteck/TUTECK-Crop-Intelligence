import re

file = 'c:/Users/SnigdhoDas/OneDrive - Tuteck Technologies Private Limited/Desktop/TUTECK-Crop-Intelligence/frontend/html/master_data_config.html'
with open(file, 'r', encoding='utf-8') as f:
    content = f.read()

new_switch = """    function switchTab(tab) {
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
    }"""
content = re.sub(r'    function switchTab\(tab\) \{[\s\S]*?loadDistricts\(\);\s*\n\s*\}', new_switch, content)


new_refresh = """    function refreshCurrentTab() {
      if (activeTab === 'storages') loadColdStorages();
      else if (activeTab === 'roles') loadRolePermissions();
      else if (activeTab === 'yield') loadYieldConfig();
      else if (activeTab === 'crop-features') { loadCropFeatures(); loadDistricts(); }
      else if (activeTab === 'credit-score') loadCreditScoreAccounts();
      showToast("Data refreshed.");
    }"""
content = re.sub(r'    function refreshCurrentTab\(\) \{[\s\S]*?showToast\("Data refreshed\."\);\s*\}', new_refresh, content)

content = re.sub(r'(loadColdStorages\(\);\s*loadRolePermissions\(\);\s*loadYieldConfig\(\);)', r'\1\n          loadCreditScoreAccounts();', content)

with open(file, 'w', encoding='utf-8') as f:
    f.write(content)
print('done')
