"""
Walk-forward backtest of the evolution engine.

At each step:
  1. Evolve for N_GENS generations using the trailing MARKET_SAMPLE markets
     (the fitness window — same slice the live daemon uses)
  2. Test the resulting champion on the next STEP_MARKETS markets
     (out-of-sample: never seen during this step's evolution)
  3. Advance the window by STEP_MARKETS and repeat

This gives genuine out-of-sample performance by construction.
Population is carried forward between steps (mimics the live daemon).
"""

import sqlite3, json, math, random, time, copy, statistics, multiprocessing
from evolve import (
    Genome, _crow3_tree, _crow60_tree, _crow_a_tree, _random_genome, _simulate, _fitness,
    _mutate, _crossover, _eval_genome,
    ELITE_COUNT, MARKET_SAMPLE, MIN_TRADES,
    _SAFE, _ensure_table, _save_genome, _get_db,
)

N_GENS          = 6       # generations per step
POPULATION_SIZE = 30      # smaller than live for speed
STEP_MARKETS    = 5       # out-of-sample markets tested per step
CULL_FRACTION   = 0.70
WORKERS         = max(1, multiprocessing.cpu_count() - 1)
DB_PATH         = 'crow.db'
COMPARE_CONFIG  = 194     # Crow-154

# ── DB helpers ─────────────────────────────────────────────────────────────────

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

def get_all_markets(db):
    rows = db.execute('''
        SELECT market_ticker FROM ticks
        GROUP BY market_ticker
        HAVING COUNT(*) >= 500 AND MIN(secs_remaining) = 0
        ORDER BY MAX(ts) ASC
    ''').fetchall()
    return [r[0] for r in rows]

# ── Evolution helpers ──────────────────────────────────────────────────────────

def eval_population(pool, population, tick_cache, markets):
    args = [(g, tick_cache, markets) for g in population]
    return pool.map(_eval_genome, args)

def breed(survivors, rng, gen):
    n_new = POPULATION_SIZE - len(survivors)
    pool  = survivors[:max(5, len(survivors) // 2)]
    children = []
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
        children.append(child)
    return children

def evolve_step(population, tick_cache, window_markets, rng):
    """Run N_GENS of evolution. Returns (champion, updated_population, final_survivors)."""
    for g in population:
        g.fitness = None

    best = None
    final_survivors = []
    with multiprocessing.Pool(processes=WORKERS) as pool:
        for gen in range(1, N_GENS + 1):
            population = eval_population(pool, population, tick_cache, window_markets)
            valid   = [g for g in population if g.fitness is not None and math.isfinite(g.fitness)]
            valid.sort(key=lambda g: g.fitness, reverse=True)
            if valid and (best is None or valid[0].fitness > best.fitness):
                best = copy.deepcopy(valid[0])
            keep_n    = max(ELITE_COUNT, int(len(valid) * (1 - CULL_FRACTION)))
            survivors = valid[:keep_n]
            final_survivors = [copy.deepcopy(g) for g in survivors]
            population = survivors + breed(survivors, rng, gen)

    return best, population, final_survivors

def simulate_returns(ticker_list, tick_cache, cfg):
    returns = []
    for t in ticker_list:
        ticks = tick_cache.get(t)
        if ticks:
            try:
                returns.extend(_simulate(ticks, cfg))
            except:
                pass
    return returns

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    _ensure_table()  # create evo_population, evo_gen_log, evo_champion_history if needed

    rng = random.Random(42)

    db = get_db()
    all_markets = get_all_markets(db)
    total = len(all_markets)
    print(f'Total markets: {total}')
    print(f'Warmup window: {MARKET_SAMPLE}  Step: {STEP_MARKETS}  Gens/step: {N_GENS}  Pop: {POPULATION_SIZE}  Workers: {WORKERS}')

    test_markets_total = total - MARKET_SAMPLE
    n_steps = math.ceil(test_markets_total / STEP_MARKETS)
    print(f'Out-of-sample markets: {test_markets_total}  Steps: {n_steps}')

    print('Loading tick data...')
    all_ticks = {t: load_ticks(db, t) for t in all_markets}

    # Market start timestamps (first tick ts) — needed for champion history promoted_at
    print('Loading market timestamps...')
    market_start_ts = {}
    for ticker in all_markets:
        row = db.execute('SELECT MIN(ts) FROM ticks WHERE market_ticker=?', [ticker]).fetchone()
        if row and row[0]:
            market_start_ts[ticker] = row[0]

    compare_cfg = None
    row = db.execute('SELECT * FROM configs WHERE id=?', [COMPARE_CONFIG]).fetchone()
    if row:
        compare_cfg = dict(row)
        print(f'Baseline: {compare_cfg["name"]}  formula={compare_cfg["formula"][:60]}')
    db.close()

    # ── Seed initial population ────────────────────────────────────────────────
    population = []
    for tree_fn in [_crow3_tree] * 5 + [_crow_a_tree] * 5:
        g = Genome(
            tree           = tree_fn(),
            threshold      = rng.choice([0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 50.0]),
            exit_threshold = rng.choice([0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 50.0]),
            short_lookback = rng.choice([10, 12, 15, 18, 20, 25]),
            rsi_period     = rng.choice([28, 42, 60]),
            regime_type    = rng.choice(['none', 'none', 'none', 'extreme']),
            regime_lo      = rng.uniform(28, 42),
            regime_hi      = rng.uniform(58, 75),
            min_entry      = rng.uniform(1, 20),
            max_entry      = rng.uniform(25, 49),
            cooldown       = rng.choice([0, 0, 15, 30]),
            exit_cooldown  = rng.choice([0, 0, 15, 30]),
        )
        population.append(g)
    while len(population) < POPULATION_SIZE:
        population.append(_random_genome(rng))

    # ── Walk-forward loop ──────────────────────────────────────────────────────
    evo_all_returns    = []
    crow154_all_returns = []
    step_log           = []
    seed_pool          = []   # accumulates evaluated genomes for daemon seeding

    hdr = (f'{"Step":>4}  {"OOS markets":>11}  '
           f'{"Fit n":>5}  {"Fit avg":>8}  {"Fit WR":>6}  '
           f'{"OOS n":>5}  {"OOS avg":>8}  {"OOS WR":>6}  {"t":>5}')
    print(f'\n{hdr}')
    print('-' * len(hdr))

    pos = MARKET_SAMPLE
    step_num = 0

    while pos < total:
        step_num += 1
        window_mkts = all_markets[max(0, pos - MARKET_SAMPLE):pos]
        oos_mkts    = all_markets[pos:pos + STEP_MARKETS]

        window_cache = {t: all_ticks[t] for t in window_mkts}

        t0 = time.time()
        champ, population, survivors = evolve_step(population, window_cache, window_mkts, rng)
        elapsed = time.time() - t0
        seed_pool.extend(survivors)

        # Log champion to history so the live sim knows who was active for these markets
        save_champion_history(champ, oos_mkts, market_start_ts)

        # Out-of-sample trades (never seen during this step's evolution)
        oos_returns = simulate_returns(oos_mkts, all_ticks, champ.to_cfg() if champ else {})
        oos_n   = len(oos_returns)
        oos_avg = sum(oos_returns) / oos_n if oos_n else float('nan')
        oos_wr  = sum(1 for r in oos_returns if r > 0) / oos_n * 100 if oos_n else float('nan')

        evo_all_returns.extend(oos_returns)

        # Crow-154 on same OOS markets
        if compare_cfg:
            crow154_all_returns.extend(simulate_returns(oos_mkts, all_ticks, compare_cfg))

        step_log.append({
            'step': step_num, 'oos_mkts': oos_mkts,
            'fit_n': champ.trades_n if champ else 0,
            'fit_avg': champ.trades_avg if champ else 0,
            'fit_wr': champ.trades_wr if champ else 0,
            'oos_n': oos_n, 'oos_avg': oos_avg, 'oos_wr': oos_wr,
        })

        fit_n   = champ.trades_n   if champ else 0
        fit_avg = champ.trades_avg if champ else 0.0
        fit_wr  = champ.trades_wr  if champ else 0.0

        print(f'{step_num:>4}  {len(oos_mkts):>11}  '
              f'{fit_n:>5}  {fit_avg:>+8.1f}%  {fit_wr:>5.1f}%  '
              f'{oos_n:>5}  {oos_avg:>+8.1f}%  {oos_wr:>5.1f}%  {elapsed:>4.1f}s',
              flush=True)

        pos += STEP_MARKETS

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f'\n{"="*65}')
    print('WALK-FORWARD SUMMARY (all OOS trades)')

    n = len(evo_all_returns)
    if n:
        avg = sum(evo_all_returns) / n
        wr  = sum(1 for r in evo_all_returns if r > 0) / n * 100
        neg = sum(1 for r in evo_all_returns if r < 0)
        std = statistics.stdev(evo_all_returns) if n > 1 else 0
        print(f'  EVOLVED:  n={n}  avg={avg:+.1f}%  wr={wr:.1f}%  neg={neg}  std={std:.1f}%')
    else:
        print('  EVOLVED:  no trades')

    n2 = len(crow154_all_returns)
    if n2 and compare_cfg:
        avg2 = sum(crow154_all_returns) / n2
        wr2  = sum(1 for r in crow154_all_returns if r > 0) / n2 * 100
        neg2 = sum(1 for r in crow154_all_returns if r < 0)
        std2 = statistics.stdev(crow154_all_returns) if n2 > 1 else 0
        print(f'  {compare_cfg["name"]}:  n={n2}  avg={avg2:+.1f}%  wr={wr2:.1f}%  neg={neg2}  std={std2:.1f}%')

    # Per-step rolling avg (smoothed view)
    if step_log:
        oos_avgs = [s['oos_avg'] for s in step_log if math.isfinite(s.get('oos_avg', float('nan')))]
        if oos_avgs:
            print(f'\n  Per-step OOS avg  min={min(oos_avgs):+.1f}%  max={max(oos_avgs):+.1f}%  '
                  f'mean={sum(oos_avgs)/len(oos_avgs):+.1f}%')
        steps_with_trades = sum(1 for s in step_log if s['oos_n'] > 0)
        steps_profitable  = sum(1 for s in step_log
                                if s['oos_n'] > 0 and math.isfinite(s.get('oos_avg', float('nan')))
                                and s['oos_avg'] > 0)
        print(f'  Steps with OOS trades: {steps_with_trades}/{len(step_log)}  '
              f'profitable steps: {steps_profitable}')

    save_final_population(seed_pool, step_num)

def save_champion_history(champion, oos_mkts, market_start_ts):
    """Log the step champion to evo_champion_history with promoted_at = start of first OOS market."""
    if not champion or not oos_mkts:
        return
    promoted_at = market_start_ts.get(oos_mkts[0])
    if not promoted_at:
        return
    cfg = champion.to_cfg()
    db = _get_db()
    db.execute('''INSERT INTO evo_champion_history
        (promoted_at, formula, threshold, exit_threshold, short_lookback, rsi_period,
         regime_filter, min_entry, max_entry, cooldown, exit_cooldown,
         sell_limit, stop_loss, loss_limit, fitness, trades_n, trades_wr, trades_avg)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [promoted_at, cfg['formula'], cfg['threshold'], cfg['exit_threshold'],
         cfg['short_lookback'], champion.rsi_period, cfg['regime_filter'],
         cfg['min_entry'], cfg['max_entry'], cfg['cooldown'], cfg['exit_cooldown'],
         cfg['sell_limit'], cfg['stop_loss'], cfg['loss_limit'],
         champion.fitness, champion.trades_n, champion.trades_wr, champion.trades_avg])
    db.commit(); db.close()


def save_final_population(seed_pool, step_num):
    """Persist the best evolved genomes to evo_population for daemon seeding.

    seed_pool is the accumulated survivors from every walk-forward step — all
    have been evaluated at least once, so fitness is finite.  We sort by
    fitness and keep the top 200 to give the daemon a strong starting gene pool.
    """
    _ensure_table()
    valid = [g for g in seed_pool
             if g.fitness is not None and math.isfinite(g.fitness)]
    valid.sort(key=lambda g: g.fitness, reverse=True)
    top = valid[:200]
    saved = 0
    for g in top:
        try:
            g.db_id = None  # force INSERT
            _save_genome(g, step_num)
            saved += 1
        except Exception as e:
            print(f'  save error: {e}')
    print(f'Saved {saved}/{len(top)} top genomes to evo_population for daemon seeding '
          f'(pool had {len(valid)} evaluated genomes).')


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
