"""The embeddable live-chat widget, served as /api/v1/channels/webchat/widget.js.

Self-contained vanilla JS (no dependencies). It reads its public key from the
embedding <script data-key="..."> tag and talks to the API on the origin the
script itself was loaded from. Requests use Content-Type text/plain so
browsers send them as "simple" cross-origin requests with no CORS preflight
(the API parses the body as JSON regardless of content type).
"""

WIDGET_JS = r"""(function () {
  'use strict';
  var script = document.currentScript || (function () {
    var all = document.getElementsByTagName('script');
    for (var i = all.length - 1; i >= 0; i--) { if (all[i].getAttribute('data-key')) return all[i]; }
    return null;
  })();
  if (!script) return;
  var key = script.getAttribute('data-key');
  if (!key || window.__autonxtChatLoaded === key) return;
  window.__autonxtChatLoaded = key;
  var origin = new URL(script.src, location.href).origin;
  var api = origin + '/api/v1/channels/webchat/' + encodeURIComponent(key);
  var storeKey = 'autonxt_chat_' + key;
  var state = { token: null, lastAt: null, open: false, seen: {}, timer: null, busy: false };
  try { var saved = JSON.parse(localStorage.getItem(storeKey) || 'null'); if (saved && saved.token) state.token = saved.token; } catch (e) {}

  function save() { try { localStorage.setItem(storeKey, JSON.stringify({ token: state.token })); } catch (e) {} }
  function request(method, url, body) {
    return fetch(url, {
      method: method,
      headers: body ? { 'Content-Type': 'text/plain' } : undefined,
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.detail || 'Request failed'); e.status = r.status; throw e; }
        return j;
      });
    });
  }
  function el(tag, css, text) {
    var n = document.createElement(tag); if (css) n.style.cssText = css; if (text) n.textContent = text; return n;
  }

  var color = '#2563eb';
  var root = el('div', 'position:fixed;right:20px;bottom:20px;z-index:2147483000;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;');
  var panel = el('div', 'display:none;flex-direction:column;width:340px;max-width:calc(100vw - 32px);height:460px;max-height:calc(100vh - 100px);background:#fff;color:#111;border:1px solid #e5e7eb;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.18);overflow:hidden;margin-bottom:12px;');
  var head = el('div', 'padding:14px 16px;color:#fff;font-weight:600;display:flex;justify-content:space-between;align-items:center;');
  var title = el('span', '', 'Chat with us');
  var close = el('button', 'background:none;border:0;color:#fff;font-size:20px;cursor:pointer;line-height:1;', '×');
  close.setAttribute('aria-label', 'Close chat');
  head.appendChild(title); head.appendChild(close);
  var list = el('div', 'flex:1;overflow-y:auto;padding:12px;background:#f9fafb;');
  list.setAttribute('role', 'log'); list.setAttribute('aria-live', 'polite');
  var status = el('div', 'padding:0 12px 6px;color:#b91c1c;font-size:12px;background:#f9fafb;min-height:16px;');
  var form = el('form', 'display:flex;gap:8px;padding:10px;border-top:1px solid #e5e7eb;background:#fff;');
  var input = el('input', 'flex:1;border:1px solid #d1d5db;border-radius:8px;padding:9px 10px;font-size:14px;outline:none;color:#111;background:#fff;');
  input.setAttribute('placeholder', 'Type your message'); input.setAttribute('maxlength', '2000'); input.setAttribute('aria-label', 'Message');
  var send = el('button', 'border:0;border-radius:8px;padding:0 14px;color:#fff;font-weight:600;cursor:pointer;', 'Send');
  send.type = 'submit';
  form.appendChild(input); form.appendChild(send);
  panel.appendChild(head); panel.appendChild(list); panel.appendChild(status); panel.appendChild(form);
  var bubble = el('button', 'width:56px;height:56px;border-radius:50%;border:0;color:#fff;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25);font-size:24px;float:right;', '💬');
  bubble.setAttribute('aria-label', 'Open chat');
  root.appendChild(panel); root.appendChild(bubble); document.body.appendChild(root);

  function paint() { head.style.background = color; send.style.background = color; bubble.style.background = color; }
  paint();

  function addMessage(m) {
    if (state.seen[m.id]) return; state.seen[m.id] = true;
    var mine = m.direction === 'inbound';
    var row = el('div', 'display:flex;margin:6px 0;justify-content:' + (mine ? 'flex-end' : 'flex-start') + ';');
    var b = el('div', 'max-width:80%;padding:8px 12px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word;' +
      (mine ? 'background:' + color + ';color:#fff;' : 'background:#fff;border:1px solid #e5e7eb;color:#111;'), m.text || '');
    row.appendChild(b); list.appendChild(row); list.scrollTop = list.scrollHeight;
    if (!state.lastAt || m.created_at > state.lastAt) state.lastAt = m.created_at;
  }

  function startSession() {
    return request('POST', api + '/session', {}).then(function (r) {
      state.token = r.session_token; save();
      (r.messages || []).forEach(addMessage);
    });
  }
  function poll() {
    if (!state.token || !state.open) return;
    var url = api + '/messages?session_token=' + encodeURIComponent(state.token) + (state.lastAt ? '&after=' + encodeURIComponent(state.lastAt) : '');
    request('GET', url).then(function (r) { (r.messages || []).forEach(addMessage); }).catch(function (e) {
      if (e.status === 404) { state.token = null; state.lastAt = null; state.seen = {}; list.textContent = ''; save(); }
    });
  }
  function openPanel() {
    state.open = true; panel.style.display = 'flex'; bubble.style.display = 'none'; input.focus();
    var ready = state.token ? request('GET', api + '/messages?session_token=' + encodeURIComponent(state.token)).then(function (r) {
      (r.messages || []).forEach(addMessage);
    }).catch(function (e) { if (e.status === 404) { state.token = null; save(); return startSession(); } throw e; }) : startSession();
    ready.catch(function (e) { status.textContent = e.message; });
    state.timer = setInterval(poll, 4000);
  }
  function closePanel() { state.open = false; panel.style.display = 'none'; bubble.style.display = 'block'; clearInterval(state.timer); }
  bubble.addEventListener('click', openPanel); close.addEventListener('click', closePanel);

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var text = input.value.trim();
    if (!text || state.busy || !state.token) return;
    state.busy = true; status.textContent = ''; input.value = '';
    request('POST', api + '/message', { session_token: state.token, text: text }).then(function (r) {
      (r.messages || []).forEach(addMessage);
    }).catch(function (e) { status.textContent = e.message; input.value = text; }).then(function () { state.busy = false; });
  });

  request('GET', api + '/config').then(function (c) {
    if (c.color && /^#[0-9a-fA-F]{3,8}$/.test(c.color)) color = c.color;
    if (c.title) title.textContent = c.title;
    paint();
  }).catch(function () { root.style.display = 'none'; });
})();
"""
