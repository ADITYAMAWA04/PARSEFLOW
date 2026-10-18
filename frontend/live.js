/* ParseFlow - the document opens as soon as its text is ready; text inside images and handwriting
   is read in the background and the open document refreshes itself when that is done. */
(function () {
  let timer = null;
  function chip(html) {
    let c = $('livechip');
    if (!c) {
      const top = document.querySelector('#app .top');
      if (!top) return;
      c = document.createElement('span'); c.id = 'livechip'; c.className = 'livechip';
      top.insertBefore(c, top.querySelector('#beb2'));
    }
    c.innerHTML = html; c.style.display = html ? '' : 'none';
  }
  function refresh(id) {
    const keep = { pg: pg, tab: tab, sel: sel };
    return fetch('/api/document/' + id).then(r => r.ok ? r.json() : Promise.reject(r)).then(d => {
      return fetch('/api/document/' + id + '/markdown').then(r => r.ok ? r.text() : null).catch(() => null).then(t => {
        if (!DOC || DOC.document_id !== id) return;            // the user opened something else meanwhile
        if (t) d._md = t;
        DOC = d; pg = Math.min(keep.pg, (d.pages || []).length || 1); tab = keep.tab; sel = keep.sel;
        renderPage(); renderTabs(); if (typeof capBadge === 'function') capBadge();
      });
    });
  }
  function watch(id) {
    clearInterval(timer);
    const total = (DOC.blocks || []).filter(b => b.pending).length;
    chip(`<i class="spin"></i>Reading text in images 0/${total}`);
    timer = setInterval(() => {
      fetch('/api/status/' + id).then(r => r.json()).then(st => {
        if (!DOC || DOC.document_id !== id) { clearInterval(timer); chip(''); return; }
        const e = st.enrich || {};
        if (e.state === 'running') chip(`<i class="spin"></i>Reading text in images ${e.done || 0}/${e.total || total}`);
        if (e.state === 'done' || e.state === 'failed' || (!e.state && st.status !== 'running')) {
          clearInterval(timer);
          refresh(id).finally(() => { chip(e.state === 'failed' ? 'Some images could not be read' : ''); });
        }
      }).catch(() => {});
    }, 1200);
  }
  const prevOpen = open;
  open = function () {
    prevOpen.apply(this, arguments);
    clearInterval(timer); chip('');
    if (DOC && DOC.document_id && !DOC.demo && (DOC.blocks || []).some(b => b.pending)) watch(DOC.document_id);
  };
})();
