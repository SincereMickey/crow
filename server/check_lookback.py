"""Compare short_lookback=15 vs 20 on new data only (markets with series)."""
import sqlite3, json, math, statistics

DB = 'crow.db'
CONFIG_ID = 194  # Crow-154

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

cfg = dict(db.execute('SELECT * FROM configs WHERE id=?', [CONFIG_ID]).fetchone())
print(f'Config: {cfg["name"]}  formula={cfg["formula"]}')
print(f'regime_filter={cfg.get("regime_filter")}  threshold={cfg.get("threshold")}')

# Get new markets that have series data
new_markets = db.execute('''
    SELECT market_ticker, COUNT(*) as ticks,
           SUM(CASE WHEN series IS NOT NULL AND series!='' THEN 1 ELSE 0 END) as has_series
    FROM ticks
    WHERE ts >= '2026-06-01'
    GROUP BY market_ticker
    HAVING ticks >= 750 AND has_series > 500
    ORDER BY MIN(ts)
''').fetchall()
print(f'\nNew markets with series: {len(new_markets)}')

def simulate(ticks_raw, cfg, lookback_override=None):
    """Stripped-down simulate_config for comparison."""
    formula         = cfg['formula']
    regime_filter   = cfg.get('regime_filter') or None
    threshold       = float(cfg.get('threshold', 0.5))
    exit_threshold  = float(cfg.get('exit_threshold') or threshold)
    short_lookback  = lookback_override or int(cfg.get('short_lookback', 15) or 15)
    min_entry       = float(cfg.get('min_entry', 0))
    max_entry       = float(cfg.get('max_entry', 99))
    cooldown        = int(cfg.get('cooldown', 0))
    exit_cooldown   = int(cfg.get('exit_cooldown', 0))
    rsi_period      = int(cfg.get('rsi_period', 42))

    _safe = {'__builtins__': {}, 'abs': abs, 'max': max, 'min': min,
             'round': round, 'floor': math.floor, 'ceil': math.ceil}

    # RSI state
    _rsi_buf, _rsi_ag, _rsi_al, _rsi_prev, _rsi_val, _rsi_tick = [], None, None, None, None, 0

    open_trade = None
    last_score = None
    last_sell_idx = None
    trades = []

    for i, tick in enumerate(ticks_raw):
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
            _rsi_ag = (_rsi_ag*(rsi_period-1)+max(0.0, change))/rsi_period
            _rsi_al = (_rsi_al*(rsi_period-1)+max(0.0,-change))/rsi_period
            _rsi_prev = btc
        if _rsi_ag is not None:
            _rsi_val = 100.0 - 100.0/(1.0+_rsi_ag/_rsi_al) if _rsi_al > 0 else 100.0

        # Recompute delta_short from series using the override lookback
        dshort = tick.get('delta_short') or 0
        raw_series = tick.get('series')
        if raw_series:
            try:
                s = json.loads(raw_series) if isinstance(raw_series, str) else raw_series
                if s and len(s) >= 2:
                    at_lb = s[-(short_lookback+1)] if len(s) > short_lookback else s[0]
                    dshort = s[-1] - at_lb
            except Exception:
                pass

        # gap_delta from ticks array
        gap_delta = 0.0
        if gap is not None and i >= short_lookback:
            past_gap = ticks_raw[i-short_lookback].get('gap')
            if past_gap is not None:
                gap_delta = gap - past_gap

        rsi_val = _rsi_val if _rsi_val is not None else 50.0
        ctx = {'deltaLong': dlong or 0, 'deltaShort': dshort, 'gapDelta': gap_delta,
               'secsRemaining': secs or 1, 'lookback': short_lookback,
               'gap': gap or 0, 'tuner': 1, 'btc': btc, 'up': up or 0,
               'down': down or 0, 'rsi': rsi_val}

        score = None
        if dlong is not None and secs and secs != 0 and gap is not None:
            try:
                v = eval(formula, _safe, ctx)
                score = float(v) if math.isfinite(float(v)) else None
            except: pass

        # Regime filter
        regime_ok = True
        if regime_filter and score is not None:
            try:
                regime_ok = bool(eval(regime_filter, _safe, ctx))
            except: pass

        # Exit
        if open_trade and score is not None:
            side = open_trade['side']
            px = up if side == 'Up' else down
            if px is not None:
                triggered = (score < exit_threshold) if side == 'Up' else (score > -exit_threshold)
                if triggered:
                    result = 'win' if px > open_trade['entry_price'] else 'loss'
                    trades.append({**open_trade, 'exit_price': px,
                                   'return_pct': round((px/open_trade['entry_price']-1)*100, 2),
                                   'result': result})
                    open_trade = None; last_sell_idx = i

        # Entry
        if (regime_ok and open_trade is None and last_score is not None and score is not None
                and secs and secs <= (900 - cooldown)):
            if last_sell_idx is None or (i - last_sell_idx) >= exit_cooldown:
                in_range = lambda px: px is not None and min_entry <= px <= max_entry
                if last_score <= threshold and score > threshold and in_range(up):
                    open_trade = {'side': 'Up', 'entry_price': up, 'entry_tick': i}
                elif last_score >= -threshold and score < -threshold and in_range(down):
                    open_trade = {'side': 'Down', 'entry_price': down, 'entry_tick': i}

        last_score = score

    return trades

# Run both lookbacks on new markets
results = {15: [], 20: []}

for row in new_markets:
    ticker = row['market_ticker']
    ticks = [dict(r) for r in db.execute(
        'SELECT ts,secs_remaining,delta_long,delta_short,gap,gap_delta,btc,up,down,series '
        'FROM ticks WHERE market_ticker=? ORDER BY ts ASC', [ticker]).fetchall()]

    for lb in [15, 20]:
        trades = simulate(ticks, cfg, lookback_override=lb)
        for t in trades:
            results[lb].append(t['return_pct'])

db.close()

print()
for lb in [15, 20]:
    rets = results[lb]
    if not rets:
        print(f'lb={lb}: no trades'); continue
    n    = len(rets)
    avg  = sum(rets)/n
    neg  = sum(1 for r in rets if r < 0)
    ge10 = sum(1 for r in rets if r >= 10)
    med  = statistics.median(rets)
    std  = statistics.stdev(rets) if n > 1 else 0
    wr   = sum(1 for r in rets if r > 0) / n * 100
    print(f'lb={lb:2d}: n={n:4d}  avg={avg:+.1f}%  med={med:+.1f}%  std={std:.1f}%  '
          f'WR={wr:.1f}%  neg={neg}  >=10%={ge10}  neg%={neg*100//n}%')
