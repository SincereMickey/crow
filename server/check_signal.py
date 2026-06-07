"""Why does Crow-154 barely fire on new data? Break down each gate."""
import sqlite3, json, math

DB = 'crow.db'
CONFIG_ID = 194

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

cfg = dict(db.execute('SELECT * FROM configs WHERE id=?', [CONFIG_ID]).fetchone())
threshold   = float(cfg.get('threshold', 0.5))
lookback    = int(cfg.get('short_lookback', 15) or 15)
rsi_period  = int(cfg.get('rsi_period', 42))
min_entry   = float(cfg.get('min_entry', 0))
max_entry   = float(cfg.get('max_entry', 99))
print(f'Config: {cfg["name"]}  threshold={threshold}  lb={lookback}  '
      f'min_entry={min_entry}  max_entry={max_entry}')

_safe = {'__builtins__': {}, 'abs': abs, 'max': max, 'min': min,
         'round': round, 'floor': math.floor, 'ceil': math.ceil}

for period, cond in [('OLD', "ts < '2026-06-01'"), ('NEW', "ts >= '2026-06-01'")]:
    mkt_rows = db.execute(
        'SELECT DISTINCT market_ticker FROM ticks WHERE ' + cond).fetchall()
    tickers = [r[0] for r in mkt_rows]

    total_ticks = 0
    rsi_pass    = 0
    score_pos   = 0   # score > threshold
    score_neg   = 0   # score < -threshold
    in_rng_up   = 0   # up in [min_entry, max_entry] when score > threshold
    in_rng_dn   = 0   # down in [min_entry, max_entry] when score < -threshold
    gap_vals    = []
    score_cross_up = 0  # actual crossings (last<=thresh, cur>thresh)
    score_cross_dn = 0

    # Sample up to 200 markets
    import random; random.seed(42)
    sample = random.sample(tickers, min(200, len(tickers)))

    for ticker in sample:
        rows = db.execute(
            'SELECT secs_remaining,delta_long,delta_short,gap,btc,up,down,series '
            'FROM ticks WHERE market_ticker=? ORDER BY ts ASC', [ticker]).fetchall()
        ticks = [dict(r) for r in rows]

        _rsi_buf, _rsi_ag, _rsi_al, _rsi_prev, _rsi_val, _rsi_tick = [], None, None, None, None, 0
        last_score = None

        for i, tick in enumerate(ticks):
            dlong = tick.get('delta_long')
            gap   = tick.get('gap')
            secs  = tick.get('secs_remaining')
            up    = tick.get('up')
            down  = tick.get('down')
            btc   = tick.get('btc') or 0

            # RSI
            _rsi_tick += 1
            if _rsi_tick <= rsi_period + 1:
                _rsi_buf.append(btc)
                if _rsi_tick == rsi_period + 1:
                    changes = [_rsi_buf[j]-_rsi_buf[j-1] for j in range(1, len(_rsi_buf))]
                    _rsi_ag = sum(max(0.0,c) for c in changes) / rsi_period
                    _rsi_al = sum(max(0.0,-c) for c in changes) / rsi_period
                    _rsi_prev = btc; _rsi_buf = []
            else:
                change = btc - _rsi_prev
                _rsi_ag = (_rsi_ag*(rsi_period-1)+max(0.0,change))/rsi_period
                _rsi_al = (_rsi_al*(rsi_period-1)+max(0.0,-change))/rsi_period
                _rsi_prev = btc
            rsi_val = (100.0 - 100.0/( 1.0+_rsi_ag/_rsi_al) if (_rsi_ag is not None and _rsi_al and _rsi_al > 0) else None)
            rsi_num = rsi_val if rsi_val is not None else 50.0

            # delta_short
            dshort = tick.get('delta_short') or 0
            raw_series = tick.get('series')
            if raw_series:
                try:
                    s = json.loads(raw_series) if isinstance(raw_series, str) else raw_series
                    if s and len(s) >= 2:
                        at_lb = s[-(lookback+1)] if len(s) > lookback else s[0]
                        dshort = s[-1] - at_lb
                except: pass

            # gap_delta
            gap_delta = 0.0
            if gap is not None and i >= lookback:
                past_gap = ticks[i-lookback].get('gap')
                if past_gap is not None:
                    gap_delta = gap - past_gap

            score = None
            if dlong is not None and secs and secs != 0 and gap is not None:
                try:
                    v = eval(cfg['formula'], _safe, {
                        'deltaLong': dlong, 'deltaShort': dshort, 'gapDelta': gap_delta,
                        'secsRemaining': secs, 'lookback': lookback,
                        'gap': gap, 'tuner': 1, 'btc': btc, 'up': up or 0,
                        'down': down or 0, 'rsi': rsi_num})
                    score = float(v) if math.isfinite(float(v)) else None
                except: pass

            total_ticks += 1
            if rsi_val is not None and (rsi_val < 40 or rsi_val > 69):
                rsi_pass += 1

            if score is not None:
                if score > threshold:
                    score_pos += 1
                    if gap is not None: gap_vals.append(abs(gap))
                    if up is not None and min_entry <= up <= max_entry:
                        in_rng_up += 1
                if score < -threshold:
                    score_neg += 1
                    if down is not None and min_entry <= down <= max_entry:
                        in_rng_dn += 1
                if last_score is not None:
                    if last_score <= threshold and score > threshold:
                        score_cross_up += 1
                    if last_score >= -threshold and score < -threshold:
                        score_cross_dn += 1

            last_score = score

    n = total_ticks or 1
    print(f'\n{period} (sample={len(sample)} markets, {total_ticks:,} ticks):')
    print(f'  RSI passes filter:         {rsi_pass:6,} = {rsi_pass*100//n}%')
    print(f'  Score > +{threshold:.0f}:            {score_pos:6,} = {score_pos*100//n}%')
    print(f'  Score < -{threshold:.0f}:            {score_neg:6,} = {score_neg*100//n}%')
    print(f'  UP crossings (entry cand): {score_cross_up:6,}')
    print(f'  DN crossings (entry cand): {score_cross_dn:6,}')
    print(f'  UP in price range:         {in_rng_up:6,} = {in_rng_up*100//(score_pos or 1)}% of above-thresh')
    if gap_vals:
        gap_vals.sort()
        ng = len(gap_vals)
        print(f'  abs(gap) when score>thresh: median={gap_vals[ng//2]:.1f}  p75={gap_vals[int(ng*0.75)]:.1f}')

db.close()
