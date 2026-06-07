import sqlite3
import json
import os
import sys
import math
import functools
import threading
import time
import uuid
import random
import subprocess
from datetime import datetime, timezone
from flask import (Flask, request, jsonify, send_from_directory,
                   session, redirect, render_template_string)

DB_PATH    = os.path.join(os.path.dirname(__file__), 'crow.db')
STATIC_DIR = os.path.dirname(__file__)
PORT       = 7429

app = Flask(__name__)
app.secret_key    = os.environ.get('CROW_SECRET',   'crow-dev-secret-please-change')
LOGIN_PASSWORD    = os.environ.get('CROW_PASSWORD',  '2027znismo')

_key_file = os.path.join(os.path.dirname(__file__), 'crow.key')
API_KEY   = os.environ.get('CROW_API_KEY') or (
    open(_key_file).read().strip() if os.path.exists(_key_file) else ''
)

_jobs      = {}   # job_id -> job dict
_jobs_lock = threading.Lock()

# ── Auth ───────────────────────────────────────────────────────
_LOGIN_PAGE = '''\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crow</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#ccc;font-family:'Segoe UI',system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px}
.box{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:36px 32px;
     width:100%;max-width:320px}
.brand{font-size:11px;letter-spacing:3px;color:#333;text-align:center;margin-bottom:32px}
label{display:block;font-size:10px;color:#555;letter-spacing:1px;margin-bottom:6px}
input{display:block;width:100%;background:#0d0d0d;border:1px solid #2a2a2a;color:#ccc;
      font:inherit;font-size:14px;padding:10px 12px;border-radius:6px;outline:none}
input:focus{border-color:#3b82f6}
button{display:block;width:100%;margin-top:16px;background:#111827;
       border:1px solid #3b82f6;color:#3b82f6;font:inherit;font-size:12px;
       font-weight:600;letter-spacing:1px;padding:10px;border-radius:6px;cursor:pointer}
button:hover{background:#1e3a5f}
.err{color:#ef4444;font-size:11px;text-align:center;margin-top:14px}
</style>
</head>
<body>
<div class="box">
  <div class="brand">&#9670; CROW</div>
  <form method="POST">
    <label>PASSWORD</label>
    <input type="password" name="password" autofocus autocomplete="current-password">
    <button type="submit">ENTER</button>
    {%- if error %}<div class="err">{{ error }}</div>{%- endif %}
  </form>
</div>
</body>
</html>
'''

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if API_KEY and request.headers.get('X-API-Key') == API_KEY:
            return f(*args, **kwargs)
        if not session.get('authed'):
            if request.path.startswith('/api/'):
                return jsonify(error='Unauthorized'), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return wrapper

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == LOGIN_PASSWORD:
            session['authed'] = True
            return redirect('/')
        return render_template_string(_LOGIN_PAGE, error='Wrong password')
    return render_template_string(_LOGIN_PAGE, error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# ── Schema ─────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS ticks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT    NOT NULL,
            market_ticker     TEXT,
            score             REAL,
            last_score        REAL,
            secs_remaining    INTEGER,
            delta_long        REAL,
            delta_short       REAL,
            gap               REAL,
            btc               REAL,
            price_at_lookback REAL,
            series            TEXT,
            gap_delta         REAL,
            gap_series        TEXT,
            target_price      REAL,
            up                REAL,
            down              REAL,
            sell              REAL,
            has_open_trade    INTEGER
        );

        CREATE TABLE IF NOT EXISTS trades (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                    TEXT    NOT NULL,
            market_ticker         TEXT,
            side                  TEXT,
            result                TEXT,
            entry_price           REAL,
            exit_price            REAL,
            entry_time            TEXT,
            close_time            TEXT,
            contract_count        REAL,
            entry_score           REAL,
            entry_last_score      REAL,
            entry_secs_remaining  INTEGER,
            entry_delta_long      REAL,
            entry_delta_short     REAL,
            entry_gap             REAL,
            entry_btc             REAL,
            entry_target_price    REAL,
            entry_up              REAL,
            entry_down            REAL,
            exit_score            REAL,
            exit_secs_remaining   INTEGER,
            exit_delta_long       REAL,
            exit_delta_short      REAL,
            exit_gap              REAL,
            exit_btc              REAL,
            exit_up               REAL,
            exit_down             REAL
        );

        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            market_ticker TEXT,
            event_type    TEXT    NOT NULL,
            data          TEXT
        );

        CREATE TABLE IF NOT EXISTS configs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL UNIQUE,
            active         INTEGER NOT NULL DEFAULT 0,
            formula        TEXT    NOT NULL DEFAULT '(deltaLong / secsRemaining) + (deltaShort / (lookback * tuner)) + (gap / 200)',
            threshold      REAL    NOT NULL DEFAULT 1.0,
            exit_threshold REAL    NOT NULL DEFAULT 0.0,
            short_lookback REAL    NOT NULL DEFAULT 15.0,
            exit_lookback  REAL    NOT NULL DEFAULT 15.0,
            tuner          REAL    NOT NULL DEFAULT 1.0,
            cooldown       REAL    NOT NULL DEFAULT 15.0,
            loss_limit     REAL    NOT NULL DEFAULT 0.0,
            sell_limit     REAL    NOT NULL DEFAULT 0.0,
            exit_cooldown  REAL    NOT NULL DEFAULT 30.0,
            min_entry      REAL    NOT NULL DEFAULT 0.0,
            max_entry      REAL    NOT NULL DEFAULT 100.0,
            trade_amount   REAL    NOT NULL DEFAULT 1.0,
            live_trading   INTEGER NOT NULL DEFAULT 0,
            stop_loss      REAL    NOT NULL DEFAULT 0.0,
            notes          TEXT,
            created_at     TEXT,
            updated_at     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_ticks_ts      ON ticks(ts);
        CREATE INDEX IF NOT EXISTS idx_ticks_market  ON ticks(market_ticker);
        CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_ticker);
        CREATE INDEX IF NOT EXISTS idx_trades_close  ON trades(close_time);
        CREATE INDEX IF NOT EXISTS idx_events_type   ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_market ON events(market_ticker);
    ''')
    db.commit()
    # Purge ticks collected after market expiry
    db.execute('DELETE FROM ticks WHERE secs_remaining < 0')
    # Purge markets with insufficient tick data
    db.execute('''
        DELETE FROM ticks WHERE market_ticker IN (
            SELECT market_ticker FROM ticks
            WHERE market_ticker IS NOT NULL
            GROUP BY market_ticker
            HAVING COUNT(*) < 750
        )
    ''')
    db.commit()

    # ── Evolution tables (created by evolve.py daemon, but need to exist for reads) ─
    db.executescript('''
        CREATE TABLE IF NOT EXISTS evo_population (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            formula     TEXT,
            threshold   REAL, exit_threshold REAL, short_lookback INTEGER,
            rsi_period  INTEGER, regime_type TEXT, regime_lo REAL, regime_hi REAL,
            min_entry   REAL, max_entry REAL, cooldown INTEGER, exit_cooldown INTEGER,
            fitness     REAL, trades_n INTEGER, trades_wr REAL, trades_avg REAL,
            generation  INTEGER, parent_ids TEXT, config_id INTEGER,
            created_at  TEXT, evaluated_at TEXT, alive INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS evo_gen_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            generation   INTEGER, ts TEXT,
            pop_size     INTEGER, valid_count INTEGER,
            best_fitness REAL, best_n INTEGER, best_wr REAL, best_avg REAL,
            best_formula TEXT
        );
        CREATE TABLE IF NOT EXISTS evo_sim_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            market_ticker TEXT,
            config_id     INTEGER,
            config_ts     TEXT,
            side          TEXT,
            result        TEXT,
            entry_price   REAL,
            exit_price    REAL,
            return_pct    REAL,
            entry_secs    INTEGER,
            entry_score   REAL
        );
        CREATE TABLE IF NOT EXISTS evo_champion_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            promoted_at    TEXT,
            formula        TEXT,
            threshold      REAL, exit_threshold REAL, short_lookback INTEGER,
            rsi_period     INTEGER, regime_filter TEXT,
            min_entry      REAL, max_entry REAL,
            cooldown       INTEGER, exit_cooldown INTEGER,
            sell_limit     REAL, stop_loss REAL, loss_limit REAL,
            fitness        REAL, trades_n INTEGER, trades_wr REAL, trades_avg REAL
        );
        CREATE TABLE IF NOT EXISTS evo_sim_markets (
            market_ticker TEXT PRIMARY KEY,
            simulated_at  TEXT,
            trades_n      INTEGER
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    ''')
    db.commit()

    # ── One-time: clear sim data built without fee adjustment ─────
    if not db.execute("SELECT 1 FROM meta WHERE key='sim_fee_v1'").fetchone():
        db.execute('DELETE FROM evo_sim_trades')
        db.execute('DELETE FROM evo_sim_markets')
        db.execute("INSERT INTO meta (key, value) VALUES ('sim_fee_v1', '1')")
        db.commit()

    # ── Migrations: add columns introduced after initial schema ───
    migrations = [
        'ALTER TABLE ticks   ADD COLUMN gap_delta  REAL',
        'ALTER TABLE ticks   ADD COLUMN gap_series TEXT',
        'ALTER TABLE configs ADD COLUMN notes         TEXT',
        'ALTER TABLE configs ADD COLUMN stop_loss     REAL NOT NULL DEFAULT 0.0',
        'ALTER TABLE configs ADD COLUMN regime_filter   TEXT',
        'ALTER TABLE configs ADD COLUMN regime_lookback REAL NOT NULL DEFAULT 1.0',
        'ALTER TABLE configs ADD COLUMN regime_offset   REAL NOT NULL DEFAULT 0.0',
        'ALTER TABLE configs ADD COLUMN rsi_period      INTEGER NOT NULL DEFAULT 42',
    ]
    for sql in migrations:
        try:
            db.execute(sql)
            db.commit()
        except Exception:
            pass  # column already exists

    db.close()

# ── CORS ───────────────────────────────────────────────────────
@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin']          = '*'
    response.headers['Access-Control-Allow-Headers']         = 'Content-Type'
    response.headers['Access-Control-Allow-Methods']         = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Private-Network'] = 'true'
    return response

# ── Ingest: tick ───────────────────────────────────────────────
@app.route('/tick', methods=['POST', 'OPTIONS'])
def ingest_tick():
    if request.method == 'OPTIONS': return '', 204
    d  = request.get_json(silent=True) or {}
    if (d.get('secsRemaining') or 0) < 0:
        return jsonify(ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    series = d.get('series')
    db = get_db()
    gap_series = d.get('gapSeries')
    db.execute('''
        INSERT INTO ticks
            (ts, market_ticker, score, last_score, secs_remaining,
             delta_long, delta_short, gap, btc, price_at_lookback, series,
             gap_delta, gap_series,
             target_price, up, down, sell, has_open_trade)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        ts, d.get('marketTicker'), d.get('score'), d.get('lastScore'),
        d.get('secsRemaining'), d.get('deltaLong'), d.get('deltaShort'),
        d.get('gap'), d.get('btc'), d.get('priceAtLookback'),
        json.dumps(series) if series is not None else None,
        d.get('gapDelta'),
        json.dumps(gap_series) if gap_series is not None else None,
        d.get('targetPrice'), d.get('up'), d.get('down'), d.get('sell'),
        1 if d.get('hasOpenTrade') else 0,
    ))
    db.commit(); db.close()
    return jsonify(ok=True)

# ── Ingest: event ──────────────────────────────────────────────
@app.route('/event', methods=['POST', 'OPTIONS'])
def ingest_event():
    if request.method == 'OPTIONS': return '', 204
    d  = request.get_json(silent=True) or {}
    ts = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute('INSERT INTO events (ts, market_ticker, event_type, data) VALUES (?,?,?,?)',
               (ts, d.get('marketTicker'), d.get('type'), json.dumps(d.get('data'))))
    db.commit(); db.close()
    return jsonify(ok=True)

# ── Ingest: trade ──────────────────────────────────────────────
@app.route('/trade', methods=['POST', 'OPTIONS'])
def ingest_trade():
    if request.method == 'OPTIONS': return '', 204
    d  = request.get_json(silent=True) or {}
    ts = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute('''
        INSERT INTO trades (
            ts, market_ticker, side, result, entry_price, exit_price,
            entry_time, close_time, contract_count,
            entry_score, entry_last_score, entry_secs_remaining,
            entry_delta_long, entry_delta_short, entry_gap,
            entry_btc, entry_target_price, entry_up, entry_down,
            exit_score, exit_secs_remaining,
            exit_delta_long, exit_delta_short, exit_gap,
            exit_btc, exit_up, exit_down
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        ts, d.get('marketTicker'), d.get('side'), d.get('result'),
        d.get('entryPrice'), d.get('exitPrice'),
        d.get('entryTime'), d.get('closeTime'), d.get('contractCount'),
        d.get('entryScore'), d.get('entryLastScore'), d.get('entrySecsRemaining'),
        d.get('entryDeltaLong'), d.get('entryDeltaShort'), d.get('entryGap'),
        d.get('entryBtc'), d.get('entryTargetPrice'), d.get('entryUp'), d.get('entryDown'),
        d.get('exitScore'), d.get('exitSecsRemaining'),
        d.get('exitDeltaLong'), d.get('exitDeltaShort'), d.get('exitGap'),
        d.get('exitBtc'), d.get('exitUp'), d.get('exitDown'),
    ))
    db.commit(); db.close()
    return jsonify(ok=True)

# ── API: config/active (no auth — called by extension via localhost) ──
@app.route('/api/config/active', methods=['GET'])
def api_config_active():
    db = get_db()
    row = db.execute('SELECT * FROM configs WHERE active = 1 LIMIT 1').fetchone()
    db.close()
    return jsonify(dict(row) if row else None)

# ── API: stats ─────────────────────────────────────────────────
@app.route('/api/stats')
@login_required
def api_stats():
    db = get_db()
    trades = db.execute('SELECT * FROM trades ORDER BY close_time ASC').fetchall()
    total       = len(trades)
    wins        = sum(1 for t in trades if t['result'] == 'win')
    total_ret   = sum((t['exit_price'] / t['entry_price'] - 1) * 100 for t in trades if t['entry_price'])
    markets     = len(set(t['market_ticker'] for t in trades if t['market_ticker']))
    tick_count  = db.execute('SELECT COUNT(*) FROM ticks').fetchone()[0]
    cumulative  = []
    cum = 0.0
    for t in trades:
        if t['entry_price']:
            cum += (t['exit_price'] / t['entry_price'] - 1) * 100
        cumulative.append({'ts': t['close_time'], 'value': round(cum, 3)})
    recent = [dict(r) for r in db.execute(
        'SELECT * FROM trades ORDER BY close_time DESC LIMIT 20').fetchall()]
    db.close()
    return jsonify(
        total=total, wins=wins, losses=total - wins,
        win_rate=round(wins / total * 100, 1) if total else 0,
        total_return=round(total_ret, 2), markets=markets,
        ticks=tick_count, cumulative=cumulative, recent=recent,
    )

# ── API: markets list ──────────────────────────────────────────
@app.route('/api/markets')
@login_required
def api_markets():
    db = get_db()
    rows = db.execute('''
        SELECT market_ticker,
               COUNT(*)  AS tick_count,
               MIN(ts)   AS first_tick,
               MAX(ts)   AS last_tick
        FROM ticks
        WHERE market_ticker IS NOT NULL
        GROUP BY market_ticker
        HAVING MIN(secs_remaining) = 0 AND COUNT(*) >= 750
        ORDER BY first_tick DESC
    ''').fetchall()
    trade_stats = {r['market_ticker']: dict(r) for r in db.execute('''
        SELECT market_ticker,
               COUNT(*) AS trades,
               SUM(result = 'win') AS wins,
               SUM((exit_price * 1.0 / entry_price) - 1) * 100 AS total_return
        FROM trades GROUP BY market_ticker
    ''').fetchall()}
    db.close()
    result = []
    for r in rows:
        d = dict(r)
        t = trade_stats.get(r['market_ticker'], {})
        d['trades']       = t.get('trades', 0)
        d['wins']         = t.get('wins', 0)
        d['total_return'] = round(t.get('total_return') or 0, 2)
        result.append(d)
    return jsonify(result)

# ── API: market ticks ──────────────────────────────────────────
@app.route('/api/markets/<path:ticker>/ticks')
@login_required
def api_market_ticks(ticker):
    db = get_db()
    rows = db.execute('''
        SELECT ts, score, last_score, secs_remaining, btc, target_price,
               delta_long, delta_short, gap, up, down, sell, has_open_trade
        FROM ticks WHERE market_ticker = ? ORDER BY ts ASC
    ''', [ticker]).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

# ── API: market trades ─────────────────────────────────────────
@app.route('/api/markets/<path:ticker>/trades')
@login_required
def api_market_trades(ticker):
    db = get_db()
    rows = db.execute(
        'SELECT * FROM trades WHERE market_ticker = ? ORDER BY entry_time ASC', [ticker]
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

# ── API: analytics ─────────────────────────────────────────────
@app.route('/api/analytics/trades')
@login_required
def api_analytics_trades():
    db = get_db()
    rows = db.execute('''
        SELECT side, result, entry_price, exit_price,
               entry_score, entry_last_score, entry_secs_remaining,
               entry_delta_long, entry_delta_short, entry_gap,
               entry_btc, entry_target_price, close_time, market_ticker
        FROM trades ORDER BY close_time ASC
    ''').fetchall()
    db.close()
    out = []
    for r in rows:
        d = dict(r)
        if d['entry_price']:
            d['return_pct'] = round((d['exit_price'] / d['entry_price'] - 1) * 100, 2)
        else:
            d['return_pct'] = None
        out.append(d)
    return jsonify(out)

# ── Config helpers ────────────────────────────────────────────
def next_crow_name(db):
    rows = db.execute("SELECT name FROM configs WHERE name LIKE 'Crow-%'").fetchall()
    nums = []
    for r in rows:
        try: nums.append(int(r['name'].split('-')[1]))
        except: pass
    return f'Crow-{max(nums) + 1 if nums else 1}'

CONFIG_FIELDS = [
    'formula','threshold','exit_threshold','short_lookback',
    'cooldown','loss_limit','sell_limit','stop_loss','exit_cooldown',
    'min_entry','max_entry','trade_amount','live_trading','regime_filter',
    'regime_lookback','regime_offset','rsi_period',
]

# ── API: configs ───────────────────────────────────────────────
@app.route('/api/configs', methods=['GET'])
@login_required
def api_configs_list():
    db = get_db()
    rows = db.execute('SELECT * FROM configs ORDER BY id ASC').fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['trade_count'] = 0
        d['trade_wins']  = 0
        result.append(d)
    db.close()
    return jsonify(result)

@app.route('/api/configs', methods=['POST', 'OPTIONS'])
@login_required
def api_configs_create():
    if request.method == 'OPTIONS': return '', 204
    d  = request.get_json(silent=True) or {}
    ts = datetime.now(timezone.utc).isoformat()
    db = get_db()
    name = d.get('name') or next_crow_name(db)
    cols = ['name', 'created_at', 'updated_at'] + [f for f in CONFIG_FIELDS if f in d]
    vals = [name, ts, ts] + [d[f] for f in CONFIG_FIELDS if f in d]
    if 'notes' in d:
        cols.append('notes'); vals.append(d['notes'])
    db.execute(f'INSERT INTO configs ({",".join(cols)}) VALUES ({",".join("?"*len(vals))})', vals)
    db.commit()
    row = db.execute('SELECT * FROM configs WHERE name = ?', [name]).fetchone()
    db.close()
    return jsonify(dict(row)), 201

@app.route('/api/configs/<int:cfg_id>', methods=['PUT', 'OPTIONS'])
@login_required
def api_configs_update(cfg_id):
    if request.method == 'OPTIONS': return '', 204
    d  = request.get_json(silent=True) or {}
    ts = datetime.now(timezone.utc).isoformat()
    db = get_db()
    fields = [f for f in CONFIG_FIELDS if f in d]
    vals   = [d[f] for f in CONFIG_FIELDS if f in d]
    if 'notes' in d:
        fields.append('notes'); vals.append(d['notes'])
    if 'name' in d:
        fields.append('name'); vals.append(d['name'])
    fields.append('updated_at'); vals.extend([ts, cfg_id])
    if fields:
        db.execute(f'UPDATE configs SET {", ".join(f+"=?" for f in fields)} WHERE id = ?', vals)
        db.commit()
    row = db.execute('SELECT * FROM configs WHERE id = ?', [cfg_id]).fetchone()
    db.close()
    return jsonify(dict(row) if row else None)

@app.route('/api/configs/<int:cfg_id>', methods=['DELETE', 'OPTIONS'])
@login_required
def api_configs_delete(cfg_id):
    if request.method == 'OPTIONS': return '', 204
    db = get_db()
    row = db.execute('SELECT active FROM configs WHERE id = ?', [cfg_id]).fetchone()
    if not row:
        db.close(); return jsonify(error='Not found'), 404
    if row['active']:
        db.close(); return jsonify(error='Cannot delete the active config'), 400
    db.execute('DELETE FROM configs WHERE id = ?', [cfg_id])
    db.commit(); db.close()
    return jsonify(ok=True)

@app.route('/api/configs/<int:cfg_id>/activate', methods=['POST', 'OPTIONS'])
@login_required
def api_configs_activate(cfg_id):
    if request.method == 'OPTIONS': return '', 204
    ts = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute('UPDATE configs SET active = 0, updated_at = ?', [ts])
    db.execute('UPDATE configs SET active = 1, updated_at = ? WHERE id = ?', [ts, cfg_id])
    db.commit(); db.close()
    return jsonify(ok=True)

# ── Simulation engine ─────────────────────────────────────────
_SIM_TRADE_AMT = 1.0  # dollars — for fee rounding; return% is ~scale-independent

def _kalshi_fee(contracts, price_frac):
    """Taker fee in dollars: ceil(0.07 × C × P × (1-P))"""
    if price_frac <= 0 or price_frac >= 1:
        return 0.0
    return math.ceil(0.07 * contracts * price_frac * (1 - price_frac) * 100) / 100

def simulate_config(ticks, cfg):
    threshold      = float(cfg.get('threshold',      1.0))
    exit_threshold = float(cfg.get('exit_threshold', 0.0))
    short_lookback = int(  cfg.get('short_lookback', 15))
    cooldown       = float(cfg.get('cooldown',       15.0))
    loss_limit     = float(cfg.get('loss_limit',     0.0))
    sell_limit     = float(cfg.get('sell_limit',     0.0))
    stop_loss      = float(cfg.get('stop_loss',      0.0))
    exit_cooldown  = float(cfg.get('exit_cooldown',  30.0))
    min_entry      = float(cfg.get('min_entry',      0.0))
    max_entry      = float(cfg.get('max_entry',      100.0))
    formula          = cfg.get('formula', '(deltaLong / secsRemaining) + (deltaShort / (lookback * tuner)) + (gap / 200)')
    regime_filter    = cfg.get('regime_filter') or None
    regime_lookback  = max(1, int(cfg.get('regime_lookback', 1) or 1))
    regime_offset    = max(0, int(cfg.get('regime_offset',   0) or 0))

    rsi_period     = int(cfg.get('rsi_period', 42))

    _safe = {'__builtins__': {}, 'abs': abs, 'max': max, 'min': min,
             'round': round, 'floor': math.floor, 'ceil': math.ceil}

    open_trade      = None
    last_score      = None
    last_sell_idx   = None
    trades          = []
    score_series    = []
    regime_history  = []   # rolling window of per-tick regime_filter results

    # RSI state (Wilder's EMA)
    _rsi_buf    = []    # accumulates prices until seeded
    _rsi_ag     = None  # avg gain
    _rsi_al     = None  # avg loss
    _rsi_val    = None  # current RSI (None until rsi_period+1 ticks seen)
    _rsi_prev   = None  # previous BTC price
    _rsi_tick   = 0     # tick counter for RSI seeding

    for i, tick in enumerate(ticks):
        dlong = tick.get('delta_long')
        gap   = tick.get('gap')
        secs  = tick.get('secs_remaining')
        up    = tick.get('up')
        down  = tick.get('down')
        btc   = tick.get('btc') or 0

        # Rolling RSI computation (Wilder's EMA, period = rsi_period)
        _rsi_tick += 1
        if _rsi_tick <= rsi_period + 1:
            _rsi_buf.append(btc)
            if _rsi_tick == rsi_period + 1:
                changes  = [_rsi_buf[j] - _rsi_buf[j-1] for j in range(1, len(_rsi_buf))]
                _rsi_ag  = sum(max(0.0, c) for c in changes) / rsi_period
                _rsi_al  = sum(max(0.0, -c) for c in changes) / rsi_period
                _rsi_prev = btc
                _rsi_buf  = []
        else:
            change    = btc - _rsi_prev
            _rsi_ag   = (_rsi_ag * (rsi_period - 1) + max(0.0,  change)) / rsi_period
            _rsi_al   = (_rsi_al * (rsi_period - 1) + max(0.0, -change)) / rsi_period
            _rsi_prev = btc
        if _rsi_ag is not None:
            _rsi_val = 100.0 - 100.0 / (1.0 + _rsi_ag / _rsi_al) if _rsi_al > 0 else 100.0

        lookback = short_lookback

        # Recompute deltaShort from stored BTC series using this config's lookback
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

        # Compute gapDelta directly from the ticks array (gap[i] - gap[i - lookback])
        gap_delta = 0.0
        if gap is not None and i >= lookback:
            past_gap = ticks[i - lookback].get('gap')
            if past_gap is not None:
                gap_delta = gap - past_gap

        # Evaluate formula
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
                    'rsi':           _rsi_val if _rsi_val is not None else 50.0,
                })
                score = float(v) if (isinstance(v, (int, float)) and not math.isnan(v) and not math.isinf(v)) else None
            except Exception:
                score = None

        score_series.append(score)

        # Check exit
        if open_trade is not None and score is not None:
            side = open_trade['side']
            px   = up if side == 'Up' else down
            if px is not None:
                triggered  = (score < exit_threshold) if side == 'Up' else (score > -exit_threshold)
                limit_hit  = sell_limit > 0 and px >= sell_limit
                stop_hit   = stop_loss  > 0 and px <= stop_loss
                if triggered or limit_hit or stop_hit:
                    result = 'win' if px > open_trade['entry_price'] else 'loss'
                    ep = open_trade['entry_price'] / 100
                    xp = px / 100
                    c  = _SIM_TRADE_AMT / ep
                    rp = ((xp - ep) * c - _kalshi_fee(c, ep) - _kalshi_fee(c, xp)) / _SIM_TRADE_AMT * 100
                    trades.append({**open_trade,
                        'exit_price': px, 'exit_tick': i,
                        'exit_score': score, 'result': result,
                        'return_pct': round(rp, 2),
                    })
                    open_trade    = None
                    last_sell_idx = i

        # Evaluate regime filter over a rolling window (separate from score/last_score tracking).
        # Only starts accumulating once tick index reaches regime_offset.
        regime_ok = True
        if regime_filter and dlong is not None and i >= regime_offset:
            try:
                rf_val = bool(eval(regime_filter, _safe, {
                    'deltaLong':     dlong,
                    'deltaShort':    dshort,
                    'gapDelta':      gap_delta,
                    'secsRemaining': secs or 1,
                    'lookback':      lookback,
                    'gap':           gap or 0,
                    'tuner':         1,
                    'btc':           btc,
                    'up':            up or 0,
                    'down':          down or 0,
                    'rsi':           _rsi_val if _rsi_val is not None else 50.0,
                }))
            except Exception:
                rf_val = True
            regime_history.append(rf_val)
            if len(regime_history) > regime_lookback:
                regime_history.pop(0)
            # Regime is OK when majority of the lookback window passes the filter
            regime_ok = sum(regime_history) / len(regime_history) >= 0.5

        # Check entry
        past_cooldown      = secs is not None and secs <= (900 - cooldown)
        past_exit_cooldown = last_sell_idx is None or (i - last_sell_idx) >= exit_cooldown

        if (regime_ok and open_trade is None and last_score is not None and score is not None
                and secs and past_cooldown and past_exit_cooldown):

            if loss_limit > 0:
                cum = sum((t['exit_price'] / t['entry_price'] - 1) * 100 for t in trades)
                if cum <= -loss_limit:
                    last_score = score
                    continue

            in_range = lambda px: px is not None and min_entry <= px <= max_entry

            entry_ctx = {'entry_score': score, 'entry_secs_remaining': secs,
                         'entry_delta_long': dlong, 'entry_delta_short': dshort,
                         'entry_gap': gap, 'entry_gap_delta': gap_delta, 'entry_btc': btc,
                         'entry_ts': tick.get('ts', '')}
            if   last_score <= threshold  and score >  threshold  and in_range(up):
                open_trade = {'side': 'Up',   'entry_price': up,   'entry_tick': i, **entry_ctx}
            elif last_score >= -threshold and score < -threshold and in_range(down):
                open_trade = {'side': 'Down', 'entry_price': down, 'entry_tick': i, **entry_ctx}

        last_score = score

    # Force-close any position still open at market end.
    # Walk backwards to find the last tick with valid settlement data,
    # since the final tick(s) often have null prices as the market expires.
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
            up_won = True  # no data — default guess

        pos_won = (side == 'Up') == up_won
        px      = 100.0 if pos_won else 0.0
        result  = 'win' if px > open_trade['entry_price'] else 'loss'
        ep = open_trade['entry_price'] / 100
        xp = px / 100
        c  = _SIM_TRADE_AMT / ep
        rp = ((xp - ep) * c - _kalshi_fee(c, ep) - _kalshi_fee(c, xp)) / _SIM_TRADE_AMT * 100
        trades.append({**open_trade,
            'exit_price': px, 'exit_tick': len(ticks) - 1,
            'exit_score': score_series[-1], 'result': result,
            'return_pct': round(rp, 2),
        })

    return score_series, trades

@app.route('/api/markets/<path:ticker>/simulate')
@login_required
def api_market_simulate(ticker):
    config_id = request.args.get('config', type=int)
    if not config_id:
        return jsonify(error='config param required'), 400
    db = get_db()
    cfg_row = db.execute('SELECT * FROM configs WHERE id = ?', [config_id]).fetchone()
    if not cfg_row:
        db.close(); return jsonify(error='Config not found'), 404
    cfg = dict(cfg_row)
    tick_rows = db.execute('''
        SELECT ts, secs_remaining, delta_long, delta_short, gap, gap_delta, gap_series, btc,
               up, down, series
        FROM ticks WHERE market_ticker = ? ORDER BY ts ASC
    ''', [ticker]).fetchall()
    db.close()

    score_series, sim_trades = simulate_config([dict(r) for r in tick_rows], cfg)

    total     = len(sim_trades)
    wins      = sum(1 for t in sim_trades if t['result'] == 'win')
    total_ret = sum(t['return_pct'] for t in sim_trades)
    return jsonify(
        score_series  = score_series,
        trades        = sim_trades,
        total         = total,
        wins          = wins,
        losses        = total - wins,
        win_rate      = round(wins / total * 100, 1) if total else 0,
        total_return  = round(total_ret, 2),
    )

# ── Backtest jobs ──────────────────────────────────────────────
@app.route('/api/backtest', methods=['POST', 'OPTIONS'])
@login_required
def api_backtest_start():
    if request.method == 'OPTIONS': return '', 204
    d = request.get_json(silent=True) or {}
    config_id    = d.get('config_id')
    market_count = max(1, min(5000, int(d.get('market_count', 20))))

    if not config_id:
        return jsonify(error='config_id required'), 400

    # ── Resolve seed ──────────────────────────────────────────────
    seed_str = (d.get('seed') or '').strip()
    cutoff = seed_int = None
    if seed_str:
        try:
            idx      = seed_str.rfind('-')
            ts_part  = seed_str[:idx].replace('Z', '+00:00')
            seed_int = int(seed_str[idx + 1:])
            cutoff   = datetime.fromisoformat(ts_part)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
            else:
                cutoff = cutoff.astimezone(timezone.utc)
        except Exception:
            cutoff = seed_int = None

    if cutoff is None:
        cutoff   = datetime.now(timezone.utc)
        seed_int = random.randint(0, 999999)

    seed_resolved = f"{cutoff.strftime('%Y-%m-%dT%H:%M:%S')}-{seed_int}"
    cutoff_iso    = cutoff.isoformat()

    db = get_db()
    cfg_row = db.execute('SELECT * FROM configs WHERE id = ?', [config_id]).fetchone()
    if not cfg_row:
        db.close(); return jsonify(error='Config not found'), 404
    cfg = dict(cfg_row)

    market_rows = db.execute('''
        SELECT market_ticker FROM ticks WHERE market_ticker IS NOT NULL
        GROUP BY market_ticker
        HAVING MIN(secs_remaining) = 0 AND COUNT(*) >= 750
          AND MAX(ts) < ?
    ''', [cutoff_iso]).fetchall()
    db.close()

    all_tickers = [r['market_ticker'] for r in market_rows]
    if not all_tickers:
        return jsonify(error='No completed markets before cutoff'), 400

    rng      = random.Random(seed_int)
    selected = rng.sample(all_tickers, min(market_count, len(all_tickers)))
    job_id   = str(uuid.uuid4())[:8]

    with _jobs_lock:
        # Keep only the 30 most-recent jobs to avoid unbounded growth
        if len(_jobs) >= 30:
            oldest = sorted(_jobs.keys(),
                            key=lambda k: _jobs[k].get('started_at', 0))[:5]
            for k in oldest:
                del _jobs[k]
        _jobs[job_id] = {
            'status':     'running',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'seed':       seed_resolved,
            'progress':   {'done': 0, 'total': len(selected)},
            'markets':    [],
            'summary':    None,
            'error':      None,
        }

    _regime_safe = {'__builtins__': {}, 'abs': abs, 'max': max, 'min': min,
                    'round': round, 'floor': math.floor, 'ceil': math.ceil}

    def _eval_regime_tick(rt, rf, lb):
        """Evaluate regime_filter expression on a single tick dict. Returns bool."""
        try:
            return bool(eval(rf, _regime_safe, {
                'deltaLong':     rt.get('delta_long')  or 0,
                'deltaShort':    rt.get('delta_short') or 0,
                'gapDelta':      rt.get('gap_delta')   or 0,
                'secsRemaining': rt.get('secs_remaining') or 1,
                'lookback':      lb,
                'gap':           rt.get('gap')  or 0,
                'tuner':         1,
                'btc':           rt.get('btc')  or 0,
                'up':            rt.get('up')   or 0,
                'down':          rt.get('down') or 0,
            }))
        except Exception:
            return True

    def run_job():
        results = []
        _rf          = cfg.get('regime_filter') or None
        _rlb         = max(1, int(cfg.get('regime_lookback', 1) or 1))
        _roff        = max(0, int(cfg.get('regime_offset',   0) or 0))
        _lb          = max(1, int(cfg.get('short_lookback', 15) or 15))
        _market_mode = _rf and _rlb > 1   # True = pre-filter whole markets

        for i, ticker in enumerate(selected):
            try:
                idb = get_db()
                rows = idb.execute('''
                    SELECT ts, secs_remaining, delta_long, delta_short, gap, gap_delta, gap_series, btc,
                           up, down, series
                    FROM ticks WHERE market_ticker = ? ORDER BY ts ASC
                ''', [ticker]).fetchall()
                idb.close()

                # Market-level regime pre-filter: assess regime over first _rlb ticks,
                # skip the market entirely if < 50% of assessment ticks pass.
                # This avoids per-tick cooldown inflation while cleanly separating
                # regime logic from the entry simulation.
                cfg_for_sim = cfg
                if _market_mode:
                    assessment = [dict(r) for r in rows[_roff:_roff + _rlb]]
                    if assessment:
                        passes = sum(_eval_regime_tick(rt, _rf, _lb) for rt in assessment)
                        if passes / len(assessment) < 0.5:
                            results.append({
                                'market_ticker': ticker, 'total': 0,
                                'wins': 0, 'losses': 0, 'win_rate': 0,
                                'total_return': 0, 'trades': [],
                            })
                            with _jobs_lock:
                                _jobs[job_id]['progress']['done'] = i + 1
                            continue
                    # Market passed: run simulation with regime_filter disabled
                    # (pre-filter already made the decision at market level)
                    cfg_for_sim = {**cfg, 'regime_filter': None}

                _, sim_trades = simulate_config([dict(r) for r in rows], cfg_for_sim)
                total = len(sim_trades)
                wins  = sum(1 for t in sim_trades if t['result'] == 'win')
                ret   = sum(t['return_pct'] for t in sim_trades)
                results.append({
                    'market_ticker': ticker,
                    'total':         total,
                    'wins':          wins,
                    'losses':        total - wins,
                    'win_rate':      round(wins / total * 100, 1) if total else 0,
                    'total_return':  round(ret, 2),
                    'trades':        sim_trades,
                })
            except Exception as e:
                results.append({
                    'market_ticker': ticker, 'error': str(e),
                    'total': 0, 'wins': 0, 'losses': 0,
                    'win_rate': 0, 'total_return': 0, 'trades': [],
                })
            with _jobs_lock:
                _jobs[job_id]['progress']['done'] = i + 1
                _jobs[job_id]['markets'] = results

        all_trades = [t for m in results for t in m.get('trades', [])]
        total = len(all_trades)
        wins  = sum(1 for t in all_trades if t['result'] == 'win')
        ret   = sum(t.get('return_pct', 0) for t in all_trades)
        with _jobs_lock:
            _jobs[job_id]['summary'] = {
                'total_trades':       total,
                'wins':               wins,
                'losses':             total - wins,
                'win_rate':           round(wins / total * 100, 1) if total else 0,
                'total_return':       round(ret, 2),
                'avg_return':         round(ret / total, 2) if total else 0,
                'markets_tested':     len(results),
                'markets_with_trades': sum(1 for m in results if m.get('total', 0) > 0),
            }
            _jobs[job_id]['status'] = 'done'

    threading.Thread(target=run_job, daemon=True).start()
    return jsonify(job_id=job_id, total=len(selected), seed=seed_resolved)


@app.route('/api/backtest/<job_id>')
@login_required
def api_backtest_status(job_id):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        return jsonify(error='Job not found'), 404
    return jsonify(job)


# ── Trades: clear ─────────────────────────────────────────────
@app.route('/api/trades/clear', methods=['POST', 'OPTIONS'])
@login_required
def api_trades_clear():
    if request.method == 'OPTIONS': return '', 204
    db = get_db()
    db.execute('DELETE FROM trades')
    db.commit()
    db.close()
    return jsonify(ok=True)

# ── Evo paper sim background worker ───────────────────────────
_evo_sim_lock = threading.Lock()

def _run_evo_sim():
    db = get_db()
    try:
        # Champion history ordered oldest → newest
        history = [dict(r) for r in db.execute(
            'SELECT * FROM evo_champion_history ORDER BY promoted_at ASC'
        ).fetchall()]
        if not history:
            return

        # All completed markets with their end time, oldest → newest
        markets = db.execute('''
            SELECT market_ticker, MAX(ts) AS end_ts
            FROM ticks
            GROUP BY market_ticker
            HAVING COUNT(*) >= 750 AND MIN(secs_remaining) = 0
            ORDER BY end_ts ASC
        ''').fetchall()

        done = set(r[0] for r in db.execute(
            'SELECT market_ticker FROM evo_sim_markets'
        ).fetchall())

        to_sim = [(r['market_ticker'], r['end_ts'])
                  for r in markets if r['market_ticker'] not in done]
        if not to_sim:
            return

        ts_now = datetime.now(timezone.utc).isoformat()
        count  = 0

        for ticker, end_ts in to_sim:
            # Champion that was active when this market ended:
            # last promotion whose promoted_at <= market end time
            champ = None
            for h in reversed(history):
                if h['promoted_at'] <= end_ts:
                    champ = h
                    break
            if not champ:
                # Market ended before any champion — mark done so we don't retry
                db.execute('INSERT OR IGNORE INTO evo_sim_markets (market_ticker, simulated_at, trades_n) VALUES (?,?,0)',
                           [ticker, ts_now])
                db.commit()
                continue

            sim_cfg = {
                'formula':        champ['formula'],
                'threshold':      champ['threshold'],
                'exit_threshold': champ['exit_threshold'],
                'short_lookback': champ['short_lookback'],
                'rsi_period':     champ['rsi_period'],
                'regime_filter':  champ['regime_filter'],
                'min_entry':      champ['min_entry'],
                'max_entry':      champ['max_entry'],
                'cooldown':       champ['cooldown'],
                'exit_cooldown':  champ['exit_cooldown'],
                'sell_limit':     champ['sell_limit'],
                'stop_loss':      champ['stop_loss'],
                'loss_limit':     champ['loss_limit'],
                'regime_lookback': 1,
                'regime_offset':   0,
            }

            rows = db.execute('''
                SELECT secs_remaining, delta_long, delta_short, gap, gap_delta, btc,
                       up, down, series, target_price
                FROM ticks WHERE market_ticker=? ORDER BY ts ASC
            ''', [ticker]).fetchall()

            _, trades = simulate_config([dict(r) for r in rows], sim_cfg)

            for t in trades:
                db.execute('''INSERT INTO evo_sim_trades
                    (ts, market_ticker, config_id, config_ts, side, result,
                     entry_price, exit_price, return_pct, entry_secs, entry_score)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    [ts_now, ticker, champ['id'], champ['promoted_at'],
                     t.get('side'), t.get('result'),
                     t.get('entry_price'), t.get('exit_price'), t.get('return_pct'),
                     t.get('entry_secs_remaining'), t.get('entry_score')])

            # Mark market as processed whether or not it had trades
            db.execute('INSERT OR IGNORE INTO evo_sim_markets (market_ticker, simulated_at, trades_n) VALUES (?,?,?)',
                       [ticker, ts_now, len(trades)])
            db.commit()

            count += 1
            if count >= 15:
                break
    finally:
        db.close()

def _evo_sim_loop():
    time.sleep(5)
    while True:
        try:
            with _evo_sim_lock:
                _run_evo_sim()
        except Exception:
            pass
        time.sleep(30)

# ── Evolution API ──────────────────────────────────────────────
@app.route('/api/evo/population')
@login_required
def api_evo_population():
    db = get_db()
    rows = db.execute('''
        SELECT id, formula, threshold, exit_threshold, short_lookback, rsi_period,
               regime_type, regime_lo, regime_hi, min_entry, max_entry,
               cooldown, exit_cooldown, fitness, trades_n, trades_wr, trades_avg,
               generation, config_id, evaluated_at
        FROM evo_population WHERE alive=1
        ORDER BY fitness DESC NULLS LAST
        LIMIT 200
    ''').fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/evo/sim-trades')
@login_required
def api_evo_sim_trades():
    db = get_db()
    trades = db.execute('''
        SELECT market_ticker, side, result, entry_price, exit_price,
               return_pct, entry_secs, entry_score
        FROM evo_sim_trades
        ORDER BY market_ticker ASC
    ''').fetchall()
    db.close()
    t_list = [dict(t) for t in trades]
    n = len(t_list)
    wins = sum(1 for t in t_list if t['result'] == 'win')
    total_ret = sum((t['return_pct'] or 0) for t in t_list)
    return jsonify({
        'trades':  t_list,
        'summary': {
            'n':         n,
            'wins':      wins,
            'losses':    n - wins,
            'wr':        round(wins / n * 100, 1) if n else 0,
            'total_ret': round(total_ret, 2),
            'avg_ret':   round(total_ret / n, 2) if n else 0,
        },
    })

@app.route('/api/evo/stats')
@login_required
def api_evo_stats():
    db = get_db()
    gen_log = db.execute(
        'SELECT * FROM evo_gen_log ORDER BY generation ASC'
    ).fetchall()
    champion = db.execute(
        "SELECT * FROM configs WHERE name='Evo-Champion' LIMIT 1"
    ).fetchone()
    pop_stats = db.execute('''
        SELECT COUNT(*) as total,
               COUNT(CASE WHEN fitness IS NOT NULL AND fitness > -1e30 THEN 1 END) as valid,
               MAX(generation) as current_gen
        FROM evo_population WHERE alive=1
    ''').fetchone()
    db.close()
    return jsonify({
        'gen_log':    [dict(r) for r in gen_log],
        'champion':   dict(champion) if champion else None,
        'population': dict(pop_stats) if pop_stats else {},
    })

# ── Evo reset ──────────────────────────────────────────────────
_evo_reset_lock = threading.Lock()
_evo_reset = {'status': 'idle', 'lines': [], 'started_at': None}
_EVOLVE_PID_FILE = os.path.join(os.path.dirname(__file__), 'evolve.pid')
_WF_SCRIPT       = os.path.join(os.path.dirname(__file__), 'evolve_walkforward.py')
_DAEMON_SCRIPT   = os.path.join(os.path.dirname(__file__), 'evolve.py')

def _kill_daemon():
    try:
        if os.path.exists(_EVOLVE_PID_FILE):
            pid = int(open(_EVOLVE_PID_FILE).read().strip())
            subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                           capture_output=True, timeout=5)
            os.remove(_EVOLVE_PID_FILE)
    except Exception:
        pass

def _start_daemon():
    subprocess.Popen(
        [sys.executable, _DAEMON_SCRIPT],
        cwd=os.path.dirname(__file__),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def _run_reset_walkforward():
    db = get_db()
    for tbl in ('evo_population', 'evo_gen_log', 'evo_champion_history',
                'evo_sim_trades', 'evo_sim_markets'):
        db.execute('DELETE FROM ' + tbl)
    db.execute("DELETE FROM meta WHERE key='sim_fee_v1'")
    db.commit(); db.close()

    with _evo_reset_lock:
        _evo_reset['lines'].append('Tables cleared. Starting walk-forward…')

    proc = subprocess.Popen(
        [sys.executable, _WF_SCRIPT],
        cwd=os.path.dirname(__file__),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in proc.stdout:
        with _evo_reset_lock:
            _evo_reset['lines'].append(line.rstrip('\n'))
    proc.wait()

    with _evo_reset_lock:
        if proc.returncode == 0:
            _evo_reset['lines'].append('Walk-forward complete. Starting daemon…')
            _start_daemon()
            _evo_reset['status'] = 'done'
        else:
            _evo_reset['lines'].append(f'Walk-forward failed (exit {proc.returncode}).')
            _evo_reset['status'] = 'error'

@app.route('/api/evo/reset', methods=['POST'])
@login_required
def api_evo_reset():
    with _evo_reset_lock:
        if _evo_reset['status'] == 'running':
            return jsonify(ok=False, error='Already running'), 409
        open_trades = get_db().execute(
            'SELECT COUNT(*) FROM trades WHERE exit_price IS NULL'
        ).fetchone()[0]
        if open_trades:
            return jsonify(ok=False, error='Cannot reset with open trades'), 409
        _kill_daemon()
        _evo_reset['status']     = 'running'
        _evo_reset['lines']      = ['Daemon stopped. Clearing tables…']
        _evo_reset['started_at'] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_run_reset_walkforward, daemon=True, name='evo-reset').start()
    return jsonify(ok=True)

@app.route('/api/evo/reset-status')
@login_required
def api_evo_reset_status():
    with _evo_reset_lock:
        return jsonify(
            status=_evo_reset['status'],
            lines=list(_evo_reset['lines']),
            started_at=_evo_reset['started_at'],
        )

# ── Dashboard ──────────────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    return send_from_directory(STATIC_DIR, 'dashboard.html')

# ── Run ────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    threading.Thread(target=_evo_sim_loop, daemon=True, name='evo-sim').start()
    print(f'Crow server  :  http://localhost:{PORT}')
    print(f'Dashboard    :  http://localhost:{PORT}/')
    print(f'Database     :  {DB_PATH}')
    print(f'Password     :  {"set via CROW_PASSWORD" if LOGIN_PASSWORD != "2027znismo" else "2027znismo"}')
    app.run(host='127.0.0.1', port=PORT, debug=False)
