import sqlite3
import os

if os.path.exists("yield_lands.db"):
    conn = sqlite3.connect("yield_lands.db")
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("Tables in yield_lands.db:", tables)
    for t in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"Row count in {t}: {count}")
        sample = conn.execute(f"SELECT * FROM {t} LIMIT 5").fetchall()
        for r in sample:
            print("  ", dict(r))
else:
    print("yield_lands.db does not exist yet")
