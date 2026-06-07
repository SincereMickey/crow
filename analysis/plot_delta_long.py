import json
import re
import requests
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from datetime import datetime, timezone, timedelta
import time

RECORD_PATH = 'records/crow-2.json'

# ── Load trades ────────────────────────────────────────────────
with open(RECORD_PATH) as f:
    trades = json.load(f)

# ── Parse close time from market ticker ───────────────────────
# Ticker format: KXBTC15M-26JUN02HHMM-15  (time = close time in EDT = UTC-4)
def parse_close_time(market_ticker):
    m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-', market_ticker)
    if not m:
        raise ValueError(f'Cannot parse ticker: {market_ticker}')
    yy, mon, dd, hh, mm = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    month_map = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
                 'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    dt_edt = datetime(2000 + int(yy), month_map[mon], int(dd),
                      int(hh), int(mm), tzinfo=timezone(timedelta(hours=-4)))
    return dt_edt.astimezone(timezone.utc)

# ── Only fetch what's missing ──────────────────────────────────
needs_strike = [t for t in trades if 'strike' not in t]
needs_btc    = [t for t in trades if 'btcAtEntry' not in t]

# ── Fetch strike prices from Kalshi ───────────────────────────
if needs_strike:
    def get_strike(market_ticker):
        event_ticker = market_ticker.rsplit('-', 1)[0]
        url = f'https://api.elections.kalshi.com/v1/cached/events/?tickers={event_ticker}'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        for event in data.get('events', []):
            for m in event.get('markets', []):
                fs = m.get('floor_strike')
                if fs is not None:
                    return float(fs)
        raise ValueError(f'No floor_strike found for {event_ticker}')

    print('Fetching missing strike prices...')
    unique_tickers = list(dict.fromkeys(t['marketTicker'] for t in needs_strike))
    strike_map = {}
    for ticker in unique_tickers:
        try:
            strike_map[ticker] = get_strike(ticker)
            print(f'  {ticker}: {strike_map[ticker]:,.2f}')
        except Exception as e:
            print(f'  {ticker}: ERROR {e}')
        time.sleep(0.15)

    for t in trades:
        if 'strike' not in t and t['marketTicker'] in strike_map:
            t['strike'] = strike_map[t['marketTicker']]

# ── Fetch BTC prices from Kraken ──────────────────────────────
if needs_btc:
    def fetch_kraken_candles(date_str='2026-06-02'):
        since = int(datetime.fromisoformat(f'{date_str}T07:00:00+00:00').timestamp())
        url = 'https://api.kraken.com/0/public/OHLC'
        candles = {}
        for start in [since, since + 360*60]:
            r = requests.get(url, params={'pair': 'XBTUSD', 'interval': 1, 'since': start}, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get('error'):
                raise ValueError(data['error'])
            ohlc = data['result'].get('XXBTZUSD') or data['result'].get('XBTUSD', [])
            for c in ohlc:
                candles[c[0]] = float(c[4])
            time.sleep(1.5)
        return candles

    print('\nFetching BTC 1m candles from Kraken (bulk)...')
    kraken_candles = fetch_kraken_candles()
    print(f'  Got {len(kraken_candles)} candles')

    def get_btc_price_at(iso_time):
        dt  = datetime.fromisoformat(iso_time.replace('Z', '+00:00'))
        ts  = int(dt.timestamp())
        minute_ts = (ts // 60) * 60
        for candidate in [minute_ts, minute_ts - 60, minute_ts + 60]:
            if candidate in kraken_candles:
                return kraken_candles[candidate]
        raise ValueError(f'No candle for {iso_time}')

    for t in trades:
        if 'btcAtEntry' not in t:
            try:
                t['btcAtEntry'] = get_btc_price_at(t['entryTime'])
            except Exception as e:
                print(f'  {t["entryTime"]}: ERROR {e}')

# ── Compute derived fields and backfill ───────────────────────
for t in trades:
    if 'strike' in t and 'btcAtEntry' in t and 'deltaLong' not in t:
        t['deltaLong'] = round(t['btcAtEntry'] - t['strike'], 2)
    if 'secsRemaining' not in t:
        try:
            close_utc  = parse_close_time(t['marketTicker'])
            entry_utc  = datetime.fromisoformat(t['entryTime'].replace('Z', '+00:00'))
            t['secsRemaining'] = max(0, int((close_utc - entry_utc).total_seconds()))
        except Exception as e:
            print(f'  secsRemaining error for {t["marketTicker"]}: {e}')

# ── Save enriched records ──────────────────────────────────────
with open(RECORD_PATH, 'w') as f:
    json.dump(trades, f, indent=4)
print(f'\nBackfilled and saved {RECORD_PATH}')

# ── Build plot points ──────────────────────────────────────────
points = [t for t in trades if 'deltaLong' in t and 'secsRemaining' in t]
print(f'{len(points)} trades with complete data\n')
for p in points:
    print(f"  {p['entryTime'][:16]}  {p['side']:4}  dL={p['deltaLong']:+.0f}  secs={p['secsRemaining']}  {p['result']}")

# ── Plot ───────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(12, 7))
fig.patch.set_facecolor('#0d0d0d')
ax.set_facecolor('#0d0d0d')

color_map  = {'win': '#22c55e', 'loss': '#ef4444'}
marker_map = {'Up': '^', 'Down': 'v'}

for p in points:
    ax.scatter(
        p['deltaLong'], p['secsRemaining'],
        color=color_map[p['result']],
        marker=marker_map[p['side']],
        s=110, alpha=0.88, zorder=3,
        edgecolors='#222', linewidths=0.4,
    )
    label = f"{p['side'][0]} {p['entryPrice']}c"
    ax.annotate(label, (p['deltaLong'], p['secsRemaining']),
                textcoords='offset points', xytext=(6, 4),
                fontsize=7.5, color='#777')

ax.axvline(0, color='#444', linewidth=0.8, linestyle='--')

ax.set_xlabel('deltaLong at entry  (BTC spot - BRTI strike,  $)', color='#aaa', fontsize=11)
ax.set_ylabel('Seconds remaining at entry', color='#aaa', fontsize=11)
ax.set_title('Crow-2  |  Seconds Remaining vs deltaLong at Entry', color='#eee', fontsize=13, pad=12)

ax.tick_params(colors='#666')
for spine in ax.spines.values():
    spine.set_edgecolor('#222')
ax.grid(True, color='#1e1e1e', linewidth=0.6)

win_patch  = mpatches.Patch(color='#22c55e', label='Win')
loss_patch = mpatches.Patch(color='#ef4444', label='Loss')
up_h   = plt.Line2D([0],[0], marker='^', color='w', markerfacecolor='#999', markersize=9, linestyle='None', label='Up')
down_h = plt.Line2D([0],[0], marker='v', color='w', markerfacecolor='#999', markersize=9, linestyle='None', label='Down')
ax.legend(handles=[win_patch, loss_patch, up_h, down_h],
          facecolor='#1a1a1a', edgecolor='#333', labelcolor='#aaa', fontsize=9)

n_win  = sum(1 for p in points if p['result'] == 'win')
n_loss = sum(1 for p in points if p['result'] == 'loss')
ax.text(0.01, 0.98, f'{n_win}W  {n_loss}L',
        transform=ax.transAxes, color='#666', fontsize=9, va='top')

plt.tight_layout()
out = 'crow-2_secs_vs_deltalong.png'
plt.savefig(out, dpi=150, facecolor=fig.get_facecolor())
print(f'Saved: {out}')
plt.show()
