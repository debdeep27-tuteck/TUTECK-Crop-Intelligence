import re

file = 'c:/Users/SnigdhoDas/OneDrive - Tuteck Technologies Private Limited/Desktop/TUTECK-Crop-Intelligence/frontend/html/master_data_config.html'
with open(file, 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace("    }\n    }\n\n    function refreshCurrentTab() {", "    }\n\n    function refreshCurrentTab() {")

with open(file, 'w', encoding='utf-8') as f:
    f.write(content)
print('done')
