import sqlite3, time

db = sqlite3.connect('crow.db', timeout=30)

print('Deleting old ticks (ts < 2026-06-01)...', flush=True)
t0 = time.time()
cur = db.execute("DELETE FROM ticks WHERE ts < '2026-06-01'")
db.commit()
print('Deleted %d rows in %.1fs' % (cur.rowcount, time.time() - t0), flush=True)

remaining = db.execute('SELECT COUNT(*) FROM ticks').fetchone()[0]
print('Remaining ticks: %d' % remaining, flush=True)

print('Running VACUUM (reclaiming disk space)...', flush=True)
t1 = time.time()
db.execute('VACUUM')
print('VACUUM done in %.1fs' % (time.time() - t1), flush=True)

db.close()
print('Done.')
