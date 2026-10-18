/* ParseFlow workspace presentation: a resizable side panel and a short first-time guide.
   Presentation only; no processing logic lives here. */
(function () {
  const KEY_W = 'pf_rtw', KEY_GUIDE = 'pf_guide_done';
  const store = { get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
                  set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} } };

  // ---------- resizable side panel ----------
  function limits() {
    const m = $('main'), w = (m && m.clientWidth) || innerWidth;   // the workspace may still be hidden
    return [440, Math.max(440, w - 124 - 320)];        // keep at least 320 px for the page
  }
  function setW(px, save) {
    const m = $('main'); if (!m) return;
    const [lo, hi] = limits();
    px = Math.round(Math.min(hi, Math.max(lo, px)));
    m.style.setProperty('--rtw', px + 'px');
    if (typeof tabWrap === 'function' && $('tabs')) tabWrap();
    if (save) store.set(KEY_W, String(px));
  }
  function initSplit() {
    const sp = $('split'), m = $('main');
    if (!sp || !m) return;
    const saved = parseInt(store.get(KEY_W) || '', 10);
    if (saved) setW(saved, false);
    let x0 = 0, w0 = 0;
    const move = e => setW(w0 + (x0 - e.clientX), false);
    const up = e => {
      sp.classList.remove('drag'); document.body.classList.remove('dragging');
      removeEventListener('pointermove', move); removeEventListener('pointerup', up);
      setW(w0 + (x0 - e.clientX), true);
    };
    sp.addEventListener('pointerdown', e => {
      e.preventDefault();
      x0 = e.clientX; w0 = m.querySelector('.rt').getBoundingClientRect().width;
      sp.classList.add('drag'); document.body.classList.add('dragging');
      addEventListener('pointermove', move); addEventListener('pointerup', up);
    });
    sp.addEventListener('dblclick', () => { m.style.removeProperty('--rtw'); store.set(KEY_W, null); });
    sp.addEventListener('keydown', e => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const w = m.querySelector('.rt').getBoundingClientRect().width;
      setW(w + (e.key === 'ArrowLeft' ? 40 : -40), true);
    });
    addEventListener('resize', () => { const v = parseInt(m.style.getPropertyValue('--rtw'), 10); if (v) setW(v, false); });
  }

  // ---------- first-time guide ----------
  function showGuide() {
    const g = $('guide');
    if (!g) return;
    if (store.get(KEY_GUIDE) || (typeof ti !== 'undefined' && ti >= 0)) { g.innerHTML = ''; return; }
    g.innerHTML = `<div class="guide" role="note"><div style="flex:1">
      <h4>How to read this page</h4>
      <ol>
        <li><b>In the middle is your document.</b> Every box is a part ParseFlow read. Green means confirmed, amber means worth a quick look, red means it could not be read.</li>
        <li><b>Click any box</b> to see exactly what was read there and how sure ParseFlow is.</li>
        <li><b>On the right:</b> Overview gives a summary, Markdown has the full text to copy or download, and Ask answers questions about the document. Drag the divider to make this panel wider.</li>
      </ol></div>
      <div style="display:flex;flex-direction:column;gap:6px"><button class="btn sm pri" onclick="guideDone()">Got it</button><button class="btn sm" onclick="guideDone();(DOC&&DOC.demo)?tourGo(0):present()">Guided tour</button></div></div>`;
  }
  window.guideDone = function () { store.set(KEY_GUIDE, '1'); const g = $('guide'); if (g) g.innerHTML = ''; };

  const origOpen = open;
  open = function () { origOpen.apply(this, arguments); const v = parseInt(store.get(KEY_W) || '', 10); if (v) setW(v, false); showGuide(); };

  const origTour = tourGo;   // the guided tour explains the page itself: hide the guide while it runs
  tourGo = function () { const g = $('guide'); if (g) g.innerHTML = ''; return origTour.apply(this, arguments); };

  initSplit();
})();
