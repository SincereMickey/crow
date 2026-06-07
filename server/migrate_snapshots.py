"""
Migrate historical snapshots (3-sec sampling) into crow.db ticks table.

Strategy: linear interpolation between consecutive snapshots to produce
~1 tick/second, matching the density expected by simulate_config.

Fields derived:
  target_price = btc_open_price  (BTC strike = price at market open)
  delta_long   = btc - target_price
  delta_short  = btc[i] - btc[i - DELTA_LB]  (rolling, after all ticks built)
  gap          = yes_ask - no_ask
  up           = yes_ask  (cost to enter UP position)
  down         = no_ask   (cost to enter DOWN position)
  secs_remaining = max(0, round(900 - time_since_open_sec))

series is stored as NULL; simulate_config falls back to stored delta_short.
gap_delta is recomputed on-the-fly by simulate_config from gap[i] - gap[i-lb].
"""

import sqlite3
from datetime import datetime, timezone

OLD_DB   = 'C:/Users/brown/Documents/sandbox/15 min btc/snapshots.db'
CROW_DB  = 'C:/Users/brown/Documents/projects/15min_kalshi/server/crow.db'

DELTA_LB      = 20    # ticks for delta_short lookback (= 20 seconds after upsampling)
MARKET_SECS   = 900   # 15-minute market
MIN_SECS_OPEN = 850   # skip incomplete markets
BATCH         = 5000  # rows per commit

def lerp(a, b, t):
    return a + (b - a) * t

def migrate():
    old  = sqlite3.connect(OLD_DB)
    old.row_factory = sqlite3.Row
    crow = sqlite3.connect(CROW_DB)

    # ── existing tickers ────────────────────────────────────────────
    existing = set(
        r[0] for r in crow.execute(
            'SELECT DISTINCT market_ticker FROM ticks WHERE market_ticker IS NOT NULL'
        ).fetchall()
    )
    print(f'Existing markets in crow.db: {len(existing)}')

    markets = old.execute(
        'SELECT ticker FROM market_opens ORDER BY open_time'
    ).fetchall()
    print(f'Markets in old DB: {len(markets)}')

    total_rows  = 0
    n_imported  = 0
    n_skipped   = 0
    n_short     = 0
    pending     = []

    def flush():
        nonlocal total_rows
        crow.executemany('''
            INSERT INTO ticks
                (ts, market_ticker, secs_remaining,
                 delta_long, delta_short, gap, btc, price_at_lookback,
                 target_price, up, down, has_open_trade)
            VALUES
                (:ts, :market_ticker, :secs_remaining,
                 :delta_long, :delta_short, :gap, :btc, :price_at_lookback,
                 :target_price, :up, :down, 0)
        ''', pending)
        crow.commit()
        total_rows += len(pending)
        pending.clear()

    for mkt_row in markets:
        ticker = mkt_row[0]

        if ticker in existing:
            n_skipped += 1
            continue

        snaps = old.execute('''
            SELECT ts, yes_ask, no_ask, btc_price, btc_open_price, time_since_open_sec
            FROM snapshots WHERE ticker = ? ORDER BY ts
        ''', [ticker]).fetchall()

        if not snaps:
            continue

        # Skip incomplete markets
        max_t = max(s['time_since_open_sec'] for s in snaps)
        if max_t < MIN_SECS_OPEN:
            n_short += 1
            continue

        target_price = snaps[0]['btc_open_price']

        # ── build interpolated ticks ──────────────────────────────
        ticks = []

        for j in range(len(snaps) - 1):
            s0, s1 = snaps[j], snaps[j + 1]
            dt = s1['ts'] - s0['ts']          # seconds between snapshots (~3)
            n  = max(1, round(dt))             # ticks to generate for this interval

            for k in range(n):
                alpha = k / dt

                btc   = lerp(s0['btc_price'], s1['btc_price'], alpha)
                up    = lerp(s0['yes_ask'],   s1['yes_ask'],   alpha)
                down  = lerp(s0['no_ask'],    s1['no_ask'],    alpha)
                t_oc  = lerp(s0['time_since_open_sec'],
                             s1['time_since_open_sec'], alpha)

                ts_iso = datetime.fromtimestamp(
                    s0['ts'] + k, tz=timezone.utc
                ).isoformat()

                ticks.append({
                    'ts':             ts_iso,
                    'market_ticker':  ticker,
                    'btc':            btc,
                    'up':             up,
                    'down':           down,
                    'gap':            up - down,
                    'delta_long':     btc - target_price,
                    'target_price':   target_price,
                    'secs_remaining': max(0, round(MARKET_SECS - t_oc)),
                    'delta_short':    0.0,       # filled below
                    'price_at_lookback': btc,    # filled below
                })

        # Add final snapshot as one tick
        s_last = snaps[-1]
        up_l   = s_last['yes_ask']
        dn_l   = s_last['no_ask']
        ticks.append({
            'ts':             datetime.fromtimestamp(s_last['ts'], tz=timezone.utc).isoformat(),
            'market_ticker':  ticker,
            'btc':            s_last['btc_price'],
            'up':             up_l,
            'down':           dn_l,
            'gap':            up_l - dn_l,
            'delta_long':     s_last['btc_price'] - target_price,
            'target_price':   target_price,
            'secs_remaining': max(0, round(MARKET_SECS - s_last['time_since_open_sec'])),
            'delta_short':    0.0,
            'price_at_lookback': s_last['btc_price'],
        })

        # ── compute delta_short from rolling BTC buffer ───────────
        btc_buf = [t['btc'] for t in ticks]
        for i, tick in enumerate(ticks):
            lb = max(0, i - DELTA_LB)
            tick['delta_short']      = btc_buf[i] - btc_buf[lb]
            tick['price_at_lookback'] = btc_buf[lb]

        pending.extend(ticks)
        n_imported += 1

        if len(pending) >= BATCH:
            flush()
            print(f'  {n_imported} markets imported, {total_rows} ticks total...', flush=True)

    if pending:
        flush()

    old.close()
    crow.close()

    print()
    print(f'Done.')
    print(f'  Imported:  {n_imported} markets, {total_rows} ticks')
    print(f'  Skipped (duplicate): {n_skipped}')
    print(f'  Skipped (incomplete): {n_short}')

if __name__ == '__main__':
    migrate()
