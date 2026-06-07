"""
Crow API helper for autonomous backtest experiments.
Usage:
    from crow_api import *
    configs = list_configs()
    result  = backtest(config_id=1, market_count=40)
    summary(result)
"""

import os, time, json
import requests

BASE     = 'http://localhost:7429'
_key_file = os.path.join(os.path.dirname(__file__), 'crow.key')
API_KEY  = open(_key_file).read().strip()
HEADERS  = {'X-API-Key': API_KEY, 'Content-Type': 'application/json'}

# ── Raw HTTP ──────────────────────────────────────────────────────────────────

def _get(path):
    r = requests.get(BASE + path, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def _post(path, body=None):
    r = requests.post(BASE + path, json=body or {}, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def _put(path, body):
    r = requests.put(BASE + path, json=body, headers=HEADERS)
    r.raise_for_status()
    return r.json()

# ── Configs ───────────────────────────────────────────────────────────────────

def list_configs():
    """Return list of all configs."""
    return _get('/api/configs')

def get_config(cfg_id):
    configs = list_configs()
    return next((c for c in configs if c['id'] == cfg_id), None)

def create_config(formula, name=None, notes=None, **params):
    """Create a new config. Returns the created config dict."""
    body = {'formula': formula}
    if name:  body['name']  = name
    if notes: body['notes'] = notes
    body.update(params)
    return _post('/api/configs', body)

def update_config(cfg_id, **kwargs):
    """Update fields on an existing config. Returns updated config dict."""
    return _put(f'/api/configs/{cfg_id}', kwargs)

def duplicate_config(cfg_id, notes=None, **overrides):
    """Clone a config, auto-naming it, with optional field overrides."""
    src = get_config(cfg_id)
    if not src:
        raise ValueError(f'Config {cfg_id} not found')
    body = {k: src[k] for k in (
        'formula','threshold','exit_threshold','short_lookback','exit_lookback',
        'tuner','cooldown','loss_limit','sell_limit','exit_cooldown',
        'min_entry','max_entry','trade_amount',
    ) if src.get(k) is not None}
    body['notes'] = notes or f"Copy of {src['name']}"
    body.update(overrides)
    return _post('/api/configs', body)

# ── Markets ───────────────────────────────────────────────────────────────────

def list_markets():
    """Return list of completed markets with tick counts."""
    return _get('/api/markets')

# ── Backtest ──────────────────────────────────────────────────────────────────

def start_backtest(config_id, market_count=40, seed=None):
    """Start a backtest job. Returns {job_id, seed, total}."""
    body = {'config_id': config_id, 'market_count': market_count}
    if seed:
        body['seed'] = seed
    return _post('/api/backtest', body)

def poll_backtest(job_id):
    """Return current job status dict."""
    return _get(f'/api/backtest/{job_id}')

def wait_backtest(job_id, poll_interval=1.5, verbose=True):
    """Block until job finishes, printing progress. Returns result dict."""
    while True:
        result = poll_backtest(job_id)
        if result.get('status') in ('done', 'error'):
            if verbose:
                print()
            return result
        if verbose:
            p = result.get('progress', {})
            print(f"\r  {p.get('done',0)}/{p.get('total',0)} markets…", end='', flush=True)
        time.sleep(poll_interval)

def backtest(config_id, market_count=40, seed=None, verbose=True):
    """Run a full backtest and return the completed result dict."""
    job = start_backtest(config_id, market_count, seed)
    if 'error' in job and 'job_id' not in job:
        raise RuntimeError(job['error'])
    if verbose:
        print(f"Job {job['job_id']}  seed={job['seed']}")
    return wait_backtest(job['job_id'], verbose=verbose)

# ── Analysis ──────────────────────────────────────────────────────────────────

def summary(result, label=None):
    """Print a one-line summary of a backtest result. Returns the summary dict."""
    s = result.get('summary', {})
    tag = f"[{label}] " if label else ''
    wr  = s.get('win_rate', 0)
    ret = s.get('total_return', 0)
    avg = s.get('avg_return', 0)
    print(
        f"{tag}seed={result.get('seed')}  "
        f"markets={s.get('markets_tested')}({s.get('markets_with_trades')} w/trades)  "
        f"trades={s.get('total_trades')}  "
        f"WR={wr}%  "
        f"return={ret:+.2f}%  avg={avg:+.2f}%"
    )
    return s

def trades_flat(result):
    """Return a flat list of all trades across all markets, each with market_ticker."""
    out = []
    for m in result.get('markets', []):
        for t in m.get('trades', []):
            out.append({**t, 'market_ticker': m['market_ticker']})
    return out

def compare(results, labels=None):
    """Print a comparison table of multiple backtest results."""
    labels = labels or [r.get('seed', f'run{i}') for i, r in enumerate(results)]
    header = f"{'Label':<40} {'Markets':>8} {'Trades':>7} {'WR':>6} {'Return':>9} {'Avg':>8}"
    print(header)
    print('-' * len(header))
    for lbl, r in zip(labels, results):
        s = r.get('summary', {})
        print(
            f"{str(lbl):<40} "
            f"{s.get('markets_with_trades',0):>8} "
            f"{s.get('total_trades',0):>7} "
            f"{s.get('win_rate',0):>5.1f}% "
            f"{s.get('total_return',0):>+8.2f}% "
            f"{s.get('avg_return',0):>+7.2f}%"
        )
