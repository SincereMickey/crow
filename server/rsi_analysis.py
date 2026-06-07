"""
RSI analysis for Crow-152 trades.

Runs Crow-152 simulation on all historical markets, computes RSI at each entry,
and checks if RSI at entry predicts wins vs losses (testing transferability of
the old system's finding: RSI 20-29 and 40-49 good, RSI 30-39 and 50+ bad).

RSI period = 42 ticks (1-sec ticks, matching the old system's 14 × 3-sec snapshots).
Uses Wilder's EMA-based smoothing.
"""

import sqlite3
import json
import math

DB_PATH = 'C:/Users/brown/Documents/projects/15min_kalshi/server/crow.db'

# Crow-152 config (id=192)
CFG = {
    'threshold':      30.0,
    'exit_threshold': -1.0,
    'short_lookback': 15,
    'cooldown':       200.0,
    'stop_loss':      10.0,
    'exit_cooldown':  30.0,
    'min_entry':      0.0,
    'max_entry':      32.0,
    'formula': '(deltaLong + deltaShort*(secsRemaining/lookback)) / abs(gap) + (gap / 200)',
    'regime_filter':   None,
    'regime_lookback': 1,
    'regime_offset':   0,
}

RSI_PERIOD = 42  # ~42 seconds = 14 × 3-sec intervals (matches old system)

def compute_rsi(btc_prices, period=RSI_PERIOD):
    """Compute streaming RSI using Wilder's EMA. Returns list of RSI values (None for first period)."""
    rsi_vals = [None] * len(btc_prices)
    if len(btc_prices) < period + 1:
        return rsi_vals

    changes = [btc_prices[i] - btc_prices[i-1] for i in range(1, len(btc_prices))]
    gains   = [max(0, c) for c in changes]
    losses  = [max(0, -c) for c in changes]

    # Seed: simple average of first `period` values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_from_avgs(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    rsi_vals[period] = rsi_from_avgs(avg_gain, avg_loss)

    for i in range(period + 1, len(btc_prices)):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rsi_vals[i] = rsi_from_avgs(avg_gain, avg_loss)

    return rsi_vals


def simulate_with_rsi(ticks, cfg):
    """Run simulate_config and record RSI at each entry."""
    threshold      = float(cfg.get('threshold',      1.0))
    exit_threshold = float(cfg.get('exit_threshold', 0.0))
    short_lookback = int(  cfg.get('short_lookback', 15))
    cooldown       = float(cfg.get('cooldown',       15.0))
    stop_loss      = float(cfg.get('stop_loss',      0.0))
    exit_cooldown  = float(cfg.get('exit_cooldown',  30.0))
    max_entry      = float(cfg.get('max_entry',      100.0))
    formula        = cfg.get('formula', '')

    _safe = {'__builtins__': {}, 'abs': abs, 'max': max, 'min': min,
             'round': round, 'floor': math.floor, 'ceil': math.ceil}

    lookback = short_lookback

    # Precompute RSI for all ticks
    btc_prices = [t.get('btc') or 0 for t in ticks]
    rsi_vals   = compute_rsi(btc_prices)

    open_trade    = None
    last_score    = None
    last_sell_idx = None
    trades        = []

    for i, tick in enumerate(ticks):
        dlong = tick.get('delta_long')
        gap   = tick.get('gap')
        secs  = tick.get('secs_remaining')
        up    = tick.get('up')
        down  = tick.get('down')
        btc   = tick.get('btc') or 0

        dshort = tick.get('delta_short') or 0
        raw_series = tick.get('series')
        if raw_series:
            try:
                s = json.loads(raw_series) if isinstance(raw_series, str) else raw_series
                if s and len(s) >= 2:
                    at_lb  = s[-(lookback + 1)] if len(s) > lookback else s[0]
                    dshort = s[-1] - at_lb
            except Exception:
                pass

        gap_delta = 0.0
        if gap is not None and i >= lookback:
            past_gap = ticks[i - lookback].get('gap')
            if past_gap is not None:
                gap_delta = gap - past_gap

        score = None
        if dlong is not None and secs and secs != 0 and gap is not None:
            try:
                v = eval(formula, _safe, {
                    'deltaLong':     dlong,
                    'deltaShort':    dshort,
                    'gapDelta':      gap_delta,
                    'secsRemaining': secs,
                    'lookback':      lookback,
                    'gap':           gap,
                    'tuner':         1,
                    'btc':           btc,
                    'up':            up or 0,
                    'down':          down or 0,
                })
                score = float(v) if (isinstance(v, (int, float)) and not math.isnan(v) and not math.isinf(v)) else None
            except Exception:
                score = None

        # Exit check
        if open_trade is not None and score is not None:
            side = open_trade['side']
            px   = up if side == 'Up' else down
            if px is not None:
                triggered = (score < exit_threshold) if side == 'Up' else (score > -exit_threshold)
                stop_hit  = stop_loss > 0 and px <= stop_loss
                if triggered or stop_hit:
                    result = 'win' if px > open_trade['entry_price'] else 'loss'
                    trades.append({
                        **open_trade,
                        'exit_price': px,
                        'result':     result,
                        'return_pct': round((px / open_trade['entry_price'] - 1) * 100, 2),
                    })
                    open_trade    = None
                    last_sell_idx = i

        past_cooldown      = secs is not None and secs <= (900 - cooldown)
        past_exit_cooldown = last_sell_idx is None or (i - last_sell_idx) >= exit_cooldown

        if (open_trade is None and last_score is not None and score is not None
                and secs and past_cooldown and past_exit_cooldown):
            in_range = lambda px: px is not None and 0 <= px <= max_entry

            if last_score <= threshold and score > threshold and in_range(up):
                open_trade = {
                    'side': 'Up', 'entry_price': up, 'entry_tick': i,
                    'entry_rsi': rsi_vals[i],
                    'entry_secs': secs,
                }
            elif last_score >= -threshold and score < -threshold and in_range(down):
                open_trade = {
                    'side': 'Down', 'entry_price': down, 'entry_tick': i,
                    'entry_rsi': rsi_vals[i],
                    'entry_secs': secs,
                }

        last_score = score

    # Force-close open position
    if open_trade and ticks:
        side = open_trade['side']
        btc_final = strike = last_up = last_down = None
        for t in reversed(ticks):
            if btc_final is None: btc_final = t.get('btc')
            if strike    is None: strike    = t.get('target_price')
            if last_up   is None: last_up   = t.get('up')
            if last_down is None: last_down = t.get('down')
            if all(v is not None for v in (btc_final, strike, last_up, last_down)):
                break

        if btc_final is not None and strike is not None:
            up_won = btc_final >= strike
        elif last_up is not None and last_down is not None:
            up_won = last_up >= last_down
        else:
            up_won = True

        pos_won = (side == 'Up') == up_won
        px      = 100.0 if pos_won else 0.0
        result  = 'win' if px > open_trade['entry_price'] else 'loss'
        trades.append({
            **open_trade,
            'exit_price': px,
            'result':     result,
            'return_pct': round((px / open_trade['entry_price'] - 1) * 100, 2),
        })

    return trades


def rsi_bucket(rsi):
    if rsi is None:
        return 'None'
    if rsi < 20:
        return '<20'
    if rsi < 30:
        return '20-29'
    if rsi < 40:
        return '30-39'
    if rsi < 50:
        return '40-49'
    if rsi < 60:
        return '50-59'
    if rsi < 70:
        return '60-69'
    if rsi < 80:
        return '70-79'
    return '80+'


def main():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Fetch all market tickers
    markets = db.execute(
        'SELECT DISTINCT market_ticker FROM ticks ORDER BY market_ticker'
    ).fetchall()
    print(f'Total markets: {len(markets)}', flush=True)

    all_trades = []
    n_markets  = 0

    for row in markets:
        ticker = row['market_ticker']
        ticks_rows = db.execute('''
            SELECT ts, secs_remaining, delta_long, delta_short, gap, btc,
                   up, down, target_price, series
            FROM ticks WHERE market_ticker = ? ORDER BY ts ASC
        ''', [ticker]).fetchall()

        ticks = [dict(r) for r in ticks_rows]
        if len(ticks) < 100:
            continue

        trades = simulate_with_rsi(ticks, CFG)
        for t in trades:
            t['ticker'] = ticker
        all_trades.extend(trades)
        n_markets += 1

    db.close()

    print(f'\nSimulated {n_markets} markets, {len(all_trades)} total trades')

    if not all_trades:
        print('No trades found.')
        return

    total_wr  = sum(1 for t in all_trades if t['result'] == 'win') / len(all_trades)
    total_avg = sum(t['return_pct'] for t in all_trades) / len(all_trades)
    print(f'Overall: WR={total_wr*100:.1f}%  AvgRet={total_avg:+.1f}%  n={len(all_trades)}')

    # RSI bucket analysis
    print('\n=== RSI at Entry: Win Rate and Avg Return ===')
    buckets = ['<20', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+', 'None']
    print(f'{"RSI":>8}  {"n":>5}  {"WR":>6}  {"AvgRet":>8}  {"AvgEntry":>9}')
    print('-' * 50)
    for bkt in buckets:
        sub = [t for t in all_trades if rsi_bucket(t.get('entry_rsi')) == bkt]
        if not sub:
            continue
        wr  = sum(1 for t in sub if t['result'] == 'win') / len(sub)
        avg = sum(t['return_pct'] for t in sub) / len(sub)
        ae  = sum(t['entry_price'] for t in sub) / len(sub)
        flag = ' *** TOXIC' if (bkt == '30-39' and wr < 0.55) else ''
        flag = ' *** SWEET' if (bkt in ('20-29', '40-49') and wr > 0.65) else flag
        print(f'{bkt:>8}  {len(sub):>5}  {wr*100:>5.1f}%  {avg:>+7.1f}%  {ae:>8.1f}c{flag}')

    # Secs remaining at entry
    print('\n=== Secs Remaining at Entry (time-in-market) ===')
    secs_buckets = [(720, 900, '0-3min'), (540, 720, '3-6min'), (360, 540, '6-9min'),
                    (180, 360, '9-12min'), (0, 180, '12-15min')]
    print(f'{"Window":>10}  {"n":>5}  {"WR":>6}  {"AvgRet":>8}')
    print('-' * 40)
    for lo, hi, lbl in secs_buckets:
        sub = [t for t in all_trades if t.get('entry_secs') is not None and lo <= t['entry_secs'] < hi]
        if not sub:
            continue
        wr  = sum(1 for t in sub if t['result'] == 'win') / len(sub)
        avg = sum(t['return_pct'] for t in sub) / len(sub)
        print(f'{lbl:>10}  {len(sub):>5}  {wr*100:>5.1f}%  {avg:>+7.1f}%')

    # Side analysis
    print('\n=== By Side ===')
    for side in ('Up', 'Down'):
        sub = [t for t in all_trades if t['side'] == side]
        if not sub:
            continue
        wr  = sum(1 for t in sub if t['result'] == 'win') / len(sub)
        avg = sum(t['return_pct'] for t in sub) / len(sub)
        print(f'  {side:<6}  n={len(sub)}  WR={wr*100:.1f}%  AvgRet={avg:+.1f}%')

    # RSI × timing cross-tab (best combinations)
    print('\n=== RSI × Secs Remaining cross-tab (n>=5 only) ===')
    print(f'{"RSI":>8}  {"Window":>10}  {"n":>5}  {"WR":>6}  {"AvgRet":>8}')
    print('-' * 50)
    for bkt in buckets:
        for lo, hi, lbl in secs_buckets:
            sub = [t for t in all_trades
                   if rsi_bucket(t.get('entry_rsi')) == bkt
                   and t.get('entry_secs') is not None
                   and lo <= t['entry_secs'] < hi]
            if len(sub) < 5:
                continue
            wr  = sum(1 for t in sub if t['result'] == 'win') / len(sub)
            avg = sum(t['return_pct'] for t in sub) / len(sub)
            print(f'{bkt:>8}  {lbl:>10}  {len(sub):>5}  {wr*100:>5.1f}%  {avg:>+7.1f}%')


if __name__ == '__main__':
    main()
