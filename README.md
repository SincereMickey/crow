# Crow — Kalshi KXBTC15M Trading Bot

Automated momentum-score trading bot for Kalshi's 15-minute BTC UP/DOWN prediction markets. Consists of a Chrome extension that runs on Kalshi's UI, a Flask backend that logs everything, a web dashboard for monitoring and config management, and a continuous evolution engine that searches for high-performing trading formulas.

---

## Architecture

```
Kalshi.com (Chrome)
  └── extension/content.js          — scores ticks, executes trades, posts data
        │
        ├── POST /tick              ─┐
        ├── POST /event              ├── server/server.py (Flask + SQLite, port 7429)
        ├── POST /trade              │     └── crow.db
        └── GET  /api/config/active ─┘           ├── ticks
                                                  ├── trades
Dashboard (browser)                               ├── events
  └── server/dashboard.html                       ├── configs
        └── GET /api/*  (auth-protected)          ├── evo_population
                                                  └── evo_champion_history
Evolution daemon (background process)
  └── server/evolve.py              — island-model GA, promotes champions to configs

Walk-forward tool (manual)
  └── server/evolve_walkforward.py  — OOS benchmark, seeds evo_population on completion

Internet access
  └── cloudflared tunnel → crow.sinceremickey.com
```

---

## How the Bot Works

### Score Formula

Each tick, the extension computes a momentum score using a configurable formula. Example (Crow-3 family):

```
(deltaLong + deltaShort*(secsRemaining/lookback)) / abs(gap) + (gap/200)
```

**Variables available in the formula:**

| Variable        | Description |
|-----------------|-------------|
| `deltaLong`     | BTC spot price minus the market's strike price ($) |
| `deltaShort`    | BTC price change over the last `lookback` seconds ($) |
| `gapDelta`      | Change in `gap` over the last `lookback` seconds |
| `secsRemaining` | Seconds until market expiry |
| `lookback`      | Short lookback window in seconds |
| `gap`           | UP price minus DOWN price (spread between contracts, ¢) |
| `rsi`           | RSI of BTC price (Wilder EMA, period configurable) |
| `btc`           | Current BTC spot price |
| `up`            | Current price to buy the UP contract (¢) |
| `down`          | Current price to buy the DOWN contract (¢) |

**Signal logic:**
- Score crosses **above +threshold** → enter **Up** position
- Score crosses **below −threshold** → enter **Down** position
- Score crosses **below +exitThreshold** (while long) → exit
- Score crosses **above −exitThreshold** (while short) → exit

### Trade Guards

Before entering a trade, the bot checks:
- `cooldown` — market seconds to wait after open before any trade is allowed (`secsRemaining ≤ 900 − cooldown`)
- `exitCooldown` — ticks to wait after a sell before re-entering
- `lossLimit` — cumulative % loss cap for the current market; stops new entries if breached
- `minEntry` / `maxEntry` — only enter if the contract price is within this range (¢)

While in a trade:
- `sellLimit` — exit immediately if the contract price reaches this value (¢)

### Live Order Execution

In live mode the bot clicks Kalshi's own UI buttons rather than calling the API directly:

1. Click the direction button (Up / Down / Sell)
2. Set the quantity input via React's internal value setter
3. Click "Review Buy" → wait 500ms → click "Submit Buy"
4. Check for "Resubmit order" button (unfilled order) → click Cancel and retry
5. Verify the position is open (looks for active "Sell" button); if not found → scrub the trade record

Sells follow the same flow. On a **flip** (exit one side, immediately enter the other), the buy waits for the sell to fully confirm before firing.

---

## Components

### `extension/content.js`

The core bot. Key responsibilities:

- **Tick loop** — fires every second, reads BTC price from Kalshi's S3 feed, reads UP/DOWN contract prices from the DOM, computes the score, checks entry/exit conditions
- **Config polling** — fetches `/api/config/active` every 5 seconds; forces paper mode if server is unreachable
- **UI readiness** — polls until the quantity input is settable via React's setter AND the price buttons are present
- **SPA navigation** — MutationObserver detects URL changes, cleans up all intervals/panels, and re-boots the bot on the new market
- **Data logging** — posts every tick, entry, exit, and order event to the backend

Three overlay panels are injected into Kalshi's page:
- **Score panel** (top-right) — current score, config name, paper/live indicator, cooldown countdown
- **Trades panel** (top-left) — open position + trades from the current market
- **Stats panel** — cumulative stats from previous markets

### `server/server.py`

Flask server on port 7429. Auth-protected routes require a session cookie (set via `/login`) or an `X-API-Key` header.

**Ingest endpoints** (no auth — called by extension via localhost):

| Method | Path | Description |
|--------|------|-------------|
| POST | `/tick` | One record per second per market |
| POST | `/event` | Entry, exit, order_filled, order_failed events |
| POST | `/trade` | Full trade record at close, with entry+exit context snapshots |
| GET | `/api/config/active` | Active config for the extension to poll |

**Dashboard API** (auth required):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stats` | Aggregate stats + cumulative return series + recent trades |
| GET | `/api/markets` | Per-market W/L/return summary |
| GET | `/api/markets/<ticker>/ticks` | All tick data for a market |
| GET | `/api/markets/<ticker>/trades` | All trades for a market |
| GET | `/api/analytics/trades` | All trades with computed return_pct for scatter plots |
| GET | `/api/configs` | List all configs |
| POST | `/api/configs` | Create a new config (auto-named Crow-N) |
| PUT | `/api/configs/<id>` | Update a config |
| DELETE | `/api/configs/<id>` | Delete a config (active config cannot be deleted) |
| POST | `/api/configs/<id>/activate` | Set a config as active |
| POST | `/api/backtest` | Run a backtest on a config over recent markets |

**Database schema** (`crow.db`):

```
ticks        — ts, market_ticker, score, last_score, secs_remaining, delta_long,
               delta_short, gap, btc, price_at_lookback, series (JSON), target_price,
               up, down, sell, has_open_trade

trades       — ts, market_ticker, side, result, entry_price, exit_price,
               entry_time, close_time, contract_count,
               entry_{score, last_score, secs_remaining, delta_long, delta_short, gap,
                      btc, target_price, up, down},
               exit_{score, secs_remaining, delta_long, delta_short, gap, btc, up, down}

events       — ts, market_ticker, event_type, data (JSON)

configs      — id, name, active, formula, threshold, exit_threshold,
               short_lookback, exit_lookback, tuner, cooldown, loss_limit,
               sell_limit, exit_cooldown, min_entry, max_entry,
               trade_amount, live_trading, created_at, updated_at

evo_population       — evolved genomes available for daemon seeding
evo_champion_history — timestamped record of each champion promoted to configs
evo_gen_log          — per-generation fitness logs from the daemon
```

### `server/evolve.py`

Continuous evolution daemon. Runs as a background process, continuously searching for better trading formulas using a genetic algorithm.

**Architecture:**
- **Island model** — 4 independent sub-populations of 50 genomes each. Islands evolve separately and exchange top migrants every 5 generations.
- **Genome** — expression tree (formula) + numeric parameters (threshold, exit_threshold, short_lookback, rsi_period, regime filter, min/max entry, cooldown, exit_cooldown)
- **Fitness** — evaluated by backtesting the genome over the most recent 30 completed markets. Score: `avg_return × wr^1.5 × count_scale × neg_rate_penalty`, with per-trade returns capped at ±150/110%.
- **Promotion** — any genome that improves on the current champion is auto-promoted to a new named config and saved to the database.
- **Seeding** — on startup and after walk-forward runs, reads `evo_population` table to warm-start with known good genomes.

**Seed formula families:**
- **Crow-3**: `(deltaLong + deltaShort*(secsRemaining/lookback)) / abs(gap) + (gap/200)`
- **Crow-60**: `(deltaLong / secsRemaining) + (gapDelta / (lookback * 2))`
- **Crow-A**: `(deltaLong - deltaShort) / secsRemaining + gapDelta / lookback`

Run via:
```powershell
python evolve.py          # starts daemon in foreground
# or backgrounded by start.ps1
```

### `server/evolve_walkforward.py`

Walk-forward backtester. Validates a population by evolving independently on each rolling window of 30 markets and measuring out-of-sample (OOS) performance on the next 5. Gives genuine OOS metrics because each step's test set was never seen during that step's evolution.

Run manually to benchmark a config or seed the daemon's population:
```powershell
python evolve_walkforward.py
```

Output: step-by-step OOS stats + aggregate summary. On completion, saves the best genomes to `evo_population` for the daemon to pick up.

### `server/dashboard.html`

Single-page app, no framework. Four tabs:

- **Stats** — 7 summary cards, cumulative return chart, recent trades table with entry context (ΔLong, secs, score)
- **Markets** — clickable market list; detail view has score timeline, BTC Δlong chart, trades table
- **Analytics** — 4 scatter plots: return vs ΔLong, return vs secsRemaining, return vs entry score, return vs entry price
- **Configs** — list of named Crow configs, editor with formula textarea + all parameters + PAPER/LIVE toggle

### `server/crow_api.py`

Python helper module for scripted experiments and REPL-driven analysis. Wraps all dashboard API endpoints:

```python
from crow_api import *
configs = list_configs()
result  = backtest(config_id=194, market_count=60)
summary(result)
```

### Analysis / Utility Scripts

| Script | Purpose |
|--------|---------|
| `check_lookback.py` | Backtest a single config with per-trade detail |
| `check_subsample.py` | Backtest over a subsample of markets |
| `check_seam.py/2/3` | Inspect market boundary behaviour |
| `check_signal.py` | Signal trace for a specific market |
| `rsi_analysis.py` | RSI-filtered backtest analysis |
| `evolve_backtest.py` | Standalone evolution + backtest loop |
| `plot_walkforward.py` | Plot walk-forward OOS results |
| `purge_old_ticks.py` | Delete ticks older than N days |
| `seed60_rsi.py` | Seed DB with Crow-60/RSI variant genomes |
| `migrate_snapshots.py` | Schema migration utility |
| `analysis/plot_delta_long.py` | Scatter plot: secsRemaining vs ΔLong, colored by result |

---

## Setup

### Prerequisites

- Python 3.11+ with `flask`
- Chrome (for the extension)
- `cloudflared` installed (`winget install Cloudflare.cloudflared`)

### Install dependencies

```powershell
pip install flask
```

### Load the extension

1. Open `chrome://extensions`
2. Enable Developer Mode
3. Load Unpacked → select the `extension/` folder

### First-time tunnel setup

```powershell
cloudflared tunnel login
cloudflared tunnel create crow
cloudflared tunnel route dns crow crow.sinceremickey.com
cloudflared tunnel list   # copy the UUID, update cloudflared.yml
```

### Start the server

```powershell
cd server
.\start.ps1
```

Or manually:

```powershell
$env:CROW_PASSWORD = "your-password"
python server.py
# in another terminal:
cloudflared tunnel --config cloudflared.yml run
# optionally start the evo daemon:
python evolve.py
```

Dashboard is at `http://localhost:7429` locally or `https://crow.sinceremickey.com` remotely.

---

## Configuration Fields

| Field | Default | Description |
|-------|---------|-------------|
| `formula` | — | Score formula evaluated each tick |
| `threshold` | 1.0 | Score magnitude required to enter a trade |
| `exit_threshold` | 0.0 | Score level to exit (0 = exit on any sign flip) |
| `short_lookback` | 15 | BTC price lookback window for deltaShort and gapDelta (s) |
| `rsi_period` | 42 | RSI period for the `rsi` formula variable |
| `cooldown` | 15 | Market seconds after open before first trade allowed (`secsRemaining ≤ 900 − cooldown`) |
| `exit_cooldown` | 30 | Ticks to wait after a sell before re-entering |
| `loss_limit` | 0 | Cumulative loss % cap for the market (0 = disabled) |
| `sell_limit` | 0 | Exit if contract price reaches this value in ¢ (0 = disabled) |
| `min_entry` | 0 | Minimum contract price to enter (¢) |
| `max_entry` | 100 | Maximum contract price to enter (¢) |
| `trade_amount` | 1.0 | Dollar amount per trade |
| `live_trading` | false | Paper mode when false; requires server connection to enable |

---

## Named Configs (Crows)

Each trading configuration is a "Crow". They're stored in the database and one can be active at a time. The active config is polled by the extension every 5 seconds. If no config is active or the server is unreachable, the extension forces paper mode.

New configs are auto-named Crow-1, Crow-2, etc. Descriptions go in the `notes` field only — the name is always the auto-generated Crow-N identifier.

The evolution daemon promotes promising genomes as new Crow configs automatically. These can be activated from the dashboard for paper or live testing.

**Session history:**
- `Crow-2` — traded 2026-06-02, 26 trades recorded in `analysis/records/crow-2.json`
- `Crow-154` — best-performing evolved config; live test started 2026-06-04. Formula: `(deltaLong + deltaShort*(secsRemaining/lookback)) / abs(gap)` with RSI regime filter (rsi < 40 or > 69). Walk-forward OOS: avg +68.8%, 0/60 negative trades.
