(function () {
  'use strict';

  const BTC_URL = 'https://kalshi-public-docs.s3.amazonaws.com/external/crypto/btc_current.json';
  const POLL_MS = 1000;
  const LS_TRADES = 'kalshi_paper_trades';

  const ORDERS_API  = 'https://api.elections.kalshi.com/v1/users';
  const BACKEND_URL = 'http://localhost:7429';

  function postBackend(path, data) {
    chrome.runtime.sendMessage({
      type: 'backend',
      url: BACKEND_URL + path,
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }, () => { void chrome.runtime.lastError; }); // fire-and-forget; suppress no-listener warning
  }

  function postEvent(type, data) {
    postBackend('/event', { type, marketTicker: state.marketTicker, data });
  }

  function applyConfig(cfg) {
    if (!cfg) return;
    state.threshold       = cfg.threshold      ?? state.threshold;
    state.exitThreshold   = cfg.exit_threshold ?? state.exitThreshold;
    state.shortLookback   = cfg.short_lookback ?? state.shortLookback;
    state.cooldown        = cfg.cooldown       ?? state.cooldown;
    state.lossLimit       = cfg.loss_limit     ?? state.lossLimit;
    state.sellLimit       = cfg.sell_limit     ?? state.sellLimit;
    state.stopLoss        = cfg.stop_loss      ?? state.stopLoss;
    state.exitCooldown    = cfg.exit_cooldown  ?? state.exitCooldown;
    state.minEntry        = cfg.min_entry      ?? state.minEntry;
    state.maxEntry        = cfg.max_entry      ?? state.maxEntry;
    state.tradeAmount     = cfg.trade_amount   ?? state.tradeAmount;
    state.liveTrading     = !!cfg.live_trading;
    state.formula         = cfg.formula        || DEFAULT_FORMULA;
    state.configName      = cfg.name           ?? null;
    state.configId        = cfg.id             ?? null;
    state.configUpdatedAt = cfg.updated_at     ?? null;
    state.regimeFilter    = cfg.regime_filter  ?? null;
    state.rsiPeriod       = cfg.rsi_period     ?? state.rsiPeriod;
  }

  function fetchActiveConfig() {
    chrome.runtime.sendMessage({
      type: 'backend',
      url: BACKEND_URL + '/api/config/active',
      method: 'GET',
    }, response => {
      void chrome.runtime.lastError;
      if (!response?.ok) {
        state.configConnected = false;
        state.liveTrading     = false;
        return;
      }
      const cfg = response.data;
      if (cfg && cfg.id) {
        state.configConnected = true;
        if (cfg.id !== state.configId || cfg.updated_at !== state.configUpdatedAt) applyConfig(cfg);
      } else {
        state.configConnected = false;
        state.liveTrading     = false;
        state.configName      = null;
        state.configId        = null;
      }
    });
  }

  // ─── Auth token capture ───────────────────────────────────────
  // Inject into the page's JS context to intercept Kalshi's own fetch calls
  // and capture the x-csrf-token and user ID that Kalshi uses.

  const _auth = { csrf: null, waf: null, userId: null };

  window.addEventListener('message', e => {
    if (e.source !== window || e.data?.__ks !== 1) return;
    if (e.data.csrf)   _auth.csrf   = e.data.csrf;
    if (e.data.waf)    _auth.waf    = e.data.waf;
    if (e.data.userId) _auth.userId = e.data.userId;
  });

  (function injectInterceptor() {
    const s = document.createElement('script');
    s.textContent = `(function(){
      const o=window.fetch;
      window.fetch=function(url,opt){
        if(typeof url==='string'&&url.includes('api.elections.kalshi.com')){
          const h=(opt&&opt.headers)||{};
          const d={__ks:1};
          if(h['x-csrf-token'])  d.csrf=h['x-csrf-token'];
          if(h['x-aws-waf-token'])d.waf=h['x-aws-waf-token'];
          const m=url.match(/\\/v1\\/users\\/([\\w-]+)\\//);
          if(m) d.userId=m[1];
          if(d.csrf||d.waf||d.userId) window.postMessage(d,'*');
        }
        return o.apply(this,arguments);
      };
    })();`;
    document.documentElement.appendChild(s);
    s.remove();
  })();

  function getAuthHeaders() {
    const csrf = _auth.csrf ?? (() => {
      try { return JSON.parse(localStorage.getItem('csrfToken') ?? 'null')?.value; } catch {}
    })();
    const waf = _auth.waf ?? localStorage.getItem('awswaf_session_storage') ?? '';
    return {
      'Content-Type':    'application/json',
      'x-aws-waf-token': waf,
      ...(csrf ? { 'x-csrf-token': csrf } : {}),
    };
  }

  // ─── State ────────────────────────────────────────────────────

  const DEFAULT_FORMULA = '(deltaLong / secsRemaining) + (deltaShort / lookback) + (gap / 200)';

  const state = {
    threshold:        1,
    exitThreshold:    0,
    minEntry:         0,
    maxEntry:         100,
    shortLookback:    15,
    cooldown:         15,
    lossLimit:        0,
    sellLimit:        0,
    stopLoss:         0,
    exitCooldown:     30,
    formula:          DEFAULT_FORMULA,
    configName:       null,
    configId:         null,
    configConnected:  false,
    configUpdatedAt:  null,
    lastScore:        null,
    lastTargetPrice:  null,
    floorStrike:      null,
    lastSecsRemaining: null,
    lastBtcSeries:    null,
    lastGapSeries:    [],
    openTrade:        null,   // { side, entryPrice, entryTime, contractCount }
    trades:           loadTrades(),
    navigating:       false,
    liveTrading:      false,
    tradeAmount:      1,
    marketTicker:     null,
    marketId:         null,   // market UUID from API
    userId:           null,   // Kalshi member UUID
    regimeFilter:     null,
    rsiPeriod:        42,
    rsi:              null,
  };

  let scoreEl, tradesEl, statsEl;
  let tickInterval      = null;
  let configPollInterval = null;
  let uiReadyPoll  = null;
  let uiReadyTime  = null;
  let lastSellTime = null;

  // ─── Persistence ──────────────────────────────────────────────

  function loadTrades() {
    try {
      return JSON.parse(localStorage.getItem(LS_TRADES) || '[]').map(t => ({
        ...t,
        entryTime: new Date(t.entryTime),
        closeTime: new Date(t.closeTime),
      }));
    } catch { return []; }
  }

  function saveTrades() {
    localStorage.setItem(LS_TRADES, JSON.stringify(state.trades));
  }

  // ─── BTC fetch ────────────────────────────────────────────────

  async function fetchBtcData() {
    const r = await fetch(BTC_URL + '?t=' + Date.now());
    const d = await r.json();
    const s = d?.timeseries?.second;
    if (!s?.length) return null;
    const lookback = Math.max(1, Math.round(state.shortLookback));
    return {
      current:         s[s.length - 1],
      priceAtLookback: s[Math.max(0, s.length - 1 - lookback)],
      series:          s,
      targetPrice:     d?.candlesticks?.['15M']?.open ?? null,
    };
  }

  // ─── DOM reads ────────────────────────────────────────────────

  function getSecondsRemaining() {
    for (const h of document.querySelectorAll('h2')) {
      const m = h.textContent.trim().match(/^(\d{1,2}):(\d{2})$/);
      if (m) return parseInt(m[1]) * 60 + parseInt(m[2]);
    }
    return null;
  }

  function sharesAncestor(a, b, maxLevels) {
    let el = a.parentElement;
    for (let i = 0; i < maxLevels; i++) {
      if (!el) break;
      if (el.contains(b)) return true;
      el = el.parentElement;
    }
    return false;
  }

  function getUpDownPrices() {
    let up = null, down = null, sell = null, upEl = null, downEl = null;
    for (const btn of document.querySelectorAll('button')) {
      if (btn.offsetParent === null) continue;
      const t = btn.textContent.trim();
      const u = t.match(/^Up\s+(\d+(?:\.\d+)?)¢$/);
      const d = t.match(/^Down\s+(\d+(?:\.\d+)?)¢$/);
      const s = t.match(/^Sell\s+(\d+(?:\.\d+)?)¢$/);
      if (u) { up = parseFloat(u[1]); upEl = btn; }
      if (d) { down = parseFloat(d[1]); downEl = btn; }
      if (s) sell = parseFloat(s[1]);
    }
    if (up != null && down != null) {
      if (up + down >= 85 && sharesAncestor(upEl, downEl, 8)) return { up, down, sell };
      return { up: null, down: null, sell };
    }
    // One button missing (position held) — derive complement
    if (up   != null && up   > 1 && up   < 99) return { up,         down: 100 - up,   sell };
    if (down != null && down > 1 && down < 99) return { up: 100 - down, down,         sell };
    return { up: null, down: null, sell };
  }

  // ─── Trading logic ────────────────────────────────────────────

  function checkEntry(score, up, down, secsRemaining, tickData) {
    if (state.openTrade != null) return;
    if (state.lastScore == null) return;
    if (!secsRemaining)          return;
    if (lastSellTime != null && (Date.now() - lastSellTime) < state.exitCooldown * 1000) return;
    if (state.lossLimit > 0) {
      const mktTrades = state.trades.filter(t => t.marketTicker === state.marketTicker);
      if (mktTrades.length > 0) {
        const mktSum = (mktTrades.reduce((s, t) => s + t.exitPrice / t.entryPrice, 0) - mktTrades.length) * 100;
        if (mktSum <= -state.lossLimit) return;
      }
    }
    if (state.regimeFilter) {
      try {
        const ok = evalFormula(state.regimeFilter, {
          deltaLong:     tickData.deltaLong     ?? 0,
          deltaShort:    tickData.deltaShort    ?? 0,
          gapDelta:      tickData.gapDelta      ?? 0,
          secsRemaining: tickData.secsRemaining ?? 1,
          lookback:      state.shortLookback,
          gap:           tickData.gap           ?? 0,
          tuner:         1,
          btc:           tickData.btc           ?? 0,
          up:            tickData.up            ?? 0,
          down:          tickData.down          ?? 0,
          rsi:           state.rsi              ?? 50.0,
        });
        if (!ok) return;
      } catch { /* filter error = allow entry */ }
    }
    const { threshold: t, minEntry, maxEntry } = state;
    const inRange = px => px != null && px >= minEntry && px <= maxEntry;
    if (state.lastScore <= t && score > t && inRange(up)) {
      state.openTrade = { side: 'Up', entryPrice: up, entryTime: new Date(), entryData: tickData };
      postEvent('entry', { side: 'Up', entryPrice: up, tickData });
    } else if (state.lastScore >= -t && score < -t && inRange(down)) {
      state.openTrade = { side: 'Down', entryPrice: down, entryTime: new Date(), entryData: tickData };
      postEvent('entry', { side: 'Down', entryPrice: down, tickData });
    }
  }

  function checkExit(score, up, down, tickData) {
    if (!state.openTrade || score == null) return;
    const { side, entryPrice } = state.openTrade;
    const px = side === 'Up' ? up : down;
    if (px == null) return;
    const et = state.exitThreshold;
    const triggered = side === 'Up' ? score < et : score > -et;
    const limitHit = state.sellLimit > 0 && px >= state.sellLimit;
    const stopHit  = state.stopLoss  > 0 && px <= state.stopLoss;
    if (triggered || limitHit || stopHit) closeTrade(px, px > entryPrice ? 'win' : 'loss', tickData);
  }

  function closeTrade(exitPrice, result, exitData) {
    const closed = { ...state.openTrade, exitPrice, result, closeTime: new Date(), marketTicker: state.marketTicker, exitData: exitData ?? null };
    state.trades.unshift(closed);
    state.openTrade = null;
    saveTrades();
    postEvent('exit', { side: closed.side, entryPrice: closed.entryPrice, exitPrice, result, exitData });
    const ed = closed.entryData ?? {};
    const xd = exitData ?? {};
    postBackend('/trade', {
      marketTicker:          closed.marketTicker,
      side:                  closed.side,
      result,
      entryPrice:            closed.entryPrice,
      exitPrice,
      entryTime:             closed.entryTime,
      closeTime:             closed.closeTime,
      contractCount:         closed.contractCount ?? null,
      entryScore:            ed.score            ?? null,
      entryLastScore:        ed.lastScore        ?? null,
      entrySecsRemaining:    ed.secsRemaining    ?? null,
      entryDeltaLong:        ed.deltaLong        ?? null,
      entryDeltaShort:       ed.deltaShort       ?? null,
      entryGap:              ed.gap              ?? null,
      entryBtc:              ed.btc              ?? null,
      entryTargetPrice:      ed.targetPrice      ?? null,
      entryUp:               ed.up               ?? null,
      entryDown:             ed.down             ?? null,
      exitScore:             xd.score            ?? null,
      exitSecsRemaining:     xd.secsRemaining    ?? null,
      exitDeltaLong:         xd.deltaLong        ?? null,
      exitDeltaShort:        xd.deltaShort       ?? null,
      exitGap:               xd.gap              ?? null,
      exitBtc:               xd.btc              ?? null,
      exitUp:                xd.up               ?? null,
      exitDown:              xd.down             ?? null,
    });
  }

  // ─── Live order execution ─────────────────────────────────────

  async function submitOrder(orderAction, positionSide, priceCents, countFp) {
    const uid = state.userId ?? _auth.userId;
    if (!uid) { showOrderError('no user ID'); return null; }
    if (!state.marketId) { showOrderError('no market ID'); return null; }
    const isSell   = orderAction === 'sell';
    const userSide = positionSide === 'Up' ? 'yes' : 'no';
    const kalshiSide = userSide;
    const body = {
      market_id:            state.marketId,
      count_fp:             countFp,
      side:                 kalshiSide,
      price_dollars:        (priceCents / 100).toFixed(4),
      max_cost_cents:       0,
      sell_position_capped: isSell,
      expiration_unix_ts:   0,
      time_in_force:        'immediate_or_cancel',
      order_action:         orderAction,
      user_side:            userSide,
      order_type:           'market',
      post_only:            false,
    };
    try {
      const r = await fetch(`${ORDERS_API}/${uid}/orders`, {
        method:      'POST',
        credentials: 'include',
        headers:     getAuthHeaders(),
        body:        JSON.stringify(body),
      });
      if (!r.ok) {
        const txt = await r.text();
        console.error('[kalshi]', orderAction, 'failed', r.status, txt);
        showOrderError(`${orderAction} ${r.status}`);
        return null;
      }
      return await r.json();
    } catch (e) {
      console.error('[kalshi]', orderAction, 'error', e);
      showOrderError(`${orderAction} error`);
      return null;
    }
  }

  async function executeUiTrade(side, action) {
    const isSell = action === 'sell';

    // Click the direction button (Up X¢ / Down X¢) or the Sell X¢ button
    const pat = isSell ? /^Sell\s+\d/ : (side === 'Up' ? /^Up\s+\d/ : /^Down\s+\d/);
    let dirBtn = null;
    for (const btn of document.querySelectorAll('button')) {
      if (btn.offsetParent !== null && pat.test(btn.textContent.trim())) { dirBtn = btn; break; }
    }
    if (!dirBtn) { showOrderError(`${action} button not found`); return false; }
    dirBtn.click();
    await new Promise(r => setTimeout(r, 200));

    if (!isSell) {
      // Set the trade amount (React-compatible)
      const amountInput = document.querySelector('input[inputmode="decimal"][data-lpignore="true"]');
      if (!amountInput) { showOrderError('amount input not found'); return false; }
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(amountInput, state.tradeAmount.toFixed(2));
      amountInput.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise(r => setTimeout(r, 200));
    }

    // Click Review, then Submit
    for (const label of [isSell ? 'Review Sell' : 'Review Buy', isSell ? 'Submit Sell' : 'Submit Buy']) {
      const btn = [...document.querySelectorAll('button')]
        .find(b => b.offsetParent !== null && b.textContent.trim() === label);
      if (!btn) { showOrderError(`"${label}" button not found`); return false; }
      btn.click();
      await new Promise(r => setTimeout(r, 500));
    }

    if (isSell) await new Promise(r => setTimeout(r, 500));

    // Check for "Resubmit order" — means the order didn't fill; cancel and retry
    await new Promise(r => setTimeout(r, 1000));
    const resubmitBtn = [...document.querySelectorAll('button')]
      .find(b => b.offsetParent !== null && b.textContent.trim() === 'Resubmit order');
    if (resubmitBtn) {
      const cancelBtn = [...document.querySelectorAll('button')]
        .find(b => b.offsetParent !== null && b.textContent.trim() === 'Cancel');
      if (!cancelBtn) { showOrderError('"Cancel" button not found after Resubmit'); return false; }
      cancelBtn.click();
      await new Promise(r => setTimeout(r, 300));
      return executeUiTrade(side, action);
    }

    if (!isSell) {
      const filled = [...document.querySelectorAll('button')]
        .some(b => b.offsetParent !== null && /^Sell\s+\d/.test(b.textContent.trim()));
      if (!filled) {
        showOrderError('entry not confirmed — Sell button not found');
        const doneBtn = [...document.querySelectorAll('button')]
          .find(b => b.offsetParent !== null && b.textContent.trim() === 'Done');
        if (doneBtn) { doneBtn.click(); await new Promise(r => setTimeout(r, 300)); }
        return false;
      }
    } else {
      const stillOpen = [...document.querySelectorAll('button')]
        .some(b => b.offsetParent !== null && /^Sell\s+\d/.test(b.textContent.trim()));
      if (stillOpen) {
        showOrderError('exit not confirmed — Sell button still present');
        const doneBtn = [...document.querySelectorAll('button')]
          .find(b => b.offsetParent !== null && b.textContent.trim() === 'Done');
        if (doneBtn) { doneBtn.click(); await new Promise(r => setTimeout(r, 300)); }
        return executeUiTrade(side, action);
      }
    }

    return true;
  }

  async function placeOrder(side, priceCents) {
    const priceDollars = priceCents / 100;
    if (priceDollars <= 0) return null;
    const ok = await executeUiTrade(side, 'buy');
    if (!ok) return null;
    const countFp = (state.tradeAmount / priceDollars).toFixed(2);
    return { countFp, fillCountFp: countFp };
  }

  async function sellPosition(side) {
    return executeUiTrade(side, 'sell');
  }

  function showOrderError(msg) {
    if (!scoreEl) return;
    const el = document.createElement('div');
    el.textContent = `⚠ ${msg}`;
    Object.assign(el.style, {
      position: 'fixed', top: '50px', right: '20px', zIndex: '2147483647',
      background: '#7f1d1d', color: '#fca5a5', fontFamily: 'ui-monospace, monospace',
      fontSize: '12px', padding: '6px 12px', borderRadius: '6px',
    });
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  // ─── Market end: expiry resolution + navigation ───────────────

  function getEventTickerFromUrl() {
    const parts = location.pathname.split('/');
    const slug = parts[parts.length - 1];
    return slug ? slug.toUpperCase() : null;
  }

  async function fetchMarketData(eventTicker) {
    const r = await fetch(
      `https://api.elections.kalshi.com/v1/cached/events/?tickers=${eventTicker}`
    );
    const d = await r.json();
    return d?.events?.[0]?.markets?.[0] ?? null;
  }

  async function fetchMarketResult(eventTicker) {
    const market = await fetchMarketData(eventTicker);
    return market?.result ?? null; // "yes" | "no" | "" | null
  }

  function settleFallback() {
    const strike = state.floorStrike ?? state.lastTargetPrice;
    if (!state.openTrade || !state.lastBtcSeries || strike == null) return;
    const s = state.lastBtcSeries;
    const settlement = s.reduce((a, b) => a + b, 0) / s.length;
    const upWon = settlement >= strike;
    const won   = (state.openTrade.side === 'Up') === upWon;
    closeTrade(won ? 100 : 0, won ? 'win' : 'loss');
  }

  async function settleFromApi(eventTicker) {
    for (let i = 0; i < 30; i++) {
      await new Promise(r => setTimeout(r, 1000));
      if (!state.openTrade) return; // cleared externally (e.g. manual nav)
      try {
        const result = await fetchMarketResult(eventTicker);
        if (result === 'yes' || result === 'no') {
          const upWon = result === 'yes';
          const won = (state.openTrade.side === 'Up') === upWon;
          closeTrade(won ? 100 : 0, won ? 'win' : 'loss');
          state.navigating = true;
          return;
        }
      } catch {}
    }
    // API didn't settle within 30s — fall back to BTC series average
    settleFallback();
    state.navigating = true;
  }

  function checkMarketEnd(secsRemaining) {
    if (!(state.lastSecsRemaining > 0 && !secsRemaining)) return;

    if (state.openTrade) {
      const eventTicker = getEventTickerFromUrl();
      if (eventTicker) {
        settleFromApi(eventTicker); // async — sets navigating when done
      } else {
        settleFallback();
        state.navigating = true;
      }
    } else {
      state.navigating = true;
    }
  }

  function tryGoToLive() {
    for (const a of document.querySelectorAll('a')) {
      if (a.textContent.trim() === 'Go to live') {
        location.href = a.href;
        return;
      }
    }
  }

  // ─── Panel factory ────────────────────────────────────────────

  function makePanel(id, { top, right, left }) {
    const el = document.createElement('div');
    el.id = id;
    Object.assign(el.style, {
      position:     'fixed',
      top:          top + 'px',
      zIndex:       '2147483647',
      background:   'rgba(12,12,18,0.93)',
      color:        '#fff',
      fontFamily:   'ui-monospace, monospace',
      fontSize:     '13px',
      padding:      '12px 16px',
      borderRadius: '10px',
      boxShadow:    '0 4px 24px rgba(0,0,0,0.55)',
      border:       '1px solid rgba(255,255,255,0.1)',
      userSelect:   'none',
      cursor:       'move',
    });
    if (right != null) el.style.right = right + 'px';
    if (left  != null) el.style.left  = left  + 'px';
    document.body.appendChild(el);
    makeDraggable(el);
    return el;
  }

  // ─── Score panel ──────────────────────────────────────────────

  function renderScore(score, { deltaLong, deltaShort, gapDelta, secsRemaining, gap, entryBlock, formulaError, current, targetPrice, cooldownSecs, exitCooldownSecs, rsi } = {}) {
    const color = score == null ? '#666' : score > 0 ? '#22c55e' : '#ef4444';
    const blockRow = formulaError
      ? `<div style="color:#f87171;word-break:break-all">formula err: ${formulaError}</div>`
      : entryBlock
        ? `<div style="color:#f87171">block: ${entryBlock}</div>`
        : '';
    const rsiColor = rsi == null ? '#555'
      : (rsi < 40 || rsi > 69) ? '#22c55e' : '#f59e0b';
    scoreEl.innerHTML = `
      <div style="font-size:10px;color:#555;letter-spacing:1.5px;margin-bottom:4px">▸ KALSHI SCORE</div>
      <div style="font-size:28px;font-weight:700;color:${color};margin-bottom:6px">
        ${score == null ? '—' : score.toFixed(3)}
      </div>
      <div style="font-size:11px;color:#555;border-top:1px solid #1e1e1e;padding-top:6px;line-height:1.9">
        <div>btc&nbsp;&nbsp;&nbsp;&nbsp; ${current != null ? current.toFixed(0) : '—'}</div>
        <div>target&nbsp; ${targetPrice != null ? targetPrice.toFixed(0) : '—'}</div>
        <div>Δlong&nbsp;&nbsp; ${fmt(deltaLong,  2)}</div>
        <div>Δshort&nbsp; ${fmt(deltaShort, 4)}</div>
        <div>Δgap&nbsp;&nbsp;&nbsp; ${gapDelta != null ? gapDelta.toFixed(2) + '¢' : '—'}</div>
        <div>secs&nbsp;&nbsp;&nbsp; ${secsRemaining ?? '—'}</div>
        <div>gap&nbsp;&nbsp;&nbsp;&nbsp; ${gap != null ? gap.toFixed(1) + '¢' : '—'}</div>
        <div style="color:${rsiColor}">rsi&nbsp;&nbsp;&nbsp;&nbsp; ${rsi != null ? rsi.toFixed(0) : '—'}</div>
        <div>last&nbsp;&nbsp;&nbsp; ${state.lastScore == null ? 'null' : state.lastScore.toFixed(3)}</div>
        ${state.configConnected
          ? `<div style="color:#3b82f6;margin-top:4px">${state.configName ?? '—'}${state.liveTrading ? ' · <span style="color:#22c55e">LIVE</span>' : ' · PAPER'}</div>`
          : `<div style="color:#ef4444;margin-top:4px">no config · PAPER</div>`
        }
        ${cooldownSecs != null ? `<div style="color:#f59e0b">cooldown ${cooldownSecs}s</div>` : ''}
        ${exitCooldownSecs != null ? `<div style="color:#f59e0b">exit cooldown ${exitCooldownSecs}s</div>` : ''}
        ${blockRow}
      </div>`;
  }

  // ─── Trades panel ─────────────────────────────────────────────

  function renderTrades() {
    const mkt    = state.marketTicker;
    const trades = state.trades.filter(t => t.marketTicker === mkt);
    const rows   = [];

    if (state.openTrade) {
      const { side, entryPrice, entryTime } = state.openTrade;
      rows.push(`
        <div style="display:flex;justify-content:space-between;gap:16px;
                    padding:5px 0;border-bottom:1px solid #1e1e1e;color:#facc15">
          <span>● ${side} @ ${entryPrice.toFixed(1)}¢</span>
          <span style="color:#666;font-size:10px">${fmtTime(entryTime)}</span>
        </div>`);
    }

    if (!state.openTrade && trades.length === 0) {
      rows.push(`<div style="color:#333;font-size:11px;padding:4px 0">No trades yet</div>`);
    }

    for (const t of trades) {
      const color  = t.result === 'win' ? '#22c55e' : '#ef4444';
      const symbol = t.result === 'win' ? '✓' : '✗';
      const pct    = feeAdjPct(t.entryPrice, t.exitPrice);
      rows.push(`
        <div style="display:flex;justify-content:space-between;align-items:center;gap:16px;
                    padding:5px 0;border-bottom:1px solid #1a1a1a;font-size:12px">
          <span style="color:${color}">${symbol} ${t.side}</span>
          <span style="color:#aaa">${t.entryPrice.toFixed(1)}→${t.exitPrice.toFixed(1)}¢</span>
          <span style="color:${color};min-width:44px;text-align:right">
            ${(pct >= 0 ? '+' : '') + pct.toFixed(1)}%
          </span>
          <span style="color:#444;font-size:10px">${fmtTime(t.closeTime)}</span>
        </div>`);
    }

    const total = trades.length;
    const wins  = trades.filter(t => t.result === 'win').length;
    let headerStats = '';
    if (total > 0) {
      const sum = trades.reduce((s, t) => s + feeAdjPct(t.entryPrice, t.exitPrice), 0);
      const sumColor = sum >= 0 ? '#22c55e' : '#ef4444';
      headerStats = `
        <span style="color:#555">${wins}W ${total - wins}L</span>
        <span style="color:${sumColor};margin-left:8px">${sum >= 0 ? '+' : ''}${sum.toFixed(1)}%</span>`;
    }

    const clearBtn = `<button id="__ks_clear"
      style="background:none;border:1px solid #333;color:#555;font-family:inherit;
             font-size:10px;padding:1px 6px;border-radius:4px;cursor:pointer;letter-spacing:0.5px"
      >clear</button>`;

    tradesEl.innerHTML = `
      <div style="font-size:10px;color:#555;letter-spacing:1.5px;margin-bottom:8px;
                  display:flex;justify-content:space-between;align-items:center">
        <span>▸ TRADES</span>
        <span style="display:flex;align-items:center;gap:8px">${headerStats}${clearBtn}</span>
      </div>
      <div style="min-width:280px;line-height:1.8">${rows.join('')}</div>`;
  }

  // ─── Stats panel ──────────────────────────────────────────────

  function renderStats() {
    if (!statsEl) return;
    const mkt      = state.marketTicker;
    const past     = state.trades.filter(t => t.marketTicker && t.marketTicker !== mkt);

    // Group by market, preserving insertion order (most recent first)
    const markets  = [];
    const byMkt    = {};
    for (const t of past) {
      if (!byMkt[t.marketTicker]) { byMkt[t.marketTicker] = []; markets.push(t.marketTicker); }
      byMkt[t.marketTicker].push(t);
    }

    const rows = [];
    for (const ticker of markets) {
      const ts    = byMkt[ticker];
      const wins  = ts.filter(t => t.result === 'win').length;
      const total = ts.length;
      const sum    = ts.reduce((s, t) => s + feeAdjPct(t.entryPrice, t.exitPrice), 0);
      const wColor = wins / total >= 0.5 ? '#22c55e' : '#ef4444';
      const aColor = sum >= 0 ? '#22c55e' : '#ef4444';
      const label  = ticker.replace(/^KXBTC15M-/, '');
      rows.push(`
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;
                    padding:4px 0;border-bottom:1px solid #1a1a1a;font-size:11px">
          <span style="color:#666">${label}</span>
          <span style="color:${wColor}">${wins}W ${total - wins}L</span>
          <span style="color:${aColor};min-width:48px;text-align:right">${sum >= 0 ? '+' : ''}${sum.toFixed(1)}%</span>
        </div>`);
    }

    if (rows.length === 0) {
      rows.push(`<div style="color:#333;font-size:11px;padding:4px 0">No history yet</div>`);
    }

    const totalSum   = past.length > 0 ? past.reduce((s, t) => s + feeAdjPct(t.entryPrice, t.exitPrice), 0) : null;
    const totalColor = totalSum != null && totalSum >= 0 ? '#22c55e' : '#ef4444';
    const totalRow   = totalSum != null ? `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;
                  padding:4px 0;border-top:1px solid #333;margin-top:2px;font-size:11px">
        <span style="color:#555">total</span>
        <span style="color:${totalColor};min-width:48px;text-align:right">${totalSum >= 0 ? '+' : ''}${totalSum.toFixed(1)}%</span>
      </div>` : '';

    const clearAllBtn = `<button id="__ks_clearall"
      style="background:none;border:1px solid #333;color:#555;font-family:inherit;
             font-size:10px;padding:1px 6px;border-radius:4px;cursor:pointer;letter-spacing:0.5px"
      >clear all</button>`;

    statsEl.innerHTML = `
      <div style="font-size:10px;color:#555;letter-spacing:1.5px;margin-bottom:8px;
                  display:flex;justify-content:space-between;align-items:center">
        <span>▸ HISTORY</span>
        ${clearAllBtn}
      </div>
      <div style="min-width:240px;line-height:1.8">${rows.join('')}${totalRow}</div>`;
  }

  // ─── RSI helper ───────────────────────────────────────────────

  function computeRsi(series, period) {
    if (!series || series.length < period + 1) return null;
    const slice   = series.slice(-(period + 1));
    const changes = slice.slice(1).map((v, i) => v - slice[i]);
    const avgGain = changes.reduce((s, c) => s + Math.max(0, c),  0) / period;
    const avgLoss = changes.reduce((s, c) => s + Math.max(0, -c), 0) / period;
    if (avgLoss === 0) return 100.0;
    return 100.0 - 100.0 / (1.0 + avgGain / avgLoss);
  }

  // ─── Formula evaluator (no eval / new Function) ───────────────
  // Recursive-descent parser for arithmetic + boolean expressions.
  // Arithmetic: + - * /  parentheses  unary minus  number literals
  //             variables from a dict  function calls: abs() max() min() round() floor() ceil()
  // Boolean:    comparisons (< > <= >= == !=)  keywords: and or

  function evalFormula(formula, vars) {
    let i = 0;
    const len = formula.length;

    function ws()   { while (i < len && formula[i] <= ' ') i++; }
    function peek() { return i < len ? formula[i] : ''; }
    function eat()  { return formula[i++]; }

    // Top-level: boolean OR
    function boolOr() {
      let v = boolAnd(); ws();
      while (i + 1 < len) {
        const seg = formula.slice(i, i + 2).toLowerCase();
        const after = formula[i + 2] || '';
        if (seg === 'or' && !/[a-zA-Z0-9_]/.test(after)) {
          i += 2; ws();
          const r = boolAnd();
          v = (v || r) ? 1 : 0;
        } else break;
      }
      return v;
    }

    // Boolean AND
    function boolAnd() {
      let v = comparison(); ws();
      while (i + 2 < len) {
        const seg = formula.slice(i, i + 3).toLowerCase();
        const after = formula[i + 3] || '';
        if (seg === 'and' && !/[a-zA-Z0-9_]/.test(after)) {
          i += 3; ws();
          const r = comparison();
          v = (v && r) ? 1 : 0;
        } else break;
      }
      return v;
    }

    // Comparison: < > <= >= == !=
    function comparison() {
      let v = expr(); ws();
      const c = peek();
      if (c === '<' || c === '>' || c === '=' || c === '!') {
        let op = eat();
        if (peek() === '=') { op += eat(); }
        ws();
        const r = expr();
        if (op === '<')       return v < r  ? 1 : 0;
        if (op === '>')       return v > r  ? 1 : 0;
        if (op === '<=')      return v <= r ? 1 : 0;
        if (op === '>=')      return v >= r ? 1 : 0;
        if (op === '==' || op === '=') return v === r ? 1 : 0;
        if (op === '!=')      return v !== r ? 1 : 0;
      }
      return v;
    }

    function expr() {
      let v = term(); ws();
      for (;;) {
        const c = peek();
        if (c !== '+' && c !== '-') break;
        eat(); ws();
        const r = term();
        v = c === '+' ? v + r : v - r; ws();
      }
      return v;
    }

    function term() {
      let v = factor(); ws();
      for (;;) {
        const c = peek();
        if (c !== '*' && c !== '/') break;
        eat(); ws();
        const r = factor();
        v = c === '*' ? v * r : v / r; ws();
      }
      return v;
    }

    function factor() {
      ws();
      const c = peek();
      if (c === '-') { eat(); return -factor(); }
      if (c === '(') { eat(); const v = boolOr(); ws(); if (peek() === ')') eat(); return v; }
      if ((c >= '0' && c <= '9') || c === '.') return num();
      if (/[a-zA-Z_]/.test(c)) return named();
      throw new Error('Unexpected "' + c + '"');
    }

    function num() {
      let s = '';
      while (i < len && ((formula[i] >= '0' && formula[i] <= '9') || formula[i] === '.')) s += eat();
      return parseFloat(s);
    }

    function named() {
      let name = '';
      while (i < len && /[a-zA-Z0-9_]/.test(formula[i])) name += eat();
      ws();
      if (peek() === '(') {
        eat(); ws();
        const args = [];
        if (peek() !== ')') {
          args.push(boolOr()); ws();
          while (peek() === ',') { eat(); args.push(boolOr()); ws(); }
        }
        if (peek() === ')') eat();
        if (name === 'abs')   return Math.abs(args[0]);
        if (name === 'max')   return Math.max(...args);
        if (name === 'min')   return Math.min(...args);
        if (name === 'round') return Math.round(args[0]);
        if (name === 'floor') return Math.floor(args[0]);
        if (name === 'ceil')  return Math.ceil(args[0]);
        throw new Error('Unknown function: ' + name);
      }
      if (name in vars) return vars[name];
      throw new Error('Unknown variable: ' + name);
    }

    ws();
    return boolOr();
  }

  // ─── Fee helpers ──────────────────────────────────────────────

  function kalshiFee(contracts, priceFrac) {
    if (priceFrac <= 0 || priceFrac >= 1) return 0;
    return Math.ceil(0.07 * contracts * priceFrac * (1 - priceFrac) * 100) / 100;
  }

  function feeAdjPct(entryPriceCents, exitPriceCents) {
    const ep = entryPriceCents / 100;
    const xp = exitPriceCents  / 100;
    const c  = 1.0 / ep;  // contracts for a $1 trade
    // (xp-ep)*c = fractional return; fees are $ on a $1 trade → multiply by 100 for %
    return ((xp - ep) * c - kalshiFee(c, ep) - kalshiFee(c, xp)) * 100;
  }

  // ─── Helpers ──────────────────────────────────────────────────

  function fmt(v, d)  { return v == null ? '—' : v.toFixed(d); }
  function fmtTime(d) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function makeDraggable(el) {
    el.addEventListener('mousedown', e => {
      const rect = el.getBoundingClientRect();
      const ox = e.clientX - rect.left, oy = e.clientY - rect.top;
      const move = mv => {
        el.style.left  = (mv.clientX - ox) + 'px';
        el.style.top   = (mv.clientY - oy) + 'px';
        el.style.right = 'auto';
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      e.preventDefault();
    });
  }

  // ─── Main loop ────────────────────────────────────────────────

  async function tick() {
    if (!scoreEl) return; // stale interval after SPA nav — stop firing
    let btc;
    try { btc = await fetchBtcData(); } catch { return; }
    if (!btc) return;

    const { current, priceAtLookback, series, targetPrice } = btc;
    const secsRemaining  = getSecondsRemaining();
    const { up, down, sell } = getUpDownPrices();
    if (targetPrice != null && secsRemaining != null) state.lastTargetPrice = targetPrice;

    const deltaLong  = targetPrice != null ? current - targetPrice : null;
    const deltaShort = current - priceAtLookback;
    const gap        = up != null && down != null ? up - down : null;

    if (gap != null) state.lastGapSeries = [...state.lastGapSeries, gap].slice(-60);
    const shortLb  = Math.max(1, Math.round(state.shortLookback));
    const gapDelta = state.lastGapSeries.length > shortLb
      ? state.lastGapSeries[state.lastGapSeries.length - 1] - state.lastGapSeries[state.lastGapSeries.length - 1 - shortLb]
      : null;

    const rsi = computeRsi(series, state.rsiPeriod);
    state.rsi = rsi;

    let score = null;
    let formulaError = null;
    if (deltaLong != null && secsRemaining != null && secsRemaining !== 0 && gap != null) {
      const lookback = state.shortLookback;
      try {
        score = evalFormula(state.formula, {
          deltaLong, deltaShort, gapDelta, secsRemaining, lookback, gap,
          tuner: 1, btc: current, up: up ?? 0, down: down ?? 0,
          rsi: rsi ?? 50.0,
        });
        if (!isFinite(score)) score = null;
      } catch(e) {
        score = null;
        formulaError = e.message;
        console.error('[crow] formula error:', e.message, '\nformula:', state.formula);
      }
    }

    const cooledDown = secsRemaining != null && secsRemaining <= (900 - state.cooldown);
    const prevTrade  = state.openTrade;

    const tickData = {
      score,
      lastScore:     state.lastScore,
      secsRemaining,
      deltaLong:     deltaLong  != null ? Math.round(deltaLong  * 100) / 100 : null,
      deltaShort:    Math.round(deltaShort * 100) / 100,
      gapDelta:      gapDelta   != null ? Math.round(gapDelta   * 100) / 100 : null,
      gap:           gap != null ? Math.round(gap * 100) / 100 : null,
      btc:           Math.round(current * 100) / 100,
      targetPrice:   targetPrice != null ? Math.round(targetPrice * 100) / 100 : null,
      up,
      down,
    };

    if (score != null) {
      if (cooledDown) checkEntry(score, up, down, secsRemaining, tickData);
      checkExit(score, up, down, tickData);
      if (cooledDown && prevTrade && !state.openTrade) {
        checkEntry(score, up, down, secsRemaining, tickData);
      }
    }

    // Live order execution — fire after all signal logic settles
    if (state.liveTrading && score != null && cooledDown) {
      const tradeJustOpened = !prevTrade && state.openTrade;
      const tradeJustClosed = prevTrade && !state.openTrade;
      const flipped         = !!(prevTrade && state.openTrade && state.openTrade !== prevTrade);
      const closedTrade     = (tradeJustClosed || flipped) ? state.trades[0] : null;

      const doBuy = trade => {
        placeOrder(trade.side, trade.entryPrice).then(res => {
          if (!res || parseFloat(res.fillCountFp) === 0) {
            if (trade === state.openTrade) state.openTrade = null;
            const idx = state.trades.findIndex(t => t.entryTime === trade.entryTime && t.side === trade.side);
            if (idx !== -1) { state.trades.splice(idx, 1); saveTrades(); }
            state.lastScore = 0;
            postEvent('order_failed', { action: 'buy', side: trade.side, entryPrice: trade.entryPrice });
            renderTrades();
            renderStats();
            return;
          }
          if (trade === state.openTrade) trade.contractCount = res.fillCountFp;
          postEvent('order_filled', { action: 'buy', side: trade.side, entryPrice: trade.entryPrice, fillCount: res.fillCountFp });
        });
      };

      if ((tradeJustClosed || flipped) && closedTrade) {
        const newTrade = flipped ? state.openTrade : null;
        sellPosition(closedTrade.side)
          .then(filled => {
            if (!filled && state.trades[0] === closedTrade) {
              state.openTrade = {
                side:          closedTrade.side,
                entryPrice:    closedTrade.entryPrice,
                entryTime:     closedTrade.entryTime,
                contractCount: closedTrade.contractCount,
              };
              state.trades.shift();
              saveTrades();
              postEvent('order_failed', { action: 'sell', side: closedTrade.side });
              renderTrades();
              renderStats();
              return;
            }
            state.lastScore = 0;
            lastSellTime = Date.now();
            postEvent('order_filled', { action: 'sell', side: closedTrade.side });
            if (newTrade && newTrade === state.openTrade) doBuy(newTrade);
          });
      } else if (tradeJustOpened) {
        doBuy(state.openTrade);
      }
    }

    const justClosed = prevTrade && !state.openTrade;

    checkMarketEnd(secsRemaining);
    if (state.navigating) tryGoToLive();

    // Diagnostic: why is entry blocked this tick?
    let entryBlock = null;
    if (!state.openTrade) {
      if (score == null) {
        if (gap == null)                entryBlock = 'no gap';
        else if (targetPrice == null)   entryBlock = 'no target';
        else if (secsRemaining == null) entryBlock = 'no secs';
      } else if (!secsRemaining) {
        entryBlock = 'no secs';
      } else if (state.lastScore == null) {
        entryBlock = 'last=null';
      } else {
        const inRange = px => px != null && px >= state.minEntry && px <= state.maxEntry;
        const t = state.threshold;
        const upCross   = state.lastScore <= t  && score > t;
        const downCross = state.lastScore >= -t && score < -t;
        if (!upCross && !downCross) {
          entryBlock = `${state.lastScore.toFixed(2)}→${score.toFixed(2)}`;
        } else if (upCross && !inRange(up)) {
          entryBlock = `up ${up?.toFixed(1) ?? '?'}¢ !range`;
        } else if (downCross && !inRange(down)) {
          entryBlock = `dn ${down?.toFixed(1) ?? '?'}¢ !range`;
        } else if (state.regimeFilter) {
          try {
            const ok = evalFormula(state.regimeFilter, {
              deltaLong: deltaLong ?? 0, deltaShort, gapDelta: gapDelta ?? 0,
              secsRemaining: secsRemaining ?? 1, lookback: state.shortLookback,
              gap: gap ?? 0, tuner: 1, btc: current, up: up ?? 0, down: down ?? 0,
              rsi: rsi ?? 50.0,
            });
            if (!ok) entryBlock = `regime rsi=${rsi != null ? rsi.toFixed(0) : '?'}`;
          } catch {}
        }
      }
    }

    // Null-score ticks (missing gap/target/secs) must not corrupt lastScore.
    // Treat the first valid score after null as 0 so the next tick detects crossing.
    if (score != null) {
      state.lastScore = (justClosed || state.lastScore == null) ? 0 : score;
    }
    state.lastSecsRemaining = secsRemaining;
    state.lastBtcSeries     = series;

    const cooldownSecs = (secsRemaining != null && !cooledDown)
      ? Math.ceil(secsRemaining - (900 - state.cooldown))
      : null;
    const exitCooldownMs   = lastSellTime != null ? state.exitCooldown * 1000 - (Date.now() - lastSellTime) : null;
    const exitCooldownSecs = exitCooldownMs != null && exitCooldownMs > 0 ? Math.ceil(exitCooldownMs / 1000) : null;
    renderScore(score, { deltaLong, deltaShort, gapDelta, secsRemaining, gap, entryBlock, formulaError, current, targetPrice, cooldownSecs, exitCooldownSecs, rsi });
    renderTrades();
    renderStats();

    postBackend('/tick', {
      marketTicker:    state.marketTicker,
      score,
      lastScore:       state.lastScore,
      secsRemaining,
      deltaLong:       deltaLong  != null ? Math.round(deltaLong  * 100) / 100 : null,
      deltaShort:      Math.round(deltaShort * 100) / 100,
      gapDelta:        gapDelta   != null ? Math.round(gapDelta   * 100) / 100 : null,
      gap:             gap        != null ? Math.round(gap        * 100) / 100 : null,
      btc:             Math.round(current * 100) / 100,
      priceAtLookback: Math.round(priceAtLookback * 100) / 100,
      series,
      gapSeries:       state.lastGapSeries,
      targetPrice:     targetPrice != null ? Math.round(targetPrice * 100) / 100 : null,
      up,
      down,
      sell,
      hasOpenTrade:    !!state.openTrade,
    });
  }

  // ─── Boot ─────────────────────────────────────────────────────

  function boot() {
    if (!location.pathname.toLowerCase().includes('kxbtc15m')) return;
    if (document.getElementById('__ks_score')) return;


    // Set market ticker, fetch market UUID + floor strike
    state.floorStrike = null;
    state.marketId    = null;
    const _et = getEventTickerFromUrl();
    state.marketTicker = _et ? _et + '-15' : null;
    if (_et) fetchMarketData(_et).then(m => {
      if (!m) return;
      const fs = parseFloat(m.floor_strike);
      if (!isNaN(fs)) state.floorStrike = fs;
      if (m.id) state.marketId = m.id;
    }).catch(() => {});

    // Read user ID from localStorage key "userId" → {"value": "<uuid>", "expiry": ...}
    if (!state.userId) {
      try {
        const id = JSON.parse(localStorage.getItem('userId') ?? 'null')?.value;
        if (id) state.userId = id;
      } catch {}
    }

    scoreEl  = makePanel('__ks_score',  { top: 80,  right: 20 });
    tradesEl = makePanel('__ks_trades', { top: 80,  left:  20 });
    statsEl  = makePanel('__ks_stats',  { top: 80,  left:  340 });

    fetchActiveConfig();
    configPollInterval = setInterval(fetchActiveConfig, 5000);

    renderScore(null);
    renderTrades();
    renderStats();

    // Clear button — clears only current market trades
    tradesEl.addEventListener('mousedown', e => { if (e.target.id === '__ks_clear') e.stopPropagation(); });
    tradesEl.addEventListener('click', e => {
      if (e.target.id === '__ks_clear') {
        const mkt = state.marketTicker;
        state.trades = state.trades.filter(t => t.marketTicker !== mkt);
        state.openTrade = null;
        saveTrades();
        renderTrades();
        renderStats();
      }
    });

    // Clear all button — clears entire trade history
    statsEl.addEventListener('mousedown', e => { if (e.target.id === '__ks_clearall') e.stopPropagation(); });
    statsEl.addEventListener('click', e => {
      if (e.target.id === '__ks_clearall') {
        state.trades = [];
        state.openTrade = null;
        saveTrades();
        renderTrades();
        renderStats();
      }
    });

    // Poll for the order UI input before enabling the bot.
    // If it doesn't appear within 10 seconds, reload the page.
    let uiPollCount = 0;
    uiReadyPoll = setInterval(() => {
      const inp = document.querySelector('input[inputmode="decimal"][data-lpignore="true"]');
      let inputReady = false;
      if (inp) {
        try {
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(inp, '');
          inp.dispatchEvent(new Event('input', { bubbles: true }));
          inputReady = true;
        } catch (e) { inputReady = false; }
      }
      const pricesReady = getUpDownPrices().up != null;
      if (inputReady && pricesReady) {
        clearInterval(uiReadyPoll);
        uiReadyPoll = null;
        uiReadyTime = true;
        tick();
        tickInterval = setInterval(tick, POLL_MS);
      } else if (++uiPollCount >= 10) {
        clearInterval(uiReadyPoll);
        uiReadyPoll = null;
        const liveLink = [...document.querySelectorAll('a')]
          .find(a => a.textContent.trim() === 'Go to live');
        if (liveLink) location.href = liveLink.href;
        else location.reload();
      }
    }, 1000);
  }

  // ─── SPA navigation ───────────────────────────────────────────

  let lastPath = location.pathname;
  new MutationObserver(() => {
    if (location.pathname === lastPath) return;
    lastPath = location.pathname;
    clearInterval(tickInterval);
    clearInterval(uiReadyPoll);
    clearInterval(configPollInterval);
    tickInterval       = null;
    uiReadyPoll        = null;
    uiReadyTime        = null;
    lastSellTime       = null;
    configPollInterval = null;
    ['__ks_score', '__ks_trades', '__ks_stats'].forEach(id => document.getElementById(id)?.remove());
    scoreEl = tradesEl = statsEl = null;
    state.lastScore         = null;
    state.lastTargetPrice   = null;
    state.floorStrike       = null;
    state.marketTicker      = null;
    state.marketId          = null;
    state.lastSecsRemaining = null;
    state.lastBtcSeries     = null;
    state.openTrade         = null;
    state.navigating        = false;
    // trades intentionally preserved
    setTimeout(boot, 600);
  }).observe(document, { subtree: true, childList: true });

  boot();
})();
