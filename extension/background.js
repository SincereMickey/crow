chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== 'backend') return false;
  const opts = { method: msg.method || 'GET', headers: msg.headers || {} };
  if (msg.body) opts.body = msg.body;
  fetch(msg.url, opts)
    .then(async r => {
      try { sendResponse({ ok: true, data: await r.json() }); }
      catch { sendResponse({ ok: true, data: {} }); }
    })
    .catch(err => sendResponse({ ok: false, error: String(err) }));
  return true; // keep message channel open for async sendResponse
});
