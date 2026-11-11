'use strict';
// Shared, read-only site shell. Workflow routing and unsaved-change guards stay in app.js.
(() => {
  const header = document.querySelector('[data-site-header]');
  if (!header) return;
  const links = [...header.querySelectorAll('[data-destination]')];
  const storageKey = 'aurora.workspace.navigation';
  function restore() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (!saved || typeof saved !== 'object') return {};
      return {
        caseId: typeof saved.caseId === 'string' && /^CASE-[A-Z0-9-]+$/.test(saved.caseId) ? saved.caseId : null,
        tab: ['workflow','evidence','recovery','audit'].includes(saved.tab) ? saved.tab : 'workflow',
        view: ['workspace','tasks','sources','audit'].includes(saved.view) ? saved.view : 'workspace',
        role: ['quality','committee','sales','production'].includes(saved.role) ? saved.role : 'quality'
      };
    } catch { return {}; }
  }
  function update(view, caseId, tab, role, persist = true) {
    if (persist && view !== 'intro' && caseId) {
      try { sessionStorage.setItem(storageKey, JSON.stringify({view,caseId,tab,role:role || restore().role || 'quality'})); } catch { /* Optional continuity only; no credentials stored. */ }
    }
    for (const link of links) {
      const destination = link.dataset.destination;
      if (destination === view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
      if (destination !== 'intro' && caseId) {
        const url = new URL('/workspace', location.origin);
        url.searchParams.set('case', caseId);
        if (destination === 'workspace' && tab && tab !== 'workflow') url.searchParams.set('tab', tab);
        if (destination !== 'workspace') url.hash = destination;
        link.setAttribute('href', url.pathname + url.search + url.hash);
      }
    }
  }
  function syncURL() {
    const workspace = /^\/workspace\/?$/.test(location.pathname) || location.pathname === '/static/index.html';
    const hash = location.hash.slice(1);
    const view = !workspace ? 'intro' : /^board-(open|blocked|complete)$/.test(hash) ? 'tasks' : ['tasks', 'sources', 'audit'].includes(hash) ? hash : 'workspace';
    const params = new URLSearchParams(location.search);
    const saved = restore();
    if (!workspace && saved.caseId) {
      // Resume the working context without retaining authentication tokens or drafts.
      update('intro', saved.caseId, saved.tab);
      const resume = new URL('/workspace', location.origin);
      resume.searchParams.set('case', saved.caseId);
      if (saved.view === 'workspace' && saved.tab !== 'workflow') resume.searchParams.set('tab', saved.tab);
      if (saved.view !== 'workspace') resume.hash = saved.view;
      document.querySelectorAll('a[href="/workspace"]:not([data-destination])').forEach(link => link.setAttribute('href', link.matches('.header-cta, .closing a') ? resume.pathname + resume.search + resume.hash : header.querySelector('[data-destination="workspace"]').getAttribute('href')));
    } else update(view, params.get('case') || saved.caseId, params.get('tab') || saved.tab, saved.role, false);
  }
  function measure() {
    const height = Math.ceil(header.getBoundingClientRect().height);
    if (height) document.documentElement.style.setProperty('--global-header-height', height + 'px');
  }
  window.AuroraShell = {update,restore};
  syncURL(); measure();
  // Workspace popstate is owned by app.js, which can cancel navigation to protect edits.
  window.addEventListener('pageshow', () => { syncURL(); measure(); });
  window.addEventListener('resize', measure);
  if (typeof ResizeObserver === 'function') new ResizeObserver(measure).observe(header);
})();
