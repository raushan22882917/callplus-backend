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
as HTML. Styling lives in one <style> block of namespaced `.axw-` classes; the
accent colour comes from the channel settings. A small "Powered by autobot360.com"
tag links back to the product.
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
  var cfg = { lead_form: false, require_phone: false, call_me: false, consent_text: '', fields: [], quick_replies: [], privacy: null };
  var state = { privacy: false, sentAny: false, token: null, name: '', phone: '', lastAt: null, open: false, seen: {}, timer: null, busy: false, called: false };
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
  function el(tag, cls, text) {
    var n = document.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n;
  }
  var NS = 'http://www.w3.org/2000/svg';
  function icon(d, size, stroke) {
    var s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size || 18); s.setAttribute('height', size || 18);
    s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', stroke || 2);
    s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round'); s.setAttribute('aria-hidden', 'true');
    var p = document.createElementNS(NS, 'path'); p.setAttribute('d', d); s.appendChild(p); return s;
  }
  var I = {
    send: 'M5 12h14M13 6l6 6-6 6',
    close: 'M6 6l12 12M18 6L6 18',
    chat: 'M21 12c0 4.400-4 8-9 8-1.300 0-2.600-.2-3.700-.7L3 21l1.600-4.200C3.600 15.500 3 13.800 3 12c0-4.400 4-8 9-8s9 3.600 9 8z',
    phone: 'M5 4h4l2 5-2.500 1.500a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z',
    lock: 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v9H5z'
  };

  var CSS = [
    '.axw{position:fixed;right:20px;bottom:20px;z-index:2147483000;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#0f172a;--ac:#2563eb;-webkit-font-smoothing:antialiased}',
    '.axw *{box-sizing:border-box;font-family:inherit}',
    '.axw-panel{display:none;flex-direction:column;width:380px;max-width:calc(100vw - 24px);height:min(640px,calc(100vh - 110px));background:#fff;border-radius:20px;overflow:hidden;margin-bottom:14px;box-shadow:0 28px 64px -16px rgba(15,23,42,.38),0 0 0 1px rgba(15,23,42,.07);animation:axw-in .22s ease-out}',
    '@keyframes axw-in{from{opacity:0;transform:translateY(10px) scale(.97)}to{opacity:1;transform:none}}',
    '.axw-head{display:flex;align-items:center;gap:12px;padding:16px 14px 16px 18px;color:#fff;background:radial-gradient(130% 160% at 0% 0%,rgba(255,255,255,.24),rgba(255,255,255,0) 55%),var(--ac)}',
    '.axw-av{display:grid;place-items:center;flex:none;width:42px;height:42px;border-radius:50%;background:rgba(255,255,255,.2);box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.35);font-weight:700;font-size:16px}',
    '.axw-ht{flex:1;min-width:0}',
    '.axw-title{font-weight:650;font-size:15.5px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.axw-sub{display:flex;align-items:center;gap:6px;margin-top:3px;font-size:12px;opacity:.92}',
    '.axw-dot{width:7px;height:7px;border-radius:50%;background:#4ade80;box-shadow:0 0 0 3px rgba(74,222,128,.28)}',
    '.axw-hb{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:0;color:#fff;background:rgba(255,255,255,.18);cursor:pointer;border-radius:999px;height:32px;padding:0 12px;font-size:12.5px;font-weight:600;transition:background .15s}',
    '.axw-hb:hover{background:rgba(255,255,255,.3)}',
    '.axw-x{width:32px;padding:0}',
    '.axw-list{flex:1;overflow-y:auto;padding:16px 14px 6px;background:#f6f8fb}',
    '.axw-row{display:flex;margin:0 0 10px}',
    '.axw-row.me{justify-content:flex-end}',
    '.axw-msg{max-width:82%}',
    '.axw-b{padding:9px 13px;border-radius:16px;white-space:pre-wrap;word-wrap:break-word;background:#fff;color:#0f172a;border:1px solid #e6eaf0;border-bottom-left-radius:5px;box-shadow:0 1px 2px rgba(15,23,42,.04)}',
    '.me .axw-b{background:var(--ac);color:#fff;border-color:transparent;border-radius:16px;border-bottom-right-radius:5px}',
    '.axw-t{margin:3px 4px 0;font-size:11px;color:#94a3b8}',
    '.me .axw-t{text-align:right}',
    '.axw-note{margin:10px 0;text-align:center;color:#64748b;font-size:12.5px}',
    '.axw-card{margin:6px 0 12px;padding:12px 14px;border:1px solid #e6eaf0;border-radius:14px;background:#fff;box-shadow:0 1px 2px rgba(15,23,42,.04)}',
    '.axw-card p{margin:0 0 9px;color:#0f172a}',
    '.axw-typing{display:inline-flex;gap:4px;padding:12px 14px}',
    '.axw-typing i{width:6px;height:6px;border-radius:50%;background:#94a3b8;animation:axw-b 1.1s infinite ease-in-out}',
    '.axw-typing i:nth-child(2){animation-delay:.15s}.axw-typing i:nth-child(3){animation-delay:.3s}',
    '@keyframes axw-b{0%,60%,100%{transform:translateY(0);opacity:.5}30%{transform:translateY(-4px);opacity:1}}',
    '.axw-err{min-height:18px;padding:0 16px 4px;color:#b91c1c;font-size:12.5px;background:#f6f8fb}',
    '.axw-form{display:flex;align-items:center;gap:8px;padding:10px 12px;border-top:1px solid #eef1f5;background:#fff}',
    '.axw-in{flex:1;min-width:0;height:44px;border:1px solid #e2e8f0;border-radius:999px;padding:0 16px;font-size:14px;color:#0f172a;background:#f8fafc;outline:none;transition:border-color .15s,box-shadow .15s,background .15s}',
    '.axw-in:focus,.axw-fi:focus{border-color:var(--ac);background:#fff;box-shadow:0 0 0 3px color-mix(in srgb,var(--ac) 18%,transparent)}',
    '.axw-send{display:grid;place-items:center;flex:none;width:44px;height:44px;border:0;border-radius:50%;color:#fff;background:var(--ac);cursor:pointer;transition:transform .15s,filter .15s}',
    '.axw-send:hover{filter:brightness(1.08);transform:translateY(-1px)}',
    '.axw-send:disabled{opacity:.55;cursor:default;transform:none}',
    '.axw-view{display:none;flex:1;overflow-y:auto;padding:22px 20px 14px;background:#fff}',
    '.axw-h{margin:0 0 6px;font-size:20px;font-weight:700;letter-spacing:-.02em;line-height:1.2;color:#0f172a}',
    '.axw-p{margin:0 0 18px;color:#64748b;font-size:13.5px}',
    '.axw-f{display:block;margin:0 0 14px;font-size:12.5px;font-weight:600;color:#334155}',
    '.axw-fi{display:block;width:100%;margin-top:6px;height:44px;border:1px solid #dbe1ea;border-radius:12px;padding:0 13px;font-size:14px;font-weight:400;color:#0f172a;background:#fff;outline:none;transition:border-color .15s,box-shadow .15s}',
    'textarea.axw-fi{height:auto;padding:11px 13px;resize:none;line-height:1.4}',
    '.axw-hint{display:block;margin-top:5px;font-size:12px;font-weight:400;color:#94a3b8}',
    '.axw-cta{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;height:46px;border:0;border-radius:12px;color:#fff;background:var(--ac);font-size:14.5px;font-weight:650;cursor:pointer;transition:filter .15s,transform .15s}',
    '.axw-cta:hover{filter:brightness(1.08);transform:translateY(-1px)}',
    '.axw-cta:disabled{opacity:.6;cursor:default;transform:none}',
    '.axw-alt{display:block;width:100%;height:42px;margin-top:8px;border:1px solid #dbe1ea;border-radius:12px;background:#fff;color:#334155;font-size:14px;font-weight:600;cursor:pointer}',
    '.axw-alt:hover{background:#f8fafc}',
    '.axw-src{margin-top:4px;font-size:11.5px;color:#94a3b8}',
    '.axw-pn p{margin:0 0 8px;font-size:12.5px;line-height:1.5;color:#475569}',
    '.axw-pl{display:block;margin:0 0 14px;font-size:12.5px;color:var(--ac)}',
    '.axw-trust{display:flex;align-items:center;justify-content:center;gap:6px;margin-top:12px;font-size:12px;color:#94a3b8}',
    '.axw-consent{display:flex;gap:9px;margin:0 0 14px;font-size:12.5px;line-height:1.45;color:#475569}',
    '.axw-consent input{margin-top:2px;accent-color:var(--ac)}',
    '.axw-ferr{min-height:18px;margin:0 0 8px;color:#b91c1c;font-size:12.5px}',
    '.axw-foot{padding:8px 12px 10px;text-align:center;font-size:11.5px;color:#94a3b8;background:#fff;border-top:1px solid #f1f4f8}',
    '.axw-foot a{color:#64748b;font-weight:600;text-decoration:none}',
    '.axw-foot a:hover{color:var(--ac)}',
    '.axw-launch{display:grid;place-items:center;float:right;width:60px;height:60px;border:0;border-radius:50%;color:#fff;background:var(--ac);cursor:pointer;box-shadow:0 12px 28px -6px color-mix(in srgb,var(--ac) 70%,#000),0 0 0 1px rgba(255,255,255,.2) inset;transition:transform .18s}',
    '.axw-launch:hover{transform:scale(1.07)}',
    '.axw-trap{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}',
    '.axw-chips{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 12px}',
    '.axw-chip{border:1px solid color-mix(in srgb,var(--ac) 35%,#fff);background:#fff;color:var(--ac);border-radius:999px;padding:8px 14px;font-size:13px;font-weight:600;line-height:1.2;text-align:left;cursor:pointer;transition:background .15s,transform .15s}',
    '.axw-chip:hover{background:color-mix(in srgb,var(--ac) 9%,#fff);transform:translateY(-1px)}',
    'select.axw-fi{appearance:auto;background:#fff}',
    '.axw-left{right:auto;left:20px}.axw-left .axw-launch{float:left}.axw-left .axw-panel{transform-origin:bottom left}',
    '.axw-launch.axw-lab{display:flex;align-items:center;gap:10px;width:auto;height:56px;padding:0 22px 0 18px;border-radius:999px;font-size:14.5px;font-weight:650}',
    '.axw-t-minimal .axw-panel{border-radius:10px}',
    '.axw-t-minimal .axw-head{color:#0f172a;background:#fff;border-bottom:1px solid #e8ecf1;box-shadow:inset 0 3px 0 var(--ac)}',
    '.axw-t-minimal .axw-av{background:var(--ac);color:#fff;box-shadow:none;border-radius:10px}',
    '.axw-t-minimal .axw-hb{color:#0f172a;background:#f1f5f9}.axw-t-minimal .axw-hb:hover{background:#e2e8f0}',
    '.axw-t-minimal .axw-b,.axw-t-minimal .me .axw-b{border-radius:6px}',
    '.axw-t-minimal .axw-in,.axw-t-minimal .axw-send,.axw-t-minimal .axw-launch{border-radius:8px}',
    '.axw-t-minimal .axw-cta,.axw-t-minimal .axw-fi,.axw-t-minimal .axw-alt{border-radius:8px}',
    '.axw-t-bold .axw-panel{border-radius:26px}',
    '.axw-t-bold .axw-head{background:#0f172a;padding:20px 16px 20px 20px}',
    '.axw-t-bold .axw-av{background:var(--ac);box-shadow:none}',
    '.axw-t-bold .axw-title{font-size:17px}',
    '.axw-t-bold .axw-b{border-radius:20px;border-bottom-left-radius:6px}.axw-t-bold .me .axw-b{border-radius:20px;border-bottom-right-radius:6px}',
    '.axw-t-bold .axw-cta,.axw-t-bold .axw-fi{border-radius:14px}',
    '.axw-t-soft .axw-panel{border-radius:24px}',
    '.axw-t-soft .axw-head{color:#0f172a;background:color-mix(in srgb,var(--ac) 11%,#fff)}',
    '.axw-t-soft .axw-av{background:var(--ac);color:#fff;box-shadow:none}',
    '.axw-t-soft .axw-hb{color:var(--ac);background:color-mix(in srgb,var(--ac) 16%,#fff)}',
    '.axw-t-soft .axw-list{background:#fff}',
    '.axw-t-soft .me .axw-b{background:color-mix(in srgb,var(--ac) 15%,#fff);color:#0f172a}',
    '.axw-t-soft .axw-b{border-radius:20px;border-bottom-left-radius:6px;background:#f4f6f9;border-color:transparent}',
    '.axw-t-soft .me .axw-b{border-radius:20px;border-bottom-right-radius:6px}',
    '@media (max-width:480px){.axw-open{inset:0}.axw-open .axw-panel{width:100%;max-width:none;height:100%;max-height:none;border-radius:0;margin:0}}',
    '@media (prefers-reduced-motion:reduce){.axw-panel,.axw-typing i{animation:none}}'
  ].join('\n');
  var styleEl = document.createElement('style'); styleEl.textContent = CSS; document.head.appendChild(styleEl);

  function field(labelText, input, hint) {
    var wrap = el('label', 'axw-f');
    var lab = document.createTextNode(labelText);
    wrap.appendChild(lab);
    input.className = 'axw-fi';
    wrap.appendChild(input);
    if (hint) wrap.appendChild(el('span', 'axw-hint', hint));
    wrap.labelNode = lab;
    return wrap;
  }
  function cta(text, withArrow) {
    var b = el('button', 'axw-cta'); b.type = 'submit';
    b.appendChild(document.createTextNode(text));
    if (withArrow) b.appendChild(icon(I.send, 17, 2.4));
    return b;
  }

  var root = el('div', 'axw');
  var panel = el('div', 'axw-panel');
  panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Chat');

  var head = el('div', 'axw-head');
  var avatar = el('div', 'axw-av', 'C');
  var headText = el('div', 'axw-ht');
  var title = el('div', 'axw-title', 'Chat with us');
  var sub = el('div', 'axw-sub'); var subText = el('span', '', 'Online \u00b7 replies in a moment'); sub.appendChild(el('span', 'axw-dot')); sub.appendChild(subText);
  headText.appendChild(title); headText.appendChild(sub);
  var callBtn = el('button', 'axw-hb'); callBtn.type = 'button'; callBtn.style.display = 'none'; callBtn.setAttribute('aria-label', 'Request a call');
  callBtn.appendChild(icon(I.phone, 14, 2.2)); callBtn.appendChild(document.createTextNode('Call me'));
  var close = el('button', 'axw-hb axw-x'); close.type = 'button'; close.setAttribute('aria-label', 'Close chat'); close.appendChild(icon(I.close, 16, 2.4));
  head.appendChild(avatar); head.appendChild(headText); head.appendChild(callBtn); head.appendChild(close);

  var list = el('div', 'axw-list'); list.setAttribute('role', 'log'); list.setAttribute('aria-live', 'polite');
  var status = el('div', 'axw-err'); status.setAttribute('role', 'alert');
  var form = el('form', 'axw-form');
  var input = el('input', 'axw-in'); input.setAttribute('placeholder', 'Type your message'); input.setAttribute('maxlength', '2000'); input.setAttribute('aria-label', 'Message');
  var send = el('button', 'axw-send'); send.type = 'submit'; send.setAttribute('aria-label', 'Send'); send.appendChild(icon(I.send, 18, 2.4));
  form.appendChild(input); form.appendChild(send);

  // Before the chat: who are you?
  var leadView = el('form', 'axw-view'); leadView.setAttribute('novalidate', 'novalidate');
  var trap = el('input', 'axw-trap'); trap.type = 'text'; trap.name = 'website'; trap.tabIndex = -1; trap.setAttribute('autocomplete', 'off'); trap.setAttribute('aria-hidden', 'true');
  var leadError = el('div', 'axw-ferr'); leadError.setAttribute('role', 'alert');
  var leadSubmit = cta('Start chat', true);
  var leadSubmitText = leadSubmit.firstChild;
  var trust = el('div', 'axw-trust'); trust.appendChild(icon(I.lock, 12, 2.2)); trust.appendChild(document.createTextNode('Your details are only used to help with your enquiry.'));
  var leadHeading = el('h3', 'axw-h', 'Start a conversation');
  var leadIntroEl = el('p', 'axw-p', 'Tell us how to reach you and we will take it from here.');
  leadView.appendChild(leadHeading);
  leadView.appendChild(leadIntroEl);
  var leadFields = el('div', 'axw-fields'); leadView.appendChild(leadFields);
    leadView.appendChild(trap); leadView.appendChild(leadError); leadView.appendChild(leadSubmit); leadView.appendChild(trust);

  // Before anything is collected: what we keep, why, and who to ask (DPDP notice + consent + age).
  var privacyView = el('form', 'axw-view'); privacyView.setAttribute('novalidate', 'novalidate');
  var privacyLines = el('div', 'axw-pn');
  var privacyLink = el('a', 'axw-pl', 'Read the full notice, or ask us to delete your data'); privacyLink.target = '_blank'; privacyLink.rel = 'noopener noreferrer';
  var agree = el('input'); agree.type = 'checkbox'; agree.id = 'axw-agree-' + key;
  var agreeLabel = el('label', 'axw-consent'); agreeLabel.setAttribute('for', agree.id);
  agreeLabel.appendChild(agree); agreeLabel.appendChild(el('span', '', 'I am 18 or older and I agree to my details being used as described above.'));
  var privacyError = el('div', 'axw-ferr'); privacyError.setAttribute('role', 'alert');
  var privacySubmit = cta('Continue', true);
  privacyView.appendChild(el('h3', 'axw-h', 'Before we start'));
  privacyView.appendChild(privacyLines); privacyView.appendChild(privacyLink);
  privacyView.appendChild(agreeLabel); privacyView.appendChild(privacyError); privacyView.appendChild(privacySubmit);

  // Ask for a call.
  var callView = el('form', 'axw-view'); callView.setAttribute('novalidate', 'novalidate');
  var callName = el('input'); callName.type = 'text'; callName.maxLength = 120; callName.autocomplete = 'name'; callName.placeholder = 'Your full name';
  var callPhone = el('input'); callPhone.type = 'tel'; callPhone.maxLength = 30; callPhone.autocomplete = 'tel'; callPhone.required = true; callPhone.placeholder = '+91 98765 43210';
  var callNote = el('textarea'); callNote.rows = 2; callNote.maxLength = 300; callNote.placeholder = 'For example, the best time to call';
  var consent = el('input'); consent.type = 'checkbox'; consent.id = 'axw-consent-' + key;
  var consentLabel = el('label', 'axw-consent'); consentLabel.setAttribute('for', consent.id);
  var consentText = el('span', '');
  consentLabel.appendChild(consent); consentLabel.appendChild(consentText);
  var callError = el('div', 'axw-ferr'); callError.setAttribute('role', 'alert');
  var callSubmit = cta('Call me now', false);
  var callCancel = el('button', 'axw-alt', 'Back to chat'); callCancel.type = 'button';
  var callNameField = field('Your name', callName);
  callView.appendChild(el('h3', 'axw-h', 'Get a call back'));
  callView.appendChild(el('p', 'axw-p', 'Our AI assistant can phone you right now.'));
  callView.appendChild(callNameField);
  callView.appendChild(field('Phone number', callPhone, 'Include your country code, like +91'));
  callView.appendChild(field('Anything we should know? (optional)', callNote));
  callView.appendChild(consentLabel); callView.appendChild(callError); callView.appendChild(callSubmit); callView.appendChild(callCancel);

  var foot = el('div', 'axw-foot');
  foot.appendChild(document.createTextNode('Powered by '));
  var brand = el('a', '', 'autobot360.com'); brand.href = 'https://autobot360.com'; brand.target = '_blank'; brand.rel = 'noopener noreferrer';
  foot.appendChild(brand);

  panel.appendChild(head); panel.appendChild(list); panel.appendChild(privacyView); panel.appendChild(leadView); panel.appendChild(callView); panel.appendChild(status); panel.appendChild(form); panel.appendChild(foot);
  var bubble = el('button', 'axw-launch'); bubble.type = 'button'; bubble.setAttribute('aria-label', 'Open chat'); bubble.appendChild(icon(I.chat, 26, 2));
  var bubbleLabel = el('span', '');
  root.appendChild(panel); root.appendChild(bubble); document.body.appendChild(root);

  var color = '#2563eb';
  var TEMPLATES = ['modern', 'minimal', 'bold', 'soft'];
  // Everything a business can customise, applied the same way to a live widget and to the designer's preview.
  function applyConfig(c) {
    if (c.color && /^#[0-9a-fA-F]{3,8}$/.test(c.color)) color = c.color;
    root.style.setProperty('--ac', color);
    var tpl = TEMPLATES.indexOf(c.template) >= 0 ? c.template : 'modern';
    TEMPLATES.forEach(function (t) { root.classList.toggle('axw-t-' + t, t === tpl); });
    root.classList.toggle('axw-left', c.position === 'left');
    var name = (c.title || 'Chat with us');
    title.textContent = name; avatar.textContent = name.trim().charAt(0).toUpperCase() || 'C';
    subText.textContent = c.subtitle || 'Online \u00b7 replies in a moment';
    cfg.lead_form = !!c.lead_form; cfg.require_phone = !!c.require_phone; cfg.call_me = !!c.call_me; cfg.consent_text = c.consent_text || '';
    consentText.textContent = cfg.consent_text || 'I agree to be called on this number.';
    cfg.privacy = c.privacy && c.privacy.required ? c.privacy : null;
    if (cfg.privacy) {
      privacyLines.textContent = '';
      (cfg.privacy.lines || []).forEach(function (t) { privacyLines.appendChild(el('p', '', t)); });
      if (cfg.privacy.url) { privacyLink.href = cfg.privacy.url; privacyLink.style.display = 'block'; } else { privacyLink.style.display = 'none'; }
    }
    cfg.fields = Array.isArray(c.fields) ? c.fields : []; cfg.quick_replies = Array.isArray(c.quick_replies) ? c.quick_replies : [];
    renderFields();
    leadHeading.textContent = c.lead_heading || 'Start a conversation';
    leadIntroEl.textContent = c.lead_intro || 'Tell us how to reach you and we will take it from here.';
    leadSubmitText.textContent = c.button_label || 'Start chat';
    bubbleLabel.textContent = c.launcher_label || '';
    if (c.launcher_label) { if (!bubbleLabel.parentNode) bubble.appendChild(bubbleLabel); bubble.classList.add('axw-lab'); }
    else { if (bubbleLabel.parentNode) bubble.removeChild(bubbleLabel); bubble.classList.remove('axw-lab'); }
    callBtn.style.display = cfg.call_me && list.style.display !== 'none' && (state.token || preview) ? 'inline-flex' : 'none';
  }
  var preview = script.hasAttribute('data-preview');

  // Exactly one of: chat, the details form, the call card.
  function show(view) {
    list.style.display = view === 'chat' ? 'block' : 'none';
    status.style.display = view === 'chat' ? 'block' : 'none';
    form.style.display = view === 'chat' ? 'flex' : 'none';
    privacyView.style.display = view === 'privacy' ? 'block' : 'none';
    leadView.style.display = view === 'lead' ? 'block' : 'none';
    callView.style.display = view === 'call' ? 'block' : 'none';
    callBtn.style.display = cfg.call_me && view === 'chat' && (state.token || preview) ? 'inline-flex' : 'none';
  }
  function focusNow(n) { try { n.focus({ preventScroll: true }); } catch (e) { n.focus(); } }
  function clock(iso) {
    try { var d = new Date(iso); return isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); } catch (e) { return ''; }
  }

  var typingRow = null;
  function showTyping() {
    if (typingRow) return;
    typingRow = el('div', 'axw-row');
    var b = el('div', 'axw-b axw-typing'); b.appendChild(el('i')); b.appendChild(el('i')); b.appendChild(el('i'));
    typingRow.appendChild(b); list.appendChild(typingRow); list.scrollTop = list.scrollHeight;
  }
  function hideTyping() { if (typingRow && typingRow.parentNode) typingRow.parentNode.removeChild(typingRow); typingRow = null; }

  function addMessage(m) {
    if (state.seen[m.id]) return; state.seen[m.id] = true;
    hideTyping();
    var mine = m.direction === 'inbound';
    if (mine) { state.sentAny = true; clearChips(); }
    var row = el('div', 'axw-row' + (mine ? ' me' : ''));
    var box = el('div', 'axw-msg');
    box.appendChild(el('div', 'axw-b', m.text || ''));
    if (!mine && m.sources && m.sources.length) { box.appendChild(el('div', 'axw-src', 'Source: ' + m.sources.map(function (x) { return x.title; }).join(', '))); }
    var when = clock(m.created_at); if (when) box.appendChild(el('div', 'axw-t', when));
    row.appendChild(box); list.appendChild(row); list.scrollTop = list.scrollHeight;
    if (!state.lastAt || m.created_at > state.lastAt) state.lastAt = m.created_at;
  }
  function addNote(text) { list.appendChild(el('div', 'axw-note', text)); list.scrollTop = list.scrollHeight; }
  function offerCall() {
    if (state.called || !cfg.call_me) return;
    var card = el('div', 'axw-card');
    card.appendChild(el('p', '', 'Would you like us to call you?'));
    var go = el('button', 'axw-cta', 'Call me'); go.type = 'button'; go.style.height = '38px'; go.style.width = 'auto'; go.style.padding = '0 18px';
    go.addEventListener('click', function () { openCall(); });
    card.appendChild(go); list.appendChild(card); list.scrollTop = list.scrollHeight;
  }

  function utmParams() {
    var out = {};
    try { new URLSearchParams(location.search).forEach(function (v, k) { if (k.indexOf('utm_') === 0) out[k] = v; }); } catch (e) {}
    return out;
  }
  function startSession(extra) {
    var body = state.privacy ? { privacy_accepted: true, adult_confirmed: true, notice_version: cfg.privacy ? cfg.privacy.version : null } : {};
    body.page_url = location.href.split('#')[0]; body.referrer = document.referrer || ''; body.utm = utmParams();
    if (extra) for (var k in extra) body[k] = extra[k];
    return request('POST', api + '/session', body).then(function (r) {
      state.token = r.session_token; save();
      (r.messages || []).forEach(addMessage);
    });
  }
  function resetSession() { state.sentAny = false; chips = null; state.token = null; state.lastAt = null; state.seen = {}; list.textContent = ''; typingRow = null; save(); }
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
        show('chat'); (r.messages || []).forEach(addMessage); showChips(); focusNow(input);
      }).catch(function (e) { if (e.status === 404) { resetSession(); return begin(); } throw e; });
    }
    if (cfg.privacy && !state.privacy) { show('privacy'); focusNow(agree); return Promise.resolve(); }
    if (cfg.lead_form) { show('lead'); focusFirstField(); return Promise.resolve(); }
    return startSession().then(function () { show('chat'); showChips(); focusNow(input); });
  }
  function openPanel() {
    state.open = true; panel.style.display = 'flex'; bubble.style.display = 'none'; root.classList.add('axw-open');
    begin().catch(function (e) { status.textContent = e.message; });
    state.timer = setInterval(poll, 4000);
  }
  function closePanel() { state.open = false; panel.style.display = 'none'; bubble.style.display = ''; root.classList.remove('axw-open'); clearInterval(state.timer); }
  bubble.addEventListener('click', openPanel); close.addEventListener('click', closePanel);

  // The details form is whatever fields the business defined; each is built from its definition.
  var fieldRefs = [];
  function buildInput(f) {
    var t = f.type, n;
    if (t === 'textarea') { n = el('textarea'); n.rows = 3; n.maxLength = 2000; }
    else if (t === 'select') {
      n = el('select');
      var first = el('option', '', f.placeholder || 'Choose one'); first.value = ''; n.appendChild(first);
      (f.options || []).forEach(function (o) { var op = el('option', '', o); op.value = o; n.appendChild(op); });
    } else {
      n = el('input'); n.type = { email: 'email', phone: 'tel', number: 'number', date: 'date' }[t] || 'text';
      n.maxLength = t === 'email' ? 254 : t === 'phone' ? 30 : 500;
      if (t === 'number') n.step = 'any';
      if (t === 'email') n.autocomplete = 'email';
      if (t === 'phone') n.autocomplete = 'tel';
    }
    if (f.maps_to === 'name') n.autocomplete = 'name';
    if (f.placeholder && t !== 'select') n.placeholder = f.placeholder;
    return n;
  }
  function renderFields() {
    leadFields.textContent = ''; fieldRefs = [];
    (cfg.fields || []).forEach(function (f) {
      var node, wrap, mark = f.required ? ' *' : '';
      if (f.type === 'checkbox') {
        node = el('input'); node.type = 'checkbox'; node.id = 'axw-f-' + key + '-' + f.key;
        wrap = el('label', 'axw-consent'); wrap.setAttribute('for', node.id);
        wrap.appendChild(node); wrap.appendChild(el('span', '', f.label + mark));
      } else {
        node = buildInput(f); wrap = field(f.label + mark, node, f.help);
      }
      leadFields.appendChild(wrap); fieldRefs.push({ def: f, node: node });
    });
  }
  function focusFirstField() { if (fieldRefs.length) focusNow(fieldRefs[0].node); }
  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  // What the visitor filled in, checked against the same rules the server applies.
  function collectLead() {
    var answers = {}, mapped = {};
    for (var i = 0; i < fieldRefs.length; i++) {
      var d = fieldRefs[i].def, n = fieldRefs[i].node;
      var v = d.type === 'checkbox' ? n.checked : String(n.value || '').trim();
      var blank = d.type === 'checkbox' ? !v : v === '';
      if (blank && d.required) return { error: 'Please fill in “' + d.label + '”.', node: n };
      if (blank && d.type !== 'checkbox') continue;
      if (d.type === 'email' && v && !EMAIL_RE.test(v)) return { error: '“' + d.label + '” doesn’t look like an email address.', node: n };
      answers[d.key] = v;
      if (d.maps_to && v !== '' && v !== false) mapped[d.maps_to] = v;
    }
    var contact = fieldRefs.filter(function (r) { return r.def.maps_to === 'phone' || r.def.maps_to === 'email'; });
    if (contact.length && !mapped.phone && !mapped.email) return { error: 'Please give us a phone number or an email address.', node: contact[0].node };
    return { answers: answers, mapped: mapped };
  }

  privacyView.addEventListener('submit', function (ev) {
    ev.preventDefault();
    privacyError.textContent = '';
    if (!agree.checked) { privacyError.textContent = 'Please tick the box to continue.'; focusNow(agree); return; }
    state.privacy = true;
    begin().catch(function (e) { privacyError.textContent = e.message; });
  });

  leadView.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (state.busy) return;
    leadError.textContent = '';
    var got = collectLead();
    if (got.error) { leadError.textContent = got.error; focusNow(got.node); return; }
    var m = got.mapped;
    state.busy = true; leadSubmit.disabled = true;
    startSession({ answers: got.answers, name: m.name || '', phone: m.phone || '', email: m.email || '', interest: m.interest || '', website: trap.value }).then(function () {
      state.name = m.name || ''; state.phone = m.phone || ''; save();
      show('chat'); showChips(); focusNow(input);
    }).catch(function (e) { leadError.textContent = e.message; }).then(function () { state.busy = false; leadSubmit.disabled = false; });
  });

  function openCall() {
    callError.textContent = ''; consent.checked = false;
    callName.value = state.name || ''; callPhone.value = state.phone || '';
    callNameField.style.display = state.name ? 'none' : 'block';
    consentText.textContent = cfg.consent_text || 'I agree to be called on this number.';
    show('call'); focusNow(state.name ? callPhone : callName);
  }
  callBtn.addEventListener('click', openCall);
  callCancel.addEventListener('click', function () { show('chat'); focusNow(input); });
  callView.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (state.busy || !state.token) return;
    var phone = callPhone.value.trim(), name = (state.name || callName.value).trim();
    callError.textContent = '';
    if (!name) { callError.textContent = 'Please tell us your name.'; focusNow(callName); return; }
    if (!phone) { callError.textContent = 'Please enter your phone number.'; focusNow(callPhone); return; }
    if (!consent.checked) { callError.textContent = 'Please tick the box to agree to a call.'; focusNow(consent); return; }
    state.busy = true; callSubmit.disabled = true;
    request('POST', api + '/call-request', { session_token: state.token, name: name, phone: phone, consent: true, note: callNote.value.trim(), page_url: location.href.split('#')[0] }).then(function (r) {
      state.name = name; state.phone = phone; state.called = true; save();
      show('chat'); addNote(r.message || 'Thanks! We will call you shortly.');
    }).catch(function (e) {
      if (e.status === 404) { resetSession(); begin(); return; }
      callError.textContent = e.message;
    }).then(function () { state.busy = false; callSubmit.disabled = false; });
  });

  // Suggested questions: tappable until the visitor says something.
  var chips = null;
  function clearChips() { if (chips && chips.parentNode) chips.parentNode.removeChild(chips); chips = null; }
  function showChips(force) {
    clearChips();
    if (!cfg.quick_replies.length || (state.sentAny && !force) || (!state.token && !preview)) return;
    chips = el('div', 'axw-chips');
    cfg.quick_replies.forEach(function (q) {
      var b = el('button', 'axw-chip', q); b.type = 'button';
      b.addEventListener('click', function () { sendText(q, false); });
      chips.appendChild(b);
    });
    list.appendChild(chips); list.scrollTop = list.scrollHeight;
  }

  function sendText(text, fromInput) {
    if (!text || state.busy || !state.token) return;
    state.busy = true; status.textContent = ''; if (fromInput) input.value = ''; send.disabled = true;
    state.sentAny = true; clearChips();
    showTyping();
    request('POST', api + '/message', { session_token: state.token, text: text }).then(function (r) {
      (r.messages || []).forEach(addMessage);
      if (r.call_offer) offerCall();
    }).catch(function (e) { status.textContent = e.message; if (fromInput) input.value = text; }).then(function () { hideTyping(); state.busy = false; send.disabled = false; });
  }
  form.addEventListener('submit', function (ev) { ev.preventDefault(); sendText(input.value.trim(), true); });

  if (preview) {
    // The designer page embeds this in an iframe: no network, demo conversation, config arrives by postMessage.
    var demo = [
      { id: 'p1', direction: 'outbound', text: 'Hi! How can we help you today?', created_at: '2026-01-01T10:00:00Z' },
      { id: 'p2', direction: 'inbound', text: 'Do you have appointments free tomorrow morning?', created_at: '2026-01-01T10:00:20Z' },
      { id: 'p3', direction: 'outbound', text: 'Yes, 10:30 is open. Shall I book it for you?', created_at: '2026-01-01T10:00:24Z' }
    ];
    var previewView = 'chat';
    var previewCfg = {};
    try { previewCfg = JSON.parse(script.getAttribute('data-config') || '{}'); } catch (e) {}
    function renderPreview() {
      state.open = true; panel.style.display = 'flex'; bubble.style.display = 'none'; root.classList.add('axw-open');
      list.textContent = ''; state.seen = {};
      demo.forEach(addMessage);
      showChips(true);
      show(previewView);
    }
    applyConfig(previewCfg); renderPreview();
    form.addEventListener('submit', function (ev) { ev.preventDefault(); ev.stopImmediatePropagation(); }, true);
    window.addEventListener('message', function (ev) {
      if (ev.source !== window.parent) return;
      var d = ev.data || {};
      if (d.type === 'axw-config' && d.config) { previewCfg = d.config; applyConfig(previewCfg); show(previewView); if (previewView === 'chat') showChips(true); }
      if (d.type === 'axw-view') { previewView = d.view; if (previewView === 'launcher') { panel.style.display = 'none'; bubble.style.display = ''; root.classList.remove('axw-open'); } else { renderPreview(); } }
    });
    close.addEventListener('click', function () { previewView = 'launcher'; panel.style.display = 'none'; bubble.style.display = ''; root.classList.remove('axw-open'); });
    bubble.addEventListener('click', function () { previewView = 'chat'; renderPreview(); });
    ['submit'].forEach(function () {
      privacyView.addEventListener('submit', function (ev) { ev.preventDefault(); ev.stopImmediatePropagation(); }, true);
      leadView.addEventListener('submit', function (ev) { ev.preventDefault(); ev.stopImmediatePropagation(); }, true);
      callView.addEventListener('submit', function (ev) { ev.preventDefault(); ev.stopImmediatePropagation(); }, true);
    });
    return;
  }

  request('GET', api + '/config').then(applyConfig).catch(function () { root.style.display = 'none'; });
})();
"""
