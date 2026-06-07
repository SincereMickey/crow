import sqlite3, time

db = sqlite3.connect('crow.db', timeout=30)

cutoff = '2026-06-02T23:00:00'

old = db.execute("SELECT COUNT(*), COUNT(DISTINCT market_ticker) FROM ticks WHERE ts < ?", [cutoff]).fetchone()
new = db.execute("SELECT COUNT(*), COUNT(DISTINCT market_ticker) FROM ticks WHERE ts >= ?", [cutoff]).fetchone()
open_trades = db.execute('SELECT COUNT(*) FROM trades WHERE exit_price IS NULL').fetchone()[0]

print('To delete: %d ticks, %d markets' % (old[0], old[1]))
print('To keep:   %d ticks, %d markets' % (new[0], new[1]))
print('Open trades: %d' % open_trades)

if open_trades > 0:
    print('ABORTING: open trades present')
    db.close()
    exit(1)

t0 = time.time()
cur = db.execute("DELETE FROM ticks WHERE ts < ?", [cutoff])
db.commit()
print('Deleted %d rows in %.1fs' % (cur.rowcount, time.time() - t0))

print('VACUUMing...', flush=True)
t1 = time.time()
db.execute('VACUUM')
print('VACUUM done in %.1fs' % (time.time() - t1))
db.close()
