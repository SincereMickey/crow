"""
Backtest the evolution engine against existing data.

Evolves for N_GENERATIONS using the last 30 markets as the fitness window,
then evaluates the best genome on ALL available markets and compares to Crow-154.
"""

import sqlite3, json, math, random, time, copy, statistics, multiprocessing
from evolve import (
    Genome, _crow3_tree, _random_genome, _simulate, _fitness,
    _mutate, _crossover, _all_nodes, _eval_genome,
    ELITE_COUNT, MARKET_SAMPLE, MIN_TRADES,
    _SAFE, _FormulaStrNode,
)

N_GENERATIONS    = 50
POPULATION_SIZE  = 50
CULL_FRACTION    = 0.70
WORKERS          = max(1, multiprocessing.cpu_count() - 1)
DB_PATH          = 'crow.db'
COMPARE_CONFIG   = 194   # Crow-154

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    return db

def load_ticks(db, ticker):
    rows = db.execute(
        'SELECT secs_remaining,delta_long,delta_short,gap,btc,up,down,series,target_price '
        'FROM ticks WHERE market_ticker=? ORDER BY ts ASC', [ticker]
    ).fetchall()
    return [dict(r) for r in rows]

def get_recent_markets(db, n):
    rows = db.execute('''
        SELECT market_ticker FROM ticks
        GROUP BY market_ticker
        HAVING COUNT(*) >= 500 AND MIN(secs_remaining) = 0
        ORDER BY MAX(ts) DESC LIMIT ?
    ''', [n]).fetchall()
    return [r[0] for r in rows]

def get_all_markets(db):
    rows = db.execute('''
        SELECT market_ticker FROM ticks
        GROUP BY market_ticker
        HAVING COUNT(*) >= 500 AND MIN(secs_remaining) = 0
        ORDER BY MAX(ts) ASC
    ''').fetchall()
    return [r[0] for r in rows]

# ── Evaluation ────────────────────────────────────────────────────────────────

def eval_population(population, tick_cache, markets):
    args = [(g, tick_cache, markets) for g in population]
    with multiprocessing.Pool(processes=WORKERS) as pool:
        results = pool.map(_eval_genome, args)
    return results

def full_eval(genome, all_tick_cache):
    """Evaluate genome on ALL markets. Returns (avg, wr, neg, n, per_market_avgs)."""
    cfg = genome.to_cfg()
    all_returns = []
    per_market  = []
    for ticker, ticks in all_tick_cache.items():
        try:
            rets = _simulate(ticks, cfg)
            all_returns.extend(rets)
            if rets:
                per_market.append(sum(rets)/len(rets))
        except: pass
    if not all_returns:
        return 0, 0, 0, 0, []
    n   = len(all_returns)
    avg = sum(all_returns) / n
    wr  = sum(1 for r in all_returns if r > 0) / n * 100
    neg = sum(1 for r in all_returns if r < 0)
    return avg, wr, neg, n, per_market

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    rng = random.Random(42)

    db = get_db()
    recent_markets = get_recent_markets(db, MARKET_SAMPLE)
    all_markets    = get_all_markets(db)
    print(f'Markets for fitness window: {len(recent_markets)}')
    print(f'Markets for full eval:      {len(all_markets)}')

    train_cache = {t: load_ticks(db, t) for t in recent_markets}
    all_cache   = {t: load_ticks(db, t) for t in all_markets}

    # Load Crow-154 for comparison
    compare_cfg = None
    row = db.execute('SELECT * FROM configs WHERE id=?', [COMPARE_CONFIG]).fetchone()
    if row:
        compare_cfg = dict(row)
        print(f'Baseline: {compare_cfg["name"]}  formula={compare_cfg["formula"][:60]}')
    db.close()

    # ── Seed population ───────────────────────────────────────────────────────
    population = []
    for _ in range(15):
        g = Genome(
            tree           = _crow3_tree(),
            threshold      = rng.choice([0.5,1.0,2.0,5.0,10.0,20.0,30.0,50.0]),
            exit_threshold = rng.choice([0.5,1.0,2.0,5.0,10.0,20.0,30.0,50.0]),
            short_lookback = rng.choice([10,12,15,18,20,25]),
            rsi_period     = rng.choice([28,42,60]),
            regime_type    = rng.choice(['none','extreme','extreme']),
            regime_lo      = rng.uniform(28,42),
            regime_hi      = rng.uniform(58,75),
            min_entry      = rng.uniform(1,20),
            max_entry      = rng.uniform(25,49),
            cooldown       = rng.choice([0,0,15,30]),
            exit_cooldown  = rng.choice([0,0,15,30]),
        )
        population.append(g)
    while len(population) < POPULATION_SIZE:
        population.append(_random_genome(rng))

    best_ever = None

    # ── Evolution loop ────────────────────────────────────────────────────────
    for gen in range(1, N_GENERATIONS + 1):
        t0 = time.time()

        population = eval_population(population, train_cache, recent_markets)

        valid   = [g for g in population if g.fitness is not None and math.isfinite(g.fitness)]
        invalid = [g for g in population if g not in valid]
        valid.sort(key=lambda g: g.fitness, reverse=True)

        best = valid[0] if valid else None
        if best and (best_ever is None or best.fitness > best_ever.fitness):
            best_ever = copy.deepcopy(best)

        elapsed = time.time() - t0
        print(f'Gen {gen:3d}  '
              f'best_fit={best.fitness:+.4f}  '
              f'n={best.trades_n}  '
              f'wr={best.trades_wr:.1f}%  '
              f'avg={best.trades_avg:+.1f}%  '
              f'({elapsed:.1f}s)',
              flush=True)

        # Cull
        keep_n    = max(ELITE_COUNT, int(len(valid) * (1 - CULL_FRACTION)))
        survivors = valid[:keep_n]
        pool      = survivors[:max(5, len(survivors)//2)]

        # Breed
        n_new = POPULATION_SIZE - len(survivors)
        new_genomes = []
        for _ in range(n_new):
            r = rng.random()
            if r < 0.50 and len(pool) >= 2:
                pa, pb = rng.sample(pool, 2)
                child  = _crossover(pa, pb, rng)
                child  = _mutate(child, rng, strength=0.5)
            elif r < 0.80 and pool:
                child = _mutate(rng.choice(pool), rng, strength=1.0)
            else:
                child = _random_genome(rng, gen=gen)
            child.generation = gen
            new_genomes.append(child)

        population = survivors + new_genomes

    # ── Full evaluation of winner ─────────────────────────────────────────────
    print(f'\n{"="*60}')
    print(f'BEST GENOME (fitness window)')
    print(f'  formula:  {best_ever.formula}')
    print(f'  regime:   {best_ever.regime_filter}')
    print(f'  threshold={best_ever.threshold:.3f}  exit={best_ever.exit_threshold:.3f}')
    print(f'  lb={best_ever.short_lookback}  rsi_period={best_ever.rsi_period}')
    print(f'  entry=[{best_ever.min_entry:.1f}, {best_ever.max_entry:.1f}]  '
          f'cooldown={best_ever.cooldown}  exit_cd={best_ever.exit_cooldown}')
    print(f'  fitness_window: n={best_ever.trades_n}  wr={best_ever.trades_wr:.1f}%  avg={best_ever.trades_avg:+.1f}%')

    print(f'\nEvaluating on all {len(all_markets)} markets...')
    avg, wr, neg, n, pm = full_eval(best_ever, all_cache)
    std = statistics.stdev(pm) if len(pm) > 1 else 0
    print(f'  EVOLVED:  n={n}  avg={avg:+.1f}%  wr={wr:.1f}%  neg={neg}  markets_traded={len(pm)}  std_per_mkt={std:.1f}%')

    if compare_cfg:
        class _CfgGenome:
            def to_cfg(self): return compare_cfg
        avg2, wr2, neg2, n2, pm2 = full_eval(_CfgGenome(), all_cache)
        std2 = statistics.stdev(pm2) if len(pm2) > 1 else 0
        print(f'  {compare_cfg["name"]}:  n={n2}  avg={avg2:+.1f}%  wr={wr2:.1f}%  neg={neg2}  markets_traded={len(pm2)}  std_per_mkt={std2:.1f}%')

if __name__ == '__main__':
    main()
