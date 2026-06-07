import sqlite3
db = sqlite3.connect('crow.db')
old_ticks = db.execute("SELECT COUNT(*) FROM ticks WHERE ts < '2026-06-01'").fetchone()[0]
new_ticks = db.execute("SELECT COUNT(*) FROM ticks WHERE ts >= '2026-06-01'").fetchone()[0]
old_mkts  = db.execute("SELECT COUNT(DISTINCT market_ticker) FROM ticks WHERE ts < '2026-06-01'").fetchone()[0]
new_mkts  = db.execute("SELECT COUNT(DISTINCT market_ticker) FROM ticks WHERE ts >= '2026-06-01'").fetchone()[0]
open_trades = db.execute('SELECT COUNT(*) FROM trades WHERE exit_price IS NULL').fetchone()[0]
db.close()
print('OLD ticks to delete: %d (%d markets)' % (old_ticks, old_mkts))
print('NEW ticks to keep:   %d (%d markets)' % (new_ticks, new_mkts))
print('Open trades:         %d' % open_trades)
