// The human-facing inspector page served at `GET /` when content negotiation
// (`worker/src/routes.js`'s `wantsHtml`) decides the request wants HTML instead of the JSON
// service card. This module carries NO data of its own: every number, hash, count, and search
// result the page shows is a live `fetch()` of the same JSON routes an agent would call --
// `page.js` only supplies markup, CSS and client-side JavaScript, all as plain committed strings.
//
// Injection rule: every value that ever arrives from a JSON response (titles, urls, snippets,
// DID facts, error strings, header values -- all of it) is untrusted and reaches the DOM ONLY
// through `textContent`, `document.createTextNode`, or `document.createElement` followed by
// `textContent`. The embedded script never uses `innerHTML`, `outerHTML`,
// `insertAdjacentHTML`, `document.write`, `DOMParser`, a template literal or string
// concatenation into the DOM, `eval`, or `new Function`. The one exception is `href` on a
// result link, and only after `new URL(value)` parses and `.protocol` is exactly `"http:"` or
// `"https:"` -- see `makeLink` in `PAGE_SCRIPT` below. No `style` attribute is ever written
// (the CSP's `style-src` is hash-only, so one would be a violation): the script toggles
// `hidden`, `disabled` and class names, nothing else.
//
// Visual register: the FLOP theme (flop.finance -- navy `#0A1128` ground, charcoal `#151D32`
// cards, `#232A3E` hairlines, cyan `#00B4D8` accent, muted `#A1A7AE`, Space Mono for headings,
// labels and hashes, Inter for prose). The page loads no external asset, so the font stacks name
// those faces first and fall back to the system monospace/sans when they are not installed.
// Dark only, like the site it matches.
//
// Regeneration recipe: edit `PAGE_STYLE`/`PAGE_SCRIPT` below, run the worker tests
// (`node --test worker/test/page.test.mjs`), the hash-pin test's failure message prints the two
// new constants; paste them in as `PAGE_STYLE_SHA256`/`PAGE_SCRIPT_SHA256`. Compute them yourself
// with `node:crypto` (`createHash("sha256").update(str, "utf8").digest("base64")`) from a scratch
// script -- this module deliberately imports nothing (not even `node:crypto`), so
// `worker/src/routes.js` can keep importing only `./search.js` and `./page.js` and
// `worker/test/` can keep running under plain Node with zero installed dependencies.

// ---------------------------------------------------------------------------------------------
// PAGE_STYLE -- the exact contents of the one <style> element. No backtick, no "${" (this value
// is concatenated, never template-substituted, into PAGE_HTML below, but the same two characters
// are still banned here -- see worker/test/page.test.mjs's page-hygiene checks).
// ---------------------------------------------------------------------------------------------

const STYLE_LINES = [
  ":root{--bg:#0A1128;--card:#151D32;--line:#232A3E;--fg:#F5F7FA;--white:#FFFFFF;--muted:#A1A7AE;--accent:#00B4D8;--accent2:#3DC5E0;--err:#FF7B72;--mono:'Space Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:Inter,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}",
  "*{box-sizing:border-box}",
  "html,body{margin:0;padding:0}",
  "body{background:var(--bg);color:var(--fg);font-family:var(--sans);line-height:1.5;max-width:72rem;margin:0 auto;padding:0 1.25rem 3rem}",
  "code,pre,h1,h2,h3,label,button,input,select,summary,th,.lbl,.status,.raw-status,.eyebrow,.kind,.brand,nav a,.foot{font-family:var(--mono)}",
  "a{color:var(--accent)}a:hover{color:var(--accent2)}",
  "code{font-size:.92em;color:var(--accent2);word-break:break-all}",
  ".topbar{display:flex;align-items:center;justify-content:space-between;gap:.75rem;flex-wrap:wrap;min-height:4rem;padding:.6rem 0;border-bottom:1px solid var(--line)}",
  ".brand{font-weight:700;font-size:1.05rem;letter-spacing:-.03em;color:var(--white)}",
  "nav ul{list-style:none;display:flex;gap:1.5rem;padding:0;margin:0;flex-wrap:wrap}",
  "nav a{font-size:.72rem;letter-spacing:.07em;text-transform:uppercase;color:var(--white);text-decoration:none;padding:.25rem 0}",
  "nav a:hover,nav a.docs{color:var(--accent)}",
  ".eyebrow{display:flex;align-items:center;gap:.5rem;margin:3rem 0 1rem;font-size:.68rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}",
  ".eyebrow::before{content:'';display:inline-block;width:.5rem;height:.5rem;background:var(--accent)}",
  "h1{font-size:2.7rem;line-height:1.05;letter-spacing:-.04em;margin:0 0 1rem;color:var(--white);max-width:56rem}",
  ".lead{font-size:1.1rem;line-height:1.6;color:var(--muted);max-width:48rem;margin:0 0 2rem}",
  ".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:1rem;margin:1rem 0}",
  ".stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:1.1rem 1.2rem;min-width:0}",
  ".stat b{display:block;font-family:var(--mono);font-size:1.35rem;font-weight:700;letter-spacing:-.02em;color:var(--accent);margin:.45rem 0;word-break:break-all}",
  ".stat small,.stat p{display:block;font-family:var(--mono);font-size:.72rem;line-height:1.5;color:var(--muted);margin:0}",
  ".lbl{font-size:.68rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}",
  "section{margin-top:3.5rem;border-top:1px solid var(--line);padding-top:2rem}",
  ".sec-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:.5rem 1rem}",
  "h2{font-size:1.5rem;letter-spacing:-.02em;margin:0;color:var(--white)}",
  "h3{font-size:.68rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:1.5rem 0 .5rem}",
  "form{display:flex;gap:.75rem;align-items:flex-end;flex-wrap:wrap;margin:1.25rem 0 1rem}",
  ".f{display:flex;flex-direction:column;gap:.35rem;min-width:0}",
  ".f.grow{flex:1 1 16rem}",
  "label{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}",
  "input,select{height:2.75rem;padding:0 .85rem;background:var(--card);border:1px solid var(--line);border-radius:4px;color:var(--fg);font-size:.9rem;min-width:0;width:100%}",
  "input[type=number]{width:6.5rem}",
  "select{width:11rem}",
  "button{height:2.75rem;padding:0 1.5rem;background:var(--accent);color:var(--bg);border:0;border-radius:4px;font-weight:700;font-size:.78rem;letter-spacing:.08em;text-transform:uppercase;cursor:pointer}",
  "button:hover{background:var(--accent2)}",
  "button:disabled{cursor:not-allowed;opacity:.5}",
  "button.ghost{height:2.25rem;padding:0 1rem;background:transparent;color:var(--accent);border:1px solid var(--accent);margin:.6rem .6rem 0 0}",
  "button.ghost:hover{background:var(--card)}",
  "button.linkbtn{height:auto;padding:0;background:transparent;color:var(--muted);text-decoration:underline;font-size:.72rem;font-weight:400;letter-spacing:0;text-transform:none}",
  "button.chip{display:inline-block;height:auto;padding:.35rem .6rem;margin:.15rem .3rem .15rem 0;background:var(--bg);color:var(--accent);border:1px solid var(--accent);font-size:.7rem;font-weight:400;letter-spacing:0;text-transform:none;text-align:left;word-break:break-all;line-height:1.35}",
  "button.chip:hover{background:var(--card)}",
  ".kind{display:inline-block;padding:.15rem .5rem;margin:.15rem .3rem .15rem 0;border:1px solid var(--line);border-radius:4px;background:var(--bg);font-size:.7rem;color:var(--fg);white-space:nowrap}",
  ".tag{display:inline-block;padding:.4rem .75rem;margin:.5rem 0;background:var(--accent);color:var(--bg);border-radius:4px;font-family:var(--mono);font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase}",
  ".two{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:1rem;margin:.75rem 0}",
  ".reason{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:1rem 1.2rem;min-width:0}",
  ".reason b{display:block;font-family:var(--mono);font-size:1rem;line-height:1.45;color:var(--white);margin:.45rem 0 .3rem}",
  ".reason small{display:block;font-size:.75rem;color:var(--muted)}",
  ".status{font-size:.8rem;color:var(--muted);margin:.5rem 0}",
  ".error{color:var(--err);font-weight:700}",
  ".muted{color:var(--muted)}",
  ".caveat{color:var(--muted);font-style:italic;font-size:.85rem;line-height:1.6;margin:.6rem 0}",
  "table{border-collapse:collapse;width:100%;margin:.75rem 0;font-size:.85rem;background:var(--card);border:1px solid var(--line)}",
  "th{font-size:.66rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.7rem .85rem;border-bottom:1px solid var(--line)}",
  "td{padding:.7rem .85rem;border-top:1px solid var(--line);vertical-align:top;word-break:break-word;line-height:1.5}",
  "td.pre{white-space:pre-wrap;color:var(--muted);font-size:.8rem}",
  "td.num{font-family:var(--mono);color:var(--accent);white-space:nowrap}",
  "td.key{font-family:var(--mono);color:var(--muted);white-space:nowrap;font-size:.78rem}",
  "td strong{display:block;color:var(--white);font-weight:600}",
  "td a{font-family:var(--mono);font-size:.78rem;word-break:break-all}",
  "details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:1rem 0;padding:0 1rem 1rem}",
  "summary{cursor:pointer;padding:.9rem 0;font-size:.68rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--white);list-style:none}",
  "summary::-webkit-details-marker{display:none}",
  "summary::before{content:'\\25B8  '}",
  "details[open] summary::before{content:'\\25BE  '}",
  "pre{margin:.5rem 0;padding:1rem;background:var(--bg);border:1px solid var(--line);border-radius:4px;font-size:.78rem;line-height:1.55;color:var(--accent2);white-space:pre-wrap;word-break:break-word}",
  ".raw-status{font-size:.76rem;color:var(--muted);margin:.4rem 0}",
  "details label{display:inline-flex;align-items:center;gap:.4rem;margin:.5rem 0;text-transform:none;letter-spacing:0;font-size:.76rem}input[type=checkbox]{height:auto;width:auto}",
  ".copy-fallback{width:100%;height:3.5rem;margin-top:.5rem;display:block;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px;font-family:var(--mono);font-size:.76rem;padding:.5rem}",
  ".hint{display:block;color:var(--muted);font-family:var(--mono);font-size:.7rem;text-transform:none;letter-spacing:0}",
  ".foot{margin-top:3.5rem;border-top:1px solid var(--line);padding-top:1.5rem;display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem 1.5rem;font-size:.74rem;color:var(--muted)}",
  ".foot a{letter-spacing:.06em;text-transform:uppercase;text-decoration:none;margin-left:1rem}",
  "[hidden]{display:none !important}",
  ":focus-visible{outline:2px solid var(--accent);outline-offset:2px}",
  "@media (max-width:640px){h1{font-size:1.9rem}form{flex-direction:column;align-items:stretch}input[type=number],select{width:100%}form button{width:100%}.foot a{margin-left:0;margin-right:1rem}}",
];

export const PAGE_STYLE = STYLE_LINES.join("\n");

// ---------------------------------------------------------------------------------------------
// PAGE_SCRIPT -- the exact contents of the one <script> element. Same two-character ban as
// PAGE_STYLE above; every string this script builds uses concatenation ("+") and single-quoted
// literals, never a template literal.
// ---------------------------------------------------------------------------------------------

const SCRIPT_LINES = [
  "'use strict';",
  "var CARD = null;",
  "var CARD_ERROR = null;",
  "var KNOWN_KINDS = [];",
  "var HEADER_NAMES = ['content-type', 'cache-control', 'x-index-generated-at', 'x-ledger-generated-at', 'x-index-db-sha256', 'retry-after'];",
  "function byId(id) { return document.getElementById(id); }",
  "function clearNode(n) { while (n.firstChild) n.removeChild(n.firstChild); }",
  "function mkEl(tag, cls) { var e = document.createElement(tag); if (cls) e.className = cls; return e; }",
  "function mkText(tag, text, cls) { var e = mkEl(tag, cls); e.textContent = text; return e; }",
  "function td(content, cls) { var c = mkEl('td', cls); if (content instanceof Node) { c.appendChild(content); } else { c.textContent = content; } return c; }",
  "function trow(cells) { var r = mkEl('tr'); for (var i = 0; i < cells.length; i++) r.appendChild(cells[i]); return r; }",
  "function buildTable(headers, rows) { var table = mkEl('table'); var thead = mkEl('thead'); var htr = mkEl('tr'); for (var i = 0; i < headers.length; i++) { var th = mkEl('th'); th.setAttribute('scope', 'col'); th.textContent = headers[i]; htr.appendChild(th); } thead.appendChild(htr); table.appendChild(thead); var tbody = mkEl('tbody'); for (var j = 0; j < rows.length; j++) tbody.appendChild(rows[j]); table.appendChild(tbody); return table; }",
  "function kvTable(pairs) { var rows = []; for (var i = 0; i < pairs.length; i++) rows.push(trow([td(pairs[i][0], 'key'), td(pairs[i][1])])); return buildTable(['field', 'value'], rows); }",
  "function statCard(label, big, small) { var card = mkEl('div', 'stat'); card.appendChild(mkText('span', label, 'lbl')); card.appendChild(mkText('b', big)); if (small instanceof Node) { card.appendChild(small); } else if (small) { card.appendChild(mkText('small', small)); } return card; }",
  "function fmtNum(n) { var x = Number(n); return isFinite(x) ? x.toLocaleString('en-US') : String(n); }",
  "function isValidHttpUrl(value) { try { var u = new URL(value); return u.protocol === 'http:' || u.protocol === 'https:'; } catch (e) { return false; } }",
  "function makeLink(urlStr, text) { if (isValidHttpUrl(urlStr)) { var a = document.createElement('a'); a.href = urlStr; a.rel = 'noopener noreferrer nofollow'; a.target = '_blank'; a.textContent = text; return a; } var span = mkEl('span'); span.textContent = text; return span; }",
  "function titleUrlCell(title, urlStr) { var cell = mkEl('td'); if (title !== undefined && title !== null && title !== '') cell.appendChild(mkText('strong', String(title))); cell.appendChild(makeLink(String(urlStr), String(urlStr))); return cell; }",
  "function kindBadge(kind) { return mkText('span', kind === null || kind === undefined ? 'null' : String(kind), 'kind'); }",
  "function pad2(n) { return n < 10 ? '0' + n : String(n); }",
  "function fmtAbs(iso) { var d = new Date(iso); if (isNaN(d.getTime())) return String(iso); return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate()) + ' ' + pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ':' + pad2(d.getUTCSeconds()) + ' UTC'; }",
  "function fmtAge(iso) { var d = new Date(iso); var ms = Date.now() - d.getTime(); if (isNaN(ms)) return ''; var future = ms < 0; var abs = Math.abs(ms); var s = Math.floor(abs / 1000); var out; if (s < 60) { out = s + ' s'; } else if (s < 3600) { out = Math.floor(s / 60) + ' min'; } else if (s < 86400) { out = Math.floor(s / 3600) + ' h'; } else { out = Math.floor(s / 86400) + ' d'; } return out + (future ? ' from now' : ' ago'); }",
  "function fmtWhen(iso) { return fmtAbs(iso) + ' (' + fmtAge(iso) + ')'; }",
  "function shellQuote(value) { var parts = value.split(String.fromCharCode(39)); var bs = String.fromCharCode(92); var q = String.fromCharCode(39); return q + parts.join(q + bs + q + q) + q; }",
  "function buildCurl(urlStr) { var abs = new URL(urlStr, location.origin).toString(); return 'curl -sS ' + shellQuote(abs); }",
  "function buildMcpCall(toolName, args) { var body = { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: toolName, arguments: args } }; return JSON.stringify(body); }",
  "function copyWithFeedback(button, text) {",
  "  var original = button.textContent;",
  "  function ok() { button.textContent = 'copied'; setTimeout(function () { button.textContent = original; }, 1500); }",
  "  function fail() {",
  "    var holder = button.parentNode;",
  "    var old = holder.querySelectorAll('.copy-fallback, .copy-fallback-msg');",
  "    for (var i = 0; i < old.length; i++) holder.removeChild(old[i]);",
  "    var msg = mkText('p', 'copy failed — text selected below, press Ctrl+C', 'error copy-fallback-msg');",
  "    var ta = mkEl('textarea', 'copy-fallback');",
  "    ta.readOnly = true;",
  "    ta.value = text;",
  "    holder.appendChild(msg);",
  "    holder.appendChild(ta);",
  "    ta.focus();",
  "    ta.select();",
  "  }",
  "  if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(ok, fail); } else { fail(); }",
  "}",
  "function renderHeadersTable(info) { var rows = []; for (var i = 0; i < HEADER_NAMES.length; i++) { var name = HEADER_NAMES[i]; var v = info.headers[name]; if (v !== undefined) rows.push(trow([td(name, 'key'), td(v)])); } if (rows.length === 0) return null; return buildTable(['header', 'value'], rows); }",
  "function buildRawBlock(info, curlLine, mcpBody, mcpEndpointLine) {",
  "  var details = mkEl('details');",
  "  var summary = mkText('summary', 'Raw JSON (what an agent gets)' + (info ? ' · HTTP ' + info.status : ''));",
  "  details.appendChild(summary);",
  "  if (!info) {",
  "    details.appendChild(mkText('p', 'network error — no response was received', 'raw-status error'));",
  "  } else {",
  "    var byteLen = new TextEncoder().encode(info.text).length;",
  "    details.appendChild(mkText('p', 'HTTP ' + info.status + ' · ' + byteLen + ' bytes as received', 'raw-status'));",
  "    var ht = renderHeadersTable(info);",
  "    if (ht) details.appendChild(ht);",
  "    var label = mkEl('label');",
  "    var cb = document.createElement('input');",
  "    cb.type = 'checkbox';",
  "    cb.checked = true;",
  "    label.appendChild(cb);",
  "    label.appendChild(document.createTextNode('pretty (unchecked: the bytes exactly as received)'));",
  "    details.appendChild(label);",
  "    var pre = mkEl('pre');",
  "    function renderPre() {",
  "      var out;",
  "      if (cb.checked) {",
  "        try { out = JSON.stringify(info.parsed, null, 2); } catch (e) { out = info.text; }",
  "      } else {",
  "        out = info.text;",
  "      }",
  "      pre.textContent = out;",
  "    }",
  "    renderPre();",
  "    cb.addEventListener('change', renderPre);",
  "    details.appendChild(pre);",
  "  }",
  "  var curlBtn = mkText('button', 'Copy curl', 'ghost');",
  "  curlBtn.type = 'button';",
  "  curlBtn.addEventListener('click', function () { copyWithFeedback(curlBtn, curlLine); });",
  "  details.appendChild(curlBtn);",
  "  var mcpBtn = mkText('button', 'Copy MCP call', 'ghost');",
  "  mcpBtn.type = 'button';",
  "  mcpBtn.addEventListener('click', function () { copyWithFeedback(mcpBtn, mcpBody); });",
  "  details.appendChild(mcpBtn);",
  "  details.appendChild(mkText('p', mcpEndpointLine, 'raw-status'));",
  "  return details;",
  "}",
  "function startCountdown(button) {",
  "  var original = button.textContent;",
  "  return function (seconds) {",
  "    var remaining = seconds;",
  "    button.disabled = true;",
  "    function tick() {",
  "      if (remaining <= 0) { button.disabled = false; button.textContent = original; return; }",
  "      button.textContent = original + ' (' + remaining + ')';",
  "      remaining -= 1;",
  "      setTimeout(tick, 1000);",
  "    }",
  "    tick();",
  "  };",
  "}",
  "function runRequest(panelKey, url, submitBtn) {",
  "  return fetch(url, { headers: { accept: 'application/json' } }).then(function (res) {",
  "    return res.text().then(function (text) {",
  "      var parsed = null;",
  "      var parseOk = true;",
  "      try { parsed = JSON.parse(text); } catch (e) { parseOk = false; }",
  "      var headers = {};",
  "      for (var i = 0; i < HEADER_NAMES.length; i++) {",
  "        var v = res.headers.get(HEADER_NAMES[i]);",
  "        if (v !== null) headers[HEADER_NAMES[i]] = v;",
  "      }",
  "      var info = { url: url, status: res.status, headers: headers, text: text, parsed: parsed };",
  "      if (!parseOk) return { kind: 'non_json', info: info };",
  "      if (res.status === 429) {",
  "        var raw = res.headers.get('retry-after');",
  "        var n = parseInt(raw, 10);",
  "        if (!isFinite(n) || n <= 0) n = 60;",
  "        if (submitBtn) startCountdown(submitBtn)(n);",
  "        return { kind: 'rate_limited', n: n, info: info };",
  "      }",
  "      if (!res.ok) {",
  "        if (parsed && typeof parsed.error === 'string') return { kind: 'app_error', info: info };",
  "        return { kind: 'http_error', info: info };",
  "      }",
  "      return { kind: 'ok', info: info };",
  "    });",
  "  }, function () {",
  "    return { kind: 'network_error', info: null };",
  "  });",
  "}",
  "function errorMessage(result) {",
  "  if (result.kind === 'network_error') return 'network error';",
  "  if (result.kind === 'non_json') return 'non-JSON body (HTTP ' + result.info.status + ')';",
  "  if (result.kind === 'rate_limited') return 'rate limited — retry in ' + result.n + ' s';",
  "  if (result.kind === 'app_error') {",
  "    var body = result.info.parsed;",
  "    var msg = result.info.status + ' ' + body.error;",
  "    if (typeof body.field === 'string') msg += ' field: ' + body.field;",
  "    if (Array.isArray(body.known)) msg += ' known: ' + body.known.join(', ');",
  "    return msg;",
  "  }",
  "  return 'HTTP ' + result.info.status;",
  "}",
  "function setStatus(el, text, isError) { el.textContent = text; el.className = isError ? 'status error' : 'status'; }",
  "function safeRender(statusEl, resultsEl, fn) { try { fn(); } catch (e) { clearNode(resultsEl); setStatus(statusEl, 'render error: ' + (e && e.message ? e.message : String(e)) + ' — see the raw JSON below', true); } }",
  "function countChips(counts) {",
  "  var wrap = mkEl('p');",
  "  var keys = Object.keys(counts).sort();",
  "  for (var i = 0; i < keys.length; i++) wrap.appendChild(mkText('span', keys[i] + ' ' + fmtNum(counts[keys[i]]), 'kind'));",
  "  return wrap;",
  "}",
  "function renderCardStatus() {",
  "  var el = byId('card-status');",
  "  clearNode(el);",
  "  if (CARD_ERROR) { el.appendChild(mkText('p', 'card load failed: ' + CARD_ERROR, 'status error')); return; }",
  "  if (!CARD) { el.appendChild(mkText('p', 'loading…', 'status')); return; }",
  "  el.appendChild(statCard('Index generated', fmtAge(CARD.generated_at), fmtAbs(CARD.generated_at)));",
  "  if (CARD.ledger) {",
  "    el.appendChild(statCard('Ledger generated', fmtAge(CARD.ledger.generated_at), fmtAbs(CARD.ledger.generated_at) + ' · ' + fmtNum(CARD.ledger.dids) + ' DIDs · ' + fmtNum(CARD.ledger.bursts) + ' bursts'));",
  "  } else {",
  "    el.appendChild(statCard('Ledger', 'none', 'this deploy has no reputation ledger'));",
  "  }",
  "  var total = 0;",
  "  var keys = Object.keys(CARD.counts);",
  "  for (var i = 0; i < keys.length; i++) total += Number(CARD.counts[keys[i]]) || 0;",
  "  el.appendChild(statCard('Documents', fmtNum(total), countChips(CARD.counts)));",
  "  var hashSmall = mkEl('p');",
  "  var fullCode = mkText('code', String(CARD.db_sha256));",
  "  fullCode.hidden = true;",
  "  var fullBtn = mkText('button', 'show full hash', 'linkbtn');",
  "  fullBtn.type = 'button';",
  "  fullBtn.addEventListener('click', function () { fullCode.hidden = !fullCode.hidden; fullBtn.textContent = fullCode.hidden ? 'show full hash' : 'hide'; });",
  "  hashSmall.appendChild(fullBtn);",
  "  hashSmall.appendChild(document.createTextNode(' '));",
  "  hashSmall.appendChild(fullCode);",
  "  el.appendChild(statCard('db_sha256', String(CARD.db_sha256).slice(0, 12) + '…', hashSmall));",
  "}",
  "function populateKindSelect() {",
  "  var sel = byId('search-kind');",
  "  var current = sel.value;",
  "  clearNode(sel);",
  "  var anyOpt = document.createElement('option');",
  "  anyOpt.value = '';",
  "  anyOpt.textContent = 'any';",
  "  sel.appendChild(anyOpt);",
  "  for (var i = 0; i < KNOWN_KINDS.length; i++) {",
  "    var opt = document.createElement('option');",
  "    opt.value = KNOWN_KINDS[i];",
  "    opt.textContent = KNOWN_KINDS[i];",
  "    sel.appendChild(opt);",
  "  }",
  "  sel.value = current;",
  "  if (sel.value !== current) sel.value = '';",
  "}",
  "function updateFragment(panel, params) {",
  "  var qs = params ? params.toString() : '';",
  "  var frag = qs ? panel + '?' + qs : panel;",
  "  history.replaceState(null, '', '#' + frag);",
  "}",
  "function renderSearchResults(body) {",
  "  var wrap = byId('search-results');",
  "  clearNode(wrap);",
  "  var n = body.results.length;",
  "  wrap.appendChild(mkText('p', 'query: ' + body.query + ' · k: ' + body.k + ' · ' + n + (n === 1 ? ' result' : ' results'), 'status'));",
  "  if (n === 0) { wrap.appendChild(mkText('p', '0 results for ' + body.query)); return; }",
  "  var rows = [];",
  "  for (var i = 0; i < n; i++) {",
  "    var r = body.results[i];",
  "    rows.push(trow([",
  "      td(String(i + 1), 'key'),",
  "      titleUrlCell(r.title, r.doc_url),",
  "      td(kindBadge(r.kind)),",
  "      td(String(r.score), 'num'),",
  "      td(r.snippet, 'pre'),",
  "    ]));",
  "  }",
  "  wrap.appendChild(buildTable(['#', 'title / url', 'kind', 'score', 'snippet'], rows));",
  "}",
  "function searchArgs() {",
  "  var q = byId('search-q').value;",
  "  var k = byId('search-k').value;",
  "  var kind = byId('search-kind').value;",
  "  var params = new URLSearchParams();",
  "  params.set('q', q);",
  "  if (k) params.set('k', k);",
  "  if (kind) params.set('kind', kind);",
  "  return params;",
  "}",
  "function runSearch() {",
  "  var params = searchArgs();",
  "  var url = location.origin + '/search?' + params.toString();",
  "  var btn = byId('search-submit');",
  "  var statusEl = byId('search-status');",
  "  var resultsEl = byId('search-results');",
  "  setStatus(statusEl, 'loading…');",
  "  updateFragment('search', params);",
  "  runRequest('search', url, btn).then(function (result) {",
  "    var q = byId('search-q').value;",
  "    var k = byId('search-k').value || '10';",
  "    var kind = byId('search-kind').value;",
  "    var mcpArgs = { q: q, k: Number(k) };",
  "    if (kind) mcpArgs.kind = kind;",
  "    if (result.kind === 'ok') {",
  "      setStatus(statusEl, '');",
  "      safeRender(statusEl, resultsEl, function () { renderSearchResults(result.info.parsed); });",
  "    } else {",
  "      setStatus(statusEl, errorMessage(result), true);",
  "      clearNode(resultsEl);",
  "    }",
  "    var wrap = byId('search-raw');",
  "    clearNode(wrap);",
  "    wrap.appendChild(buildRawBlock(result.info, buildCurl(url), buildMcpCall('search', mcpArgs), 'POST ' + location.origin + '/mcp · tools/call search'));",
  "  });",
  "}",
  "function renderFactsTable(facts) {",
  "  var keys = Object.keys(facts);",
  "  var rows = [];",
  "  for (var i = 0; i < keys.length; i++) {",
  "    var k = keys[i];",
  "    var v = facts[k];",
  "    if (v === null) {",
  "      rows.push(trow([td(k, 'key'), td('null')]));",
  "    } else if (Array.isArray(v)) {",
  "      if (v.length > 0 && Array.isArray(v[0])) {",
  "        for (var m = 0; m < v.length; m++) {",
  "          var inner = v[m].map(function (x) { return String(x); }).join(' · ');",
  "          rows.push(trow([td(k, 'key'), td(inner)]));",
  "        }",
  "      } else {",
  "        var joined = v.length === 0 ? '—' : v.map(function (x) { return String(x); }).join(', ');",
  "        rows.push(trow([td(k, 'key'), td(joined)]));",
  "      }",
  "    } else if (typeof v === 'object') {",
  "      rows.push(trow([td(k, 'key'), td(JSON.stringify(v))]));",
  "    } else {",
  "      rows.push(trow([td(k, 'key'), td(String(v))]));",
  "    }",
  "  }",
  "  return buildTable(['fact', 'value'], rows);",
  "}",
  "function renderDidResult(status, body) {",
  "  var wrap = byId('did-results');",
  "  clearNode(wrap);",
  "  if (status === 404 && body.error === 'unknown_did') {",
  "    var scope = CARD && CARD.ledger ? 'not among the ' + fmtNum(CARD.ledger.dids) + ' DIDs in the ledger built ' + CARD.ledger.generated_at + ' from the polled rooms' : 'not in the ledger';",
  "    wrap.appendChild(mkText('p', 'unknown_did — well-formed DID, ' + scope + ' — absence is not evidence', 'status'));",
  "    return;",
  "  }",
  "  if (status === 404 && body.error === 'ledger_not_built') { wrap.appendChild(mkText('p', 'ledger_not_built — this deploy has no reputation ledger', 'status')); return; }",
  "  if (typeof body.error === 'string') { wrap.appendChild(mkText('p', status + ' ' + body.error, 'status error')); return; }",
  "  var grid = mkEl('div', 'two');",
  "  var scoreSmall = mkEl('p');",
  "  scoreSmall.textContent = 'burst: ' + String(body.burst) + (body.burst ? ' · burst_id ' + String(body.burst_id) + ' · burst members score 0.0 by construction' : ' · known, non-burst') + ' · HTTP ' + status;",
  "  grid.appendChild(statCard('score', String(body.score), scoreSmall));",
  "  var fu = mkEl('div', 'reason');",
  "  fu.appendChild(mkText('span', 'facts_used', 'lbl'));",
  "  fu.appendChild(buildTable(['name', 'value'], body.facts_used.map(function (pair) { return trow([td(String(pair[0]), 'key'), td(String(pair[1]))]); })));",
  "  grid.appendChild(fu);",
  "  wrap.appendChild(mkText('p', 'did: ' + body.did, 'status'));",
  "  wrap.appendChild(grid);",
  "  wrap.appendChild(mkText('h3', 'facts'));",
  "  if (body.facts === null) {",
  "    wrap.appendChild(mkText('p', 'facts: null (burst member — the compact ledger keeps only four facts)', 'status'));",
  "  } else {",
  "    wrap.appendChild(renderFactsTable(body.facts));",
  "  }",
  "  wrap.appendChild(mkText('h3', 'provenance'));",
  "  var prov = body.provenance;",
  "  wrap.appendChild(kvTable([",
  "    ['ledger_generated_at', prov.ledger_generated_at],",
  "    ['log_rows', String(prov.log_rows)],",
  "    ['posts', String(prov.posts)],",
  "    ['dids', String(prov.dids)],",
  "    ['bursts', String(prov.bursts)],",
  "    ['schema', prov.schema],",
  "  ]));",
  "}",
  "function runDid(didValue, scroll) {",
  "  var did = didValue !== undefined ? didValue : byId('did-input').value;",
  "  var url = location.origin + '/did/' + encodeURIComponent(did);",
  "  var btn = byId('did-submit');",
  "  var statusEl = byId('did-status');",
  "  var resultsEl = byId('did-results');",
  "  setStatus(statusEl, 'loading…');",
  "  var params = new URLSearchParams();",
  "  params.set('did', did);",
  "  updateFragment('did', params);",
  "  if (scroll) { var sec = byId('did'); if (sec.scrollIntoView) sec.scrollIntoView(); }",
  "  runRequest('did', url, btn).then(function (result) {",
  "    var freshEl = byId('did-freshness');",
  "    if (result.kind === 'ok' || result.kind === 'app_error') {",
  "      setStatus(statusEl, '');",
  "      safeRender(statusEl, resultsEl, function () { renderDidResult(result.info.status, result.info.parsed); });",
  "    } else {",
  "      setStatus(statusEl, errorMessage(result), true);",
  "      clearNode(resultsEl);",
  "    }",
  "    if (result.info && result.info.headers['x-ledger-generated-at']) {",
  "      freshEl.textContent = 'X-Ledger-Generated-At: ' + result.info.headers['x-ledger-generated-at'] + ' (' + fmtAge(result.info.headers['x-ledger-generated-at']) + ')';",
  "    } else {",
  "      freshEl.textContent = '';",
  "    }",
  "    var wrap = byId('did-raw');",
  "    clearNode(wrap);",
  "    wrap.appendChild(buildRawBlock(result.info, buildCurl(url), buildMcpCall('did_lookup', { did: did }), 'POST ' + location.origin + '/mcp · tools/call did_lookup'));",
  "  });",
  "}",
  "function reasonCard(label, value, reason, note) { var box = mkEl('div', 'reason'); box.appendChild(mkText('span', label + ' · ' + value, 'lbl')); box.appendChild(mkText('b', String(reason))); box.appendChild(mkText('small', note)); return box; }",
  "function renderRouteResult(body) {",
  "  var wrap = byId('route-results');",
  "  clearNode(wrap);",
  "  wrap.appendChild(mkText('span', 'advisory: ' + String(body.advisory), 'tag'));",
  "  wrap.appendChild(mkText('p', 'observations_query: ' + body.observations_query + ' · ' + body.observations.length + ' observations', 'status'));",
  "  var grid = mkEl('div', 'two');",
  "  grid.appendChild(reasonCard('candidates', String(body.candidates.length), body.candidates_reason, 'candidates_reason, verbatim from the response'));",
  "  grid.appendChild(reasonCard('ranking', String(body.ranking), body.ranking_reason, 'ranking_reason, verbatim from the response'));",
  "  wrap.appendChild(grid);",
  "  wrap.appendChild(mkText('h3', 'query (as echoed by the server)'));",
  "  wrap.appendChild(kvTable(Object.keys(body.query).map(function (k) { return [k, String(body.query[k])]; })));",
  "  wrap.appendChild(mkText('h3', 'offer_shape'));",
  "  wrap.appendChild(kvTable([",
  "    ['published', String(body.offer_shape.published)],",
  "    ['source', body.offer_shape.source],",
  "    ['watch', body.offer_shape.watch.join(' · ')],",
  "    ['binds', body.offer_shape.binds.join(', ')],",
  "  ]));",
  "  wrap.appendChild(mkText('h3', 'observations'));",
  "  var rows = [];",
  "  for (var i = 0; i < body.observations.length; i++) {",
  "    var obs = body.observations[i];",
  "    var didsCell = mkEl('td');",
  "    for (var j = 0; j < obs.dids.length; j++) {",
  "      var entry = obs.dids[j];",
  "      var chip = mkText('button', entry.did, 'chip');",
  "      chip.type = 'button';",
  "      (function (theDid) {",
  "        chip.addEventListener('click', function () { jumpToDid(theDid); });",
  "      })(entry.did);",
  "      didsCell.appendChild(chip);",
  "      var statusText = entry.ledger && entry.ledger.error ? entry.ledger.error : ('score ' + entry.ledger.score + ' · burst ' + entry.ledger.burst);",
  "      didsCell.appendChild(mkText('span', statusText, 'hint'));",
  "    }",
  "    if (obs.dids.length === 0) didsCell.appendChild(mkText('span', '—', 'muted'));",
  "    var urlCell = mkEl('td');",
  "    urlCell.appendChild(makeLink(String(obs.url), String(obs.url)));",
  "    urlCell.appendChild(mkEl('br'));",
  "    urlCell.appendChild(kindBadge(obs.kind));",
  "    rows.push(trow([",
  "      td(String(i + 1), 'key'),",
  "      urlCell,",
  "      td(String(obs.score), 'num'),",
  "      td(obs.text, 'pre'),",
  "      didsCell,",
  "    ]));",
  "  }",
  "  wrap.appendChild(buildTable(['#', 'observation', 'score', 'text', 'DIDs mentioned'], rows));",
  "  wrap.appendChild(mkText('p', 'index_generated_at ' + body.index_generated_at + ' · ledger_generated_at ' + String(body.ledger_generated_at) + ' · click a DID chip to run it in the DID panel', 'status'));",
  "}",
  "function jumpToDid(did) {",
  "  byId('did-input').value = did;",
  "  runDid(did, true);",
  "}",
  "function routeArgs() {",
  "  var params = new URLSearchParams();",
  "  params.set('model_hash', byId('route-model-hash').value);",
  "  var precision = byId('route-precision').value;",
  "  if (precision) params.set('precision', precision);",
  "  var maxLatency = byId('route-max-latency').value;",
  "  if (maxLatency) params.set('max_latency_ms', maxLatency);",
  "  var k = byId('route-k').value;",
  "  if (k) params.set('k', k);",
  "  return params;",
  "}",
  "function runRoute() {",
  "  var params = routeArgs();",
  "  var url = location.origin + '/route?' + params.toString();",
  "  var btn = byId('route-submit');",
  "  var statusEl = byId('route-status');",
  "  var resultsEl = byId('route-results');",
  "  setStatus(statusEl, 'loading…');",
  "  updateFragment('route', params);",
  "  runRequest('route', url, btn).then(function (result) {",
  "    var mcpArgs = { model_hash: byId('route-model-hash').value, k: Number(byId('route-k').value || '10') };",
  "    var precision = byId('route-precision').value;",
  "    if (precision) mcpArgs.precision = precision;",
  "    var maxLatency = byId('route-max-latency').value;",
  "    if (maxLatency) mcpArgs.max_latency_ms = Number(maxLatency);",
  "    if (result.kind === 'ok') {",
  "      setStatus(statusEl, '');",
  "      safeRender(statusEl, resultsEl, function () { renderRouteResult(result.info.parsed); });",
  "    } else {",
  "      setStatus(statusEl, errorMessage(result), true);",
  "      clearNode(resultsEl);",
  "    }",
  "    var wrap = byId('route-raw');",
  "    clearNode(wrap);",
  "    wrap.appendChild(buildRawBlock(result.info, buildCurl(url), buildMcpCall('route', mcpArgs), 'POST ' + location.origin + '/mcp · tools/call route'));",
  "  });",
  "}",
  "function renderHealthResult(body) {",
  "  var wrap = byId('health-results');",
  "  clearNode(wrap);",
  "  var grid = mkEl('div', 'stats');",
  "  grid.appendChild(statCard('status', String(body.status), 'HTTP 200 · rate-limit exempt'));",
  "  grid.appendChild(statCard('index', fmtNum(body.index.indexed), 'indexed · failed ' + body.index.failed + ' · superseded ' + body.index.superseded + ' · refused ' + body.index.refused));",
  "  grid.appendChild(statCard('lexical', fmtNum(body.lexical.terms), 'terms · ' + fmtNum(body.lexical.docs) + ' docs · ' + fmtNum(body.lexical.postings) + ' postings'));",
  "  if (body.ledger === null) {",
  "    grid.appendChild(statCard('ledger', 'null', 'no reputation ledger in this deploy'));",
  "  } else {",
  "    grid.appendChild(statCard('ledger', fmtNum(body.ledger.dids), 'DIDs · ' + fmtNum(body.ledger.bursts) + ' bursts · ' + body.ledger.generated_at));",
  "  }",
  "  wrap.appendChild(grid);",
  "  wrap.appendChild(mkText('h3', 'kinds'));",
  "  wrap.appendChild(countChips(body.kinds));",
  "  wrap.appendChild(mkText('p', 'generated_at ' + fmtWhen(body.generated_at), 'status'));",
  "  var hashP = mkText('p', 'db_sha256 ', 'status');",
  "  hashP.appendChild(mkText('code', String(body.db_sha256)));",
  "  wrap.appendChild(hashP);",
  "}",
  "function renderCardExtra() {",
  "  var wrap = byId('health-card-extra');",
  "  clearNode(wrap);",
  "  if (!CARD) return;",
  "  var grid = mkEl('div', 'two');",
  "  var left = mkEl('div', 'reason');",
  "  left.appendChild(mkText('span', 'routes · tools (from the service card)', 'lbl'));",
  "  left.appendChild(mkText('p', CARD.routes.join(' · '), 'status'));",
  "  left.appendChild(mkText('p', CARD.tools.join(' · '), 'status'));",
  "  grid.appendChild(left);",
  "  var right = mkEl('div', 'reason');",
  "  right.appendChild(mkText('span', 'MCP endpoint', 'lbl'));",
  "  right.appendChild(mkText('p', 'POST ' + location.origin + '/mcp', 'status'));",
  "  var configText = '{\"mcpServers\":{\"openagentsearch\":{\"url\":\"' + location.origin + '/mcp\"}}}';",
  "  var pretty;",
  "  try { pretty = JSON.stringify(JSON.parse(configText), null, 2); } catch (e) { pretty = configText; }",
  "  right.appendChild(mkText('pre', pretty));",
  "  var cfgBtn = mkText('button', 'Copy client config', 'ghost');",
  "  cfgBtn.type = 'button';",
  "  cfgBtn.addEventListener('click', function () { copyWithFeedback(cfgBtn, pretty); });",
  "  right.appendChild(cfgBtn);",
  "  grid.appendChild(right);",
  "  wrap.appendChild(grid);",
  "}",
  "function runHealth() {",
  "  var url = location.origin + '/healthz';",
  "  var btn = byId('health-submit');",
  "  var statusEl = byId('health-status');",
  "  var resultsEl = byId('health-results');",
  "  setStatus(statusEl, 'loading…');",
  "  updateFragment('health', null);",
  "  runRequest('health', url, btn).then(function (result) {",
  "    if (result.kind === 'ok') {",
  "      setStatus(statusEl, '');",
  "      safeRender(statusEl, resultsEl, function () { renderHealthResult(result.info.parsed); });",
  "    } else {",
  "      setStatus(statusEl, errorMessage(result), true);",
  "      clearNode(resultsEl);",
  "    }",
  "    renderCardExtra();",
  "    var wrap = byId('health-raw');",
  "    clearNode(wrap);",
  "    wrap.appendChild(buildRawBlock(result.info, buildCurl(url), buildMcpCall('index_info', {}), 'POST ' + location.origin + '/mcp · tools/call index_info'));",
  "  });",
  "}",
  "function parseFragment(hash) {",
  "  var raw = hash.indexOf('#') === 0 ? hash.slice(1) : hash;",
  "  if (!raw) return null;",
  "  var qIdx = raw.indexOf('?');",
  "  var panel = qIdx === -1 ? raw : raw.slice(0, qIdx);",
  "  var rest = qIdx === -1 ? '' : raw.slice(qIdx + 1);",
  "  return { panel: panel, params: new URLSearchParams(rest) };",
  "}",
  "var PANEL_PARAM_NAMES = { search: ['q', 'k', 'kind'], did: ['did'], route: ['model_hash', 'precision', 'max_latency_ms', 'k'], health: [] };",
  "var PANEL_REQUIRED = { search: 'q', did: 'did', route: 'model_hash', health: null };",
  "var PANEL_INPUT_IDS = {",
  "  search: { q: 'search-q', k: 'search-k', kind: 'search-kind' },",
  "  did: { did: 'did-input' },",
  "  route: { model_hash: 'route-model-hash', precision: 'route-precision', max_latency_ms: 'route-max-latency', k: 'route-k' },",
  "};",
  "function applyFragmentAndRun(parsedFrag) {",
  "  if (!parsedFrag) return;",
  "  var panel = parsedFrag.panel;",
  "  var names = PANEL_PARAM_NAMES[panel];",
  "  if (!names) return;",
  "  var ids = PANEL_INPUT_IDS[panel];",
  "  if (ids) {",
  "    for (var i = 0; i < names.length; i++) {",
  "      var name = names[i];",
  "      var val = parsedFrag.params.get(name);",
  "      if (val !== null && ids[name]) byId(ids[name]).value = val;",
  "    }",
  "  }",
  "  var required = PANEL_REQUIRED[panel];",
  "  if (required !== null && !parsedFrag.params.has(required)) return;",
  "  if (panel === 'search') runSearch();",
  "  else if (panel === 'did') runDid(undefined, false);",
  "  else if (panel === 'route') runRoute();",
  "  else if (panel === 'health') runHealth();",
  "}",
  "function onHashChange() { applyFragmentAndRun(parseFragment(location.hash)); }",
  "function loadCard() {",
  "  return fetch(location.origin + '/?format=json', { headers: { accept: 'application/json' } }).then(function (res) {",
  "    return res.text().then(function (text) {",
  "      var parsed;",
  "      try { parsed = JSON.parse(text); } catch (e) { throw new Error('non-JSON card body (HTTP ' + res.status + ')'); }",
  "      if (!res.ok) throw new Error('HTTP ' + res.status);",
  "      CARD = parsed;",
  "      KNOWN_KINDS = Object.keys(parsed.counts).sort();",
  "    });",
  "  }).catch(function (e) {",
  "    CARD_ERROR = e && e.message ? e.message : 'network error';",
  "  });",
  "}",
  "function init() {",
  "  byId('search-form').addEventListener('submit', function (ev) { ev.preventDefault(); runSearch(); });",
  "  byId('did-form').addEventListener('submit', function (ev) { ev.preventDefault(); runDid(undefined, false); });",
  "  byId('route-form').addEventListener('submit', function (ev) { ev.preventDefault(); runRoute(); });",
  "  byId('health-form').addEventListener('submit', function (ev) { ev.preventDefault(); runHealth(); });",
  "  window.addEventListener('hashchange', onHashChange);",
  "  renderCardStatus();",
  "  loadCard().then(function () {",
  "    populateKindSelect();",
  "    renderCardStatus();",
  "    renderCardExtra();",
  "    applyFragmentAndRun(parseFragment(location.hash));",
  "  });",
  "}",
  "init();",
];

// Leading indentation inside the lines above is for readability here only; it is stripped at
// join time so the shipped page stays inside its byte budget.
export const PAGE_SCRIPT = SCRIPT_LINES.map((line) => line.trimStart()).join("\n");

// ---------------------------------------------------------------------------------------------
// PAGE_HTML -- assembled from PAGE_STYLE, PAGE_SCRIPT and the static markup below, entirely by
// string concatenation ("+"), never a template literal, so the markup is free to contain literal
// backticks (the caveat text quoted verbatim from docs/api.md uses them) without any risk of
// breaking a template literal's own delimiters.
// ---------------------------------------------------------------------------------------------

const DOCS_URL = "https://github.com/djd39448/openagentsearch/blob/main/docs/api.md";

const TOPBAR_MARKUP =
  '<div class="topbar">' +
  '<span class="brand">OPENAGENTSEARCH</span>' +
  '<nav aria-label="panels"><ul>' +
  '<li><a href="#search">Search</a></li>' +
  '<li><a href="#did">DID</a></li>' +
  '<li><a href="#route">Route</a></li>' +
  '<li><a href="#health">Health</a></li>' +
  '<li><a class="docs" href="' + DOCS_URL + '" rel="noopener noreferrer">API docs →</a></li>' +
  "</ul></nav>" +
  "</div>";

const HEADER_MARKUP =
  "<header>" +
  '<p class="eyebrow">Inspector · live routes, rendered</p>' +
  "<h1>The same four routes an agent calls, rendered.</h1>" +
  '<p class="lead">Agents get JSON on this URL; this page is the same routes, rendered. Raw JSON on every panel is byte-identical to what an agent receives.</p>' +
  '<div id="card-status" class="stats" aria-live="polite"><p class="status">loading…</p></div>' +
  "</header>";

function sectionHead(number, title, label) {
  return '<div class="sec-head"><h2>' + number + " &nbsp;" + title + '</h2><span class="lbl">' + label + "</span></div>";
}

const SEARCH_MARKUP =
  '<section id="search">' +
  sectionHead("01", "Search", "GET /search · MCP tool search") +
  '<form id="search-form">' +
  '<div class="f grow"><label for="search-q">q</label>' +
  '<input type="text" id="search-q" name="q" required maxlength="512"></div>' +
  '<div class="f"><label for="search-k">k</label>' +
  '<input type="number" id="search-k" name="k" min="1" max="50" value="10"></div>' +
  '<div class="f"><label for="search-kind">kind</label>' +
  '<select id="search-kind" name="kind"><option value="">any</option></select></div>' +
  '<button type="submit" id="search-submit">Run</button>' +
  "</form>" +
  '<p id="search-status" class="status" aria-live="polite"></p>' +
  '<div id="search-results"></div>' +
  '<p class="caveat">Lexical, not semantic. Ranking is BM25 keyword overlap; there is no embedding model behind `/search` or the `search` tool, and no notion of synonymy or paraphrase.</p>' +
  '<p class="caveat">Forward-only data. Every document in the index is whatever `pipeline.publish` last exported; nothing here is a live crawl or a live feed. `generated_at` says exactly how stale a given deploy is.</p>' +
  '<div id="search-raw"></div>' +
  "</section>";

const DID_MARKUP =
  '<section id="did">' +
  sectionHead("02", "DID lookup", "GET /did/{did} · MCP tool did_lookup") +
  '<form id="did-form">' +
  '<div class="f grow"><label for="did-input">did</label>' +
  '<input type="text" id="did-input" name="did" required maxlength="200">' +
  '<span class="hint">did:key:z… (1–120 base58 characters); the server is the validator (a malformed value shows the 400 an agent gets)</span></div>' +
  '<button type="submit" id="did-submit">Run</button>' +
  "</form>" +
  '<p id="did-status" class="status" aria-live="polite"></p>' +
  '<div id="did-results"></div>' +
  '<p class="caveat">No signature verification, ever. A `200 /did/{did}` answer relays exactly what `openagentsearch.reputation` computed from the message log — nothing anywhere in this repository verifies a post\'s `sig` against its `sender` (see docs/reputation.md\'s "What this is NOT"). A score is evidence, never an endorsement, and a Worker or server deployed without the compact ledger artifact still answers `ledger_not_built` for every syntactically valid `did:key`. Burst members score 0.0 by construction.</p>' +
  '<p class="caveat" id="did-freshness" aria-live="polite"></p>' +
  '<div id="did-raw"></div>' +
  "</section>";

const ROUTE_MARKUP =
  '<section id="route">' +
  sectionHead("03", "Route", "GET /route · MCP tool route") +
  '<form id="route-form">' +
  '<div class="f grow"><label for="route-model-hash">model_hash</label>' +
  '<input type="text" id="route-model-hash" name="model_hash" required maxlength="128"></div>' +
  '<div class="f"><label for="route-precision">precision</label>' +
  '<input type="text" id="route-precision" name="precision" maxlength="32"></div>' +
  '<div class="f"><label for="route-max-latency">max_latency_ms</label>' +
  '<input type="number" id="route-max-latency" name="max_latency_ms" min="1" max="600000"></div>' +
  '<div class="f"><label for="route-k">k</label>' +
  '<input type="number" id="route-k" name="k" min="1" max="50" value="10"></div>' +
  '<button type="submit" id="route-submit">Run</button>' +
  "</form>" +
  '<p id="route-status" class="status" aria-live="polite"></p>' +
  '<div id="route-results"></div>' +
  '<p class="caveat">advisory: these are NOT candidates and NOT ranked against each other in any FLOP-aware sense — `score` is plain BM25 or cosine relevance to the query tokens, nothing more.</p>' +
  '<p class="caveat">mentions, not authorship: a DID appearing in a document\'s abstract says nothing about who wrote it.</p>' +
  '<p class="caveat">`max_latency_ms` (optional, ASCII digits only, `1..600000` — accepted and echoed back in `query`, never used: no latency facts exist anywhere in this repository).</p>' +
  '<p class="caveat">a ledger body is evidence from one message log, never an endorsement.</p>' +
  '<div id="route-raw"></div>' +
  "</section>";

const HEALTH_MARKUP =
  '<section id="health">' +
  sectionHead("04", "Health", "GET /healthz · MCP tool index_info") +
  '<form id="health-form">' +
  '<button type="submit" id="health-submit">Run</button>' +
  "</form>" +
  '<p id="health-status" class="status" aria-live="polite"></p>' +
  '<div id="health-results"></div>' +
  '<div id="health-card-extra"></div>' +
  '<p class="caveat">Not the same as the full manifest. `index.failed`, `index.superseded` and `index.refused` are always `0` here.</p>' +
  '<p class="caveat">`generated_at` in every response is the authoritative freshness signal — expect it to move once a day, and read a stale value as "the last refresh did not complete", not as "the service is down".</p>' +
  '<div id="health-raw"></div>' +
  "</section>";

const FOOTER_MARKUP =
  '<footer class="foot">' +
  "<span>openagentsearch · advisory, evidence not endorsement · 60 requests / 60 s per IP (/ and /healthz exempt)</span>" +
  "<span>" +
  '<a href="' + DOCS_URL + '" rel="noopener noreferrer">docs/api.md →</a>' +
  '<a href="https://djd39448.github.io/openagentsearch/" rel="noopener noreferrer">static index →</a>' +
  '<a href="/?format=json">JSON card →</a>' +
  "</span>" +
  "</footer>";

const BODY_MARKUP = TOPBAR_MARKUP + HEADER_MARKUP + SEARCH_MARKUP + DID_MARKUP + ROUTE_MARKUP + HEALTH_MARKUP + FOOTER_MARKUP;

export const PAGE_HTML =
  "<!doctype html>\n" +
  '<html lang="en">' +
  "<head>" +
  '<meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1">' +
  '<meta name="color-scheme" content="dark">' +
  "<title>OpenAgentSearch inspector</title>" +
  '<link rel="alternate" type="application/json" href="/?format=json">' +
  "<style>" + PAGE_STYLE + "</style>" +
  "</head>" +
  "<body>" +
  BODY_MARKUP +
  "<script>" + PAGE_SCRIPT + "</script>" +
  "</body>" +
  "</html>\n";

// ---------------------------------------------------------------------------------------------
// Hash pins for the CSP's script-src/style-src (computed offline via node:crypto -- see the
// regeneration recipe in the module header comment above).
// ---------------------------------------------------------------------------------------------

export const PAGE_STYLE_SHA256 = "4RfcEMggLib82F2Hsb3wXowQp2t5QsNbjBTUHslHTyE=";
export const PAGE_SCRIPT_SHA256 = "D8FyOLV+NNXIaK0bPQQxvr2vGipLOmpdeJM1sHFL3zE=";
