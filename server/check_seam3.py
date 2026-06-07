import sqlite3, json, math

db = sqlite3.connect('crow.db')

# deltaLong = current_btc - targetPrice (btc at market open / strike)
# Check what deltaLong looks like at different stages of the market (secs_remaining)
print('=== deltaLong by secs_remaining bucket (OLD vs NEW) ===')
buckets = [(870, 900, 'tick 0-30 (early)'), (450, 480, 'mid (~tick 420)'), (0, 30, 'final ticks')]
for lo, hi, label in buckets:
    print(f'\n{label}:')
    for period, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
        rows = db.execute('''SELECT delta_long FROM ticks WHERE ''' + cond +
            ''' AND secs_remaining >= ? AND secs_remaining < ?
               AND delta_long IS NOT NULL LIMIT 20000''', [lo, hi]).fetchall()
        vals = sorted(abs(r[0]) for r in rows if r[0] is not None)
        if vals:
            n = len(vals)
            print('  %s n=%d: avg_abs=%.1f  p25=%.1f  p50=%.1f  p75=%.1f' % (
                period, n, sum(vals)/n, vals[int(n*0.25)], vals[n//2], vals[int(n*0.75)]))

# Check typical formula score distribution for Crow-3 formula on old vs new
# formula: (deltaLong + deltaShort*(secsRemaining/lookback)) / abs(gap) + (gap / 200)
print()
print('=== Crow-3 score distribution (OLD vs NEW) ===')
lookback = 15
for period, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    rows = db.execute('''SELECT delta_long, delta_short, secs_remaining, gap FROM ticks WHERE ''' + cond +
        ''' AND delta_long IS NOT NULL AND delta_short IS NOT NULL
           AND secs_remaining IS NOT NULL AND secs_remaining > 0
           AND gap IS NOT NULL AND gap != 0 LIMIT 50000''').fetchall()
    scores = []
    for r in rows:
        dl, ds, secs, gap = r
        try:
            score = (dl + ds * (secs / lookback)) / abs(gap) + (gap / 200)
            if math.isfinite(score):
                scores.append(score)
        except:
            pass
    if scores:
        scores.sort()
        n = len(scores)
        # What fraction exceed threshold 0.5 (typical entry threshold)
        pos_thresh = sum(1 for s in scores if s > 0.5)
        neg_thresh = sum(1 for s in scores if s < -0.5)
        print('%s n=%d: p5=%.2f p25=%.2f p50=%.2f p75=%.2f p95=%.2f  |>0.5|=%d%%  |<-0.5|=%d%%' % (
            period, n, scores[int(n*0.05)], scores[int(n*0.25)], scores[n//2],
            scores[int(n*0.75)], scores[int(n*0.95)],
            pos_thresh*100//n, neg_thresh*100//n))

# Check entry price distribution in actual sim trades (old vs new markets)
print()
print('=== Real sim entry prices (Crow-3, threshold search) ===')
# Look at actual completed markets grouped by period
rows = db.execute('''
    SELECT market_ticker, MIN(ts) as open_ts, MAX(ts) as close_ts, COUNT(*) as ticks
    FROM ticks GROUP BY market_ticker
    HAVING ticks >= 500
    ORDER BY open_ts
''').fetchall()
old_markets = [r[0] for r in rows if r[1] < '2026-06-01']
new_markets  = [r[0] for r in rows if r[1] >= '2026-06-01']
print('Markets: old=%d  new=%d' % (len(old_markets), len(new_markets)))

db.close()
