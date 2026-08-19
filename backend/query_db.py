import sqlite3
conn = sqlite3.connect('cold_storage.db')
conn.row_factory = sqlite3.Row

print("=== Snapshots ===")
snapshots = conn.execute('SELECT COUNT(*) as cnt FROM storage_snapshots').fetchone()
print('Total snapshots:', snapshots['cnt'])

print("\n=== Sample Rajasthan facilities ===")
sample = conn.execute("SELECT name, district, total_capacity_mt, storage_type, source, data_quality FROM cold_storages WHERE state='rajasthan' LIMIT 5").fetchall()
for r in sample:
    print(dict(r))

print("\n=== Top districts by capacity ===")
dist = conn.execute("SELECT district, COUNT(*) as cnt, SUM(total_capacity_mt) as total FROM cold_storages WHERE state='rajasthan' GROUP BY district ORDER BY total DESC LIMIT 10").fetchall()
for r in dist:
    print(dict(r))

print("\n=== Crop capacity rows ===")
cc = conn.execute("SELECT COUNT(*) as cnt FROM cold_storage_crop_capacity").fetchone()
print('Crop capacity rows:', cc['cnt'])
