/* ParseFlow - document library: every document you converted, with search, filters, downloads,
   rename, star, share link, delete, and the Settings / Privacy & data panels.
   Uses HIST / ME / histLoad from history.js and show / track / openAI from the main page. */
let LIB = { filter: 'all', q: '', sort: 'new', sel: null, timer: null };

const IC = {
  up: '<path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/>',
  home: '<path d="M4 11l8-7 8 7v9H4z"/><path d="M10 20v-5h4v5"/>',
  docs: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
  star: '<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.8z"/>',
  flag: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
  link: '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 00-2-1.2L14 3h-4l-.5 2.6a7 7 0 00-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 005 12a7 7 0 00.1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 002 1.2L10 21h4l.5-2.6a7 7 0 002-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  dl: '<path d="M12 4v12m0 0l-4.5-4.5M12 16l4.5-4.5M4 20h16"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 00-1-1H5a1 1 0 00-1 1v10a1 1 0 001 1h3"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
};
const ico = (k, cls = '') => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${IC[k]}</svg>`;

const ready = r => r.available && (r.status == 'complete' || r.status == 'partial');
const busy = r => r.status == 'processing' || r.status == 'running';
const needsReview = r => !!(r.summary && r.summary.blocks && r.summary.verified_pct < 75);
const fmtBytes = b => !b ? '0 KB' : b > 1048576 ? (b / 1048576).toFixed(b > 104857600 ? 0 : 1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';
const extOf = n => ((n || '').split('.').pop() || '').toLowerCase();
const EXTC = { pdf: 'pdf', doc: 'doc', docx: 'doc', odt: 'doc', rtf: 'doc', xls: 'xls', xlsx: 'xls', xlsm: 'xls', csv: 'xls', ods: 'xls', ppt: 'ppt', pptx: 'ppt', odp: 'ppt',
  png: 'img', jpg: 'img', jpeg: 'img', tif: 'img', tiff: 'img', bmp: 'img', gif: 'img', webp: 'img' };

function libOpen(filter) {
  if (filter) LIB.filter = filter;
  show('library');
  try { if (location.hash != '#library') history.replaceState(null, '', '#library'); } catch (e) {}
  libRender();
  histLoad().then(libRender);
}

function libRows() {
  const q = LIB.q.trim().toLowerCase();
  let rows = HIST.filter(r => !q || r.filename.toLowerCase().includes(q));
  if (LIB.filter == 'starred') rows = rows.filter(r => r.starred);
  if (LIB.filter == 'review') rows = rows.filter(needsReview);
  if (LIB.filter == 'shared') rows = rows.filter(r => r.shared);
  const by = { new: (a, b) => b.created - a.created, old: (a, b) => a.created - b.created,
    name: (a, b) => a.filename.localeCompare(b.filename), pages: (a, b) => (b.pages || 0) - (a.pages || 0) }[LIB.sort];
  return rows.slice().sort(by);
}

function libRender() {
  const el = $('library');
  if (!el || !el.classList.contains('on')) return;
  const rows = libRows(), all = HIST, tot = { pages: all.reduce((s, r) => s + (r.pages || 0), 0), bytes: all.reduce((s, r) => s + (r.size || 0), 0) };
  const cnt = { all: all.length, starred: all.filter(r => r.starred).length, review: all.filter(needsReview).length, shared: all.filter(r => r.shared).length };
  const signed = ME && ME.signed_in, hrs = ME && ME.guest_hours > 0 ? (ME.guest_hours >= 48 ? Math.round(ME.guest_hours / 24) + ' days' : ME.guest_hours + ' hours') : '';
  const nav = (k, icon, label) => `<button class="lnav ${LIB.filter == k ? 'on' : ''}" onclick="LIB.filter='${k}';LIB.sel=null;libRender()">${ico(icon)}<span>${label}</span>${cnt[k] ? `<em>${cnt[k]}</em>` : ''}</button>`;
  const title = { all: 'All documents', starred: 'Starred', review: 'Needs a look', shared: 'Shared by link' }[LIB.filter];
  if (LIB.sel && !all.find(r => r.doc_id == LIB.sel)) LIB.sel = null;
  if (!LIB.sel && rows.length && window.innerWidth > 1100) LIB.sel = rows[0].doc_id;
  el.innerHTML = `<aside class="lside">
    <a class="brand sm" href="/" onclick="event.preventDefault();show('landing');history.replaceState(null,'','/')"><span class="mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 19V5h6a4 4 0 010 8H7"/><path d="M13.5 16.5l2 2 4-4.5"/></svg></span>ParseFlow</a>
    <button class="upbtn sm" onclick="fi.click()">${ico('up')}Upload a document</button>
    <nav class="lgroup"><button class="lnav" onclick="show('landing');history.replaceState(null,'','/')">${ico('home')}<span>Home</span></button>
      ${nav('all', 'docs', 'All documents')}${nav('starred', 'star', 'Starred')}${nav('review', 'flag', 'Needs a look')}${nav('shared', 'link', 'Shared')}</nav>
    ${all.length ? `<div class="lcap">Recent</div><div class="lrecent">${all.slice(0, 4).map(r => `<button onclick="LIB.sel='${r.doc_id}';libRender()" title="${escA(r.filename)}">${esc(r.filename)}</button>`).join('')}</div>` : ''}
    <div class="lcap">Account</div>
    <nav class="lgroup"><button class="lnav" onclick="libSettings()">${ico('gear')}<span>Settings</span></button><button class="lnav" onclick="libPrivacy()">${ico('shield')}<span>Privacy &amp; data</span></button></nav>
    <div class="lspace"></div>
    <div class="lstore"><b>${all.length}</b> document${all.length == 1 ? '' : 's'} · <b>${tot.pages}</b> page${tot.pages == 1 ? '' : 's'}<br><span>${fmtBytes(tot.bytes)} ${signed ? 'saved in your account' : 'kept for this browser'}</span>
      <div class="lmeter"><i style="width:${Math.min(100, Math.max(2, tot.bytes / 1048576 / 5))}%"></i></div></div>
    <div class="luser">${signed ? `<span class="av">${esc(ME.user.username.slice(0, 2).toUpperCase())}</span><div><b>${esc(ME.user.username)}</b><span>Saved account</span></div><button class="btn sm" onclick="fetch('/api/auth/logout',{method:'POST'}).finally(()=>location.href='/')">Log out</button>`
      : `<span class="av g">G</span><div><b>Guest</b><span>${hrs ? 'Kept ' + hrs : 'This browser'}</span></div>${ME && ME.accounts ? '<a class="btn sm dark" href="/login">Log in</a>' : ''}`}</div>
  </aside>
  <main class="lmain">
    <div class="lhead"><h1>${title}</h1><span class="muted">${rows.length} of ${all.length}</span><span style="flex:1"></span>
      <label class="lsearch">${ico('search')}<input id="libq" placeholder="Search by name" value="${escA(LIB.q)}" oninput="LIB.q=this.value;libList()" aria-label="Search your documents"><kbd>/</kbd></label>
      <select class="lsort" onchange="LIB.sort=this.value;libList()" aria-label="Sort">${[['new', 'Newest first'], ['old', 'Oldest first'], ['name', 'Name'], ['pages', 'Most pages']].map(([v, l]) => `<option value="${v}" ${LIB.sort == v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div class="ldrop" onclick="fi.click()"><span class="ldi">${ico('up')}</span><div><b>Drop a file here to convert it</b><span>PDF, Word, PowerPoint, Excel, images, text and more · ${signed ? 'saved to your account automatically' : hrs ? 'kept ' + hrs + ' in this browser' : 'opens right away'}</span></div><span class="btn sm">Browse</span></div>
    <div id="liblist"></div>
  </main>
  <aside class="ldetail" id="libdetail"></aside>`;
  libList();
}

function libList() {
  const box = $('liblist');
  if (!box) return;
  const rows = libRows();
  if (!HIST.length) {
    box.innerHTML = `<div class="lempty"><div class="lei">${ico('docs')}</div><h3>No documents yet</h3><p>Convert your first document and it appears here, ready to open, download or share.</p><button class="btn pri" onclick="fi.click()">Upload a document</button> <button class="btn" onclick="startDemo()">Open the sample</button></div>`;
  } else if (!rows.length) {
    box.innerHTML = `<div class="lempty"><h3>Nothing here</h3><p>${LIB.q ? 'No document name contains “' + esc(LIB.q) + '”.' : 'No documents in this list yet.'}</p></div>`;
  } else {
    box.innerHTML = `<table class="ltab"><thead><tr><th class="c-star"><span class="sr">Star</span></th><th>Name</th><th class="c-num">Pages</th><th class="c-content">Content</th><th>Checked</th><th class="c-when">Uploaded</th></tr></thead><tbody>${rows.map(libRow).join('')}</tbody></table>`;
  }
  libDetail();
  clearTimeout(LIB.timer);
  if (HIST.some(busy)) LIB.timer = setTimeout(() => { if ($('library').classList.contains('on')) histLoad().then(libList); }, 3000);
}

function libRow(r) {
  const s = r.summary, e = extOf(r.filename), on = r.doc_id == LIB.sel;
  const content = s ? [s.tables && `${s.tables} table${s.tables > 1 ? 's' : ''}`, s.equations && `${s.equations} equation${s.equations > 1 ? 's' : ''}`,
    s.visuals && `${s.visuals} visual${s.visuals > 1 ? 's' : ''}`, s.handwritten && 'handwriting', s.scanned && 'scanned · OCR'].filter(Boolean) : [];
  const check = busy(r) ? '<span class="lbusy"><i class="spin"></i>Reading…</span>'
    : !ready(r) ? `<span class="lbad">${esc(ST_LABEL[r.status] || r.status)}</span>`
    : s && !s.verified_pct ? '<span class="lchk" title="Nothing on this document could be confirmed by a second source (for example a scan or handwriting). Open it to check the reading.">To check</span>'
    : s ? `<span class="lver" title="${s.verified_pct}% of blocks confirmed by an independent source"><span class="lbar"><i style="width:${s.verified_pct}%"></i></span>${s.verified_pct}%</span>` : '<span class="muted">Ready</span>';
  return `<tr class="${on ? 'on' : ''}" onclick="LIB.sel='${r.doc_id}';libList()" ondblclick="if(${ready(r) || busy(r)})histOpen('${r.doc_id}')">
    <td class="c-star"><button class="starb ${r.starred ? 'on' : ''}" onclick="event.stopPropagation();libStar('${r.doc_id}')" aria-label="${r.starred ? 'Unstar' : 'Star'} ${escA(r.filename)}">${ico('star')}</button></td>
    <td><div class="lname"><span class="ft ${EXTC[e] || 'oth'}">${esc((e || '?').slice(0, 4).toUpperCase())}</span><span class="nm" title="${escA(r.filename)}">${esc(r.filename)}</span>${r.shared ? `<span class="tagl" title="Shared by link">${ico('link')}</span>` : ''}</div></td>
    <td class="c-num">${r.pages || '–'}</td>
    <td class="c-content">${content.length ? content.map(c => `<span class="cchip">${c}</span>`).join('') : '<span class="muted">Text</span>'}</td>
    <td>${check}</td><td class="c-when">${fmtWhen(r.created)}</td></tr>`;
}

function libDetail() {
  const box = $('libdetail');
  if (!box) return;
  const r = HIST.find(x => x.doc_id == LIB.sel);
  $('library').classList.toggle('has-detail', !!r);
  if (!r) { box.innerHTML = ''; return; }
  const s = r.summary || {}, ok = ready(r), id = r.doc_id;
  const stat = (v, l, k = '') => `<div class="dstat ${k}"><b>${v}</b><span>${l}</span></div>`;
  box.innerHTML = `<div class="dhead"><button class="btn sm ghost dclose" onclick="LIB.sel=null;libList()" aria-label="Close">${ico('x')}</button></div>
    <div class="dthumb">${ok ? `<img src="/api/page/${id}/1.jpg?w=640" alt="First page of ${escA(r.filename)}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'noimg',textContent:'No preview'}))">` : `<div class="noimg">${busy(r) ? 'Reading…' : 'No preview'}</div>`}</div>
    <h3 class="dname" title="${escA(r.filename)}">${esc(r.filename)}</h3>
    <p class="dmeta">${[r.pages ? r.pages + ' page' + (r.pages > 1 ? 's' : '') : '', r.size ? fmtBytes(r.size) : '', 'uploaded ' + fmtWhen(r.created)].filter(Boolean).join(' · ')}</p>
    ${r.summary ? `<div class="dgrid">${stat(s.blocks, 'blocks')}${stat(s.tables, 'tables')}${stat(s.verified_pct + '%', 'confirmed', s.verified_pct >= 80 ? 'g' : '')}${stat(s.review, 'to look at', s.review ? 'w' : '')}${stat(s.equations, 'equations')}${stat(s.headers, 'headers/footers set aside')}</div>` : ''}
    <button class="btn pri dopen" ${ok || busy(r) ? '' : 'disabled'} onclick="histOpen('${id}')">Open in viewer</button>
    <div class="dacts">
      <a class="btn ${ok ? '' : 'dis'}" href="/api/document/${id}/markdown?download=1">${ico('dl')}Markdown</a>
      <a class="btn ${ok ? '' : 'dis'}" href="/api/document/${id}?download=1">${ico('dl')}JSON</a>
      <button class="btn" ${ok ? '' : 'disabled'} onclick="libCopy('${id}')">${ico('copy')}Copy text</button>
      <button class="btn" onclick="libStar('${id}')">${ico('star', r.starred ? 'fill' : '')}${r.starred ? 'Starred' : 'Star'}</button>
      <button class="btn" onclick="libRename('${id}')">${ico('pen')}Rename</button>
      <button class="btn danger" onclick="libDelete('${id}')">${ico('trash')}Delete</button>
    </div>
    ${ME && ME.signed_in ? `<div class="dshare"><div><b>${ico('link')} Share a read-only link</b><span>${r.shared ? 'Anyone with the link can open this document.' : 'Off. Only you can open this document.'}</span></div>
      ${r.shared ? `<button class="btn sm" onclick="libShare('${id}',true)">Copy link</button><button class="btn sm ghost" onclick="libShare('${id}',false)">Stop</button>` : `<button class="btn sm" ${ok ? '' : 'disabled'} onclick="libShare('${id}',true)">Create link</button>`}</div>`
      : `<p class="dnote">${ME && ME.accounts ? '<a href="/login">Log in</a> to keep this document and share it by link.' : ''}</p>`}`;
}

function libPatch(id, body) {
  return fetch('/api/history/' + id, { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
    .then(r => r.json()).then(j => { if (j.ok) { const r = HIST.find(x => x.doc_id == id); if (r) Object.assign(r, body); libList(); } else toast(j.error ? j.error.message : 'Not saved.'); return j; });
}
function libStar(id) { const r = HIST.find(x => x.doc_id == id); if (r) libPatch(id, { starred: !r.starred }); }
function libRename(id) {
  const r = HIST.find(x => x.doc_id == id);
  if (!r) return;
  const n = prompt('New name for this document:', r.filename);
  if (n != null && n.trim() && n.trim() != r.filename) libPatch(id, { filename: n.trim() });
}
function libDelete(id) {
  const r = HIST.find(x => x.doc_id == id);
  if (!r || !confirm(`Delete "${r.filename}" and its results? This cannot be undone.`)) return;
  fetch('/api/history/' + id, { method: 'DELETE' }).then(x => { if (x.ok) { HIST = HIST.filter(h => h.doc_id != id); LIB.sel = null; libRender(); toast('Deleted.'); } });
}
function libCopy(id) {
  fetch(`/api/document/${id}/markdown`).then(r => r.text()).then(t => navigator.clipboard.writeText(t)).then(() => toast('Markdown copied.'), () => toast('Could not copy here; use the Markdown download.'));
}
function libShare(id, on) {
  fetch(`/api/history/${id}/share`, { method: on ? 'POST' : 'DELETE' }).then(r => r.json()).then(j => {
    if (!j.ok) return toast(j.error ? j.error.message : 'Not changed.');
    const r = HIST.find(x => x.doc_id == id);
    if (r) r.shared = on;
    libList();
    if (on) navigator.clipboard.writeText(j.url).then(() => toast('Link copied: ' + j.url), () => prompt('Copy this link:', j.url));
    else toast('Sharing stopped. The old link no longer opens it.');
  });
}

if (typeof toast != 'function') {
  window.toast = function (msg) {
    const t = document.createElement('div');
    t.className = 'toast'; t.textContent = msg; document.body.appendChild(t);
    setTimeout(() => t.classList.add('out'), 2600); setTimeout(() => t.remove(), 3200);
  };
}

/* ---------- Settings and Privacy & data ---------- */
function libModal(html) {
  let m = $('libmodal');
  if (!m) { m = document.createElement('div'); m.id = 'libmodal'; m.className = 'modalbg'; m.onclick = e => { if (e.target == m) m.style.display = 'none'; }; document.body.appendChild(m); }
  m.innerHTML = `<div class="modal lmodal" role="dialog">${html}</div>`;
  m.style.display = 'flex';
}
const libClose = () => { const m = $('libmodal'); if (m) m.style.display = 'none'; };
function themeSet(t) {
  try { t == 'system' ? localStorage.removeItem('pf_theme') : localStorage.setItem('pf_theme', t); } catch (e) {}
  if (t == 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  libSettings();
}
function libSettings() {
  let cur = 'system';
  try { cur = localStorage.getItem('pf_theme') || 'system'; } catch (e) {}
  const ai = typeof BE != 'undefined' && BE && BE.llm && BE.llm.configured;
  libModal(`<div class="flow"><h3 style="margin:0">Settings</h3><button class="btn sm" style="margin-left:auto" onclick="libClose()">✕</button></div>
    <div class="sset"><b>Appearance</b><div class="seg">${['system', 'light', 'dark'].map(t => `<button class="${cur == t ? 'on' : ''}" onclick="themeSet('${t}')">${t[0].toUpperCase() + t.slice(1)}</button>`).join('')}</div></div>
    <div class="sset"><b>AI model</b><span class="muted">${ai ? 'Connected: ' + esc(BE.llm.provider || '') + ' ' + esc(BE.llm.model || '') : 'Not connected. Everything works offline; AI adds explained answers and reads images.'}</span>
      ${typeof openAI == 'function' && BE && BE.ai_settings_editable ? `<button class="btn sm" onclick="libClose();openAI()">${ai ? 'Change' : 'Connect AI'}</button>` : ''}</div>
    ${ME && ME.signed_in ? `<div class="sset col"><b>Change password</b>
      <input id="pw0" type="password" placeholder="Current password" autocomplete="current-password"><input id="pw1" type="password" placeholder="New password (8+ characters)" autocomplete="new-password">
      <div id="pwmsg" class="auth-msg"></div><button class="btn sm pri" onclick="libPw()">Change password</button></div>` : ''}`);
}
function libPw() {
  fetch('/api/auth/password', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ current: $('pw0').value, new: $('pw1').value }) })
    .then(r => r.json()).then(j => { if (j.ok) location.href = '/login'; else $('pwmsg').textContent = j.error.message; });
}
function libPrivacy() {
  const signed = ME && ME.signed_in, hrs = ME && ME.guest_hours;
  libModal(`<div class="flow"><h3 style="margin:0">Privacy &amp; data</h3><button class="btn sm" style="margin-left:auto" onclick="libClose()">✕</button></div>
    <ul class="plist"><li>${signed ? 'Your documents are kept in your account until you delete them, and only you can open them (or people you give a share link).' : `Without an account your documents are kept${hrs ? ' for ' + hrs + ' hours' : ''}, for this browser only.`}</li>
      <li>Documents are read on this ParseFlow server. Nothing is sent to an AI service unless the server has an AI model connected.</li>
      <li>Passwords are stored only as salted scrypt hashes.</li></ul>
    <div class="sset"><b>Download everything</b><span class="muted">Markdown and JSON of every document, in one ZIP.</span><a class="btn sm" href="/api/history/export.zip">Download ZIP</a></div>
    <div class="sset"><b>Delete all documents</b><span class="muted">${HIST.length} document${HIST.length == 1 ? '' : 's'} and their results.</span><button class="btn sm danger" onclick="libWipe()">Delete all</button></div>
    ${signed ? `<div class="sset col"><b>Delete account</b><span class="muted">Removes your account and every document in it. Type your password to confirm.</span>
      <input id="delpw" type="password" placeholder="Password" autocomplete="current-password"><div id="delmsg" class="auth-msg"></div><button class="btn sm danger" onclick="libDelAcct()">Delete my account</button></div>` : ''}`);
}
function libWipe() {
  if (!HIST.length || !confirm(`Delete all ${HIST.length} documents? This cannot be undone.`)) return;
  fetch('/api/history', { method: 'DELETE' }).then(r => r.json()).then(j => { HIST = []; LIB.sel = null; libClose(); libRender(); toast(`Deleted ${j.deleted} document${j.deleted == 1 ? '' : 's'}.`); });
}
function libDelAcct() {
  fetch('/api/auth/delete', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password: $('delpw').value }) })
    .then(r => r.json()).then(j => { if (j.ok) location.href = '/?deleted=1'; else $('delmsg').textContent = j.error.message; });
}

/* ---------- wiring ---------- */
(function () {
  const orig = show;
  show = function (v) { orig(v); if (v != 'library') { try { if (location.hash == '#library') history.replaceState(null, '', location.pathname + location.search); } catch (e) {} } };
  try { const t = localStorage.getItem('pf_theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
  document.addEventListener('keydown', e => {
    if (!$('library') || !$('library').classList.contains('on')) return;
    if (e.key == '/' && document.activeElement.tagName != 'INPUT') { e.preventDefault(); const q = $('libq'); if (q) q.focus(); }
    if (e.key == 'Escape') { libClose(); if (LIB.sel) { LIB.sel = null; libList(); } }
  });
  window.addEventListener('hashchange', () => { if (location.hash == '#library') libOpen(); });
  const p = new URLSearchParams(location.search);
  if (p.get('open')) {          // opened from a share link
    const id = p.get('open');
    history.replaceState(null, '', '/');
    show('procv');
    Promise.all([fetch('/api/document/' + id).then(r => r.ok ? r.json() : Promise.reject()), fetch('/api/document/' + id + '/markdown').then(r => r.ok ? r.text() : null)])
      .then(([d, md]) => track({ document_id: id, filename: d.filename || 'Shared document', type: 'Shared with you', pages: (d.pages || []).length, document: d, markdown: md }, ''))
      .catch(() => { show('landing'); toast('That document is not available.'); });
  } else if (p.get('shared') == 'gone') {
    history.replaceState(null, '', '/');
    setTimeout(() => toast('That share link has been switched off by its owner.'), 300);
  } else if (location.hash == '#library') {
    setTimeout(() => libOpen(), 0);
  }
})();
