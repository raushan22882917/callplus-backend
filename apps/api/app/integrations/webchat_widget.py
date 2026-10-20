"""The embeddable live-chat widget, served as /api/v1/channels/webchat/widget.js.

Self-contained vanilla JS (no dependencies). It reads its public key from the
embedding <script data-key="..."> tag and talks to the API on the origin the
script itself was loaded from. Requests use Content-Type text/plain so
browsers send them as "simple" cross-origin requests with no CORS preflight
(the API parses the body as JSON regardless of content type).

Optional, switched on per business in the channel settings:
  * a short "tell us about you" form before the chat starts (lead capture)
  * a "Call me" button, and a call card offered when the visitor asks for a call,
    which needs the visitor to tick a consent box before the AI agent phones them.
Everything the visitor or the server sends is inserted with textContent, never
as HTML.
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
  var cfg = { lead_form: false, require_phone: false, call_me: false, consent_text: '' };
  var state = { token: null, name: '', phone: '', lastAt: null, open: false, seen: {}, timer: null, busy: false, called: false };
  try {
    var saved = JSON.parse(localStorage.getItem(storeKey) || 'null');
    if (saved && saved.token) { state.token = saved.token; state.name = saved.name || ''; state.phone = saved.phone || ''; state.called = !!saved.called; }
  } catch (e) {}

  function save() { try { localStorage.setItem(storeKey, JSON.stringify({ token: state.token, name: state.name, phone: state.phone, called: state.called })); } catch (e) {} }
  function request(method, url, body) {
    return fetch(url, {
      method: method,
      headers: body ? { 'Content-Type': 'text/plain' } : undefined,
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(typeof j.detail === 'string' ? j.detail : 'Something went wrong. Please try again.'); e.status = r.status; throw e; }
        return j;
      });
    });
  }
  function el(tag, css, text) {
    var n = document.createElement(tag); if (css) n.style.cssText = css; if (text) n.textContent = text; return n;
  }
  var FIELD = 'width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:8px;padding:9px 10px;font-size:14px;outline:none;color:#111;background:#fff;font-family:inherit;';
  function field(labelText, input, hint) {
    var wrap = el('label', 'display:block;margin-bottom:10px;font-size:12px;color:#374151;font-weight:600;');
    wrap.appendChild(document.createTextNode(labelText));
    input.style.cssText = FIELD + 'margin-top:4px;font-weight:400;';
    wrap.appendChild(input);
    if (hint) wrap.appendChild(el('span', 'display:block;margin-top:3px;font-weight:400;color:#6b7280;', hint));
    return wrap;
  }

  var color = '#2563eb';
  var root = el('div', 'position:fixed;right:20px;bottom:20px;z-index:2147483000;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;');
  var panel = el('div', 'display:none;flex-direction:column;width:340px;max-width:calc(100vw - 32px);height:460px;max-height:calc(100vh - 100px);background:#fff;color:#111;border:1px solid #e5e7eb;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.18);overflow:hidden;margin-bottom:12px;');
  panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Chat');
  var head = el('div', 'padding:14px 16px;color:#fff;font-weight:600;display:flex;justify-content:space-between;align-items:center;gap:8px;');
  var title = el('span', 'flex:1;', 'Chat with us');
  var callBtn = el('button', 'display:none;background:rgba(255,255,255,.2);border:0;color:#fff;font-size:13px;cursor:pointer;border-radius:8px;padding:4px 10px;font-weight:600;', 'Call me');
  callBtn.type = 'button'; callBtn.setAttribute('aria-label', 'Request a call');
  var close = el('button', 'background:none;border:0;color:#fff;font-size:20px;cursor:pointer;line-height:1;', '×');
  close.type = 'button'; close.setAttribute('aria-label', 'Close chat');
  head.appendChild(title); head.appendChild(callBtn); head.appendChild(close);
  var list = el('div', 'flex:1;overflow-y:auto;padding:12px;background:#f9fafb;');
  list.setAttribute('role', 'log'); list.setAttribute('aria-live', 'polite');
  var status = el('div', 'padding:0 12px 6px;color:#b91c1c;font-size:12px;background:#f9fafb;min-height:16px;');
  status.setAttribute('role', 'alert');
  var form = el('form', 'display:flex;gap:8px;padding:10px;border-top:1px solid #e5e7eb;background:#fff;');
  var input = el('input', 'flex:1;border:1px solid #d1d5db;border-radius:8px;padding:9px 10px;font-size:14px;outline:none;color:#111;background:#fff;');
  input.setAttribute('placeholder', 'Type your message'); input.setAttribute('maxlength', '2000'); input.setAttribute('aria-label', 'Message');
  var send = el('button', 'border:0;border-radius:8px;padding:0 14px;color:#fff;font-weight:600;cursor:pointer;', 'Send');
  send.type = 'submit';
  form.appendChild(input); form.appendChild(send);

  // Before the chat: who are you?
  var leadView = el('form', 'display:none;flex:1;overflow-y:auto;padding:14px;background:#fff;');
  leadView.setAttribute('novalidate', 'novalidate');
  var leadName = el('input'); leadName.type = 'text'; leadName.maxLength = 120; leadName.autocomplete = 'name'; leadName.required = true;
  var leadPhone = el('input'); leadPhone.type = 'tel'; leadPhone.maxLength = 30; leadPhone.autocomplete = 'tel';
  var leadEmail = el('input'); leadEmail.type = 'email'; leadEmail.maxLength = 254; leadEmail.autocomplete = 'email';
  var leadInterest = el('textarea'); leadInterest.rows = 2; leadInterest.maxLength = 500;
  var trap = el('input', 'position:absolute;left:-9999px;width:1px;height:1px;opacity:0;');
  trap.type = 'text'; trap.name = 'website'; trap.tabIndex = -1; trap.setAttribute('autocomplete', 'off'); trap.setAttribute('aria-hidden', 'true');
  var leadError = el('div', 'color:#b91c1c;font-size:12px;min-height:16px;margin-bottom:6px;');
  leadError.setAttribute('role', 'alert');
  var leadSubmit = el('button', 'width:100%;border:0;border-radius:8px;padding:10px;color:#fff;font-weight:600;cursor:pointer;font-size:14px;', 'Start chat');
  leadSubmit.type = 'submit';
  var leadIntro = el('p', 'margin:0 0 12px;color:#374151;', 'Tell us a little about you so we can help.');
  var leadPhoneField, leadEmailField;
  leadView.appendChild(leadIntro);
  leadView.appendChild(field('Your name', leadName));
  leadPhoneField = field('Phone number', leadPhone, 'Include your country code, like +91');
  leadView.appendChild(leadPhoneField);
  leadEmailField = field('Email (optional)', leadEmail);
  leadView.appendChild(leadEmailField);
  leadView.appendChild(field('How can we help? (optional)', leadInterest));
  leadView.appendChild(trap); leadView.appendChild(leadError); leadView.appendChild(leadSubmit);

  // Ask for a call.
  var callView = el('form', 'display:none;flex:1;overflow-y:auto;padding:14px;background:#fff;');
  callView.setAttribute('novalidate', 'novalidate');
  var callName = el('input'); callName.type = 'text'; callName.maxLength = 120; callName.autocomplete = 'name';
  var callPhone = el('input'); callPhone.type = 'tel'; callPhone.maxLength = 30; callPhone.autocomplete = 'tel'; callPhone.required = true;
  var callNote = el('textarea'); callNote.rows = 2; callNote.maxLength = 300;
  var consent = el('input', 'margin-top:3px;'); consent.type = 'checkbox'; consent.id = 'autonxt-consent-' + key;
  var consentLabel = el('label', 'display:flex;gap:8px;font-size:12px;color:#374151;line-height:1.4;margin-bottom:10px;');
  consentLabel.setAttribute('for', consent.id);
  var consentText = el('span', '');
  consentLabel.appendChild(consent); consentLabel.appendChild(consentText);
  var callError = el('div', 'color:#b91c1c;font-size:12px;min-height:16px;margin-bottom:6px;');
  callError.setAttribute('role', 'alert');
  var callSubmit = el('button', 'width:100%;border:0;border-radius:8px;padding:10px;color:#fff;font-weight:600;cursor:pointer;font-size:14px;margin-bottom:8px;', 'Call me now');
  callSubmit.type = 'submit';
  var callCancel = el('button', 'width:100%;border:1px solid #d1d5db;border-radius:8px;padding:9px;background:#fff;color:#374151;cursor:pointer;font-size:14px;', 'Back to chat');
  callCancel.type = 'button';
  var callNameField = field('Your name', callName);
  callView.appendChild(el('p', 'margin:0 0 12px;color:#374151;', 'Our AI assistant can call you right now.'));
  callView.appendChild(callNameField);
  callView.appendChild(field('Phone number', callPhone, 'Include your country code, like +91'));
  callView.appendChild(field('Anything we should know? (optional)', callNote));
  callView.appendChild(consentLabel); callView.appendChild(callError); callView.appendChild(callSubmit); callView.appendChild(callCancel);

  panel.appendChild(head); panel.appendChild(list); panel.appendChild(leadView); panel.appendChild(callView); panel.appendChild(status); panel.appendChild(form);
  var bubble = el('button', 'width:56px;height:56px;border-radius:50%;border:0;color:#fff;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25);font-size:24px;float:right;', '💬');
  bubble.type = 'button'; bubble.setAttribute('aria-label', 'Open chat');
  root.appendChild(panel); root.appendChild(bubble); document.body.appendChild(root);

  function paint() { head.style.background = color; send.style.background = color; bubble.style.background = color; leadSubmit.style.background = color; callSubmit.style.background = color; }
  paint();

  // Exactly one of: chat, the details form, the call card.
  function show(view) {
    list.style.display = view === 'chat' ? 'block' : 'none';
    status.style.display = view === 'chat' ? 'block' : 'none';
    form.style.display = view === 'chat' ? 'flex' : 'none';
    leadView.style.display = view === 'lead' ? 'block' : 'none';
    callView.style.display = view === 'call' ? 'block' : 'none';
    callBtn.style.display = cfg.call_me && view === 'chat' && state.token ? 'inline-block' : 'none';
  }

  function addMessage(m) {
    if (state.seen[m.id]) return; state.seen[m.id] = true;
    var mine = m.direction === 'inbound';
    var row = el('div', 'display:flex;margin:6px 0;justify-content:' + (mine ? 'flex-end' : 'flex-start') + ';');
    var b = el('div', 'max-width:80%;padding:8px 12px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word;' +
      (mine ? 'background:' + color + ';color:#fff;' : 'background:#fff;border:1px solid #e5e7eb;color:#111;'), m.text || '');
    row.appendChild(b); list.appendChild(row); list.scrollTop = list.scrollHeight;
    if (!state.lastAt || m.created_at > state.lastAt) state.lastAt = m.created_at;
  }
  function addNote(text) {
    var row = el('div', 'margin:8px 0;text-align:center;color:#6b7280;font-size:12px;', text);
    list.appendChild(row); list.scrollTop = list.scrollHeight;
  }
  function offerCall() {
    if (state.called || !cfg.call_me) return;
    var card = el('div', 'margin:8px 0;padding:10px 12px;border:1px solid #e5e7eb;border-radius:12px;background:#fff;');
    card.appendChild(el('div', 'margin-bottom:8px;color:#111;', 'Would you like us to call you?'));
    var go = el('button', 'border:0;border-radius:8px;padding:7px 14px;color:#fff;font-weight:600;cursor:pointer;background:' + color + ';', 'Call me');
    go.type = 'button';
    go.addEventListener('click', function () { openCall(); });
    card.appendChild(go); list.appendChild(card); list.scrollTop = list.scrollHeight;
  }

  function utmParams() {
    var out = {};
    try { new URLSearchParams(location.search).forEach(function (v, k) { if (k.indexOf('utm_') === 0) out[k] = v; }); } catch (e) {}
    return out;
  }
  function startSession(extra) {
    var body = { page_url: location.href.split('#')[0], referrer: document.referrer || '', utm: utmParams() };
    if (extra) for (var k in extra) body[k] = extra[k];
    return request('POST', api + '/session', body).then(function (r) {
      state.token = r.session_token; save();
      (r.messages || []).forEach(addMessage);
    });
  }
  function resetSession() { state.token = null; state.lastAt = null; state.seen = {}; list.textContent = ''; save(); }
  function poll() {
    if (!state.token || !state.open) return;
    var url = api + '/messages?session_token=' + encodeURIComponent(state.token) + (state.lastAt ? '&after=' + encodeURIComponent(state.lastAt) : '');
    request('GET', url).then(function (r) { (r.messages || []).forEach(addMessage); }).catch(function (e) {
      if (e.status === 404) { resetSession(); begin(); }
    });
  }

  // Decide what the visitor sees first.
  function begin() {
    status.textContent = '';
    if (state.token) {
      return request('GET', api + '/messages?session_token=' + encodeURIComponent(state.token)).then(function (r) {
        show('chat'); (r.messages || []).forEach(addMessage); input.focus();
      }).catch(function (e) { if (e.status === 404) { resetSession(); return begin(); } throw e; });
    }
    if (cfg.lead_form) { show('lead'); leadName.focus(); return Promise.resolve(); }
    return startSession().then(function () { show('chat'); input.focus(); });
  }
  function openPanel() {
    state.open = true; panel.style.display = 'flex'; bubble.style.display = 'none';
    begin().catch(function (e) { status.textContent = e.message; });
    state.timer = setInterval(poll, 4000);
  }
  function closePanel() { state.open = false; panel.style.display = 'none'; bubble.style.display = 'block'; clearInterval(state.timer); }
  bubble.addEventListener('click', openPanel); close.addEventListener('click', closePanel);

  leadView.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (state.busy) return;
    var name = leadName.value.trim(), phone = leadPhone.value.trim(), email = leadEmail.value.trim();
    leadError.textContent = '';
    if (!name) { leadError.textContent = 'Please tell us your name.'; leadName.focus(); return; }
    if (cfg.require_phone && !phone) { leadError.textContent = 'Please enter your phone number.'; leadPhone.focus(); return; }
    if (!phone && !email) { leadError.textContent = 'Please give us a phone number or an email address.'; leadPhone.focus(); return; }
    state.busy = true; leadSubmit.disabled = true;
    startSession({ name: name, phone: phone, email: email, interest: leadInterest.value.trim(), website: trap.value }).then(function () {
      state.name = name; state.phone = phone; save();
      show('chat'); input.focus();
    }).catch(function (e) { leadError.textContent = e.message; }).then(function () { state.busy = false; leadSubmit.disabled = false; });
  });

  function openCall() {
    callError.textContent = ''; consent.checked = false;
    callName.value = state.name || ''; callPhone.value = state.phone || '';
    callNameField.style.display = state.name ? 'none' : 'block';
    consentText.textContent = cfg.consent_text || 'I agree to be called on this number.';
    show('call'); (state.name ? callPhone : callName).focus();
  }
  callBtn.addEventListener('click', openCall);
  callCancel.addEventListener('click', function () { show('chat'); input.focus(); });
  callView.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (state.busy || !state.token) return;
    var phone = callPhone.value.trim(), name = (state.name || callName.value).trim();
    callError.textContent = '';
    if (!name) { callError.textContent = 'Please tell us your name.'; callName.focus(); return; }
    if (!phone) { callError.textContent = 'Please enter your phone number.'; callPhone.focus(); return; }
    if (!consent.checked) { callError.textContent = 'Please tick the box to agree to a call.'; consent.focus(); return; }
    state.busy = true; callSubmit.disabled = true;
    request('POST', api + '/call-request', { session_token: state.token, name: name, phone: phone, consent: true, note: callNote.value.trim(), page_url: location.href.split('#')[0] }).then(function (r) {
      state.name = name; state.phone = phone; state.called = true; save();
      show('chat'); addNote(r.message || 'Thanks! We will call you shortly.');
    }).catch(function (e) {
      if (e.status === 404) { resetSession(); begin(); return; }
      callError.textContent = e.message;
    }).then(function () { state.busy = false; callSubmit.disabled = false; });
  });

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var text = input.value.trim();
    if (!text || state.busy || !state.token) return;
    state.busy = true; status.textContent = ''; input.value = '';
    request('POST', api + '/message', { session_token: state.token, text: text }).then(function (r) {
      (r.messages || []).forEach(addMessage);
      if (r.call_offer) offerCall();
    }).catch(function (e) { status.textContent = e.message; input.value = text; }).then(function () { state.busy = false; });
  });

  request('GET', api + '/config').then(function (c) {
    if (c.color && /^#[0-9a-fA-F]{3,8}$/.test(c.color)) color = c.color;
    if (c.title) title.textContent = c.title;
    cfg.lead_form = !!c.lead_form; cfg.require_phone = !!c.require_phone; cfg.call_me = !!c.call_me; cfg.consent_text = c.consent_text || '';
    leadPhoneField.firstChild.textContent = cfg.require_phone ? 'Phone number' : 'Phone number (or email below)';
    paint();
  }).catch(function () { root.style.display = 'none'; });
})();
"""
