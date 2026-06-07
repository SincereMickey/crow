"""
Continuous evolutionary optimization of Crow trading configs.

Each genome = expression tree (formula) + numeric parameters.
Fitness = backtest over a rolling window of recent markets.
Best genome auto-promotes to live trading slot.
"""

import sqlite3, json, math, random, time, copy, logging, sys, os, urllib.request
import ast as _pyast
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config ────────────────────────────────────────────────────────────────────

POPULATION_SIZE    = 200
N_ISLANDS          = 4       # independent sub-populations
ISLAND_SIZE        = POPULATION_SIZE // N_ISLANDS   # 50 each
MIGRATION_INTERVAL = 5       # gens between inter-island exchanges
MIGRATION_COUNT    = 2       # top-N migrants per island per exchange
ELITE_COUNT        = 3       # elite survivors per island (× N_ISLANDS total)
CULL_FRACTION      = 0.7     # remove bottom 70% each generation
MARKET_SAMPLE      = 30      # use the most recent N completed markets
MIN_TRADES         = 15      # minimum trades for valid fitness
SIZE_BUDGET        = 20      # formula nodes before parsimony penalty kicks in
SIZE_PENALTY       = 0.01    # fitness multiplier reduction per node over budget
FORMULA_MAX_DEPTH  = 5
WORKERS            = 6       # parallel evaluation threads
PROMOTE_MARGIN     = 0.0     # promote if fitness > champion (any improvement)
GENERATION_SLEEP   = 5       # seconds between generations
SIMULATION_TRADE_AMOUNT = 1.0  # dollars — used for fee rounding in _simulate

DB_PATH           = 'crow.db'
KEY_PATH          = 'crow.key'
BASE_URL          = 'http://127.0.0.1:7429'
LOG_FILE          = 'evolve.log'
PID_FILE          = 'evolve.pid'
EVO_CHAMPION_NAME = 'Evo-Champion'

VARS = ['deltaLong', 'deltaShort', 'gapDelta', 'secsRemaining',
        'lookback', 'gap', 'rsi']

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Expression Tree ───────────────────────────────────────────────────────────

class Node:
    def to_str(self):  raise NotImplementedError
    def size(self):    raise NotImplementedError
    def clone(self):   return copy.deepcopy(self)

class VarNode(Node):
    def __init__(self, name):  self.name = name
    def to_str(self):          return self.name
    def size(self):            return 1

class ConstNode(Node):
    def __init__(self, v):     self.value = float(v)
    def to_str(self):
        v = self.value
        return str(int(v)) if v == int(v) and abs(v) < 10000 else str(round(v, 4))
    def size(self):            return 1

class BinNode(Node):
    def __init__(self, op, left, right):
        self.op, self.left, self.right = op, left, right
    def to_str(self):
        l, r = self.left.to_str(), self.right.to_str()
        if self.op == '/':
            return f'({l} / (abs({r}) + 0.01))'
        return f'({l} {self.op} {r})'
    def size(self): return 1 + self.left.size() + self.right.size()

class UnaryNode(Node):
    def __init__(self, op, child):
        self.op, self.child = op, child
    def to_str(self):
        c = self.child.to_str()
        return f'(-{c})' if self.op == 'neg' else f'{self.op}({c})'
    def size(self): return 1 + self.child.size()

_SAFE = {'__builtins__': {}, 'abs': abs, 'max': max, 'min': min,
         'round': round, 'floor': math.floor, 'ceil': math.ceil}

SIGNAL_VARS = ['deltaLong', 'deltaShort', 'gapDelta']  # must have at least one

def _is_degenerate(formula):
    """True if formula has no real momentum signal (deltaLong/deltaShort/gapDelta)."""
    return not any(v in formula for v in SIGNAL_VARS)

def _kalshi_fee(contracts, price_frac):
    """Taker fee in dollars: ceil(0.07 × C × P × (1-P))"""
    if price_frac <= 0 or price_frac >= 1:
        return 0.0
    return math.ceil(0.07 * contracts * price_frac * (1 - price_frac) * 100) / 100

def _random_terminal(rng):
    if rng.random() < 0.55:
        return VarNode(rng.choice(VARS))
    r = rng.random()
    if r < 0.25:   v = rng.uniform(-5, 5)
    elif r < 0.55: v = rng.uniform(-200, 200)
    elif r < 0.80: v = float(rng.choice([1,2,5,10,15,20,50,100,200,0.5,0.1,0.01]))
    else:          v = rng.uniform(-1000, 1000)
    return ConstNode(v)

def _random_tree(max_depth, rng, method='grow'):
    if max_depth == 0 or (method == 'grow' and rng.random() < 0.35):
        return _random_terminal(rng)
    op = rng.choice(['+', '-', '*', '/', '+', '-', '*', 'abs', 'neg'])
    if op in ('+', '-', '*', '/'):
        return BinNode(op, _random_tree(max_depth-1, rng, method),
                           _random_tree(max_depth-1, rng, method))
    return UnaryNode(op, _random_tree(max_depth-1, rng, method))

def _all_nodes(tree):
    """Return list of (node, setter) pairs for every node in the tree."""
    result = []
    def _walk(node, setter):
        result.append((node, setter))
        if isinstance(node, BinNode):
            _walk(node.left,  lambda n, nd=node: setattr(nd, 'left',  n))
            _walk(node.right, lambda n, nd=node: setattr(nd, 'right', n))
        elif isinstance(node, UnaryNode):
            _walk(node.child, lambda n, nd=node: setattr(nd, 'child', n))
    _walk(tree, None)
    return result

# ── Genome ────────────────────────────────────────────────────────────────────

@dataclass
class Genome:
    tree:           Node
    threshold:      float  = 1.0
    exit_threshold: float  = 1.0
    short_lookback: int    = 15
    rsi_period:     int    = 42
    regime_type:    str    = 'extreme'   # none | extreme | low | high
    regime_lo:      float  = 35.0
    regime_hi:      float  = 65.0
    min_entry:      float  = 1.0
    max_entry:      float  = 49.0
    cooldown:          int    = 0
    exit_cooldown:     int    = 0

    fitness:        Optional[float] = None
    trades_n:       int    = 0
    trades_wr:      float  = 0.0
    trades_avg:     float  = 0.0
    generation:     int    = 0
    db_id:          Optional[int]   = None
    parent_ids:     List[int]       = field(default_factory=list)

    @property
    def formula(self): return self.tree.to_str()

    @property
    def regime_filter(self):
        lo, hi = round(self.regime_lo, 1), round(self.regime_hi, 1)
        if self.regime_type == 'none':    return None
        if self.regime_type == 'extreme': return f'rsi < {lo} or rsi > {hi}'
        if self.regime_type == 'low':     return f'rsi < {lo}'
        if self.regime_type == 'high':    return f'rsi > {hi}'
        return None

    def to_cfg(self):
        return {
            'formula':        self.formula,
            'threshold':      round(self.threshold, 4),
            'exit_threshold': round(self.exit_threshold, 4),
            'short_lookback': int(self.short_lookback),
            'rsi_period':     int(self.rsi_period),
            'regime_filter':  self.regime_filter,
            'min_entry':      round(self.min_entry, 2),
            'max_entry':      round(self.max_entry, 2),
            'cooldown':       int(self.cooldown),
            'exit_cooldown':  int(self.exit_cooldown),
            'sell_limit':          0.0,
            'stop_loss':           0.0,
            'loss_limit':          0.0,
        }

def _crow3_tree():
    # (deltaLong + deltaShort*(secsRemaining/lookback)) / abs(gap) + (gap/200)
    momentum = BinNode('+', VarNode('deltaLong'),
                       BinNode('*', VarNode('deltaShort'),
                               BinNode('/', VarNode('secsRemaining'), VarNode('lookback'))))
    return BinNode('+',
        BinNode('/', momentum, UnaryNode('abs', VarNode('gap'))),
        BinNode('/', VarNode('gap'), ConstNode(200)))

def _crow60_tree():
    # (deltaLong / secsRemaining) + (gapDelta / (lookback * 2))
    return BinNode('+',
        BinNode('/', VarNode('deltaLong'), VarNode('secsRemaining')),
        BinNode('/', VarNode('gapDelta'), BinNode('*', VarNode('lookback'), ConstNode(2))))

def _crow_a_tree():
    # (deltaLong - deltaShort) / secsRemaining + gapDelta / lookback
    # Signed order-flow velocity (net buying pressure per second) + gap momentum
    return BinNode('+',
        BinNode('/', BinNode('-', VarNode('deltaLong'), VarNode('deltaShort')), VarNode('secsRemaining')),
        BinNode('/', VarNode('gapDelta'), VarNode('lookback')))

def _random_genome(rng, gen=0):
    method = 'full' if rng.random() < 0.5 else 'grow'
    depth  = rng.randint(2, FORMULA_MAX_DEPTH)
    tree   = _random_tree(depth, rng, method)
    lo = rng.uniform(20, 45)
    hi = rng.uniform(55, 80)
    return Genome(
        tree           = tree,
        threshold      = rng.choice([0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]),
        exit_threshold = rng.choice([0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]),
        short_lookback = rng.choice([5, 8, 10, 12, 15, 18, 20, 25, 30]),
        rsi_period     = rng.choice([14, 20, 28, 42, 60]),
        regime_type    = rng.choice(['none', 'extreme', 'extreme', 'low', 'high']),
        regime_lo      = lo,
        regime_hi      = hi,
        min_entry      = rng.uniform(1, 25),
        max_entry      = rng.uniform(26, 49),
        cooldown       = rng.choice([0, 0, 5, 15, 30, 60]),
        exit_cooldown  = rng.choice([0, 0, 5, 15, 30, 60]),
        generation          = gen,
    )

# ── Simulation ────────────────────────────────────────────────────────────────

def _simulate(ticks, cfg):
    """Stripped simulate_config. Returns list of trade return_pct values."""
    formula         = cfg['formula']
    regime_filter   = cfg.get('regime_filter') or None
    threshold       = float(cfg.get('threshold', 0.5))
    exit_threshold  = float(cfg.get('exit_threshold') or threshold)
    lb              = int(cfg.get('short_lookback', 15) or 15)
    min_entry       = float(cfg.get('min_entry', 0))
    max_entry       = float(cfg.get('max_entry', 99))
    cooldown        = int(cfg.get('cooldown', 0))
    exit_cd         = int(cfg.get('exit_cooldown', 0))
    rsi_period          = int(cfg.get('rsi_period', 42))

    rsi_buf, rsi_ag, rsi_al, rsi_prev, rsi_val, rsi_tick = [], None, None, None, None, 0
    open_trade = last_score = last_sell_idx = None
    returns = []

    for i, tick in enumerate(ticks):
        dl   = tick.get('delta_long')
        gap  = tick.get('gap')
        secs = tick.get('secs_remaining')
        up   = tick.get('up')
        down = tick.get('down')
        btc  = tick.get('btc') or 0.0

        # RSI (Wilder's EMA)
        rsi_tick += 1
        if rsi_tick <= rsi_period + 1:
            rsi_buf.append(btc)
            if rsi_tick == rsi_period + 1:
                ch = [rsi_buf[j]-rsi_buf[j-1] for j in range(1, len(rsi_buf))]
                rsi_ag = sum(max(0.0,c) for c in ch) / rsi_period
                rsi_al = sum(max(0.0,-c) for c in ch) / rsi_period
                rsi_prev = btc; rsi_buf = []
        else:
            ch = btc - rsi_prev
            rsi_ag = (rsi_ag*(rsi_period-1)+max(0.0, ch))/rsi_period
            rsi_al = (rsi_al*(rsi_period-1)+max(0.0,-ch))/rsi_period
            rsi_prev = btc
        rsi_num = (100.0-100.0/(1.0+rsi_ag/rsi_al) if rsi_ag is not None and rsi_al and rsi_al>0
                   else (100.0 if rsi_ag is not None and rsi_ag > 0 else 50.0))

        # delta_short from series
        ds = tick.get('delta_short') or 0.0
        raw = tick.get('series')
        if raw:
            try:
                s = json.loads(raw) if isinstance(raw, str) else raw
                if s and len(s) >= 2:
                    ds = s[-1] - (s[-(lb+1)] if len(s) > lb else s[0])
            except: pass

        # gap_delta
        gd = 0.0
        if gap is not None and i >= lb:
            pg = ticks[i-lb].get('gap')
            if pg is not None: gd = gap - pg

        ctx = {'deltaLong': dl or 0, 'deltaShort': ds, 'gapDelta': gd,
               'secsRemaining': secs or 1, 'lookback': lb,
               'gap': gap or 0, 'tuner': 1, 'btc': btc,
               'up': up or 0, 'down': down or 0, 'rsi': rsi_num}

        score = None
        if dl is not None and secs and gap is not None:
            try:
                v = eval(formula, _SAFE, ctx)
                v = float(v)
                score = v if math.isfinite(v) else None
            except: pass

        regime_ok = True
        if regime_filter and score is not None:
            try: regime_ok = bool(eval(regime_filter, _SAFE, ctx))
            except: pass

        if open_trade is not None and score is not None:
            side = open_trade['side']
            px   = up if side == 'Up' else down
            if px is not None:
                if (score < exit_threshold if side == 'Up' else score > -exit_threshold):
                    ep = open_trade['price'] / 100
                    xp = px / 100
                    c  = SIMULATION_TRADE_AMOUNT / ep
                    net_pct = ((xp - ep) * c - _kalshi_fee(c, ep) - _kalshi_fee(c, xp)) / SIMULATION_TRADE_AMOUNT * 100
                    returns.append(round(net_pct, 2))
                    open_trade = None; last_sell_idx = i

        if (regime_ok and open_trade is None and last_score is not None
                and score is not None and secs and secs <= (900 - cooldown)):
            if last_sell_idx is None or (i - last_sell_idx) >= exit_cd:
                in_rng = lambda px: px is not None and min_entry <= px <= max_entry
                if last_score <= threshold and score > threshold and in_rng(up):
                    open_trade = {'side': 'Up',   'price': up}
                elif last_score >= -threshold and score < -threshold and in_rng(down):
                    open_trade = {'side': 'Down', 'price': down}

        last_score = score

    # Force-close any trade still open at market end (match server.py behaviour)
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
        px = 100.0 if pos_won else 0.0
        ep = open_trade['price'] / 100
        xp = px / 100
        c  = SIMULATION_TRADE_AMOUNT / ep
        net_pct = ((xp - ep) * c - _kalshi_fee(c, ep) - _kalshi_fee(c, xp)) / SIMULATION_TRADE_AMOUNT * 100
        returns.append(round(net_pct, 2))

    return returns

def _fitness(returns):
    n = len(returns)
    if n < MIN_TRADES: return float('-inf'), n, 0.0, 0.0
    capped   = [max(-110.0, min(r, 150.0)) for r in returns]
    avg      = sum(capped) / n
    wr       = sum(1 for r in capped if r > 0) / n
    neg_rate = sum(1 for r in capped if r < 0) / n
    count_scale = min(1.0, n / 25)
    if avg <= 0:
        return avg * count_scale, n, wr * 100, avg
    score = avg * (wr ** 1.5) * count_scale * max(0.1, 1.0 - neg_rate * 2.5)
    return score, n, wr * 100, avg

# ── Evolution operators ───────────────────────────────────────────────────────

def _mutate_tree(tree, rng, strength=1.0):
    tree = copy.deepcopy(tree)
    nodes = _all_nodes(tree)
    r = rng.random()

    if r < 0.30 or len(nodes) <= 1:
        # Point mutation: tweak a constant or swap a variable
        terminals = [(n, s) for n, s in nodes if isinstance(n, (ConstNode, VarNode))]
        if terminals:
            node, setter = rng.choice(terminals)
            if isinstance(node, ConstNode):
                node.value *= rng.gauss(1.0, 0.3 * strength)
                node.value  = max(-10000, min(10000, node.value))
            else:
                node.name = rng.choice(VARS)

    elif r < 0.65:
        # Subtree replacement
        non_root = [(n, s) for n, s in nodes[1:] if s is not None]
        if non_root:
            _, setter = rng.choice(non_root)
            depth = rng.randint(1, max(1, FORMULA_MAX_DEPTH - 2))
            setter(_random_tree(depth, rng))

    elif r < 0.80:
        # Hoist: replace tree with one of its subtrees
        non_root = [n for n, _ in nodes[1:] if isinstance(n, (BinNode, UnaryNode))]
        if non_root:
            return rng.choice(non_root).clone()

    else:
        # Operator mutation
        bin_nodes = [(n, s) for n, s in nodes if isinstance(n, BinNode)]
        if bin_nodes:
            node, _ = rng.choice(bin_nodes)
            node.op = rng.choice(['+', '-', '*', '/'])

    return tree

def _mutate(genome, rng, strength=1.0):
    g = copy.deepcopy(genome)
    g.fitness = None
    g.db_id   = None

    # Always mutate formula
    g.tree = _mutate_tree(g.tree, rng, strength)

    # Randomly mutate parameters too
    if rng.random() < 0.4:
        fac = rng.gauss(1.0, 0.25 * strength)
        g.threshold = max(0.001, g.threshold * fac)
    if rng.random() < 0.3:
        fac = rng.gauss(1.0, 0.25 * strength)
        g.exit_threshold = max(0.001, g.exit_threshold * fac)
    if rng.random() < 0.3:
        g.short_lookback = max(3, min(60, g.short_lookback + rng.randint(-5, 5)))
    if rng.random() < 0.2:
        g.rsi_period = rng.choice([14, 20, 28, 42, 60])
    if rng.random() < 0.2:
        g.regime_type = rng.choice(['none', 'extreme', 'extreme', 'low', 'high'])
    if rng.random() < 0.3:
        d = rng.gauss(0, 3 * strength)
        g.regime_lo = max(5,  min(49, g.regime_lo + d))
        g.regime_hi = max(51, min(95, g.regime_hi - d))
    if rng.random() < 0.25:
        lo = rng.uniform(1, 30)
        g.min_entry = lo
        g.max_entry = max(lo + 5, min(49, g.max_entry + rng.gauss(0, 3 * strength)))
    if rng.random() < 0.15:
        g.cooldown = rng.choice([0, 0, 5, 15, 30, 60])
    if rng.random() < 0.15:
        g.exit_cooldown = rng.choice([0, 0, 5, 15, 30, 60])
    # Clamp tree size
    if g.tree.size() > 60:
        nodes = _all_nodes(g.tree)
        non_root = [n for n, _ in nodes[1:] if isinstance(n, (BinNode, UnaryNode))]
        if non_root:
            g.tree = rng.choice(non_root).clone()

    return g

def _crossover(a, b, rng):
    """Subtree crossover on formula; uniform crossover on parameters."""
    child = copy.deepcopy(a)
    child.fitness = None
    child.db_id   = None
    child.parent_ids = [a.db_id or 0, b.db_id or 0]

    # Subtree crossover
    child.tree = copy.deepcopy(a.tree)
    nodes_a = _all_nodes(child.tree)
    nodes_b = _all_nodes(b.tree)
    non_root_a = [(n, s) for n, s in nodes_a[1:] if s is not None]
    non_root_b = [n for n, _ in nodes_b[1:]]
    if non_root_a and non_root_b:
        _, setter = rng.choice(non_root_a)
        donor     = rng.choice(non_root_b)
        setter(copy.deepcopy(donor))

    # Uniform parameter crossover
    src = b if rng.random() < 0.5 else a
    if rng.random() < 0.5: child.threshold      = src.threshold
    if rng.random() < 0.5: child.exit_threshold  = src.exit_threshold
    if rng.random() < 0.5: child.short_lookback  = src.short_lookback
    if rng.random() < 0.5: child.rsi_period      = src.rsi_period
    if rng.random() < 0.5: child.regime_type      = src.regime_type
    if rng.random() < 0.5: child.regime_lo        = src.regime_lo
    if rng.random() < 0.5: child.regime_hi        = src.regime_hi
    if rng.random() < 0.5: child.min_entry        = src.min_entry
    if rng.random() < 0.5: child.max_entry        = src.max_entry
    if rng.random() < 0.5: child.cooldown            = src.cooldown
    if rng.random() < 0.5: child.exit_cooldown       = src.exit_cooldown
    child.min_entry = min(child.min_entry, child.max_entry - 2)
    child.max_entry = max(child.min_entry + 2, child.max_entry)

    return child

# ── Database helpers ──────────────────────────────────────────────────────────

def _get_db():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    return db

def _ensure_table():
    db = _get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS evo_population (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        formula     TEXT,
        threshold   REAL, exit_threshold REAL, short_lookback INTEGER,
        rsi_period  INTEGER, regime_type TEXT, regime_lo REAL, regime_hi REAL,
        min_entry   REAL, max_entry REAL, cooldown INTEGER, exit_cooldown INTEGER,
        hold_to_settlement INTEGER DEFAULT 0, stop_loss_cents REAL DEFAULT 0.0,
        fitness     REAL, trades_n INTEGER, trades_wr REAL, trades_avg REAL,
        generation  INTEGER, parent_ids TEXT,
        config_id   INTEGER,
        created_at  TEXT, evaluated_at TEXT,
        alive       INTEGER DEFAULT 1
    )''')
    for col, defn in [('hold_to_settlement', 'INTEGER DEFAULT 0'),
                      ('stop_loss_cents',    'REAL DEFAULT 0.0')]:
        try: db.execute(f'ALTER TABLE evo_population ADD COLUMN {col} {defn}')
        except: pass
    db.execute('''CREATE TABLE IF NOT EXISTS evo_gen_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        generation   INTEGER,
        ts           TEXT,
        pop_size     INTEGER,
        valid_count  INTEGER,
        best_fitness REAL,
        best_n       INTEGER,
        best_wr      REAL,
        best_avg     REAL,
        best_formula TEXT
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS evo_champion_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        promoted_at    TEXT,
        formula        TEXT,
        threshold      REAL, exit_threshold REAL, short_lookback INTEGER,
        rsi_period     INTEGER, regime_filter TEXT,
        min_entry      REAL, max_entry REAL,
        cooldown       INTEGER, exit_cooldown INTEGER,
        sell_limit     REAL, stop_loss REAL, loss_limit REAL,
        fitness        REAL, trades_n INTEGER, trades_wr REAL, trades_avg REAL
    )''')
    db.commit(); db.close()

def _save_genome(g, gen):
    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    if g.db_id:
        db.execute('''UPDATE evo_population SET
            formula=?,threshold=?,exit_threshold=?,short_lookback=?,rsi_period=?,
            regime_type=?,regime_lo=?,regime_hi=?,min_entry=?,max_entry=?,
            cooldown=?,exit_cooldown=?,
            fitness=?,trades_n=?,trades_wr=?,trades_avg=?,
            generation=?,parent_ids=?,evaluated_at=?,alive=1
            WHERE id=?''',
            [g.formula, g.threshold, g.exit_threshold, g.short_lookback, g.rsi_period,
             g.regime_type, g.regime_lo, g.regime_hi, g.min_entry, g.max_entry,
             g.cooldown, g.exit_cooldown,
             g.fitness, g.trades_n, g.trades_wr,
             g.trades_avg, gen, json.dumps(g.parent_ids), now, g.db_id])
    else:
        cur = db.execute('''INSERT INTO evo_population
            (formula,threshold,exit_threshold,short_lookback,rsi_period,
             regime_type,regime_lo,regime_hi,min_entry,max_entry,
             cooldown,exit_cooldown,
             fitness,trades_n,trades_wr,trades_avg,
             generation,parent_ids,config_id,created_at,evaluated_at,alive)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,1)''',
            [g.formula, g.threshold, g.exit_threshold, g.short_lookback, g.rsi_period,
             g.regime_type, g.regime_lo, g.regime_hi, g.min_entry, g.max_entry,
             g.cooldown, g.exit_cooldown,
             g.fitness, g.trades_n, g.trades_wr,
             g.trades_avg, gen, json.dumps(g.parent_ids), now, now])
        g.db_id = cur.lastrowid
    db.commit(); db.close()

def _mark_culled(ids):
    if not ids: return
    db = _get_db()
    db.execute(f"UPDATE evo_population SET alive=0 WHERE id IN ({','.join('?'*len(ids))})", ids)
    db.commit(); db.close()

def _load_population():
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM evo_population WHERE alive=1 ORDER BY fitness DESC NULLS LAST'
    ).fetchall()
    db.close()
    pop = []
    for r in rows:
        g = Genome(
            tree           = _crow3_tree(),  # placeholder; formula stored as string
            threshold      = r['threshold'],
            exit_threshold = r['exit_threshold'],
            short_lookback = r['short_lookback'],
            rsi_period     = r['rsi_period'],
            regime_type    = r['regime_type'],
            regime_lo      = r['regime_lo'],
            regime_hi      = r['regime_hi'],
            min_entry      = r['min_entry'],
            max_entry      = r['max_entry'],
            cooldown            = r['cooldown'],
            exit_cooldown       = r['exit_cooldown'],
            fitness        = r['fitness'],
            trades_n       = r['trades_n'] or 0,
            trades_wr      = r['trades_wr'] or 0.0,
            trades_avg     = r['trades_avg'] or 0.0,
            generation     = r['generation'],
            db_id          = r['id'],
        )
        g.tree = _parse_formula(r['formula'])
        pop.append(g)
    return pop

class _FormulaStrNode(Node):
    """Fallback for formula strings that fail to parse. Evaluates correctly but can't mutate."""
    def __init__(self, s): self.s = s
    def to_str(self):      return self.s
    def size(self):        return 10  # approximate

def _unwrap_abs_guard(node):
    """BinNode('/') serialises denominator as (abs(r) + 0.01). Unwrap back to r."""
    if isinstance(node, _pyast.BinOp) and isinstance(node.op, _pyast.Add):
        lhs, rhs = node.left, node.right
        if (isinstance(lhs, _pyast.Call) and
                isinstance(lhs.func, _pyast.Name) and lhs.func.id == 'abs' and
                len(lhs.args) == 1):
            cv = None
            if isinstance(rhs, _pyast.Constant) and isinstance(rhs.value, (int, float)):
                cv = rhs.value
            elif hasattr(_pyast, 'Num') and isinstance(rhs, _pyast.Num):
                cv = rhs.n
            if cv is not None and abs(cv - 0.01) < 1e-6:
                return _ast_to_node(lhs.args[0])
    return _ast_to_node(node)

def _ast_to_node(node):
    """Recursively map a Python AST node back to our Node tree."""
    if isinstance(node, _pyast.BinOp):
        left = _ast_to_node(node.left)
        if isinstance(node.op, _pyast.Add):  return BinNode('+', left, _ast_to_node(node.right))
        if isinstance(node.op, _pyast.Sub):  return BinNode('-', left, _ast_to_node(node.right))
        if isinstance(node.op, _pyast.Mult): return BinNode('*', left, _ast_to_node(node.right))
        if isinstance(node.op, _pyast.Div):  return BinNode('/', left, _unwrap_abs_guard(node.right))
    if isinstance(node, _pyast.UnaryOp):
        if isinstance(node.op, _pyast.USub):
            op = node.operand
            if isinstance(op, _pyast.Constant) and isinstance(op.value, (int, float)):
                return ConstNode(-op.value)
            return UnaryNode('neg', _ast_to_node(op))
        if isinstance(node.op, _pyast.UAdd):
            return _ast_to_node(node.operand)
    if isinstance(node, _pyast.Call):
        fn = node.func
        if isinstance(fn, _pyast.Name) and fn.id == 'abs' and len(node.args) == 1:
            return UnaryNode('abs', _ast_to_node(node.args[0]))
    if isinstance(node, _pyast.Name):
        if node.id in VARS: return VarNode(node.id)
        if node.id == 'tuner': return ConstNode(1.0)
    if isinstance(node, _pyast.Constant) and isinstance(node.value, (int, float)):
        return ConstNode(float(node.value))
    if hasattr(_pyast, 'Num') and isinstance(node, _pyast.Num):
        return ConstNode(float(node.n))
    try:    return _FormulaStrNode(_pyast.unparse(node))
    except: return ConstNode(0.0)

def _parse_formula(formula_str):
    """Parse a stored formula string back into a mutable Node tree."""
    try:
        return _ast_to_node(_pyast.parse(formula_str, mode='eval').body)
    except Exception:
        return _FormulaStrNode(formula_str)

def _get_markets(db):
    rows = db.execute('''
        SELECT market_ticker FROM ticks
        GROUP BY market_ticker
        HAVING COUNT(*) >= 500 AND MIN(secs_remaining) = 0
        ORDER BY MAX(ts) DESC
        LIMIT ?
    ''', [MARKET_SAMPLE]).fetchall()
    return [r[0] for r in rows]

def _load_ticks(db, ticker):
    rows = db.execute(
        'SELECT secs_remaining,delta_long,delta_short,gap,btc,up,down,series,target_price '
        'FROM ticks WHERE market_ticker=? ORDER BY ts ASC', [ticker]
    ).fetchall()
    return [dict(r) for r in rows]

# ── Fitness evaluation ────────────────────────────────────────────────────────

def _eval_genome(args):
    """Top-level function for thread pool: (genome, tick_cache, sample_tickers) → genome."""
    genome, tick_cache, sample_tickers = args
    if _is_degenerate(genome.formula):
        genome.fitness = float('-inf')
        return genome
    cfg = genome.to_cfg()
    all_returns = []
    for ticker in sample_tickers:
        ticks = tick_cache.get(ticker)
        if ticks:
            try:
                all_returns.extend(_simulate(ticks, cfg))
            except: pass
    genome.fitness, genome.trades_n, genome.trades_wr, genome.trades_avg = _fitness(all_returns)
    if genome.fitness > float('-inf'):
        excess = max(0, genome.tree.size() - SIZE_BUDGET)
        if excess:
            genome.fitness *= max(0.1, 1.0 - excess * SIZE_PENALTY)
    return genome

# ── Promotion ─────────────────────────────────────────────────────────────────

def _api(method, path, data=None):
    key  = open(KEY_PATH).read().strip()
    body = json.dumps(data).encode() if data else None
    req  = urllib.request.Request(BASE_URL + path, data=body, method=method,
           headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _open_trades():
    try:
        db = _get_db()
        n  = db.execute('SELECT COUNT(*) FROM trades WHERE exit_price IS NULL').fetchone()[0]
        db.close()
        return n
    except: return 1  # safe default

def _market_in_progress():
    """True if a market is currently active.
    Reads the single most recent tick: secs_remaining > 0 means mid-market,
    secs_remaining = 0 means just ended (we're in the between-market gap).
    No recent ticks (> 5 min) → assume between markets."""
    try:
        from datetime import timedelta
        db = _get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        row = db.execute(
            'SELECT secs_remaining FROM ticks WHERE ts > ? ORDER BY ts DESC LIMIT 1',
            [cutoff]
        ).fetchone()
        db.close()
        return bool(row and (row[0] or 0) > 0)
    except: return False

def _current_champion_fitness():
    try:
        db = _get_db()
        row = db.execute('SELECT id FROM configs WHERE name=?', [EVO_CHAMPION_NAME]).fetchone()
        db.close()
        if not row: return float('-inf')
        db = _get_db()
        row2 = db.execute(
            'SELECT fitness FROM evo_population WHERE config_id=? AND alive=1 ORDER BY fitness DESC LIMIT 1',
            [row[0]]
        ).fetchone()
        db.close()
        return row2['fitness'] if row2 else float('-inf')
    except: return float('-inf')

def _promote(genome, gen):
    """Update Evo-Champion config in-place. Never activates — user controls activation."""
    try:
        db  = _get_db()
        row = db.execute('SELECT id, active FROM configs WHERE name=?', [EVO_CHAMPION_NAME]).fetchone()
        cfg_id    = row['id']    if row else None
        is_active = bool(row['active']) if row else False
        db.close()
    except Exception as e:
        log.warning('Promotion: DB query failed: %s', e)
        return False

    if _market_in_progress():
        log.info('Promotion skipped: market in progress')
        return False
    if is_active and _open_trades() > 0:
        log.info('Promotion skipped: Evo-Champion is active with open trade')
        return False

    cfg   = genome.to_cfg()
    notes = (f'evo gen={gen} fit={genome.fitness:.3f} '
             f'n={genome.trades_n} wr={genome.trades_wr:.1f}% avg={genome.trades_avg:+.1f}% '
             f'rsi={genome.rsi_period}')
    ts = datetime.now(timezone.utc).isoformat()

    db = _get_db()
    if cfg_id:
        db.execute('''UPDATE configs SET
            formula=?,threshold=?,exit_threshold=?,short_lookback=?,
            regime_filter=?,min_entry=?,max_entry=?,cooldown=?,
            exit_cooldown=?,sell_limit=?,stop_loss=?,loss_limit=?,
            notes=?,updated_at=? WHERE id=?''',
            [cfg['formula'], cfg['threshold'], cfg['exit_threshold'], cfg['short_lookback'],
             cfg['regime_filter'], cfg['min_entry'], cfg['max_entry'], cfg['cooldown'],
             cfg['exit_cooldown'], cfg['sell_limit'], cfg['stop_loss'], cfg['loss_limit'],
             notes, ts, cfg_id])
    else:
        db.execute('''INSERT INTO configs
            (name,formula,threshold,exit_threshold,short_lookback,regime_filter,
             min_entry,max_entry,cooldown,exit_cooldown,sell_limit,stop_loss,loss_limit,
             notes,created_at,updated_at,active,live_trading,trade_amount)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,1.0)''',
            [EVO_CHAMPION_NAME, cfg['formula'], cfg['threshold'], cfg['exit_threshold'],
             cfg['short_lookback'], cfg['regime_filter'], cfg['min_entry'], cfg['max_entry'],
             cfg['cooldown'], cfg['exit_cooldown'], cfg['sell_limit'], cfg['stop_loss'],
             cfg['loss_limit'], notes, ts, ts])
        cfg_id = db.execute('SELECT id FROM configs WHERE name=?', [EVO_CHAMPION_NAME]).fetchone()['id']
    db.commit()
    if genome.db_id:
        db.execute('UPDATE evo_population SET config_id=? WHERE id=?', [cfg_id, genome.db_id])
        db.commit()
    db.close()

    db = _get_db()
    db.execute('''INSERT INTO evo_champion_history
        (promoted_at, formula, threshold, exit_threshold, short_lookback, rsi_period,
         regime_filter, min_entry, max_entry, cooldown, exit_cooldown,
         sell_limit, stop_loss, loss_limit, fitness, trades_n, trades_wr, trades_avg)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [ts, cfg['formula'], cfg['threshold'], cfg['exit_threshold'], cfg['short_lookback'],
         genome.rsi_period, cfg['regime_filter'], cfg['min_entry'], cfg['max_entry'],
         cfg['cooldown'], cfg['exit_cooldown'], cfg['sell_limit'], cfg['stop_loss'],
         cfg['loss_limit'], genome.fitness, genome.trades_n, genome.trades_wr, genome.trades_avg])
    db.commit(); db.close()

    log.info('PROMOTED Evo-Champion (id=%d)  fit=%.3f  n=%d  wr=%.1f%%  avg=%+.1f%%',
             cfg_id, genome.fitness, genome.trades_n, genome.trades_wr, genome.trades_avg)
    log.info('  formula: %s', genome.formula[:120])
    return True


def _log_generation(gen, best, valid_count, pop_size):
    try:
        db = _get_db()
        db.execute('''INSERT INTO evo_gen_log
            (generation,ts,pop_size,valid_count,best_fitness,best_n,best_wr,best_avg,best_formula)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            [gen, datetime.now(timezone.utc).isoformat(), pop_size, valid_count,
             best.fitness   if best else None,
             best.trades_n  if best else None,
             best.trades_wr if best else None,
             best.trades_avg if best else None,
             best.formula[:200] if best else None])
        db.commit(); db.close()
    except Exception as e:
        log.debug('gen log error: %s', e)

# ── Main evolution loop (island model) ───────────────────────────────────────

def _make_seed(tree_fn, rng):
    return Genome(
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

def _seed_island(existing, target_size, rng):
    """Fill an island up to target_size with Crow-3/Crow-A seeds then random genomes."""
    island = list(existing)
    seed_fns = [_crow3_tree, _crow_a_tree, _crow3_tree, _crow_a_tree]
    for fn in seed_fns:
        if len(island) >= target_size: break
        island.append(_make_seed(fn, rng))
    while len(island) < target_size:
        island.append(_random_genome(rng))
    return island


def _evolve_island(island, tick_cache, markets, rng, gen):
    """One generation of selection + breeding for a single island. Returns new island."""
    valid = [g for g in island if g.fitness is not None and math.isfinite(g.fitness)]
    valid.sort(key=lambda g: g.fitness, reverse=True)

    # Deduplicate by formula — keep best per unique formula within this island
    seen_f, deduped = set(), []
    for g in valid:
        if g.formula not in seen_f:
            seen_f.add(g.formula)
            deduped.append(g)
    valid = deduped

    valid_ids = {id(g) for g in valid}
    non_valid = [g for g in island if id(g) not in valid_ids]

    keep_n    = max(ELITE_COUNT, int(len(valid) * (1 - CULL_FRACTION)))
    survivors = valid[:keep_n]
    culled    = valid[keep_n:] + non_valid

    pool = survivors[:max(3, len(survivors) // 2)]
    n_new = ISLAND_SIZE - len(survivors)
    new_genomes = []
    for _ in range(n_new):
        r = rng.random()
        if r < 0.40 and len(pool) >= 2:
            pa, pb = rng.sample(pool, 2)
            child  = _crossover(pa, pb, rng)
            child  = _mutate(child, rng, strength=0.5)
        elif r < 0.65 and pool:
            child  = _mutate(rng.choice(pool), rng, strength=1.0)
        else:
            child  = _random_genome(rng, gen=gen)
        child.generation = gen
        new_genomes.append(child)

    # Save children before culling so a mid-generation kill leaves alive rows in DB
    for g in new_genomes:
        try: _save_genome(g, gen)
        except: pass
    _mark_culled([g.db_id for g in culled if g.db_id])

    return survivors + new_genomes, valid[0] if valid else None, len(valid)


def run():
    import atexit
    with open(PID_FILE, 'w') as _f: _f.write(str(os.getpid()))
    atexit.register(lambda: os.remove(PID_FILE) if os.path.exists(PID_FILE) else None)

    _ensure_table()
    rng = random.Random()
    log.info('Evolution engine starting  population=%d  islands=%d  island_size=%d  sample=%d  workers=%d',
             POPULATION_SIZE, N_ISLANDS, ISLAND_SIZE, MARKET_SAMPLE, WORKERS)

    # ── Distribute loaded population across islands (round-robin by index) ────
    loaded = _load_population()
    islands = [[] for _ in range(N_ISLANDS)]
    for i, g in enumerate(loaded):
        islands[i % N_ISLANDS].append(g)
    for i in range(N_ISLANDS):
        islands[i] = _seed_island(islands[i], ISLAND_SIZE, rng)

    champion_fitness = _current_champion_fitness()
    gen = max((g.generation for isl in islands for g in isl), default=0)

    while True:
        gen += 1
        t0  = time.time()

        # ── Load shared market data ───────────────────────────────────────
        db = _get_db()
        markets = _get_markets(db)
        if len(markets) < 5:
            db.close()
            log.warning('Not enough completed markets (%d), waiting...', len(markets))
            time.sleep(60); continue
        tick_cache = {t: _load_ticks(db, t) for t in markets}
        db.close()

        # ── Evaluate all genomes across all islands in parallel ───────────
        all_genomes = [g for isl in islands for g in isl]
        args = [(g, tick_cache, markets) for g in all_genomes]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(_eval_genome, a): i for i, a in enumerate(args)}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    all_genomes[idx] = fut.result()
                except Exception as e:
                    log.debug('eval error: %s', e)
                    all_genomes[idx].fitness = float('-inf')
        for g in all_genomes:
            try: _save_genome(g, gen)
            except: pass

        # Redistribute back into islands
        pos = 0
        for i, isl in enumerate(islands):
            islands[i] = all_genomes[pos:pos + len(isl)]
            pos += len(isl)

        # ── Per-island selection + breeding ───────────────────────────────
        global_best   = None
        global_valid  = 0
        island_bests  = []
        for i in range(N_ISLANDS):
            islands[i], ibest, ivalid = _evolve_island(islands[i], tick_cache, markets, rng, gen)
            global_valid += ivalid
            island_bests.append(ibest)
            if ibest and (global_best is None or ibest.fitness > global_best.fitness):
                global_best = ibest

        # ── Migration every MIGRATION_INTERVAL generations ────────────────
        if gen % MIGRATION_INTERVAL == 0:
            for i in range(N_ISLANDS):
                src_valid = sorted(
                    [g for g in islands[i] if g.fitness is not None and math.isfinite(g.fitness)],
                    key=lambda g: g.fitness, reverse=True
                )
                migrants = src_valid[:MIGRATION_COUNT]
                if not migrants: continue
                dst_idx = (i + 1) % N_ISLANDS
                dst = islands[dst_idx]
                # Replace weakest genomes in destination with fresh copies of migrants
                dst_by_fit = sorted(
                    [(j, g) for j, g in enumerate(dst) if g.fitness is not None and math.isfinite(g.fitness)],
                    key=lambda x: x[1].fitness
                )
                for m, (dst_j, _) in zip(migrants, dst_by_fit):
                    clone = copy.deepcopy(m)
                    clone.db_id = None
                    clone.fitness = None  # force re-evaluation on destination island
                    dst[dst_j] = clone
            log.info('Gen %d: migration between islands', gen)

        # ── Report ────────────────────────────────────────────────────────
        elapsed = time.time() - t0
        log.info('Gen %d  valid=%d  elapsed=%.1fs', gen, global_valid, elapsed)
        for i, ibest in enumerate(island_bests):
            if ibest:
                log.info('  Island %d  fit=%.4f  n=%d  wr=%.1f%%  avg=%+.1f%%  formula=%s',
                         i, ibest.fitness, ibest.trades_n, ibest.trades_wr, ibest.trades_avg,
                         ibest.formula[:70])

        _log_generation(gen, global_best, global_valid, POPULATION_SIZE)

        # ── Promote ───────────────────────────────────────────────────────
        if global_best and global_best.fitness > champion_fitness + PROMOTE_MARGIN:
            if _promote(global_best, gen):
                champion_fitness = global_best.fitness

        sleep = max(0, GENERATION_SLEEP - elapsed)
        if sleep > 0: time.sleep(sleep)


if __name__ == '__main__':
    run()
