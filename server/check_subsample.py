"""Compare new data at full rate vs every-3rd-tick vs old data on Crow-154."""
import sqlite3, json, math, statistics, random

DB = 'crow.db'
CONFIG_ID = 194

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

cfg = dict(db.execute('SELECT * FROM configs WHERE id=?', [CONFIG_ID]).fetchone())
print(f'Config: {cfg["name"]}  threshold={cfg.get("threshold")}  lb={cfg.get("short_lookback")}')

_safe = {'__builtins__': {}, 'abs': abs, 'max': max, 'min': min,
         'round': round, 'floor': math.floor, 'ceil': math.ceil}

def simulate(ticks_raw, cfg, stride=1):
    formula        = cfg['formula']
    regime_filter  = cfg.get('regime_filter') or None
    threshold      = float(cfg.get('threshold', 0.5))
    exit_threshold = float(cfg.get('exit_threshold') or threshold)
    short_lookback = int(cfg.get('short_lookback', 15) or 15)
    min_entry      = float(cfg.get('min_entry', 0))
    max_entry      = float(cfg.get('max_entry', 99))
    cooldown       = int(cfg.get('cooldown', 0))
    exit_cooldown  = int(cfg.get('exit_cooldown', 0))
    rsi_period     = int(cfg.get('rsi_period', 42))

    # Subsample
    ticks = ticks_raw[::stride]

    _rsi_buf, _rsi_ag, _rsi_al, _rsi_prev, _rsi_val, _rsi_tick = [], None, None, None, None, 0
    open_trade = None; last_score = None; last_sell_idx = None; trades = []

    for i, tick in enumerate(ticks):
        dlong = tick.get('delta_long')
        gap   = tick.get('gap')
        secs  = tick.get('secs_remaining')
        up    = tick.get('up')
        down  = tick.get('down')
        btc   = tick.get('btc') or 0

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
        rsi_val = (100.0-100.0/(1.0+_rsi_ag/_rsi_al) if (_rsi_ag is not None and _rsi_al and _rsi_al>0) else None)
        rsi_num = rsi_val if rsi_val is not None else 50.0

        dshort = tick.get('delta_short') or 0
        raw_series = tick.get('series')
        if raw_series:
            try:
                s = json.loads(raw_series) if isinstance(raw_series, str) else raw_series
                if s and len(s) >= 2:
                    at_lb = s[-(short_lookback+1)] if len(s) > short_lookback else s[0]
                    dshort = s[-1] - at_lb
            except: pass

        gap_delta = 0.0
        if gap is not None and i >= short_lookback:
            past_gap = ticks[i-short_lookback].get('gap')
            if past_gap is not None:
                gap_delta = gap - past_gap

        ctx = {'deltaLong': dlong or 0, 'deltaShort': dshort, 'gapDelta': gap_delta,
               'secsRemaining': secs or 1, 'lookback': short_lookback,
               'gap': gap or 0, 'tuner': 1, 'btc': btc,
               'up': up or 0, 'down': down or 0, 'rsi': rsi_num}

        score = None
        if dlong is not None and secs and secs != 0 and gap is not None:
            try:
                v = eval(formula, _safe, ctx)
                score = float(v) if math.isfinite(float(v)) else None
            except: pass

        regime_ok = True
        if regime_filter and score is not None:
            try: regime_ok = bool(eval(regime_filter, _safe, ctx))
            except: pass

        if open_trade and score is not None:
            side = open_trade['side']
            px = up if side == 'Up' else down
            if px is not None:
                if (score < exit_threshold if side == 'Up' else score > -exit_threshold):
                    result = 'win' if px > open_trade['entry_price'] else 'loss'
                    trades.append({**open_trade, 'exit_price': px, 'result': result,
                                   'return_pct': round((px/open_trade['entry_price']-1)*100, 2)})
                    open_trade = None; last_sell_idx = i

        if (regime_ok and open_trade is None and last_score is not None
                and score is not None and secs and secs <= (900 - cooldown)):
            if last_sell_idx is None or (i-last_sell_idx) >= exit_cooldown:
                in_range = lambda px: px is not None and min_entry <= px <= max_entry
                if last_score <= threshold and score > threshold and in_range(up):
                    open_trade = {'side': 'Up', 'entry_price': up}
                elif last_score >= -threshold and score < -threshold and in_range(down):
                    open_trade = {'side': 'Down', 'entry_price': down}

        last_score = score

    return trades

def report(label, all_returns):
    if not all_returns:
        print(f'  {label}: no trades'); return
    n   = len(all_returns)
    avg = sum(all_returns)/n
    wr  = sum(1 for r in all_returns if r > 0)/n*100
    neg = sum(1 for r in all_returns if r < 0)
    ge10= sum(1 for r in all_returns if r >= 10)
    med = statistics.median(all_returns)
    std = statistics.stdev(all_returns) if n > 1 else 0
    print(f'  {label}: n={n:4d}  avg={avg:+.1f}%  med={med:+.1f}%  std={std:.1f}%  '
          f'WR={wr:.1f}%  neg={neg}  >=10%={ge10}')

random.seed(42)

for period, cond, min_ticks in [
    ('OLD',  "ts < '2026-06-01'",  750),
    ('NEW',  "ts >= '2026-06-01'", 750),
]:
    mkt_rows = db.execute(f'''
        SELECT market_ticker FROM ticks WHERE {cond}
        GROUP BY market_ticker HAVING COUNT(*) >= {min_ticks}
    ''').fetchall()
    tickers = [r[0] for r in mkt_rows]
    sample  = random.sample(tickers, min(200, len(tickers)))
    print(f'\n{period} ({len(tickers)} markets, sampling {len(sample)}):')

    results = {1: [], 3: [], 9: []}
    for ticker in sample:
        rows = db.execute(
            'SELECT secs_remaining,delta_long,delta_short,gap,btc,up,down,series '
            'FROM ticks WHERE market_ticker=? ORDER BY ts ASC', [ticker]).fetchall()
        ticks = [dict(r) for r in rows]
        for stride in results:
            for t in simulate(ticks, cfg, stride=stride):
                results[stride].append(t['return_pct'])

    report('stride=1 (every tick) ', results[1])
    report('stride=3 (every 3rd)  ', results[3])
    report('stride=9 (every 9th)  ', results[9])

db.close()
