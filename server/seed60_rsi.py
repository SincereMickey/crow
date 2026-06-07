"""60-seed validation for Crow-154 (RSI-filtered Crow-152)."""
import json, time, urllib.request, statistics, sys

API_KEY   = open('C:/Users/brown/Documents/projects/15min_kalshi/server/crow.key').read().strip()
BASE      = 'http://127.0.0.1:7429'
CONFIG_ID = 194   # Crow-154

def api(method, path, data=None):
    url = BASE + path
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
          headers={'X-API-Key': API_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def run_seed(seed, market_count=150):
    d = api('POST', '/api/backtest', {'config_id': CONFIG_ID, 'market_count': market_count, 'seed': seed})
    job_id = d.get('job_id', '')
    if not job_id:
        return None, None

    for _ in range(120):
        time.sleep(1)
        r = api('GET', f'/api/backtest/{job_id}')
        if r.get('status') == 'done' or r.get('summary'):
            trades = []
            for mkt in r.get('markets', []):
                trades.extend(mkt.get('trades', []))
            n   = len(trades)
            avg = sum(t.get('return_pct', 0) for t in trades) / n if n else 0.0
            return avg, n
        if r.get('status') == 'error':
            return None, None
    return None, None

seeds = [f"2026-06-05T00:00:00-{i*111}" for i in range(1, 61)]
returns = []
avg_ns  = []
neg     = 0
below10 = 0

print(f"=== 60-seed: Crow-154 RSI-filter (id={CONFIG_ID}) ===", flush=True)

for idx, seed in enumerate(seeds, 1):
    ret, n = run_seed(seed)
    if ret is None:
        print(f"  seed {idx} FAILED", flush=True)
        continue
    returns.append(ret)
    avg_ns.append(n or 0)
    if ret < 0:
        neg += 1
    if ret < 10:
        below10 += 1
    if idx % 10 == 0:
        mean = sum(returns) / len(returns)
        print(f"  [{idx}/60] mean={mean:+.1f}%  neg={neg}", flush=True)

if returns:
    mean   = sum(returns) / len(returns)
    median = statistics.median(returns)
    std    = statistics.stdev(returns) if len(returns) > 1 else 0
    ge10   = len(returns) - below10
    avgN   = sum(avg_ns) / len(avg_ns) if avg_ns else 0
    n_sum  = len(returns)

    print(f"\n  RESULT: mean={mean:+.1f}%  median={median:+.1f}%  std={std:.1f}%")
    print(f"          neg={neg}/{n_sum}  >=10%={ge10}/{n_sum}  avgN={avgN:.1f}")
    print()
    print("  BASELINE Crow-152: mean=+56.7%  neg=0/60  >=10%=60/60  avgN=24.0  std=13.2%")
    print(f"  Crow-154 RSI-filt: mean={mean:+.1f}%  neg={neg}/{n_sum}  >=10%={ge10}/{n_sum}  avgN={avgN:.1f}  std={std:.1f}%")
