import sqlite3, json, math

db = sqlite3.connect('crow.db')

# delta_short distribution
print('=== delta_short distribution ===')
for label, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    rows = db.execute('SELECT delta_short FROM ticks WHERE ' + cond + ' AND delta_short IS NOT NULL LIMIT 50000').fetchall()
    vals = sorted(abs(r[0]) for r in rows if r[0] is not None)
    if vals:
        n = len(vals)
        zeros = sum(1 for v in vals if v == 0)
        print('%s: avg_abs=%.4f  zeros=%d%%  p50=%.4f  p95=%.4f' % (
            label, sum(vals)/n, zeros*100//n, vals[n//2], vals[int(n*0.95)]))

# What does delta_long look like normalized by BTC price?
print()
print('=== delta_long / btc (scale check) ===')
for label, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    rows = db.execute('SELECT delta_long, btc FROM ticks WHERE ' + cond +
                      ' AND delta_long IS NOT NULL AND btc IS NOT NULL AND btc > 0 LIMIT 50000').fetchall()
    ratios = sorted(abs(r[0]) / r[1] for r in rows if r[1])
    if ratios:
        n = len(ratios)
        print('%s: avg=%.6f  p50=%.6f  p95=%.6f' % (label, sum(ratios)/n, ratios[n//2], ratios[int(n*0.95)]))

# What does delta_long look like normalized by gap?
print()
print('=== abs(delta_long) / abs(gap) [formula weight] ===')
for label, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    rows = db.execute('SELECT delta_long, gap FROM ticks WHERE ' + cond +
                      ' AND delta_long IS NOT NULL AND gap IS NOT NULL AND gap != 0 LIMIT 50000').fetchall()
    ratios = sorted(abs(r[0]) / abs(r[1]) for r in rows if r[1])
    if ratios:
        n = len(ratios)
        print('%s: avg=%.3f  p50=%.3f  p95=%.3f' % (label, sum(ratios)/n, ratios[n//2], ratios[int(n*0.95)]))

# Sample actual tick records to understand what delta_long represents
print()
print('=== Sample ticks (old) ===')
rows = db.execute('''SELECT ts, delta_long, delta_short, gap, up, down, btc, series
    FROM ticks WHERE ts < '2026-06-01' AND delta_long IS NOT NULL AND up IS NOT NULL
    AND ABS(delta_long) > 10 LIMIT 5''').fetchall()
for r in rows:
    series_len = len(json.loads(r[7])) if r[7] else 0
    print('  dl=%.2f  ds=%.2f  gap=%.1f  up=%.1f  down=%.1f  btc=%.0f  series_len=%d' % (
        r[1], r[2], r[3], r[4], r[5], r[6] or 0, series_len))

print()
print('=== Sample ticks (new) ===')
rows = db.execute('''SELECT ts, delta_long, delta_short, gap, up, down, btc, series
    FROM ticks WHERE ts >= '2026-06-01' AND delta_long IS NOT NULL AND up IS NOT NULL
    AND ABS(delta_long) > 10 LIMIT 5''').fetchall()
for r in rows:
    s = r[7]
    if s:
        parsed = json.loads(s) if isinstance(s, str) else s
        series_len = len(parsed)
        # delta_short as recomputed from series with lookback=15
        lb = 15
        at_lb = parsed[-(lb+1)] if len(parsed) > lb else parsed[0]
        ds_recomputed = parsed[-1] - at_lb
    else:
        series_len = 0
        ds_recomputed = r[2]
    print('  dl=%.2f  ds_stored=%.2f  ds_recomp(lb15)=%.2f  gap=%.1f  up=%.1f  down=%.1f  btc=%.0f  series_len=%d' % (
        r[1], r[2], ds_recomputed, r[3], r[4], r[5], r[6] or 0, series_len))

db.close()
