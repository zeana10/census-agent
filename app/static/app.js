"use strict";
const log = document.getElementById('log');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
let sessionId = null;
try { sessionId = localStorage.getItem('census_session'); } catch (e) {}
// A random id for this browser: scopes conversations and long-term memory. Not a login.
let clientId = null;
try { clientId = localStorage.getItem('census_client'); } catch (e) {}
if (!/^[0-9a-f]{32}$/.test(clientId || '')) {
  clientId = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  try { localStorage.setItem('census_client', clientId); } catch (e) {}
}
let busy = false;
let controller = null;
// Outcomes where the answer is an apology rather than an answer: offer a retry.
const FAILED = new Set(['timeout', 'llm_error', 'warehouse_unavailable', 'internal_error', 'degraded', 'rate_limited', 'busy']);

function setBusy(b) {
  busy = b;
  sendBtn.textContent = b ? 'Stop' : 'Send';
  sendBtn.classList.toggle('stop', b);
  sendBtn.setAttribute('aria-label', b ? 'Stop answering' : 'Send');
}

function addMeta(m, { requestId, retryQuestion }) {
  const row = document.createElement('div'); row.className = 'meta';
  if (retryQuestion) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = 'Retry';
    b.addEventListener('click', () => { row.remove(); ask(retryQuestion); });
    row.appendChild(b);
  }
  if (requestId) {
    const ref = document.createElement('span'); ref.textContent = 'Ref: ' + requestId;
    ref.title = 'Quote this reference when reporting a problem';
    row.appendChild(ref);
  }
  if (row.childNodes.length) m.el.appendChild(row);
}

function saveSession(id) { sessionId = id; try { localStorage.setItem('census_session', id); } catch (e) {} }
function md(text) { return DOMPurify.sanitize(marked.parse(text || '')); }
function scroll() { window.scrollTo({ top: document.body.scrollHeight }); }

function addUser(text) {
  const d = document.createElement('div'); d.className = 'msg user'; d.textContent = text; log.appendChild(d); scroll();
}
function addAssistant(text) {
  const d = document.createElement('div'); d.className = 'msg';
  const body = document.createElement('div'); body.className = 'body'; body.innerHTML = md(text || '');
  const st = document.createElement('div'); st.className = 'status';
  const tables = document.createElement('div');
  d.append(body, tables, st); log.appendChild(d); scroll();
  return { el: d, body, st, tables, text: text || '', queries: [] };
}

// A full query result, rendered from the data itself (never retyped by the model), with downloads.
function addTable(m, t) {
  const box = document.createElement('div'); box.className = 'rtable';
  const head = document.createElement('div'); head.className = 'rtable-head';
  const title = document.createElement('span'); title.className = 'title';
  title.textContent = (t.purpose || 'Query result') + ' · ' + t.rows.length.toLocaleString() + ' rows'
    + (t.truncated ? ' (first ' + t.max_rows.toLocaleString() + ' only)' : '');
  const dl = document.createElement('span'); dl.className = 'dl';
  for (const [fmt, label] of [['xlsx', 'Download Excel'], ['csv', 'Download CSV']]) {
    const a = document.createElement('a');
    a.href = '/api/tables/' + encodeURIComponent(t.session_id) + '/' + encodeURIComponent(t.id) + '.' + fmt;
    a.textContent = label; a.setAttribute('download', '');
    dl.appendChild(a);
  }
  head.append(title, dl);
  const scrollBox = document.createElement('div'); scrollBox.className = 'rtable-scroll';
  const table = document.createElement('table');
  const thead = table.createTHead().insertRow();
  for (const c of t.columns) { const th = document.createElement('th'); th.textContent = c; thead.appendChild(th); }
  const tbody = table.createTBody();
  for (const row of t.rows) {
    const tr = tbody.insertRow();
    for (const v of row) {
      const td = tr.insertCell();
      if (typeof v === 'number') { td.className = 'num'; td.textContent = Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 }); }
      else td.textContent = v == null ? '' : String(v);
    }
  }
  scrollBox.appendChild(table);
  box.append(head, scrollBox);
  m.tables.appendChild(box); scroll();
}
function setStatus(m, s) { m.st.innerHTML = s ? '<span class="dot"></span>' : ''; if (s) m.st.append(s); }
function render(m) { m.body.innerHTML = md(m.text); scroll(); }
function addQueries(m) {
  if (!m.queries.length) return;
  const det = document.createElement('details');
  det.innerHTML = '<summary>How I got this (' + m.queries.length + ' quer' + (m.queries.length === 1 ? 'y' : 'ies') + ')</summary>';
  for (const q of m.queries) {
    const p = document.createElement('div');
    p.textContent = (q.ok ? '✓ ' : '✗ ') + (q.purpose || 'query') + (q.ok && q.rows != null ? ` (${q.rows} rows, ${q.ms} ms)` : q.ok ? '' : ' (failed, retried)');
    const pre = document.createElement('pre'); pre.textContent = q.sql;
    det.append(p, pre);
  }
  if (m.queries.some(q => q.capped)) {
    const n = document.createElement('div'); n.className = 'note';
    n.textContent = 'Queries also run with an automatic row limit as a safety measure; it is not shown above.';
    det.appendChild(n);
  }
  m.el.appendChild(det);
}

async function ask(question) {
  if (busy || !question.trim()) return;
  setBusy(true);
  controller = new AbortController();
  document.getElementById('intro')?.remove();
  addUser(question);
  const m = addAssistant('');
  setStatus(m, 'Thinking…');
  let outcome = null, requestId = null;
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, client_id: clientId, message: question }),
      signal: controller.signal,
    });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        let ev = 'message', data = '';
        for (const line of chunk.split('\n')) {
          if (line.startsWith('event: ')) ev = line.slice(7);
          else if (line.startsWith('data: ')) data += line.slice(6);
        }
        if (!data) continue;
        const d = JSON.parse(data);
        if (ev === 'session') saveSession(d.session_id);
        else if (ev === 'status') setStatus(m, d.text);
        else if (ev === 'delta') { m.text += d.text; render(m); }
        else if (ev === 'reset') { m.text = ''; render(m); }
        else if (ev === 'replace') { m.text = d.text; render(m); }
        else if (ev === 'sql') m.queries.push(d);
        else if (ev === 'table') addTable(m, d);
        else if (ev === 'memory') addMemoryNote(m, d);
        else if (ev === 'done') { setStatus(m, ''); addQueries(m); outcome = d.outcome; requestId = d.request_id; }
      }
    }
    if (!m.text) { m.text = '_No answer was returned._'; render(m); outcome = outcome || 'internal_error'; }
    if (FAILED.has(outcome)) { m.body.classList.add('err'); addMeta(m, { requestId, retryQuestion: question }); }
  } catch (e) {
    if (e.name === 'AbortError') {
      // The server discards a cancelled turn, so it is not part of the conversation history.
      m.text += (m.text ? '\n\n' : '') + '_Stopped. This question was not added to the conversation._';
      render(m); addMeta(m, { retryQuestion: question });
    } else {
      m.text = 'I couldn\'t reach the server (' + e.message + '). Check your connection and try again.';
      render(m); m.body.classList.add('err'); addMeta(m, { retryQuestion: question });
    }
  } finally {
    setStatus(m, ''); setBusy(false); controller = null; input.focus();
  }
}

document.getElementById('form').addEventListener('submit', e => {
  e.preventDefault();
  if (busy) { controller?.abort(); return; }  // the button reads "Stop" while answering
  const q = input.value; input.value = ''; autosize(); ask(q);
});
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!busy) document.getElementById('form').requestSubmit(); }
  if (e.key === 'Escape' && busy) controller?.abort();
});
function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 160) + 'px'; }
input.addEventListener('input', autosize);
document.querySelectorAll('.examples button').forEach(b => b.addEventListener('click', () => ask(b.textContent)));
document.getElementById('newchat').addEventListener('click', async () => {
  if (busy) return;
  const r = await fetch('/api/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId || '', client_id: clientId }) });
  if (r.ok) { saveSession((await r.json()).session_id); location.reload(); }
});

// ---- long-term memory ----------------------------------------------------------
function addMemoryNote(m, d) {
  const n = document.createElement('div'); n.className = 'memnote';
  n.textContent = d.action === 'saved' ? 'Saved to memory: ' + d.memory.text : 'Removed from memory.';
  m.el.insertBefore(n, m.st);
  if (document.getElementById('mempanel').classList.contains('open')) loadMemories();
}
async function loadMemories() {
  const list = document.getElementById('memlist'); list.textContent = '';
  try {
    const r = await fetch('/api/memory?client_id=' + clientId);
    const { memories } = await r.json();
    if (!memories.length) { const li = document.createElement('li'); li.textContent = 'Nothing yet.'; list.appendChild(li); }
    for (const mem of memories) {
      const li = document.createElement('li');
      const text = document.createElement('span');
      const kind = document.createElement('span'); kind.className = 'kind'; kind.textContent = mem.kind;
      text.append(kind, document.createTextNode(mem.text));
      const del = document.createElement('button'); del.type = 'button'; del.textContent = 'Forget';
      del.addEventListener('click', async () => {
        await fetch('/api/memory/' + mem.id + '?client_id=' + clientId, { method: 'DELETE' }); loadMemories();
      });
      li.append(text, del); list.appendChild(li);
    }
  } catch (e) { const li = document.createElement('li'); li.textContent = 'Couldn\'t load memories.'; list.appendChild(li); }
}
document.getElementById('memorybtn').addEventListener('click', e => {
  const panel = document.getElementById('mempanel');
  const open = panel.classList.toggle('open');
  e.currentTarget.setAttribute('aria-expanded', String(open));
  if (open) loadMemories();
});
document.getElementById('memclear').addEventListener('click', async () => {
  if (!confirm('Forget everything the assistant remembers about you?')) return;
  await fetch('/api/memory?client_id=' + clientId, { method: 'DELETE' }); loadMemories();
});

// Restore an existing conversation after a page reload.
(async () => {
  if (!sessionId) return;
  try {
    const r = await fetch('/api/history?session_id=' + encodeURIComponent(sessionId) + '&client_id=' + clientId);
    if (!r.ok) return;
    const h = await r.json();
    saveSession(h.session_id);
    if (h.messages.length) document.getElementById('intro')?.remove();
    for (const msg of h.messages) {
      if (msg.role === 'user') { addUser(msg.text); continue; }
      const m = addAssistant(msg.text);
      for (const t of msg.tables || []) addTable(m, t);  // tables come back from the server's store
    }
  } catch (e) {}
})();
