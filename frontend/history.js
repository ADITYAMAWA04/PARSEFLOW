/* ParseFlow - document history (no accounts).
   The server remembers this browser by an anonymous cookie and lists the documents processed in it. */
let HIST = [], HIST_ALL = false, HIST_ON = true, ME = null;

const fmtWhen = t => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  const d = new Date(t * 1000);
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: d.getFullYear() == new Date().getFullYear() ? undefined : 'numeric' });
};

function histLoad() {
  if (!HIST_ON) return Promise.resolve();
  return fetch('/api/history').then(r => r.ok ? r.json() : null).then(j => {
    if (!j) return;
    HIST_ON = j.enabled !== false;
    HIST = j.documents || [];
    histRender();
  }).catch(() => {});
}

const ST_LABEL = { complete: 'Ready', partial: 'Ready, some pages failed', processing: 'Processing', running: 'Processing', failed: 'Failed', cancelled: 'Cancelled' };

function histRender() {
  const box = $('recent');
  if (!box) return;
  if (!HIST.length) { box.hidden = true; box.innerHTML = ''; return; }
  const rows = HIST_ALL ? HIST : HIST.slice(0, 5);
  box.hidden = false;
  box.innerHTML = `<div class="rh"><h2>Your documents</h2><button class="linkbtn" onclick="libOpen('all')">Open your library →</button><span class="muted">${ME && ME.signed_in
      ? 'Saved in your account. Log in on any computer to open them.'
      : (ME && ME.accounts ? `Kept for ${ME.guest_hours > 0 ? (ME.guest_hours >= 48 ? Math.round(ME.guest_hours / 24) + ' days' : ME.guest_hours + ' hours') : 'now'}, in this browser only. <a href="/login">Log in</a> to save them.` : 'Documents you converted in this browser.')}</span></div>
    <ul class="rlist">${rows.map(r => {
      const ready = r.available && (r.status == 'complete' || r.status == 'partial');
      const busy = r.status == 'processing' || r.status == 'running';
      return `<li><button class="rname" ${ready || busy ? '' : 'disabled'} onclick="histOpen('${r.doc_id}')" title="${escA(r.filename)}">${esc(r.filename)}</button>
        <span class="rmeta">${r.pages ? r.pages + ' page' + (r.pages > 1 ? 's' : '') : ''}</span>
        <span class="rmeta">${fmtWhen(r.created)}</span>
        <span class="rst ${ready ? 'ok' : busy ? 'run' : 'bad'}">${esc(ST_LABEL[r.status] || r.status)}</span>
        <span class="ract">${ready || busy ? `<button class="btn sm" onclick="histOpen('${r.doc_id}')">Open</button>` : ''}<button class="btn sm ghost" onclick="histDelete('${r.doc_id}')" aria-label="Delete ${escA(r.filename)}">Delete</button></span></li>`;
    }).join('')}</ul>
    ${HIST.length > 5 ? `<button class="linkbtn" onclick="HIST_ALL=!HIST_ALL;histRender()">${HIST_ALL ? 'Show fewer' : 'Show all ' + HIST.length + ' documents'}</button>` : ''}`;
}

function histOpen(id) {
  const r = HIST.find(x => x.doc_id == id);
  if (!r) return;
  const ext = (r.filename.split('.').pop() || '').toUpperCase();
  show('procv');
  $('upb').style.width = '100%';
  drawStages(STG.map(s => ({ name: s[0], detail: s[1], state: r.status == 'complete' || r.status == 'partial' ? 'done' : 'pending' })));
  track({ document_id: r.doc_id, filename: r.filename, type: ext, pages: r.pages, status: r.status }, fmtWhen(r.created));
}

function histDelete(id) {
  const r = HIST.find(x => x.doc_id == id);
  if (!r || !confirm(`Delete "${r.filename}" and its results from ParseFlow? This cannot be undone.`)) return;
  fetch('/api/history/' + id, { method: 'DELETE' }).then(x => { if (x.ok) { HIST = HIST.filter(h => h.doc_id != id); histRender(); } });
}

// refresh the list whenever the start page is shown (e.g. after "New document")
(function () {
  const orig = show;
  show = function (v) { orig(v); if (v == 'landing') histLoad(); };
})();

function acctRender() {
  const el = $('acct');
  if (!el) return;
  choicesRender();
  if (!ME || !ME.accounts) { el.innerHTML = ''; return; }
  if (!ME.signed_in) { el.innerHTML = '<span class="authbtns"><a class="btn" href="/login">Log in</a><a class="btn dark" href="/login?mode=register">Sign up</a></span>'; return; }
  const u = ME.user.username;
  el.innerHTML = `<div class="acct-menu"><button class="acct-btn" onclick="acctToggle(event)" aria-haspopup="true"><span class="av">${esc(u.slice(0, 2).toUpperCase())}</span><span class="nm">${esc(u)}</span></button>
    <div class="acct-pop" id="acctpop" hidden><div class="who">Logged in as <b>${esc(u)}</b></div>
    <button onclick="libOpen('all')">Your documents</button><button onclick="libSettings()">Settings</button><button onclick="libPrivacy()">Privacy &amp; data</button>
    <button onclick="fetch('/api/auth/logout',{method:'POST'}).finally(()=>location.href='/')">Log out</button></div></div>`;
}
function choicesRender() {
  const el = $('choices');
  if (!el) return;
  const hrs = ME && ME.guest_hours > 0 ? (ME.guest_hours >= 48 ? Math.round(ME.guest_hours / 24) + ' days' : ME.guest_hours + ' hours') : '';
  if (ME && ME.signed_in) {
    el.innerHTML = `<div class="choice on wide"><b>&#10003; Saving to your account</b><span>Logged in as ${esc(ME.user.username)}. Every upload is kept until you delete it, and only you can open it.</span></div>`;
  } else if (ME && ME.accounts) {
    el.innerHTML = `<div class="choice on"><b>&#10003; Continue without an account</b><span>Nothing to fill in.${hrs ? ' Kept ' + hrs + ' in this browser.' : ''}</span></div>
      <a class="choice" href="/login"><b>&#128274; Log in to save</b><span>Keep every upload and reopen it from any computer.</span></a>`;
  } else {
    el.innerHTML = '<div class="choice on wide"><b>&#10003; No account needed</b><span>Your document opens as soon as it is read.</span></div>';
  }
}
function acctToggle(e) { e.stopPropagation(); const p = $('acctpop'); if (p) p.hidden = !p.hidden; }
document.addEventListener('click', () => { const p = $('acctpop'); if (p) p.hidden = true; });

fetch('/api/me').then(r => r.ok ? r.json() : null).then(j => {
  ME = j; acctRender(); histLoad();
  if (j && j.signed_in && new URLSearchParams(location.search).get('welcome')) history.replaceState(null, '', '/');
}).catch(() => histLoad());
