"""Plot walk-forward backtest results from hardcoded step data."""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

# (step, oos_n, oos_avg, oos_wr)
STEPS = [
    ( 1,  2,  -93.2,   0.0),
    ( 2,  4,  +15.7,  25.0),
    ( 3,  4,  -59.5,  25.0),
    ( 4,  1, +1983.3, 100.0),
    ( 5,  6,  +31.4, 100.0),
    ( 6,  2, -100.0,   0.0),
    ( 7,  1, +2027.7, 100.0),
    ( 8,  1,  +50.0, 100.0),
    ( 9,  0,    None,  None),
    (10,  3,  -29.5,  66.7),
    (11,  1,   +7.3, 100.0),
    (12,  5,  -38.5,  60.0),
    (13,  3,  +35.8, 100.0),
    (14,  1,   +4.8, 100.0),
    (15,  7,  +25.2,  85.7),
    (16,  2, +184.4, 100.0),
    (17,  1, -100.0,   0.0),
    (18,  0,    None,  None),
    (19,  1, -100.0,   0.0),
    (20,  1,  +19.1, 100.0),
    (21,  1, -100.0,   0.0),
    (22,  3,  +34.4, 100.0),
    (23,  4,  +47.0,  75.0),
    (24,  3,  +36.9,  66.7),
    (25,  2,  -25.0,  50.0),
    (26,  4,  +56.1,  75.0),
    (27,  0,    None,  None),
    (28,  0,    None,  None),
    (29,  0,    None,  None),
    (30,  1,  -43.8,   0.0),
    (31,  0,    None,  None),
    (32,  1, -100.0,   0.0),
    (33, 10,  +12.0,  80.0),
    (34,  3,   +7.1, 100.0),
    (35,  4,  -42.3,  50.0),
    (36,  0,    None,  None),
]

# Build trade sequence: approximate each step's n trades as all having step avg
trade_returns = []
trade_steps   = []
for step, n, avg, wr in STEPS:
    if n > 0 and avg is not None:
        for _ in range(n):
            trade_returns.append(avg)
            trade_steps.append(step)

trade_nums = np.arange(1, len(trade_returns) + 1)
cumulative = np.cumsum(trade_returns)

# Capped version (strip outliers > ±300%)
CAP = 300.0
capped = [max(-100.0, min(CAP, r)) for r in trade_returns]
cum_capped = np.cumsum(capped)

# ── Plot ───────────────────────────────────────────────────────────────────────
fig, axes = plt.subplots(3, 1, figsize=(14, 11),
                         gridspec_kw={'height_ratios': [3, 2, 1]},
                         sharex=False)
fig.suptitle('Walk-Forward Backtest: Evolution Engine  (genuine out-of-sample)',
             fontsize=13, fontweight='bold', y=0.98)

# ── Panel 1: Equity curve ──────────────────────────────────────────────────────
ax1 = axes[0]

ax1.plot(trade_nums, cum_capped, color='steelblue', linewidth=1.8,
         label=f'Equity curve (trade returns capped at +/-{CAP:.0f}%)', zorder=4)
ax1.plot(trade_nums, cumulative, color='orange', linewidth=1.0, linestyle='--',
         alpha=0.65, label='Uncapped (two +2000% outliers dominate)', zorder=3)
ax1.axhline(0, color='black', linewidth=0.6, linestyle='-')

ax1.fill_between(trade_nums, 0, cum_capped,
                 where=cum_capped >= 0, alpha=0.10, color='green', interpolate=True)
ax1.fill_between(trade_nums, 0, cum_capped,
                 where=cum_capped < 0,  alpha=0.10, color='red',   interpolate=True)

# Annotate outlier trades
for i, (ret, step) in enumerate(zip(trade_returns, trade_steps)):
    if abs(ret) > 500:
        ax1.annotate(f'Step {step}: {ret:+.0f}%',
                     xy=(trade_nums[i], cum_capped[i]),
                     xytext=(trade_nums[i] + 1.5, cum_capped[i] + 100),
                     fontsize=7.5, color='darkorange',
                     arrowprops=dict(arrowstyle='->', color='darkorange', lw=0.8))

ax1.set_ylabel('Cumulative return (% per trade-stake)', fontsize=9)
ax1.set_title('Equity Curve  (82 OOS trades, chronological)', fontsize=10)
ax1.legend(fontsize=8, loc='upper left')
ax1.grid(True, alpha=0.25)
ax1.set_xlim(0.5, len(trade_nums) + 0.5)

# ── Panel 2: Per-step avg bar chart ───────────────────────────────────────────
ax2 = axes[1]

BAR_CAP = 250.0
xs, ys, cs, ns_bar = [], [], [], []
for step, n, avg, wr in STEPS:
    if n > 0 and avg is not None:
        clipped = max(-100.0, min(BAR_CAP, avg))
        xs.append(step)
        ys.append(clipped)
        cs.append('#2ecc71' if avg >= 0 else '#e74c3c')
        ns_bar.append(n)

bars = ax2.bar(xs, ys, color=cs, alpha=0.75, width=0.65, zorder=3)
ax2.axhline(0,    color='black',    linewidth=0.6)
ax2.axhline(51.2, color='steelblue', linewidth=1.2, linestyle='--',
            alpha=0.8, label='Overall OOS avg +51.2%')
ax2.axhline(10.0, color='gray',     linewidth=0.8, linestyle=':',
            alpha=0.6, label='Deployment target +10%')

# Label clipped bars
for x, orig_avg, step in [(s, a, st) for (st, n, a, w) in STEPS if n > 0 and a is not None
                           for s in [st]]:
    if orig_avg > BAR_CAP:
        ax2.text(x, BAR_CAP + 5, f'{orig_avg/1000:.1f}k%', ha='center',
                 fontsize=6.5, color='darkorange', rotation=90, va='bottom')

ax2.set_ylabel(f'OOS avg (capped +{BAR_CAP:.0f}%)', fontsize=9)
ax2.set_xlabel('Walk-forward step  (each covers 5 unseen markets)', fontsize=9)
ax2.set_title('Per-Step OOS Average Return', fontsize=10)
ax2.legend(fontsize=8, loc='upper right')
ax2.grid(True, alpha=0.25, axis='y')
ax2.set_xlim(0.5, 36.5)
ax2.xaxis.set_major_locator(mticker.MultipleLocator(2))

# ── Panel 3: Trade count ───────────────────────────────────────────────────────
ax3 = axes[2]

all_steps = [s[0] for s in STEPS]
all_ns    = [s[1] for s in STEPS]
bar_colors = ['#3498db' if n > 0 else '#ecf0f1' for n in all_ns]
ax3.bar(all_steps, all_ns, color=bar_colors, alpha=0.85, width=0.7, zorder=3)
ax3.set_ylabel('OOS trades', fontsize=9)
ax3.set_xlabel('Step', fontsize=9)
ax3.set_title('OOS Trade Count per Step  (grey = zero trades)', fontsize=10)
ax3.grid(True, alpha=0.25, axis='y')
ax3.set_xlim(0.5, 36.5)
ax3.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
ax3.xaxis.set_major_locator(mticker.MultipleLocator(2))

plt.tight_layout(rect=[0, 0, 1, 0.97])
out = 'walkforward_performance.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
print(f'Saved: {out}')
