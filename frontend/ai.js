/* ParseFlow — AI features in the UI: chat-style Ask, "Read with AI" for visuals, Connect AI.
   Loaded after index.html's main script; uses its globals (DOC, BE, tab, $, esc, escA, renderTabs,
   source, renderTex, srvDoc, ask). API keys never reach the browser: the key is posted once to the
   local server, which stores it in .env and only ever reports configured / provider / model. */

let CHAT = [], CHAT_BUSY = false, RA = new Set(), RA_ERR = {}, RA_ALL = null;

/* ---------- helpers ---------- */
const aiOn = () => !!(BE && BE.llm && BE.llm.configured);
const visionOn = () => !!(BE && BE.vision && BE.vision.configured);
const provName = p => ({ anthropic: 'Claude', ollama: 'Local', groq: 'Groq', openai: 'OpenAI', openrouter: 'OpenRouter', compatible: 'Custom' })[p] || 'Gemini';
const aiName = () => BE && BE.llm && BE.llm.configured ? provName(BE.llm.provider) + ' · ' + BE.llm.model : '';
function refreshHealth() { return fetch('/api/health').then(r => r.json()).then(h => { if (h && h.ok) BE = h; capBadge(); aiButton(); }).catch(() => {}); }

/* ---------- tabs: hide Handwritten when there is none ---------- */
function visTabs() {
  const hw = DOC && DOC.blocks && DOC.blocks.some(isHW);
  if (!hw && tab === 'Handwritten') tab = 'Visuals';
  return TABS.filter(t => t !== 'Handwritten' || hw);
}

/* ---------- small, safe Markdown renderer for answers ---------- */
function mdToHtml(src, cites) {
  const code = [];
  let t = String(src || '').replace(/```[a-zA-Z0-9]*\n?([\s\S]*?)```/g, (_, c) => { code.push(c); return '\u0000C' + (code.length - 1) + '\u0000'; });
  const disp = [];
  t = t.replace(/\$\$([\s\S]+?)\$\$/g, (_, m) => { disp.push(m); return '\u0000D' + (disp.length - 1) + '\u0000'; });
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<i>$2</i>')
    .replace(/\$([^$\n]{1,200})\$/g, (_, m) => `<span class="texi" data-texi="${escA(m)}">${esc(m)}</span>`)
    .replace(/\[(S\d+)\]/g, (m, s) => cites && cites[s] ? `<button class="cite" title="${escA(cites[s].title)}" onclick="source('${cites[s].block}')">${s}</button>` : '')
    .replace(/\(general knowledge\)/gi, '<span class="gk">general knowledge</span>');
  const lines = t.split('\n'), out = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    let m;
    if (/^\u0000C\d+\u0000$/.test(l.trim())) { out.push(`<pre class="code">${esc(code[+l.trim().slice(2, -1)])}</pre>`); i++; continue; }
    if (/^\u0000D\d+\u0000$/.test(l.trim())) { out.push(`<div class="tex" data-tex="${escA(disp[+l.trim().slice(2, -1)])}">${esc(disp[+l.trim().slice(2, -1)])}</div>`); i++; continue; }
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { const h = Math.min(4, m[1].length + 2); out.push(`<h${h}>${inline(m[2])}</h${h}>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const row = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const head = row(l); i += 2; const body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
      out.push(`<div class="tw"><table class="t"><tr>${head.map(c => `<th>${inline(c)}</th>`).join('')}</tr>${body.map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</table></div>`);
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d+[.)]/.test(l), items = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        let it = lines[i++].replace(/^\s*([-*•]|\d+[.)])\s+/, '');
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) it += ' ' + lines[i++].trim();
        items.push(`<li>${inline(it)}</li>`);
      }
      out.push(ordered ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`);
      continue;
    }
    if (/^>\s?/.test(l)) { const q = []; while (i < lines.length && /^>\s?/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/, '')); out.push(`<blockquote>${inline(q.join(' '))}</blockquote>`); continue; }
    if (!l.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*([-*•]|\d+[.)])\s+|>\s?|\s*\|)/.test(lines[i]) && !/^\u0000/.test(lines[i].trim())) para.push(lines[i++]);
    if (!para.length) { out.push(`<p>${inline(l)}</p>`); i++; continue; }
    out.push(`<p>${para.map(inline).join('<br>')}</p>`);
  }
  return out.join('');
}

/* ---------- chat ---------- */
const SUGGEST_DOC = ['Summarize this document', 'Explain this to a beginner', 'What are the key steps or procedures?', 'Generate three practice questions', 'Explain the main table or figure'];
const SUGGEST_DEMO = ['What rotation is shown in the diagram?', 'What does page 2 explain?', 'What was the total revenue in FY24?', 'What does the handwritten note say?'];

function newChat() { CHAT = []; renderTabs(); }

function answerQ(q) {
  q = (q || '').trim();
  if (!q || CHAT_BUSY) return;
  const history = CHAT.filter(m => !m.pending && !m.error && m.content).map(m => ({ role: m.role, content: m.content })).slice(-8);
  CHAT.push({ role: 'user', content: q });
  if (!srvDoc()) {                       // browser-only / demo mode: local keyword search, labelled as such
    const r = ask(q);
    CHAT.push({ role: 'assistant', local: true, content: r ? r.ans : "I couldn't find this in the extracted blocks.", blocks: r ? r.top : [] });
    renderTabs(); return;
  }
  const msg = { role: 'assistant', pending: true, content: '' };
  CHAT.push(msg); CHAT_BUSY = true; renderTabs();
  const body = { document_id: DOC.document_id, question: q, history };
  if (BE && BE.mode === 'serverless') body.document = Object.assign({}, DOC, { _md: undefined });
  fetch('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(r => r.json())
    .then(s => {
      msg.pending = false;
      if (s.error) { msg.error = s.error.message || String(s.error); msg.content = ''; return; }
      Object.assign(msg, { content: s.answer || '', mode: s.mode, model: s.model, sources: s.sources || [], warnings: s.warnings || [], cited: s.cited || [] });
    })
    .catch(() => { msg.pending = false; msg.error = 'The ParseFlow server could not be reached.'; })
    .finally(() => { CHAT_BUSY = false; if (tab === 'Ask') { renderTabs(); const box = $('chatlog'); if (box) box.scrollTop = box.scrollHeight; } });
}

function chatMsg(m, k) {
  if (m.role === 'user') return `<div class="msg me"><div class="bub">${esc(m.content).replace(/\n/g, '<br>')}</div></div>`;
  if (m.pending) return `<div class="msg ai"><div class="av">PF</div><div class="bub"><span class="typing"><i></i><i></i><i></i></span> <span class="muted">${aiOn() ? 'Reading the document and thinking…' : 'Searching the document…'}</span></div></div>`;
  if (m.error) return `<div class="msg ai"><div class="av">PF</div><div class="bub"><div class="warn">${esc(m.error)}</div></div></div>`;
  if (m.local) return `<div class="msg ai"><div class="av">PF</div><div class="bub"><p>${esc(m.content)}</p>${(m.blocks || []).map(b => `<button class="cite" onclick="source('${b.block_id}')">p${b.page}</button>`).join(' ')}<div class="meta">Keyword search in your browser — no AI model was used.</div></div></div>`;
  const cites = {};
  (m.sources || []).forEach(s => { cites[s.label] = { block: (s.block_ids || [])[0], title: `page ${(s.pages || []).join(', ')}${s.section ? ' · ' + s.section : ''}` }; });
  const llm = m.mode === 'llm';
  const shown = llm ? (m.sources || []).filter(s => (m.cited || []).includes(s.label)) : (m.sources || []);
  const srcList = shown.map(s => `<div class="src"><div class="flow"><span>[${s.label}] page ${s.pages.join(', ')}${s.section ? ' · ' + esc(s.section) : ''}</span><button class="btn sm" onclick="source('${(s.block_ids || [])[0]}')">View Source</button></div><div class="muted" style="font-size:12.5px;margin-top:3px">${esc(s.excerpt)}${s.excerpt.length >= 400 ? '…' : ''}</div></div>`).join('');
  const meta = llm ? `Answered by ${esc(m.model || 'AI')} from ${(m.cited || []).length} cited passage${(m.cited || []).length === 1 ? '' : 's'} · general knowledge is labelled`
    : `Search only — no AI model was called.`;
  const warn = (m.warnings || []).filter(w => !/No AI connected/.test(w)).map(w => `<div class="note">${esc(w)}</div>`).join('');
  return `<div class="msg ai"><div class="av">PF</div><div class="bub">${mdToHtml(m.content, cites)}
    ${!llm ? `<div style="margin:8px 0"><button class="btn sm pri" onclick="openAI()">Connect AI for explained answers</button></div>` : ''}
    ${warn}<div class="meta">${meta}</div>
    ${shown.length ? `<details class="srcs"><summary>${llm ? 'Sources' : 'Matching passages'} (${shown.length})</summary>${srcList}</details>` : ''}</div></div>`;
}

function renderAsk(p) {
  const sugg = (DOC.demo ? SUGGEST_DEMO : SUGGEST_DOC);
  const status = srvDoc() ? (aiOn() ? `<span class="aichip on" title="${escA(aiName())}">● ${provName(BE.llm.provider)}</span>` : `<button class="aichip off" onclick="openAI()">○ Connect AI</button>`) : `<span class="aichip off">Search only</span>`;
  p.innerHTML = `<div class="chat">
    <div class="chathead"><b style="white-space:nowrap">Ask ParseFlow</b>${status}${CHAT.length ? `<button class="btn sm" style="margin-left:auto" onclick="newChat()">New chat</button>` : ''}</div>
    <div class="chatlog" id="chatlog">${CHAT.length ? CHAT.map(chatMsg).join('') : `<div class="welcome"><div class="av big">PF</div><h3>Ask anything about ${esc(DOC.filename || 'this document')}</h3>
      <p class="muted">${aiOn() ? 'Answers explain and teach, cite the pages they come from, and label anything that comes from general knowledge.' : 'Without an AI key this is a plain search. Connect a free Gemini key to get real, explained answers.'}</p>
      <div class="chips">${sugg.map(q => `<button onclick="answerQ('${escA(q)}')">${esc(q)}</button>`).join('')}</div></div>`}</div>
    <div class="composer"><textarea id="qi" rows="1" placeholder="Message ParseFlow…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();if(this.value.trim()){const v=this.value;this.value='';answerQ(v)}}" oninput="this.style.height='auto';this.style.height=Math.min(160,this.scrollHeight)+'px'"></textarea><button class="btn pri send" ${CHAT_BUSY ? 'disabled' : ''} onclick="const i=$('qi');if(i.value.trim()){const v=i.value;i.value='';answerQ(v)}">Send</button></div>
  </div>`;
  const box = $('chatlog'); if (box) box.scrollTop = box.scrollHeight;
  renderTex();
  const qi = $('qi'); if (qi && !CHAT_BUSY) qi.focus();
}

/* ---------- visuals: read with AI ---------- */
const VT_LABEL = { chart: 'Chart', diagram: 'Diagram', flowchart: 'Flowchart', text_image: 'Image with text', picture: 'Picture', photo: 'Photo',
  figure: 'Figure', screenshot: 'Screenshot', drawing: 'Drawing', handwriting: 'Handwriting', table: 'Table', equation: 'Equation' };

function visActions(b) {
  if (!srvDoc()) return '';
  const copy = b.content ? `<button class="btn sm" onclick="navigator.clipboard.writeText(DOC.blocks.find(x=>x.block_id==='${b.block_id}').content||'');this.textContent='Copied ✓'">Copy text</button>` : '';
  let ai;
  if (RA.has(b.block_id)) ai = `<button class="btn sm" disabled><span class="typing"><i></i><i></i><i></i></span> Reading with AI…</button>`;
  else if (b.ai_read) ai = `<span class="aichip on">Read by AI</span><button class="btn sm" onclick="readAI('${b.block_id}')">Re-read</button>`;
  else if (visionOn()) ai = `<button class="btn sm pri" onclick="readAI('${b.block_id}')">Read with AI</button>`;
  else ai = `<button class="btn sm pri" onclick="openAI()">Connect AI to read this image</button>`;
  const e = RA_ERR[b.block_id] ? `<div class="warn">${esc(RA_ERR[b.block_id])}</div>` : '';
  return `${e}<div class="flow" style="gap:6px;margin-top:8px;flex-wrap:wrap">${ai}${copy}<button class="btn sm" onclick="source('${b.block_id}')">View Source</button></div>`;
}

function visHeader(L) {
  if (!srvDoc() || !L.length) return '';
  const unread = L.filter(b => !b.ai_read);
  if (RA_ALL) return `<div class="vhead"><span class="typing"><i></i><i></i><i></i></span> Reading images with AI… ${RA_ALL.done}/${RA_ALL.total}<button class="btn sm" style="margin-left:auto" onclick="RA_ALL.stop=true">Stop</button></div>`;
  if (!visionOn()) return `<div class="vhead">Connect AI to read every image (screenshots, charts, diagrams, handwriting) and describe it.<button class="btn sm pri" style="margin-left:auto" onclick="openAI()">Connect AI</button></div>`;
  const slow = BE.vision.local ? ' Local model: about a minute per image.' : '';
  return unread.length ? `<div class="vhead">${unread.length} image${unread.length > 1 ? 's' : ''} not yet read by AI. Handwritten notes move to the Handwritten tab once read.${slow}<button class="btn sm pri" style="margin-left:auto" onclick="readAllAI()">Read all with AI</button></div>`
    : `<div class="vhead">All images were read by AI (${esc(BE.vision.model)}). Text marked “Review” is self-reported by the model.</div>`;
}

function _refreshMd() {
  return fetch('/api/document/' + DOC.document_id + '/markdown').then(r => r.ok ? r.text() : null).then(t => { if (t) DOC._md = t; }).catch(() => {});
}

function readAI(id, quiet) {
  RA.add(id); delete RA_ERR[id]; if (!quiet && tab === 'Visuals') renderTabs();
  return fetch(`/api/visual/${DOC.document_id}/${id}`, { method: 'POST' }).then(r => r.json()).then(s => {
    if (s.error) { RA_ERR[id] = s.error.message; return false; }
    const i = DOC.blocks.findIndex(x => x.block_id === id);
    if (i >= 0) DOC.blocks[i] = s.block;
    return true;
  }).catch(() => { RA_ERR[id] = 'The ParseFlow server could not be reached.'; return false; })
    .finally(() => { RA.delete(id); if (!quiet) { _refreshMd(); if (tab === 'Visuals' || tab === 'Handwritten' || tab === 'Inspector') renderTabs(); } });
}

async function readAllAI() {
  const L = DOC.blocks.filter(b => (isViz(b) || isHW(b)) && !b.ai_read && b.type !== 'equation');
  RA_ALL = { done: 0, total: L.length, stop: false }; renderTabs();
  for (const b of L) {
    if (RA_ALL.stop) break;
    const ok = await readAI(b.block_id, true);
    RA_ALL.done++;
    if (!ok && /rate-limit|429/.test(RA_ERR[b.block_id] || '')) { await new Promise(r => setTimeout(r, 15000)); }
    if (tab === 'Visuals') renderTabs();
  }
  RA_ALL = null; await _refreshMd(); renderTabs();
}

/* ---------- Connect AI ---------- */
function aiButton() {
  const host = document.querySelector('#app .top');
  if (!host) return;
  let b = $('aibtn');
  if (!b) {
    b = document.createElement('button'); b.id = 'aibtn'; b.className = 'btn sm aibtn';
    b.onclick = openAI;
    const go = host.querySelector('.go'); host.insertBefore(b, go || null);
  }
  b.style.display = BE ? '' : 'none';
  b.innerHTML = aiOn() ? `<span class="dot on"></span>AI: ${esc(provName(BE.llm.provider))}` : `Connect AI`;
}

function openAI() {
  let m = $('aimodal');
  if (!m) { m = document.createElement('div'); m.id = 'aimodal'; m.className = 'modalbg'; m.onclick = e => { if (e.target === m) closeAI(); }; document.body.appendChild(m); }
  const editable = BE && BE.ai_settings_editable;
  m.innerHTML = `<div class="modal" role="dialog" aria-label="Connect AI">
    <div class="flow"><h3 style="margin:0">Connect AI</h3><button class="btn sm" style="margin-left:auto" onclick="closeAI()">✕</button></div>
    <p class="muted">One key makes ParseFlow read the text in every image (screenshots, charts, diagrams, handwriting) and answer your questions like a chat assistant. The key is stored only on the computer running ParseFlow (in <code>.env</code>) and is never sent back to the browser.</p>
    ${aiOn() ? `<div class="okbox">Connected: <b>${esc(aiName())}</b></div>` : ''}
    ${editable ? `
    <div class="prov">
      <label><input type="radio" name="prov" value="gemini" checked> <b>Google Gemini</b> <span class="muted">— free tier. Get a key at <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">aistudio.google.com/apikey</a> (starts with AIza)</span></label>
      <label><input type="radio" name="prov" value="groq"> <b>Groq</b> <span class="muted">— free tier, fast, and a good second choice when Google is busy. Key from <a href="https://console.groq.com/keys" target="_blank" rel="noopener">console.groq.com/keys</a> (starts with gsk_)</span></label>
      <label><input type="radio" name="prov" value="anthropic"> <b>Anthropic Claude</b> <span class="muted">— paid. Key from <a href="https://console.anthropic.com" target="_blank" rel="noopener">console.anthropic.com</a> (starts with sk-ant-)</span></label>
      <label><input type="radio" name="prov" value="openai"> <b>OpenAI</b> <span class="muted">— paid. Key from <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">platform.openai.com</a> (starts with sk-)</span></label>
      <label><input type="radio" name="prov" value="ollama"> <b>Local AI (Ollama)</b> <span class="muted">— free, offline, no key. Runs a model on this computer (slower: about a minute per image on a laptop CPU).</span></label>
    </div>
    <div id="aikeybox"><input id="aikey" type="password" autocomplete="off" placeholder="Paste your API key" style="width:100%"></div>
    <div id="aolbox" style="display:none"></div>
    <div id="aimsg" class="muted" style="min-height:20px;margin:8px 0"></div>
    <div class="flow" style="gap:8px;flex-wrap:wrap"><button class="btn pri" id="aisave" onclick="saveAI()">Save and test</button>${aiOn() ? `<button class="btn" id="aicheck" onclick="checkAI()">Test all models</button><button class="btn" onclick="clearAI()">Disconnect</button>` : ''}</div>
    <div id="aicheckout"></div>`
    : `<p>This deployment is hosted, so the key is set by whoever runs it: add <code>AI_API_KEY</code> to the platform's environment variables and redeploy.</p>`}
  </div>`;
  m.style.display = 'flex';
  document.querySelectorAll('input[name=prov]').forEach(r => r.onchange = provChanged);
  const k = $('aikey'); if (k) k.focus();
}

function provChanged() {
  const local = (document.querySelector('input[name=prov]:checked') || {}).value === 'ollama';
  $('aikeybox').style.display = local ? 'none' : '';
  const box = $('aolbox'); box.style.display = local ? '' : 'none';
  if (!local) return;
  box.innerHTML = '<span class="muted">Looking for Ollama on this computer…</span>';
  fetch('/api/settings/ai/local', { headers: { 'X-ParseFlow': '1' } }).then(r => r.json()).then(s => {
    if (s.error) { box.innerHTML = `<div class="warn">${esc(s.error.message)}</div>`; return; }
    if (!s.reachable) {
      box.innerHTML = `<div class="warn">Ollama is not running. 1) Install it from <a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com/download</a> and open it. 2) In a terminal pull a model that can read images (one marked <i>vision</i> on <a href="https://ollama.com/search?c=vision" target="_blank" rel="noopener">ollama.com</a>), for example <code>ollama pull gemma3:4b</code>. 3) Click Local AI again.</div>`;
      return;
    }
    if (!s.models.length) { box.innerHTML = `<div class="warn">Ollama is running but has no models. Pull one that can read images, e.g. <code>ollama pull gemma3:4b</code>, then click Local AI again.</div>`; return; }
    const opt = (ms, sel) => ms.map(m => `<option ${m.name === sel ? 'selected' : ''}>${esc(m.name)}</option>`).join('');
    const vis = s.models.filter(m => m.vision);
    box.innerHTML = `<label class="muted" style="font-size:12.5px">Model for answers</label><select id="aolm" style="width:100%">${opt(s.models, (vis[0] || s.models[0]).name)}</select>
      <label class="muted" style="font-size:12.5px;margin-top:6px;display:block">Model for reading images and handwriting</label>${vis.length ? `<select id="aolv" style="width:100%">${opt(vis, vis[0].name)}</select>` : `<div class="warn">None of your models can read images. Pull one marked <i>vision</i>, e.g. <code>ollama pull gemma3:4b</code>.</div>`}`;
  }).catch(() => { box.innerHTML = '<div class="warn">The ParseFlow server could not be reached.</div>'; });
}
function closeAI() { const m = $('aimodal'); if (m) m.style.display = 'none'; }

function saveAI() {
  const key = ($('aikey').value || '').trim(), prov = (document.querySelector('input[name=prov]:checked') || {}).value || '';
  const msg = $('aimsg'), btn = $('aisave'), local = prov === 'ollama';
  const body = local ? { provider: 'ollama', model: ($('aolm') || {}).value || '', vision_model: ($('aolv') || {}).value || '' } : { api_key: key, provider: prov };
  if (!local && !key) { msg.textContent = 'Paste a key first.'; return; }
  if (local && !body.model) { msg.textContent = 'Ollama needs at least one model (see above).'; return; }
  btn.disabled = true; msg.textContent = local ? 'Testing the local model (the first load can take a minute)…' : 'Testing the key with a tiny request…';
  fetch('/api/settings/ai', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-ParseFlow': '1' }, body: JSON.stringify(body) })
    .then(r => r.json()).then(s => {
      if (s.error) { msg.innerHTML = `<span style="color:var(--er)">${esc(s.error.message)}</span>`; return; }
      msg.innerHTML = s.note ? `<span style="color:var(--wn)">${esc(s.note)}</span>` : `<span style="color:var(--ok)">Connected to ${esc(s.ai.model)}. Ask now gives real answers; use “Read with AI” in Visuals.</span>`;
      $('aikey').value = '';
      return refreshHealth().then(() => { if (DOC) renderTabs(); setTimeout(closeAI, s.note ? 4500 : 1400); });
    }).catch(() => { msg.textContent = 'The ParseFlow server could not be reached.'; }).finally(() => { btn.disabled = false; });
}
function checkAI() {
  const out = $('aicheckout'), b = $('aicheck');
  b.disabled = true; out.innerHTML = '<p class="muted">Sending a tiny request to each model…</p>';
  fetch('/api/settings/ai/check', { method: 'POST', headers: { 'X-ParseFlow': '1' } }).then(r => r.json()).then(s => {
    if (s.error) { out.innerHTML = `<div class="warn">${esc(s.error.message)}</div>`; return; }
    out.innerHTML = `<div class="tw" style="margin-top:10px"><table class="t"><tr><th>Model</th><th>Result</th><th>Time</th></tr>${s.results.map(r => `<tr><td>${esc(r.model)}</td><td style="color:var(--${r.ok ? 'ok' : 'er'})">${r.ok ? 'Works' : 'HTTP ' + esc(String(r.status)) + (r.detail ? ': ' + esc(r.detail.slice(0, 90)) : '')}</td><td>${r.seconds}s</td></tr>`).join('')}</table></div>
      <p class="muted" style="font-size:13px">${s.working.length ? 'ParseFlow will use a working model automatically.' : 'No model answered. 503 means the provider is busy (temporary); 429 means the free quota is used up for now. A Groq key is a free second route.'}</p>`;
    refreshHealth();
  }).catch(() => { out.innerHTML = '<div class="warn">The ParseFlow server could not be reached.</div>'; }).finally(() => { b.disabled = false; });
}
function clearAI() {
  fetch('/api/settings/ai', { method: 'DELETE', headers: { 'X-ParseFlow': '1' } }).then(() => refreshHealth()).then(() => { closeAI(); if (DOC) renderTabs(); });
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAI(); });
setTimeout(aiButton, 0);
