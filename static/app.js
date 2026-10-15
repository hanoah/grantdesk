/* GrantDesk front-end — vanilla JS, no build step. */
(() => {
  'use strict';

  // ------------------------------------------------------------------ utils
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const fmtMoney = (n) => (n == null || n === '' ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 }));
  const fmtDate = (s) => (s ? String(s).slice(0, 10) : '—');
  const initials = (name) => (name || '?').split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  const CLOSED = new Set(['Awarded', 'Declined']);

  async function api(path, opts = {}) {
    const o = { headers: {}, ...opts };
    if (o.body && !(o.body instanceof FormData)) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(o.body); }
    const r = await fetch(path, o);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }

  let toastTimer;
  function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2600); }

  function modal(html, { narrow = false } = {}) {
    const root = $('#modal-root');
    const bg = el(`<div class="modal-bg"><div class="modal ${narrow ? 'narrow' : ''}">${html}</div></div>`);
    bg.addEventListener('click', (e) => { if (e.target === bg) close(); });
    function close() { bg.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    root.appendChild(bg);
    $$('[data-close]', bg).forEach((b) => b.addEventListener('click', close));
    return { bg, box: $('.modal', bg), close };
  }

  // Tennis court indicator: red = ball in OUR court, green = ball in THEIR court.
  function court(c, size = 44) {
    const ours = c === 'Ours';
    const fill = ours ? '#d93a3f' : '#2a9d5c';
    const ballX = ours ? 10 : 34;
    const title = ours ? 'Ball is in OUR court — we need to act' : 'Ball is in THEIR court — waiting on them';
    return `<svg class="court" width="${size}" height="${size * 0.6}" viewBox="0 0 44 26" role="img" aria-label="${title}"><title>${title}</title>
      <rect x="1" y="1" width="42" height="24" rx="3" fill="${fill}" opacity=".18"/>
      <rect x="1" y="1" width="42" height="24" rx="3" fill="none" stroke="${fill}" stroke-width="1.5"/>
      <line x1="22" y1="1" x2="22" y2="25" stroke="${fill}" stroke-width="1.5"/>
      <line x1="8" y1="7" x2="36" y2="7" stroke="${fill}" stroke-width="1" opacity=".6"/>
      <line x1="8" y1="19" x2="36" y2="19" stroke="${fill}" stroke-width="1" opacity=".6"/>
      <line x1="8" y1="7" x2="8" y2="19" stroke="${fill}" stroke-width="1" opacity=".6"/>
      <line x1="36" y1="7" x2="36" y2="19" stroke="${fill}" stroke-width="1" opacity=".6"/>
      <circle cx="${ballX}" cy="13" r="3.4" fill="#e6f24a" stroke="#9aa500" stroke-width="1"/></svg>`;
  }

  function idleClass(d) { return d == null ? '' : d >= 14 ? 'bad' : d >= 7 ? 'warn' : ''; }
  function scoreClass(s) { return s >= 5 ? 'hi' : s < 1.5 ? 'lo' : ''; }

  // ------------------------------------------------------------------ state
  const state = { grants: [], settings: { stages: [] }, filters: { q: '', stage: '', await: false, starred: false, hideClosed: true }, sort: { key: 'default', asc: false } };

  async function loadAll() {
    const [grants, settings] = await Promise.all([api('/api/grants'), api('/api/settings')]);
    state.grants = grants; state.settings = settings;
    $('#ai-pill').textContent = settings.has_api_key ? 'AI: on' : 'AI: rules';
    $('#ai-pill').className = 'pill ' + (settings.has_api_key ? 'green' : 'muted');
  }

  async function patchGrant(id, body) {
    const g = await api(`/api/grants/${id}`, { method: 'PATCH', body });
    const i = state.grants.findIndex((x) => x.id === id);
    if (i >= 0) state.grants[i] = { ...state.grants[i], ...g };
    return g;
  }

  // ------------------------------------------------------------------ router
  const routes = { '': renderPipeline, board: renderBoard, import: renderImport, grant: renderDetail };
  async function route() {
    const hash = location.hash.replace(/^#\/?/, '');
    const [name, arg] = hash.split('/');
    $$('.tabs a').forEach((a) => a.classList.toggle('active', a.dataset.route === (name || 'pipeline')));
    const view = $('#view');
    view.innerHTML = '<div class="empty">Loading…</div>';
    try {
      await loadAll();
      await (routes[name] || renderPipeline)(view, arg);
    } catch (e) { view.innerHTML = `<div class="empty">Could not load: ${esc(e.message)}<br><small>Is app.py running?</small></div>`; }
  }
  window.addEventListener('hashchange', route);

  // ------------------------------------------------------------------ pipeline
  function filtered() {
    const f = state.filters; const q = f.q.toLowerCase();
    let rows = state.grants.filter((g) =>
      (!q || `${g.name} ${g.org} ${g.summary} ${g.notes}`.toLowerCase().includes(q)) &&
      (!f.stage || g.stage === f.stage) && (!f.await || g.awaiting_action) && (!f.starred || g.starred) &&
      (!f.hideClosed || !CLOSED.has(g.stage)));
    const { key, asc } = state.sort;
    const cmp = {
      default: (a, b) => (b.starred - a.starred) || (b.awaiting_action - a.awaiting_action) || (b.score - a.score),
      name: (a, b) => a.name.localeCompare(b.name),
      stage: (a, b) => a.stage_index - b.stage_index,
      score: (a, b) => a.score - b.score,
      days_idle: (a, b) => (a.days_idle ?? -1) - (b.days_idle ?? -1),
      amount: (a, b) => (a.amount ?? -1) - (b.amount ?? -1),
      deadline: (a, b) => (a.deadline || '9999').localeCompare(b.deadline || '9999'),
      court: (a, b) => (a.court === 'Ours' ? 0 : 1) - (b.court === 'Ours' ? 0 : 1),
    }[key] || (() => 0);
    rows.sort(cmp);
    if (key !== 'default' && !asc) rows.reverse();
    return rows;
  }

  async function renderPipeline(view) {
    const f = state.filters; const S = state.settings.stages;
    const active = state.grants.filter((g) => !CLOSED.has(g.stage));
    const stats = { total: state.grants.length, active: active.length, awaiting: active.filter((g) => g.awaiting_action).length,
      stale: active.filter((g) => (g.days_idle || 0) >= 14).length, inbound: active.filter((g) => g.stage === 'Inbound').length };
    view.innerHTML = `
      <div class="toolbar">
        <input type="text" id="f-q" placeholder="Search grants, orgs, notes…" value="${esc(f.q)}">
        <select id="f-stage"><option value="">All stages</option>${S.map((s) => `<option ${f.stage === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
        <span class="toggle ${f.await ? 'on' : ''}" id="f-await">🔴 Needs my action</span>
        <span class="toggle ${f.starred ? 'on' : ''}" id="f-starred">★ Starred</span>
        <span class="toggle ${f.hideClosed ? 'on' : ''}" id="f-closed">Hide closed</span>
        <span class="muted" style="margin-left:auto" id="count"></span>
        <button class="btn primary" id="btn-triage" ${stats.inbound ? '' : 'disabled'}>⚡ Triage inbound (${stats.inbound})</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th data-sort="default" style="width:34px">★</th>
          <th data-sort="court" title="Red = ball in our court. Green = ball in theirs.">Court</th>
          <th data-sort="name">Grant</th><th data-sort="stage">Stage</th>
          <th data-sort="score" title="Reach × Impact × Confidence ÷ Effort">Score</th>
          <th data-sort="days_idle">Days idle</th><th data-sort="amount">Amount</th>
        </tr></thead><tbody></tbody></table>
        <div class="empty" id="empty" hidden></div>
      </div>`;
    const tbody = $('tbody', view);

    function draw() {
      const rows = filtered();
      $('#count').textContent = `${rows.length} of ${state.grants.length}`;
      $$('th[data-sort]', view).forEach((th) => { th.classList.toggle('sorted', th.dataset.sort === state.sort.key && state.sort.key !== 'default'); th.classList.toggle('asc', state.sort.asc); });
      tbody.innerHTML = rows.map((g) => `
        <tr class="${g.awaiting_action ? 'await' : ''}" data-id="${g.id}">
          <td><button class="star ${g.starred ? 'on' : ''}" data-star title="Star this grant">★</button></td>
          <td>${court(g.court, 40)}</td>
          <td class="name"><a href="#/grant/${g.id}">${esc(g.name)}</a><div class="org">${esc(g.org || '')}</div></td>
          <td><select class="stage-select" data-stage>${S.map((s) => `<option ${g.stage === s ? 'selected' : ''}>${s}</option>`).join('')}</select></td>
          <td><span class="score ${scoreClass(g.score)}" data-score title="Click to triage: Reach ${g.reach} × Impact ${g.impact} × ${g.confidence}% ÷ Effort ${g.effort}"><b>${g.score}</b></span></td>
          <td class="idle ${idleClass(g.days_idle)}">${g.days_idle == null ? '—' : g.days_idle + 'd'}</td>
          <td>${fmtMoney(g.amount)}</td>
        </tr>`).join('');
      $('#empty').hidden = rows.length > 0;
      $('#empty').textContent = state.grants.length ? 'No grants match these filters.' : 'No grants yet — use Import to load your spreadsheet or demo data, or add a prospect.';
    }
    draw();

    $('#f-q').addEventListener('input', (e) => { f.q = e.target.value; draw(); });
    $('#f-stage').addEventListener('change', (e) => { f.stage = e.target.value; draw(); });
    for (const [id, key] of [['#f-await', 'await'], ['#f-starred', 'starred'], ['#f-closed', 'hideClosed']]) {
      $(id).addEventListener('click', (e) => { f[key] = !f[key]; e.currentTarget.classList.toggle('on', f[key]); draw(); });
    }
    $$('th[data-sort]', view).forEach((th) => th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (state.sort.key === k) state.sort.asc = !state.sort.asc; else state.sort = { key: k, asc: k === 'name' || k === 'stage' };
      draw();
    }));
    tbody.addEventListener('click', async (e) => {
      const tr = e.target.closest('tr'); if (!tr) return;
      const id = Number(tr.dataset.id); const g = state.grants.find((x) => x.id === id);
      if (e.target.closest('[data-star]')) { await patchGrant(id, { starred: !g.starred }); draw(); }
      else if (e.target.closest('[data-score]')) ricePopover(e.target.closest('[data-score]'), g, draw);
    });
    tbody.addEventListener('change', async (e) => {
      if (!e.target.matches('[data-stage]')) return;
      const id = Number(e.target.closest('tr').dataset.id);
      await patchGrant(id, { stage: e.target.value }); draw(); toast('Stage updated');
    });
    $('#btn-triage').addEventListener('click', () => triageWizard(draw));
  }

  // RICE popover for one-click triage from the table
  function ricePopover(anchor, g, onDone) {
    $$('.popover').forEach((p) => p.remove());
    const r = anchor.getBoundingClientRect();
    const pop = el(`<div class="popover" style="top:${r.bottom + window.scrollY + 6}px;left:${Math.min(r.left + window.scrollX, window.innerWidth - 320)}px">
      <h3>Triage · ${esc(g.name)}</h3>${riceForm(g)}
      <div class="modal-foot" style="margin-top:8px"><button class="btn small" data-x>Done</button></div></div>`);
    document.body.appendChild(pop);
    wireRice(pop, async (vals) => { await patchGrant(g.id, vals); onDone(); });
    const off = (e) => { if (!pop.contains(e.target) && e.target !== anchor) { pop.remove(); document.removeEventListener('mousedown', off); } };
    setTimeout(() => document.addEventListener('mousedown', off), 0);
    $('[data-x]', pop).addEventListener('click', () => { pop.remove(); document.removeEventListener('mousedown', off); });
  }

  const RICE = [
    ['reach', 'Reach', 1, 5, 'How many people does it affect? 1 = a handful · 5 = thousands'],
    ['impact', 'Impact', 1, 5, 'For those people, how big is the effect? 1 = minor · 5 = transformational'],
    ['confidence', 'Confidence', 0, 100, 'Is it a bet or a sure thing? 50% = a bet · 90% = near-certain'],
    ['effort', 'Effort', 1, 5, 'How far are we from realising it? 1 = trivial · 5 = heavy lift'],
  ];
  function riceForm(g) {
    return RICE.map(([k, label, lo, hi, hint]) => `
      <div class="rice-row"><label>${label}</label><input type="range" min="${lo}" max="${hi}" step="${k === 'confidence' ? 5 : 1}" name="${k}" value="${g[k]}"><output>${g[k]}${k === 'confidence' ? '%' : ''}</output><div class="hint">${hint}</div></div>`).join('') +
      `<div class="score-live">Score <span data-live>${g.score}</span></div>`;
  }
  function calcScore(v) { return Math.round((v.reach * v.impact * (v.confidence / 100) / Math.max(1, v.effort)) * 100) / 100; }
  function readRice(root) { const v = {}; RICE.forEach(([k]) => { v[k] = Number($(`[name=${k}]`, root).value); }); return v; }
  function wireRice(root, onChange) {
    let t;
    $$('input[type=range]', root).forEach((inp) => inp.addEventListener('input', () => {
      const v = readRice(root);
      inp.nextElementSibling.textContent = inp.value + (inp.name === 'confidence' ? '%' : '');
      $('[data-live]', root).textContent = calcScore(v);
      clearTimeout(t); t = setTimeout(() => onChange(v), 350);
    }));
  }

  // Step-through triage of everything in Inbound
  function triageWizard(onDone) {
    const queue = state.grants.filter((g) => g.stage === 'Inbound').sort((a, b) => (a.days_idle ?? 0) - (b.days_idle ?? 0));
    let i = 0;
    const m = modal('<div></div>');
    function show() {
      if (i >= queue.length) { m.close(); onDone(); toast('Inbox zero 🎉'); return; }
      const g = queue[i];
      m.box.innerHTML = `
        <div class="modal-head"><h2>⚡ Triage ${i + 1} of ${queue.length}</h2><button class="btn ghost" data-close>✕</button></div>
        <div class="triage-card">
          <div><b style="font-size:16px">${esc(g.name)}</b> <span class="muted">· ${esc(g.org || 'unknown org')}</span> ${g.amount ? `<span class="pill">${fmtMoney(g.amount)}</span>` : ''} ${g.deadline ? `<span class="pill amber">due ${fmtDate(g.deadline)}</span>` : ''}</div>
          <div class="summary">${esc(g.summary || 'No summary yet.')}${g.notes ? `<br><span class="muted">${esc(g.notes)}</span>` : ''}</div>
          ${riceForm(g)}
          <div class="modal-foot">
            <button class="btn" data-act="skip">Skip <kbd>S</kbd></button>
            <button class="btn danger" data-act="Declined">Decline <kbd>D</kbd></button>
            <button class="btn primary" data-act="Prospect">Keep → Prospect <kbd>↵</kbd></button>
          </div>
          <div class="muted" style="font-size:12px;text-align:right">Scores save as you drag · <a href="#/grant/${g.id}">open full record</a></div>
        </div>`;
      wireRice(m.box, (v) => patchGrant(g.id, v));
      $$('[data-act]', m.box).forEach((b) => b.addEventListener('click', () => act(b.dataset.act)));
      $('[data-close]', m.box).addEventListener('click', () => { m.close(); onDone(); });
    }
    async function act(a) {
      const g = queue[i];
      if (a !== 'skip') { await patchGrant(g.id, { ...readRice(m.box), stage: a }); }
      i++; show();
    }
    const key = (e) => { if (!document.body.contains(m.bg)) { document.removeEventListener('keydown', key); return; }
      if (e.target.tagName === 'INPUT' && e.key !== 'Enter') return;
      if (e.key === 'Enter') act('Prospect'); else if (e.key.toLowerCase() === 'd') act('Declined'); else if (e.key.toLowerCase() === 's') act('skip'); };
    document.addEventListener('keydown', key);
    show();
  }

  // ------------------------------------------------------------------ board
  async function renderBoard(view) {
    const S = state.settings.stages;
    view.innerHTML = `<div class="toolbar"><h1 style="margin:0">Board</h1><span class="muted">Drag cards between stages. Red edge = waiting on us.</span></div><div class="board"></div>`;
    const board = $('.board', view);
    board.innerHTML = S.map((s) => {
      const cards = state.grants.filter((g) => g.stage === s).sort((a, b) => (b.starred - a.starred) || (b.score - a.score));
      return `<div class="col" data-stage="${s}"><h3>${s}<span>${cards.length}</span></h3>${cards.map((g) => `
        <div class="kcard ${g.awaiting_action ? 'await' : ''}" draggable="true" data-id="${g.id}">
          <div class="t"><a href="#/grant/${g.id}">${g.starred ? '★ ' : ''}${esc(g.name)}</a>${court(g.court, 30)}</div>
          <div class="m"><span class="pill ${scoreClass(g.score) === 'hi' ? 'green' : ''}">${g.score}</span><span>${esc(g.org || '')}</span>
            <span class="idle ${idleClass(g.days_idle)}" style="margin-left:auto">${g.days_idle == null ? '' : g.days_idle + 'd'}</span></div>
        </div>`).join('')}</div>`;
    }).join('');
    let dragId = null;
    board.addEventListener('dragstart', (e) => { const c = e.target.closest('.kcard'); if (!c) return; dragId = Number(c.dataset.id); e.dataTransfer.effectAllowed = 'move'; });
    $$('.col', board).forEach((col) => {
      col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('over'); });
      col.addEventListener('dragleave', () => col.classList.remove('over'));
      col.addEventListener('drop', async (e) => {
        e.preventDefault(); col.classList.remove('over');
        if (dragId == null) return;
        await patchGrant(dragId, { stage: col.dataset.stage }); dragId = null;
        renderBoard(view); toast(`Moved to ${col.dataset.stage}`);
      });
    });
  }

  // ------------------------------------------------------------------ import
  async function renderImport(view) {
    view.innerHTML = `
      <h1>Import grants</h1>
      <p class="muted">Bring in the spreadsheet you already have. Fill the template, upload it, check the column mapping, done.</p>
      <div class="steps">
        <div class="card"><h2><span class="step-num">1</span>Get the template</h2>
          <p>Download the Excel template, paste your grants in (one per row) and save. Only <b>Grant Name</b> is required — everything else can be filled in later.</p>
          <a class="btn primary" href="/api/template.xlsx" download>⬇ Download template (.xlsx)</a>
          <p class="muted" style="margin-top:10px;font-size:12px">Working in Google Sheets? File → Download → Microsoft Excel (.xlsx), then upload that.</p></div>
        <div class="card"><h2><span class="step-num">2</span>Upload it</h2>
          <div class="drop" id="drop">Drop an <b>.xlsx</b> or <b>.csv</b> here, or click to choose<input type="file" id="file" accept=".xlsx,.xlsm,.csv" hidden></div>
          <label class="field" style="margin-top:10px"><span>If a grant already exists…</span>
            <select id="mode"><option value="append">Add these as new grants (append)</option><option value="replace">Replace everything with this file</option></select></label></div>
        <div class="card soon-card"><h2><span class="step-num">3</span>Connect Google Sheets <span class="pill soon">coming soon</span></h2>
          <p class="muted">Paste a Google Sheets link and GrantDesk will keep the pipeline in sync automatically. For now, download as .xlsx and upload in step 2.</p>
          <input type="text" placeholder="https://docs.google.com/spreadsheets/d/…" disabled></div>
      </div>
      <div id="preview-area"></div>
      <div class="card" style="margin-top:16px"><h2>Demo & backup</h2>
        <div class="toolbar" style="margin:0">
          <button class="btn" id="btn-seed">Load demo data</button>
          <a class="btn" href="/api/export.xlsx" download>Export all grants (.xlsx)</a>
          <a class="btn" href="/api/export.json" download>Backup (.json)</a>
          <span class="muted" style="font-size:12px">Demo data replaces the current grants — handy for a first look. Your data lives in <code>grantdesk.db</code> next to app.py.</span>
        </div></div>`;
    const drop = $('#drop'); const file = $('#file');
    drop.addEventListener('click', () => file.click());
    drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) preview(e.dataTransfer.files[0]); });
    file.addEventListener('change', () => { if (file.files[0]) preview(file.files[0]); });
    $('#btn-seed').addEventListener('click', async () => {
      if (!confirm('Replace current grants with 14 demo grants?')) return;
      const r = await api('/api/seed', { method: 'POST', body: {} }); toast(`Loaded ${r.created} demo grants`); location.hash = '#/';
    });

    async function preview(f) {
      const fd = new FormData(); fd.append('file', f);
      const area = $('#preview-area'); area.innerHTML = '<div class="card">Reading file…</div>';
      let p;
      try { p = await api('/api/import/preview', { method: 'POST', body: fd }); } catch (e) { area.innerHTML = `<div class="card" style="color:var(--red)">${esc(e.message)}</div>`; return; }
      const fieldOpts = (sel) => `<option value="">— ignore —</option>` + p.fields.map(([f, h]) => `<option value="${f}" ${sel === f ? 'selected' : ''}>${h}</option>`).join('');
      const inv = {}; Object.entries(p.mapping).forEach(([f, i]) => { inv[i] = f; });
      area.innerHTML = `<div class="card" style="margin-top:16px">
        <h2>Preview · ${esc(p.filename)} <span class="muted" style="font-weight:400">${p.total} row(s)</span></h2>
        <p class="muted">Columns were matched automatically. Fix any mismatch using the dropdowns, then import. Rows without a Grant Name are skipped.</p>
        <div class="preview"><table><thead><tr>${p.headers.map((h, i) => `<th>${esc(h)}<select data-col="${i}">${fieldOpts(inv[i])}</select></th>`).join('')}</tr></thead>
          <tbody>${p.rows.slice(1, 9).map((r) => `<tr>${p.headers.map((_, i) => `<td>${esc(r[i] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
        ${p.total > 8 ? `<p class="muted" style="font-size:12px">Showing first 8 of ${p.total}.</p>` : ''}
        <div class="modal-foot"><span id="map-warn" style="color:var(--red);margin-right:auto"></span><button class="btn primary" id="btn-commit">Import ${p.total} grant(s)</button></div></div>`;
      const check = () => { const has = $$('select[data-col]').some((s) => s.value === 'name'); $('#map-warn').textContent = has ? '' : 'Pick which column is the Grant Name.'; $('#btn-commit').disabled = !has; };
      $$('select[data-col]').forEach((s) => s.addEventListener('change', (e) => {
        $$('select[data-col]').forEach((o) => { if (o !== e.target && o.value && o.value === e.target.value) o.value = ''; }); check(); }));
      check();
      $('#btn-commit').addEventListener('click', async () => {
        const mapping = {}; $$('select[data-col]').forEach((s) => { if (s.value) mapping[s.value] = Number(s.dataset.col); });
        const mode = $('#mode').value;
        if (mode === 'replace' && !confirm('This deletes all existing grants first. Continue?')) return;
        const r = await api('/api/import/commit', { method: 'POST', body: { rows: p.rows, mapping, mode } });
        toast(`Imported ${r.created} grant(s)${r.skipped ? `, skipped ${r.skipped} without a name` : ''}`); location.hash = '#/';
      });
    }
  }

  // ------------------------------------------------------------------ detail
  async function renderDetail(view, id) {
    const S = state.settings.stages;
    let g;
    try { g = await api(`/api/grants/${id}`); } catch { view.innerHTML = '<div class="empty">Grant not found. <a href="#/">Back to pipeline</a></div>'; return; }
    const stageIdx = S.indexOf(g.stage);
    const stepBtn = (s, i) => { const terminal = CLOSED.has(s); const cls = [s === g.stage ? 'cur' : (!terminal && i < stageIdx ? 'done' : ''), terminal ? 'terminal' : '', s === 'Declined' ? 'bad' : s === 'Awarded' ? 'good' : ''].join(' '); return `<button class="${cls}" data-stage="${s}">${s}</button>`; };
    view.innerHTML = `
      <div class="toolbar" style="margin-bottom:6px"><a href="#/">← Pipeline</a> <span class="muted">/</span> <span class="muted">${esc(g.org || '')}</span></div>
      <div class="detail-head">
        ${court(g.court, 64)}
        <div style="flex:1;min-width:280px">
          <h1><button class="star ${g.starred ? 'on' : ''}" id="d-star">★</button><input id="d-name" value="${esc(g.name)}"></h1>
          <div class="stepper">${S.map(stepBtn).join('')}</div>
          <div class="toolbar" style="margin:0;gap:14px">
            <span class="court-toggle"><button class="ours ${g.court === 'Ours' ? 'on' : ''}" data-court="Ours">🔴 Our court</button><button class="theirs ${g.court === 'Theirs' ? 'on' : ''}" data-court="Theirs">🟢 Their court</button></span>
            ${g.court_override ? `<span class="muted" style="font-size:12px">set manually · <a href="#" id="d-auto">follow emails again</a></span>` : `<span class="muted" style="font-size:12px">follows the last email automatically</span>`}
            <span class="idle ${idleClass(g.days_idle)}">${g.days_idle == null ? 'No interactions yet' : `${g.days_idle} day(s) since last interaction`}</span>
            ${g.awaiting_action ? '<span class="pill red">! waiting for your action</span>' : ''}
          </div>
        </div>
        <div><button class="btn danger small" id="d-delete">Delete</button></div>
      </div>
      <div class="detail-grid">
        <div>
          <div class="card" style="margin-bottom:16px"><h3>Priority score · <span id="d-score" style="color:var(--text)">${g.score}</span></h3>${riceForm(g)}</div>
          <div class="card" style="margin-bottom:16px"><h3>Details</h3>
            <dl class="kv">
              <dt>Organisation</dt><dd><input id="d-org" value="${esc(g.org || '')}"></dd>
              <dt>Amount</dt><dd><input id="d-amount" type="number" value="${g.amount ?? ''}" placeholder="—"></dd>
              <dt>Deadline</dt><dd><input id="d-deadline" type="date" value="${esc(g.deadline || '')}"></dd>
              <dt>Last interaction</dt><dd>${fmtDate(g.last_interaction)}</dd>
              <dt>Created</dt><dd>${fmtDate(g.created_at)}</dd>
            </dl>
            <label class="field" style="margin-top:10px"><span>Summary</span><textarea id="d-summary">${esc(g.summary || '')}</textarea></label>
            <label class="field"><span>Notes</span><textarea id="d-notes">${esc(g.notes || '')}</textarea></label>
            <div class="muted" style="font-size:12px">Changes save automatically.</div>
          </div>
        </div>
        <div>
          <div class="card ai-box" style="margin-bottom:16px">
            <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">✨ Proposed next step</h3>
              <span>${state.settings.has_api_key ? '<span class="pill green">AI</span>' : '<span class="pill soon" title="Add an API key in Settings for full AI suggestions">rule-based · AI coming soon</span>'}</span></div>
            <div id="ai-body" class="muted">Thinking…</div>
          </div>
          <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><h3 style="margin:0">Activity log</h3>
              <span><button class="btn small" id="btn-log">+ Log email · meeting · call · note</button> ${state.settings.has_api_key ? '<button class="btn small" id="btn-summarise">Summarise with AI</button>' : ''}</span></div>
            <div id="threads">${threadsHtml(g)}</div>
            <div class="compose">
              <h3>Reply / send email</h3>
              <label class="field"><span>To</span><input id="c-to" value="${esc(g.contacts.filter((k) => k.email).map((k) => k.email).join(', '))}"></label>
              <label class="field"><span>Subject</span><input id="c-subject" value="${esc(g.threads[0] ? (g.threads[0].subject.match(/^re:/i) ? g.threads[0].subject : 'Re: ' + g.threads[0].subject) : g.name)}"></label>
              <label class="field"><span>Message</span><textarea id="c-body" placeholder="Write your reply…"></textarea></label>
              <div class="send-row">
                <button class="btn primary" id="c-send">Send ✉︎</button><span class="pill soon">sending coming soon — saved to the thread as queued</span>
                <button class="btn" id="c-sent" title="Already sent it from your mail client? Log it here so the court flips.">Log as already sent</button>
              </div>
            </div>
          </div>
        </div>
        <div>
          <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">People</h3><button class="btn small" id="btn-person">+ Add</button></div>
            <div id="people">${peopleHtml(g)}</div></div>
        </div>
      </div>`;

    // --- wiring
    const save = debounce(async (body) => { await patchGrant(g.id, body); toast('Saved'); }, 500);
    $('#d-name').addEventListener('input', (e) => save({ name: e.target.value }));
    $('#d-org').addEventListener('input', (e) => save({ org: e.target.value }));
    $('#d-amount').addEventListener('input', (e) => save({ amount: e.target.value }));
    $('#d-deadline').addEventListener('change', (e) => save({ deadline: e.target.value }));
    $('#d-summary').addEventListener('input', (e) => save({ summary: e.target.value }));
    $('#d-notes').addEventListener('input', (e) => save({ notes: e.target.value }));
    $('#d-star').addEventListener('click', async (e) => { g.starred = !g.starred; e.target.classList.toggle('on', g.starred); await patchGrant(g.id, { starred: g.starred }); });
    $$('.stepper button', view).forEach((b) => b.addEventListener('click', async () => { await patchGrant(g.id, { stage: b.dataset.stage }); renderDetail(view, id); }));
    $$('[data-court]', view).forEach((b) => b.addEventListener('click', async () => { await patchGrant(g.id, { court: b.dataset.court }); renderDetail(view, id); }));
    $('#d-auto')?.addEventListener('click', async (e) => { e.preventDefault(); await patchGrant(g.id, { court_override: false }); renderDetail(view, id); });
    $('#d-delete').addEventListener('click', async () => { if (!confirm(`Delete "${g.name}" and all its emails?`)) return; await api(`/api/grants/${g.id}`, { method: 'DELETE' }); location.hash = '#/'; });
    wireRice(view, async (v) => { const u = await patchGrant(g.id, v); $('#d-score').textContent = u.score; });

    // AI suggestion
    api(`/api/grants/${g.id}/suggest`).then((s) => {
      $('#ai-body').className = '';
      $('#ai-body').innerHTML = `<div class="headline">${esc(s.headline)}</div>${s.steps?.length ? `<ul>${s.steps.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${s.draft_subject ? `<div style="margin-top:10px"><button class="btn small" id="ai-draft">Use suggested email draft</button></div>` : ''}
        ${s.llm_error ? `<div class="muted" style="font-size:11px;margin-top:6px">AI call failed (${esc(s.llm_error)}) — showing rule-based suggestion.</div>` : ''}`;
      $('#ai-draft')?.addEventListener('click', () => { $('#c-subject').value = s.draft_subject; $('#c-body').value = s.draft_body || ''; $('#c-body').focus(); });
    }).catch((e) => { $('#ai-body').textContent = 'Could not get a suggestion: ' + e.message; });

    $('#btn-summarise')?.addEventListener('click', async (e) => {
      e.target.disabled = true; e.target.textContent = 'Summarising…';
      const r = await api(`/api/grants/${g.id}/summarise`, { method: 'POST', body: {} });
      g.threads = r.threads; $('#threads').innerHTML = threadsHtml(g); e.target.disabled = false; e.target.textContent = 'Summarise with AI';
    });

    // compose
    const queueMsg = async (status) => {
      const body = $('#c-body').value.trim(); if (!body) { toast('Write a message first'); return; }
      await api(`/api/grants/${g.id}/messages`, { method: 'POST', body: { kind: 'email', direction: 'out', status, sender: state.settings.our_email, recipients: $('#c-to').value, subject: $('#c-subject').value, body } });
      toast(status === 'queued' ? 'Saved to thread as queued (sending is coming soon). Ball is now in their court.' : 'Logged as sent. Ball is now in their court.');
      renderDetail(view, id);
    };
    $('#c-send').addEventListener('click', () => queueMsg('queued'));
    $('#c-sent').addEventListener('click', () => queueMsg('sent'));
    $('#btn-log').addEventListener('click', () => logInboundModal(g, () => renderDetail(view, id)));

    // people
    $('#btn-person').addEventListener('click', () => personModal(g, null, () => renderDetail(view, id)));
    $('#people').addEventListener('click', async (e) => {
      const p = e.target.closest('.person'); if (!p) return;
      const cid = Number(p.dataset.id); const k = g.contacts.find((x) => x.id === cid);
      if (e.target.closest('[data-del]')) { if (confirm(`Remove ${k.name}?`)) { await api(`/api/contacts/${cid}`, { method: 'DELETE' }); renderDetail(view, id); } }
      else if (e.target.closest('[data-edit]')) personModal(g, k, () => renderDetail(view, id));
      else if (e.target.closest('[data-mail]')) { $('#c-to').value = k.email; $('#c-body').focus(); }
    });
    $('#threads').addEventListener('click', async (e) => {
      const b = e.target.closest('[data-del-msg]'); if (!b) return;
      if (confirm('Delete this message from the record?')) { await api(`/api/messages/${b.dataset.delMsg}`, { method: 'DELETE' }); renderDetail(view, id); }
    });
  }

  function threadsHtml(g) {
    if (!g.threads.length) return '<div class="muted" style="padding:10px 0">Nothing logged yet. Add an email, meeting, call or note.</div>';
    const icon = { email: '✉︎', meeting: '🤝', call: '📞', note: '📝' };
    return g.threads.map((t, i) => `<details class="thread" ${i === 0 ? 'open' : ''}>
      <summary><span class="kind-ic">${icon[t.kind] || '✉︎'}</span><b>${esc(t.subject)}</b><span class="sum">${esc(t.summary)}</span></summary>
      ${t.messages.map((m) => `<div class="msg ${m.direction}">
        <div class="h"><b>${m.kind !== 'email' ? `${icon[m.kind]} ${m.kind}` : m.direction === 'in' ? '📥 ' + esc(m.sender || 'them') : '📤 us'}</b>${m.kind === 'email' ? `<span>→ ${esc(m.recipients)}</span>` : m.direction === 'in' ? '<span>→ ball to us</span>' : m.direction === 'out' ? '<span>→ ball to them</span>' : ''}<span>${esc(m.sent_at.slice(0, 16))}</span>
          ${m.status === 'queued' ? '<span class="pill soon">queued · not sent</span>' : ''}<button class="btn ghost small" style="margin-left:auto" data-del-msg="${m.id}" title="Delete">✕</button></div>
        <pre>${esc(m.body)}</pre></div>`).join('')}</details>`).join('');
  }
  function peopleHtml(g) {
    if (!g.contacts.length) return '<div class="muted" style="padding:10px 0">No one linked yet.</div>';
    return g.contacts.map((k) => `<div class="person" data-id="${k.id}"><span class="avatar">${initials(k.name)}</span>
      <div class="who"><b>${esc(k.name)}${k.is_primary ? ' <span class="pill" style="font-size:10px">primary</span>' : ''}</b><span>${esc(k.role || '')}</span><span>${esc(k.email || '')}</span></div>
      <button class="btn ghost small" data-mail title="Email">✉︎</button><button class="btn ghost small x" data-edit title="Edit">✎</button><button class="btn ghost small x" data-del title="Remove">✕</button></div>`).join('');
  }

  function personModal(g, k, done) {
    const m = modal(`<div class="modal-head"><h2>${k ? 'Edit' : 'Add'} person</h2><button class="btn ghost" data-close>✕</button></div>
      <label class="field"><span>Name</span><input id="p-name" value="${esc(k?.name || '')}"></label>
      <label class="field"><span>Email</span><input id="p-email" type="email" value="${esc(k?.email || '')}"></label>
      <label class="field"><span>Role</span><input id="p-role" value="${esc(k?.role || '')}" placeholder="e.g. Programme lead"></label>
      <div class="modal-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="p-save">Save</button></div>`, { narrow: true });
    $('#p-name', m.box).focus();
    $('#p-save', m.box).addEventListener('click', async () => {
      const body = { name: $('#p-name', m.box).value, email: $('#p-email', m.box).value, role: $('#p-role', m.box).value };
      if (k) await api(`/api/contacts/${k.id}`, { method: 'PATCH', body }); else await api(`/api/grants/${g.id}/contacts`, { method: 'POST', body: { ...body, is_primary: g.contacts.length === 0 } });
      m.close(); done();
    });
  }

  function logInboundModal(g, done) {
    const m = modal(`<div class="modal-head"><h2>Log an interaction</h2><button class="btn ghost" data-close>✕</button></div>
      <div class="kind-tabs">
        <button class="on" data-kind="email">✉︎ Email received</button><button data-kind="meeting">🤝 Meeting</button><button data-kind="call">📞 Call</button><button data-kind="note">📝 Note</button>
      </div>
      <div data-only="email"><label class="field"><span>From</span><input id="l-from" value="${esc(g.contacts[0]?.email || '')}"></label></div>
      <label class="field"><span id="l-subject-label">Subject</span><input id="l-subject" value="${esc(g.threads.find((t) => t.kind === 'email') ? 'Re: ' + g.threads.find((t) => t.kind === 'email').subject.replace(/^re:\s*/i, '') : g.name)}"></label>
      <label class="field"><span id="l-body-label">Email text</span><textarea id="l-body" style="min-height:150px"></textarea></label>
      <div class="row">
        <label class="field"><span>When</span><input id="l-date" type="date" value="${new Date().toISOString().slice(0, 10)}"></label>
        <label class="field" data-only="meeting call"><span>Who has the next action?</span>
          <select id="l-next"><option value="in">Us — ball in our court</option><option value="out">Them — ball in their court</option></select></label>
      </div>
      <p class="muted" style="font-size:12px" data-only="email">Forwarding emails straight into GrantDesk from your inbox <span class="pill soon">coming soon</span></p>
      <div class="modal-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="l-save">Add to log</button></div>`);
    let kind = 'email';
    const labels = { email: ['Subject', 'Email text'], meeting: ['Meeting title', 'What happened / decisions'], call: ['Call with', 'Notes from the call'], note: ['Title', 'Note'] };
    const subjDefault = { meeting: `Meeting with ${g.org || g.name}`, call: `Call with ${g.contacts[0]?.name || g.org || ''}`.trim(), note: '' };
    const emailSubj = $('#l-subject', m.box).value;
    function setKind(k) {
      kind = k;
      $$('.kind-tabs button', m.box).forEach((b) => b.classList.toggle('on', b.dataset.kind === k));
      $$('[data-only]', m.box).forEach((e) => { e.hidden = !e.dataset.only.split(' ').includes(k); });
      $('#l-subject-label', m.box).textContent = labels[k][0]; $('#l-body-label', m.box).textContent = labels[k][1];
      $('#l-subject', m.box).value = k === 'email' ? emailSubj : subjDefault[k];
    }
    setKind('email');
    $$('.kind-tabs button', m.box).forEach((b) => b.addEventListener('click', () => setKind(b.dataset.kind)));
    $('#l-body', m.box).focus();
    $('#l-body', m.box).addEventListener('input', (e) => { if (kind !== 'email') return; const t = e.target.value; const s2 = t.match(/^\s*subject\s*:\s*(.+)$/im); const f = t.match(/^\s*from\s*:\s*(.+)$/im); if (s2) $('#l-subject', m.box).value = s2[1].trim(); if (f) $('#l-from', m.box).value = f[1].trim(); });
    $('#l-save', m.box).addEventListener('click', async () => {
      const body = $('#l-body', m.box).value.trim(); if (!body) return;
      const direction = kind === 'email' ? 'in' : kind === 'note' ? 'note' : $('#l-next', m.box).value;
      await api(`/api/grants/${g.id}/messages`, { method: 'POST', body: { kind, direction, sender: kind === 'email' ? $('#l-from', m.box).value : '', recipients: kind === 'email' ? state.settings.our_email : '', subject: $('#l-subject', m.box).value, body, sent_at: $('#l-date', m.box).value + ' 09:00:00' } });
      m.close(); done(); toast(kind === 'note' ? 'Note added' : direction === 'in' ? 'Logged — ball is in our court' : 'Logged — ball is in their court');
    });
  }

  // ------------------------------------------------------------------ add prospect
  function addProspectModal() {
    const m = modal(`<div class="modal-head"><h2>Add new prospect</h2><button class="btn ghost" data-close>✕</button></div>
      <div class="row" style="align-items:flex-start">
        <div style="flex:1.1">
          <label class="field"><span>Paste the email or opportunity text</span><textarea id="a-text" style="min-height:220px" placeholder="From: Maya Patel <maya@greenways.org>&#10;Subject: Funding for Riverside garden&#10;&#10;Hi, we'd love to apply for £25k to…"></textarea></label>
          <div class="toolbar" style="margin:0"><button class="btn" id="a-parse">✨ Fill in from text</button><span class="muted" style="font-size:12px">pulls out names, emails, org, amount</span></div>
          <p class="muted" style="font-size:12px;margin-top:14px">Or forward opportunities to <code>inbox@grantdesk</code> and they appear here automatically <span class="pill soon">coming soon</span></p>
        </div>
        <div style="flex:1">
          <label class="field"><span>Grant name *</span><input id="a-name"></label>
          <label class="field"><span>Organisation</span><input id="a-org"></label>
          <label class="field"><span>Summary</span><textarea id="a-summary" style="min-height:60px"></textarea></label>
          <div class="row"><label class="field"><span>Amount</span><input id="a-amount" type="number"></label><label class="field"><span>Deadline</span><input id="a-deadline" type="date"></label></div>
          <label class="field"><span>Stage</span><select id="a-stage">${state.settings.stages.map((s) => `<option>${s}</option>`).join('')}</select></label>
          <label class="field"><span>People (Name &lt;email&gt;, one per line)</span><textarea id="a-contacts" style="min-height:60px"></textarea></label>
          <label class="field"><input type="checkbox" id="a-log" checked> Log the pasted text as the first inbound email</label>
        </div>
      </div>
      <div class="modal-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="a-save">Add prospect</button></div>`);
    const box = m.box; $('#a-text', box).focus();
    const parse = async () => {
      const text = $('#a-text', box).value; if (!text.trim()) return;
      const b = $('#a-parse', box); b.disabled = true; b.textContent = 'Reading…';
      try {
        const p = await api('/api/parse-email', { method: 'POST', body: { text } });
        const set = (id, v) => { if (v != null && v !== '' && !$(id, box).value) $(id, box).value = v; };
        set('#a-name', p.name); set('#a-org', p.org); set('#a-summary', p.summary); set('#a-amount', p.amount);
        set('#a-contacts', (p.contacts || []).map((k) => `${k.name} <${k.email}>${k.role ? ' · ' + k.role : ''}`).join('\n'));
        box._rice = ['reach', 'impact', 'confidence', 'effort'].reduce((o, k) => (p[k] != null ? { ...o, [k]: p[k] } : o), {});
        toast(p.source === 'llm' ? 'Filled in with AI' : 'Filled in from the text');
      } catch (e) { toast('Could not parse: ' + e.message); }
      b.disabled = false; b.textContent = '✨ Fill in from text';
    };
    $('#a-parse', box).addEventListener('click', parse);
    $('#a-text', box).addEventListener('paste', () => setTimeout(() => { if (!$('#a-name', box).value) parse(); }, 50));
    $('#a-save', box).addEventListener('click', async () => {
      const name = $('#a-name', box).value.trim(); if (!name) { toast('Give it a name'); $('#a-name', box).focus(); return; }
      const contacts = $('#a-contacts', box).value.split(/\n|;/).map((l) => l.trim()).filter(Boolean).map((l) => {
        const mm = l.match(/^(.*?)\s*<([^>]+)>\s*(?:·\s*(.*))?$/); return mm ? { name: mm[1].trim(), email: mm[2].trim(), role: mm[3] || '' } : (l.includes('@') ? { name: l.split('@')[0], email: l } : { name: l, email: '' }); });
      const g = await api('/api/grants', { method: 'POST', body: { name, org: $('#a-org', box).value, summary: $('#a-summary', box).value, amount: $('#a-amount', box).value, deadline: $('#a-deadline', box).value,
        stage: $('#a-stage', box).value, contacts, source_text: $('#a-text', box).value, log_as_email: $('#a-log', box).checked, ...(box._rice || {}) } });
      m.close(); toast('Prospect added'); location.hash = `#/grant/${g.id}`;
    });
  }

  // ------------------------------------------------------------------ settings
  function settingsModal() {
    const s = state.settings;
    const m = modal(`<div class="modal-head"><h2>Settings</h2><button class="btn ghost" data-close>✕</button></div>
      <label class="field"><span>Your organisation</span><input id="s-org" value="${esc(s.org_name)}"></label>
      <label class="field"><span>Your grants inbox (shown as sender on logged emails)</span><input id="s-email" value="${esc(s.our_email)}"></label>
      <hr style="border:0;border-top:1px solid var(--line);margin:14px 0">
      <h3>AI suggestions</h3>
      <p class="muted" style="font-size:12px">Optional. With an Anthropic API key, next-step suggestions, thread summaries and prospect parsing use a real model. Without one, GrantDesk uses built-in rules. The key is stored only in <code>grantdesk.db</code> on this computer and sent only to api.anthropic.com.</p>
      <label class="field"><span>Anthropic API key ${s.has_api_key ? '<span class="pill green">set</span>' : ''}</span><input id="s-key" type="password" placeholder="${s.has_api_key ? '•••••••• (leave blank to keep)' : 'sk-ant-…'}" autocomplete="off"></label>
      <label class="field"><span>Model</span><input id="s-model" value="${esc(s.model)}"></label>
      <div class="modal-foot">${s.has_api_key ? '<button class="btn danger" id="s-clear" style="margin-right:auto">Remove key</button>' : ''}<button class="btn" data-close>Cancel</button><button class="btn primary" id="s-save">Save</button></div>`, { narrow: true });
    $('#s-save', m.box).addEventListener('click', async () => {
      const body = { org_name: $('#s-org', m.box).value, our_email: $('#s-email', m.box).value, model: $('#s-model', m.box).value };
      const key = $('#s-key', m.box).value.trim(); if (key) body.api_key = key;
      await api('/api/settings', { method: 'POST', body }); m.close(); toast('Settings saved'); route();
    });
    $('#s-clear', m.box)?.addEventListener('click', async () => { await api('/api/settings', { method: 'POST', body: { api_key: '' } }); m.close(); toast('API key removed'); route(); });
  }

  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

  // ------------------------------------------------------------------ boot
  $('#btn-add').addEventListener('click', addProspectModal);
  $('#btn-settings').addEventListener('click', settingsModal);
  route();
})();
