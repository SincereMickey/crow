import sqlite3, statistics

db = sqlite3.connect('crow.db')

for label, cond in [('OLD (pre Jun1)', "ts < '2026-06-01'"), ('NEW (Jun1+)', "ts >= '2026-06-01'")]:
    r = db.execute('''SELECT
        COUNT(*) as n,
        SUM(CASE WHEN series IS NULL OR series='' THEN 1 ELSE 0 END) as no_series,
        SUM(CASE WHEN gap_delta IS NULL THEN 1 ELSE 0 END) as no_gapdelta,
        SUM(CASE WHEN delta_long IS NULL THEN 1 ELSE 0 END) as no_dl,
        SUM(CASE WHEN delta_short IS NULL THEN 1 ELSE 0 END) as no_ds,
        SUM(CASE WHEN up IS NULL THEN 1 ELSE 0 END) as no_up,
        SUM(CASE WHEN btc IS NULL THEN 1 ELSE 0 END) as no_btc,
        SUM(CASE WHEN target_price IS NULL THEN 1 ELSE 0 END) as no_target,
        AVG(CASE WHEN up IS NOT NULL AND down IS NOT NULL THEN (up + down) / 2.0 END) as avg_mid,
        AVG(CASE WHEN up IS NOT NULL AND down IS NOT NULL THEN ABS(up - down) END) as avg_gap,
        AVG(ABS(delta_long)) as avg_abs_dl,
        AVG(up) as avg_up,
        AVG(down) as avg_down
        FROM ticks WHERE ''' + cond).fetchone()
    n = r[0] or 1
    print(label, 'n=%d' % r[0])
    names = ['series_null','gapdelta_null','dl_null','ds_null','up_null','btc_null','target_null']
    for i, nm in enumerate(names):
        val = r[i+1] or 0
        print('  %s=%d%%' % (nm, val*100//n), end='')
    print()
    print('  avg_mid=%.1f  avg_gap=%.2f  avg_abs_dl=%.4f  avg_up=%.1f  avg_down=%.1f' % (
        r[8] or 0, r[9] or 0, r[10] or 0, r[11] or 0, r[12] or 0))

# Check series format in each cohort
print()
print('=== Series sample ===')
for label, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    rows = db.execute('SELECT series FROM ticks WHERE ' + cond + ' AND series IS NOT NULL AND series != "" LIMIT 3').fetchall()
    print(label + ':')
    for row in rows:
        s = row[0]
        print('  len=%d  preview=%s' % (len(s), s[:80]))

# Check delta_long distribution
print()
print('=== delta_long distribution ===')
for label, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    rows = db.execute('SELECT delta_long FROM ticks WHERE ' + cond + ' AND delta_long IS NOT NULL LIMIT 50000').fetchall()
    vals = [r[0] for r in rows if r[0] is not None]
    if vals:
        vals.sort()
        n = len(vals)
        print('%s: min=%.4f p5=%.4f p25=%.4f median=%.4f p75=%.4f p95=%.4f max=%.4f' % (
            label, vals[0], vals[int(n*0.05)], vals[int(n*0.25)],
            vals[n//2], vals[int(n*0.75)], vals[int(n*0.95)], vals[-1]))

# Check gap distribution
print()
print('=== gap distribution (up-down) ===')
for label, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    rows = db.execute('SELECT gap FROM ticks WHERE ' + cond + ' AND gap IS NOT NULL LIMIT 50000').fetchall()
    vals = sorted(r[0] for r in rows if r[0] is not None)
    if vals:
        n = len(vals)
        print('%s: min=%.1f p5=%.1f p25=%.1f median=%.1f p75=%.1f p95=%.1f max=%.1f' % (
            label, vals[0], vals[int(n*0.05)], vals[int(n*0.25)],
            vals[n//2], vals[int(n*0.75)], vals[int(n*0.95)], vals[-1]))

db.close()
