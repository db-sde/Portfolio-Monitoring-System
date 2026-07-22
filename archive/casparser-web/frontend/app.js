(() => {
  'use strict';

  const CHECK_SVG = '<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>';

  const TYPE_LABELS = {
    PURCHASE: 'Purchase', PURCHASE_SIP: 'SIP Purchase', REDEMPTION: 'Redemption',
    DIVIDEND_PAYOUT: 'IDCW Payout', DIVIDEND_REINVEST: 'IDCW Reinvest',
    SWITCH_IN: 'Switch In', SWITCH_IN_MERGER: 'Switch In (Merger)',
    SWITCH_OUT: 'Switch Out', SWITCH_OUT_MERGER: 'Switch Out (Merger)',
    STT_TAX: 'STT', STAMP_DUTY_TAX: 'Stamp Duty', TDS_TAX: 'TDS',
    SEGREGATION: 'Segregation', GIFT_IN: 'Gift In', GIFT_OUT: 'Gift Out',
    MISC: 'Misc', UNKNOWN: 'Unknown', REVERSAL: 'Reversal',
  };
  const TYPE_GROUPS = [
    { label: 'Money in', types: ['PURCHASE', 'PURCHASE_SIP', 'DIVIDEND_REINVEST', 'SWITCH_IN', 'SWITCH_IN_MERGER', 'GIFT_IN'] },
    { label: 'Money out', types: ['REDEMPTION', 'SWITCH_OUT', 'SWITCH_OUT_MERGER', 'GIFT_OUT'] },
    { label: 'Charges & tax', types: ['STT_TAX', 'STAMP_DUTY_TAX', 'TDS_TAX'] },
    { label: 'Other', types: ['DIVIDEND_PAYOUT', 'SEGREGATION', 'REVERSAL', 'MISC', 'UNKNOWN'] },
  ];
  const ALL_TYPES = TYPE_GROUPS.flatMap(g => g.types);
  // scheme.type comes from an ISIN database lookup and is only ever the
  // literal strings "EQUITY" or "DEBT" when it resolves — anything else
  // (unmatched schemes come back as "N/A", not "UNKNOWN") must still show
  // up somewhere, so every other value is folded into a catch-all bucket.
  const CAT_ORDER = ['EQUITY', 'DEBT', 'OTHER'];
  const CAT_LABEL = { EQUITY: 'Equity', DEBT: 'Debt', OTHER: 'Other' };
  const normalizeCat = (type) => {
    const t = (type || '').toUpperCase();
    return (t === 'EQUITY' || t === 'DEBT') ? t : 'OTHER';
  };
  const DAY_MS = 86400000;

  // ---------- formatters ----------
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (n, d = 0) => {
    n = Number(n) || 0;
    const neg = n < 0;
    return (neg ? '−' : '') + '₹' + Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  const units = (n) => Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  const num0 = (n) => { n = Number(n) || 0; return (n < 0 ? '−' : '') + Math.round(Math.abs(n)).toLocaleString('en-IN'); };
  const num4 = (n) => Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  const fdate = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
  };
  const sign = (n) => (n >= 0 ? '+' : '−');
  const maskPan = (pan) => (pan && pan.length >= 4 ? pan.slice(0, 2) + '•'.repeat(Math.max(0, pan.length - 4)) + pan.slice(-2) : (pan || '—'));
  const gainClass = (n) => (n >= 0 ? 'money-in' : 'money-out');

  // ---------- state ----------
  const state = {
    screen: 'upload',       // upload | parsing | dashboard
    mode: 'mf',             // mf | demat
    tab: 'summary',         // summary | transactions | gains
    file: null,
    password: '',
    schemeFilter: 'all',
    typeFilter: new Set(ALL_TYPES),
    filterOpen: false,
    gainsFy: null,
    raw: null,              // { data, gains, gifts, gains_error, isSample }
  };

  const $ = (id) => document.getElementById(id);
  const screens = { upload: $('screen-upload'), parsing: $('screen-parsing'), dashboard: $('screen-dashboard') };

  function showScreen(name) {
    state.screen = name;
    Object.entries(screens).forEach(([k, el]) => { el.hidden = k !== name; });
  }

  // ---------- upload screen wiring ----------
  const dropzone = $('dropzone');
  const fileInput = $('file-input');
  const dropzoneLabel = $('dropzone-label');
  const passwordInput = $('password-input');
  const parseBtn = $('parse-btn');
  const uploadError = $('upload-error');
  const uploadErrorText = $('upload-error-text');

  function setFile(f) {
    state.file = f || null;
    dropzoneLabel.innerHTML = f ? esc(f.name) : 'Drop your CAS PDF here, or <em>browse</em>';
    parseBtn.disabled = !f;
  }
  function showUploadError(msg, retryable) {
    uploadErrorText.textContent = msg;
    $('parser-retry').hidden = !retryable;
    uploadError.hidden = false;
  }
  function clearUploadError() { uploadError.hidden = true; $('parser-retry').hidden = true; }

  fileInput.addEventListener('change', (e) => { clearUploadError(); setFile(e.target.files[0]); });
  ['dragover'].forEach(evt => dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(evt => dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove('drag'); }));
  dropzone.addEventListener('drop', (e) => {
    clearUploadError();
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) setFile(f);
  });
  passwordInput.addEventListener('input', (e) => { state.password = e.target.value; });
  $('password-toggle').addEventListener('click', () => {
    const shown = passwordInput.type === 'text';
    passwordInput.type = shown ? 'password' : 'text';
    $('password-toggle').setAttribute('aria-label', shown ? 'Show password' : 'Hide password');
    $('password-toggle').innerHTML = shown
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z"/><circle cx="12" cy="12" r="3"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a18.6 18.6 0 0 1 5.06-5.94M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a18.6 18.6 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>';
  });
  parseBtn.addEventListener('click', () => submitParse({ forcePdfminer: false }));
  $('parser-retry').addEventListener('click', () => submitParse({ forcePdfminer: true }));
  $('sample-cams').addEventListener('click', () => loadSample('mf'));
  $('sample-nsdl').addEventListener('click', () => loadSample('demat'));
  $('reset-btn').addEventListener('click', resetToUpload);
  $('download-json').addEventListener('click', downloadJson);
  $('download-csv').addEventListener('click', downloadFullCsv);
  $('export-pdf').addEventListener('click', () => window.print());

  function resetToUpload() {
    setFile(null);
    passwordInput.value = '';
    state.password = '';
    state.schemeFilter = 'all';
    state.typeFilter = new Set(ALL_TYPES);
    state.tab = 'summary';
    clearUploadError();
    showScreen('upload');
  }

  async function submitParse({ forcePdfminer = false } = {}) {
    if (!state.file) return;
    clearUploadError();
    $('parsing-filename').textContent = state.file.name + (forcePdfminer ? ' (alternate parser)' : '');
    showScreen('parsing');
    const start = performance.now();

    const fd = new FormData();
    fd.append('file', state.file);
    fd.append('password', state.password || '');
    fd.append('include_gains', 'true');
    fd.append('force_pdfminer', forcePdfminer ? 'true' : 'false');

    try {
      const res = await fetch('/api/parse', { method: 'POST', body: fd });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        const detail = body && body.detail;
        const msg = (detail && detail.message) || 'Something went wrong parsing this statement.';
        // Only offer the alternate-parser retry for a genuine "couldn't read
        // this PDF" failure, on the first attempt — not for a wrong password
        // or an already-retried attempt.
        const retryable = !forcePdfminer && detail && detail.error_code === 'UNREADABLE_FILE';
        const err = new Error(msg);
        err.retryable = retryable;
        throw err;
      }
      // keep the progress animation from feeling instant/jarring on fast parses
      const elapsed = performance.now() - start;
      if (elapsed < 900) await new Promise(r => setTimeout(r, 900 - elapsed));
      state.raw = { data: body.data, gains: body.gains, gifts: body.gifts, gains_error: body.gains_error, mode: body.mode, isSample: false };
      state.mode = body.mode;
      state.tab = 'summary';
      state.schemeFilter = 'all';
      state.typeFilter = new Set(ALL_TYPES);
      renderDashboard();
      showScreen('dashboard');
    } catch (err) {
      showScreen('upload');
      showUploadError(err.message || 'Could not reach the server. Please try again.', !!err.retryable);
    }
  }

  async function loadSample(mode) {
    clearUploadError();
    $('parsing-filename').textContent = mode === 'demat' ? 'NSDL_CAS_sample.pdf (demo)' : 'CAMS_CAS_sample.pdf (demo)';
    showScreen('parsing');
    try {
      const res = await fetch('/api/sample?mode=' + mode);
      const body = await res.json();
      await new Promise(r => setTimeout(r, 500));
      state.raw = { data: body.data, gains: body.gains, gifts: body.gifts, gains_error: body.gains_error, mode: body.mode, isSample: true };
      state.mode = body.mode;
      state.tab = 'summary';
      state.schemeFilter = 'all';
      state.typeFilter = new Set(ALL_TYPES);
      renderDashboard();
      showScreen('dashboard');
    } catch (err) {
      showScreen('upload');
      showUploadError('Could not load the sample right now.');
    }
  }

  function downloadJson() {
    if (!state.raw) return;
    const blob = new Blob([JSON.stringify(state.raw.data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'casparser-output.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function download(name, text, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const csvField = (v) => {
    if (v == null) return '';
    const s = String(v).replace(/\n/g, ' ');
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const csvRow = (fields) => fields.map(csvField).join(',');

  // Mirrors casparser's own cas2csv column layout exactly, so this lines up
  // with what `casparser -o out.csv` would produce from the same statement.
  function downloadFullCsv() {
    if (!state.raw) return;
    const data = state.raw.data;
    const lines = [];
    if (state.mode === 'demat') {
      lines.push(csvRow(['account', 'account_type', 'dp_id', 'client_id', 'category', 'holding', 'isin', 'symbol', 'qty', 'price_or_nav', 'value']));
      data.accounts.forEach(acc => {
        (acc.equities || []).forEach(e => lines.push(csvRow([acc.name, acc.type, acc.dp_id, acc.client_id, 'Equity', e.name, e.isin, e.symbol, e.num_shares, e.price, e.value])));
        (acc.mutual_funds || []).forEach(m => lines.push(csvRow([acc.name, acc.type, acc.dp_id, acc.client_id, 'Mutual Fund', m.name, m.isin, '', m.balance, m.nav, m.value])));
        (acc.bonds || []).forEach(b => lines.push(csvRow([acc.name, acc.type, acc.dp_id, acc.client_id, 'Bond', b.name, b.isin, '', b.num_bonds, b.market_price, b.value])));
      });
      download('casparser-holdings.csv', lines.join('\n'), 'text/csv');
    } else {
      lines.push(csvRow(['amc', 'folio', 'pan', 'scheme', 'advisor', 'isin', 'amfi', 'date', 'description', 'amount', 'units', 'nav', 'balance', 'type', 'dividend']));
      data.folios.forEach(f => f.schemes.forEach(sc => sc.transactions.forEach(t => lines.push(csvRow([
        f.amc, f.folio, f.PAN, sc.scheme, sc.advisor, sc.isin, sc.amfi,
        t.date, t.description, t.amount, t.units, t.nav, t.balance, t.type, t.dividend_rate,
      ])))));
      download('casparser-transactions.csv', lines.join('\n'), 'text/csv');
    }
  }

  // ---------- dashboard ----------
  function renderDashboard() {
    const { data, isSample } = state.raw;
    const isDemat = state.mode === 'demat';
    const inv = data.investor_info;

    $('statement-pill').textContent = isSample
      ? 'Sample data'
      : (isDemat ? data.file_type + ' · Demat' : data.file_type + ' · ' + (data.cas_type === 'DETAILED' ? 'Detailed' : 'Summary'));
    $('investor-name').textContent = inv.name;
    $('investor-pan').textContent = 'PAN · ' + maskPan(isDemat ? (data.accounts[0].owners[0] || {}).PAN : firstPan(data));
    $('investor-email').textContent = inv.email;
    $('statement-period').textContent = 'Statement period · ' + fdate(data.statement_period.from) + ' – ' + fdate(data.statement_period.to);

    $('mf-view').hidden = isDemat;
    $('demat-view').hidden = !isDemat;
    renderParseWarnings(data.parse_warnings || []);

    if (isDemat) {
      renderDemat(data);
    } else {
      renderBandStatsMf(data);
      wireTabs();
      renderPortfolio(data);
      renderTransactions(data);
      renderGains();
      setTab(state.tab);
    }
  }

  function renderParseWarnings(warnings) {
    const el = $('parse-warnings');
    if (!warnings.length) { el.innerHTML = ''; return; }
    el.innerHTML = `
      <div class="warning-banner">
        <svg viewBox="0 0 24 24" fill="none" stroke="#8a6a1a" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4M12 17h.01"/></svg>
        <div>
          <div style="font-weight:600;margin-bottom:4px">${warnings.length} data-quality issue${warnings.length > 1 ? 's' : ''} detected while parsing</div>
          <div style="color:var(--ink-2)">${warnings.map(esc).join('<br>')}</div>
        </div>
      </div>`;
  }

  function firstPan(data) {
    for (const f of data.folios) if (f.PAN) return f.PAN;
    return null;
  }

  function renderBandStatsMf(data) {
    let value = 0, cost = 0, count = 0;
    const amcs = new Set();
    data.folios.forEach(f => { amcs.add(f.amc); f.schemes.forEach(sc => { value += sc.valuation.value; cost += sc.valuation.cost; count++; }); });
    const gain = value - cost;
    const pct = cost ? (gain / cost * 100) : 0;
    const stats = [
      { label: 'Current value', value: money(value), cls: '' },
      { label: 'Total invested', value: money(cost), cls: '' },
      { label: 'Overall gain · ' + sign(gain) + Math.abs(pct).toFixed(1) + '%', value: sign(gain) + money(Math.abs(gain)), cls: gainClass(gain) },
      { label: data.folios.length + ' folios · ' + amcs.size + ' AMCs', value: String(count), cls: '' },
    ];
    $('band-stats').innerHTML = stats.map(s => `
      <div class="band-stat">
        <div class="band-stat-label">${esc(s.label)}</div>
        <div class="band-stat-value ${s.cls}" style="color:${s.cls ? 'var(--band-accent)' : 'var(--band-ink)'}">${esc(s.value)}</div>
      </div>`).join('');
    // gain row should read red on the dark band when negative
    if (gain < 0) {
      const el = document.querySelectorAll('#band-stats .band-stat-value')[2];
      if (el) el.style.color = '#ff8a75';
    }
  }

  function wireTabs() {
    document.querySelectorAll('#tabs .tab').forEach(btn => {
      btn.onclick = () => setTab(btn.dataset.tab);
    });
  }
  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll('#tabs .tab').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    $('panel-summary').hidden = tab !== 'summary';
    $('panel-transactions').hidden = tab !== 'transactions';
    $('panel-gains').hidden = tab !== 'gains';
  }

  // ---------- Portfolio tab ----------
  function renderPortfolio(data) {
    const endDate = new Date(data.statement_period.to) || new Date();
    const firstBuy = (sc) => {
      let m = null;
      sc.transactions.forEach(t => {
        if ((t.type === 'PURCHASE' || t.type === 'PURCHASE_SIP') && t.date) {
          const d = new Date(t.date);
          if (!isNaN(d) && (!m || d < m)) m = d;
        }
      });
      return m;
    };
    const cagr = (cost, value, days) => (cost > 0 && days > 0) ? (Math.pow(value / cost, 365 / days) - 1) * 100 : (cost > 0 ? (value - cost) / cost * 100 : 0);

    const rows = [];
    data.folios.forEach(f => f.schemes.forEach(sc => {
      const v = sc.valuation;
      const buy = firstBuy(sc);
      const days = buy ? Math.max(1, Math.round((endDate - buy) / DAY_MS)) : 0;
      const pNav = sc.close ? v.cost / sc.close : 0;
      const gain = v.value - v.cost;
      const abs = v.cost ? gain / v.cost * 100 : 0;
      rows.push({
        cat: normalizeCat(sc.type), folio: f.folio, scheme: sc.scheme, isin: sc.isin,
        unitsStr: units(sc.close), pNavStr: num4(pNav), cNavStr: num4(v.nav),
        pValStr: num0(v.cost), cValStr: num0(v.value), gainStr: num0(gain), gainCls: gainClass(gain),
        daysStr: String(days), absStr: abs.toFixed(2), cagrStr: cagr(v.cost, v.value, days).toFixed(2),
        _cost: v.cost, _value: v.value, _days: days,
      });
    }));

    const byCat = {};
    rows.forEach(r => { (byCat[r.cat] = byCat[r.cat] || []).push(r); });

    const subtotal = (rs, label) => {
      let c = 0, val = 0, wd = 0;
      rs.forEach(r => { c += r._cost; val += r._value; wd += r._days * r._cost; });
      const days = c ? Math.round(wd / c) : 0;
      const gain = val - c;
      const abs = c ? gain / c * 100 : 0;
      return { label, pValStr: num0(c), cValStr: num0(val), gainStr: num0(gain), gainCls: gainClass(gain), daysStr: String(days), absStr: abs.toFixed(2), cagrStr: cagr(c, val, days).toFixed(2) };
    };

    const rowHtml = (r) => `
      <div class="prow ptable-cols">
        <div>${esc(r.folio)}</div>
        <div><div class="scheme-name">${esc(r.scheme)}</div><div class="scheme-isin">${esc(r.isin || '')}</div></div>
        <div>${r.unitsStr}</div><div>${r.pNavStr}</div><div>${r.cNavStr}</div>
        <div>${r.pValStr}</div><div class="cell-value">${r.cValStr}</div>
        <div class="${r.gainCls}" style="font-weight:600">${r.gainStr}</div>
        <div>${r.daysStr}</div><div>${r.absStr}</div><div>${r.cagrStr}</div>
      </div>`;
    const subtotalHtml = (s, cls) => `
      <div class="${cls} ptable-cols">
        <div>${esc(s.label)}</div>
        <div>${s.pValStr}</div><div>${s.cValStr}</div>
        <div class="${s.gainCls}">${s.gainStr}</div>
        <div>${s.daysStr}</div><div>${s.absStr}</div><div>${s.cagrStr}</div>
      </div>`;

    let body = '';
    let allRows = [];
    CAT_ORDER.forEach(cat => {
      const rs = byCat[cat];
      if (!rs || !rs.length) return;
      allRows = allRows.concat(rs);
      body += `<div class="cat-title">${CAT_LABEL[cat]}</div>`;
      body += rs.map(rowHtml).join('');
      body += subtotalHtml(subtotal(rs, 'Sub Total – ' + CAT_LABEL[cat] + ' :'), 'subtotal-row');
    });
    const grand = subtotal(allRows, 'Grand Total :');

    const panel = $('panel-summary');
    if (!allRows.length) {
      panel.innerHTML = `<div class="card"><div class="gains-empty">No holdings found in this statement.</div></div>`;
      return;
    }
    panel.innerHTML = `
      <div class="card ptable" style="padding:0;overflow:hidden">
        <div class="table-scroll"><div class="ptable">
          <div class="ptable-head ptable-cols">
            <div>Folio</div><div>Scheme</div><div>Balance Units</div><div>Purchase NAV</div><div>Current NAV</div>
            <div>Purchase Value</div><div>Current Value</div><div>Gain</div><div>Holding Days</div><div>Abs Return (%)</div><div>CAGR (%)</div>
          </div>
          ${body}
          ${subtotalHtml(grand, 'grand-row')}
        </div></div>
      </div>`;
  }

  // ---------- Transactions tab ----------
  function renderTransactions(data) {
    let allTx = [];
    data.folios.forEach(f => f.schemes.forEach(sc => sc.transactions.forEach(t => allTx.push(Object.assign({}, t, { scheme: sc.scheme, isin: sc.isin })))));

    const schemeOptions = [{ value: 'all', label: 'All schemes' }].concat(
      [...new Map(data.folios.flatMap(f => f.schemes.map(sc => [sc.isin, sc.scheme]))).entries()].map(([isin, scheme]) => ({ value: isin, label: scheme }))
    );

    renderTxnToolbar(schemeOptions);
    renderTxnTable(allTx);
  }

  function renderTxnToolbar(schemeOptions) {
    const activeCount = state.typeFilter.size;
    const total = ALL_TYPES.length;
    const allOn = activeCount === total;

    const panel = $('panel-transactions');
    panel.innerHTML = `
      <div class="txn-toolbar">
        <div class="txn-count" id="txn-count"></div>
        <div class="txn-controls no-print">
          <select id="scheme-filter" class="select-scheme">
            ${schemeOptions.map(o => `<option value="${esc(o.value)}" ${o.value === state.schemeFilter ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
          </select>
          <div class="filter-wrap">
            <button id="filter-btn" class="filter-btn ${allOn ? '' : 'active-filter'}">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/></svg>
              Transaction type
              ${allOn ? '' : `<span class="filter-count">${total - activeCount}</span>`}
            </button>
            <div id="filter-panel-wrap"></div>
          </div>
        </div>
      </div>
      <div class="card" style="padding:0;overflow:hidden">
        <div class="table-scroll"><div class="ttable">
          <div class="ttable-head ttable-cols"><span>Date</span><span>Scheme</span><span>Type</span><span class="right">Amount</span><span class="right">Units</span><span class="right">NAV</span><span class="right">Balance</span></div>
          <div id="txn-rows"></div>
        </div></div>
      </div>`;

    $('scheme-filter').addEventListener('change', (e) => { state.schemeFilter = e.target.value; renderTransactions(state.raw.data); });
    $('filter-btn').addEventListener('click', () => { state.filterOpen = !state.filterOpen; renderFilterPanel(); });
  }

  function renderFilterPanel() {
    const wrap = $('filter-panel-wrap');
    if (!state.filterOpen) { wrap.innerHTML = ''; return; }
    const groupHtml = (g) => `
      <div class="filter-col-label">${esc(g.label)}</div>
      <div class="filter-list">
        ${g.types.map(t => {
          const checked = state.typeFilter.has(t);
          return `<label class="filter-item" data-type="${t}">
            <span class="checkbox-box ${checked ? 'checked' : ''}">${checked ? CHECK_SVG : ''}</span>
            <span>${TYPE_LABELS[t]}</span>
          </label>`;
        }).join('')}
      </div>`;
    const allOn = state.typeFilter.size === ALL_TYPES.length;
    wrap.innerHTML = `
      <div class="filter-scrim" id="filter-scrim"></div>
      <div class="filter-panel cp-fade">
        <div class="filter-panel-head">
          <div class="filter-cols">${TYPE_GROUPS.map(groupHtml).join('')}</div>
          <button class="filter-close" id="filter-close">
            <svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="filter-panel-foot">
          <button class="btn btn-primary" id="filter-apply">Apply</button>
          <button class="filter-toggle-all" id="filter-toggle-all">${allOn ? 'Unselect all' : 'Select all'}</button>
        </div>
      </div>`;
    $('filter-scrim').onclick = () => { state.filterOpen = false; renderFilterPanel(); };
    $('filter-close').onclick = () => { state.filterOpen = false; renderFilterPanel(); };
    $('filter-apply').onclick = () => { state.filterOpen = false; renderFilterPanel(); renderTransactions(state.raw.data); };
    $('filter-toggle-all').onclick = () => {
      state.typeFilter = allOn ? new Set() : new Set(ALL_TYPES);
      renderFilterPanel();
    };
    wrap.querySelectorAll('.filter-item').forEach(el => {
      el.onclick = () => {
        const t = el.dataset.type;
        if (state.typeFilter.has(t)) state.typeFilter.delete(t); else state.typeFilter.add(t);
        renderFilterPanel();
      };
    });
  }

  function renderTxnTable(allTx) {
    let filtered = state.schemeFilter === 'all' ? allTx : allTx.filter(t => t.isin === state.schemeFilter);
    filtered = filtered.filter(t => state.typeFilter.has(t.type));
    filtered.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));

    $('txn-count').textContent = filtered.length + ' transaction' + (filtered.length === 1 ? '' : 's');
    $('txn-rows').innerHTML = filtered.map(t => {
      const amountCls = t.amount == null ? '' : (t.amount < 0 ? 'money-out' : '');
      const unitsCls = t.units == null ? '' : (t.units < 0 ? 'money-out' : 'money-in');
      return `<div class="trow ttable-cols">
        <span class="num">${fdate(t.date)}</span>
        <span>${esc(t.scheme)}</span>
        <span><span class="type-tag">${TYPE_LABELS[t.type] || t.type}</span></span>
        <span class="right num ${amountCls}" style="font-weight:600">${t.amount == null ? '—' : (t.amount < 0 ? '−' : '') + money(Math.abs(t.amount), 2)}</span>
        <span class="right num ${unitsCls}">${t.units == null ? '—' : (t.units < 0 ? '−' : '+') + units(Math.abs(t.units))}</span>
        <span class="right num">${t.nav == null ? '—' : money(t.nav, 2)}</span>
        <span class="right num">${t.balance == null ? '—' : units(t.balance)}</span>
      </div>`;
    }).join('') || `<div class="gains-empty">No transactions match these filters.</div>`;
  }

  // ---------- Capital gains tab ----------
  function renderGains() {
    const panel = $('panel-gains');
    const gains = state.raw.gains || [];
    const gifts = state.raw.gifts || [];
    const gainsError = state.raw.gains_error;

    if (!gains.length) {
      panel.innerHTML = `
        ${gainsError ? `<div class="gains-error-banner"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v5M12 16h.01"/></svg><span>${esc(gainsError)}</span></div>` : ''}
        <div class="card"><div class="gains-empty">No realised sales found in this statement — gains only appear once units are actually redeemed or switched out.</div></div>`;
      return;
    }

    const fys = [...new Set(gains.map(g => g.fy))].sort().reverse();
    if (!state.gainsFy || !fys.includes(state.gainsFy)) state.gainsFy = fys[0];

    let stcg = 0, ltcg = 0;
    gains.forEach(g => { stcg += g.stcg; ltcg += g.ltcg; });
    const net = stcg + ltcg;

    const fyRows = gains.filter(g => g.fy === state.gainsFy);

    panel.innerHTML = `
      ${gainsError ? `<div class="gains-error-banner"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v5M12 16h.01"/></svg><span>${esc(gainsError)}</span></div>` : ''}
      <div class="gains-stats">
        <div class="card gains-stat"><div class="gains-stat-label">Short-term (STCG)</div><div class="gains-stat-value ${gainClass(stcg)}">${sign(stcg)}${money(Math.abs(stcg))}</div></div>
        <div class="card gains-stat"><div class="gains-stat-label">Long-term (LTCG)</div><div class="gains-stat-value ${gainClass(ltcg)}">${sign(ltcg)}${money(Math.abs(ltcg))}</div></div>
        <div class="card gains-stat net"><div class="gains-stat-label">Net realised gain (all years)</div><div class="gains-stat-value">${sign(net)}${money(Math.abs(net))}</div></div>
      </div>
      <div class="card" style="padding:0;overflow:hidden">
        <div class="gains-toolbar">
          <div>
            <div class="gains-toolbar-title">Realised transactions</div>
            <div class="gains-toolbar-sub">Schedule 112A format</div>
          </div>
          <div class="no-print" style="display:flex;gap:10px;align-items:center">
            <select id="fy-select" class="select-scheme">
              ${fys.map(fy => `<option value="${fy}" ${fy === state.gainsFy ? 'selected' : ''}>FY ${fy}</option>`).join('')}
            </select>
            <button id="export-112a" class="btn btn-primary">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
              Export 112A CSV
            </button>
          </div>
        </div>
        <div class="table-scroll"><div class="gtable">
          <div class="gtable-head gtable-cols"><span>Scheme</span><span>Acquired</span><span>Sold</span><span class="right">Units</span><span class="right">Cost</span><span class="right">Sale value</span><span class="right">Gain / Loss</span></div>
          ${fyRows.map(g => `
            <div class="grow gtable-cols">
              <div><div style="font-weight:600;line-height:1.3">${esc(g.scheme)}</div><span class="term-tag" style="color:${g.gain_type === 'LTCG' ? 'var(--accent-strong)' : 'var(--ink-2)'}">${g.gain_type}</span></div>
              <span class="num">${fdate(g.purchase_date)}</span>
              <span class="num">${fdate(g.sale_date)}</span>
              <span class="right">${units(g.units)}</span>
              <span class="right">${money(g.acquisition_value)}</span>
              <span class="right" style="font-weight:600">${money(g.sale_value)}</span>
              <span class="right ${gainClass(g.gain)}" style="font-weight:600">${sign(g.gain)}${money(Math.abs(g.gain))}</span>
            </div>`).join('') || `<div class="gains-empty">No realised sales in FY ${state.gainsFy}.</div>`}
        </div></div>
      </div>
      ${gifts.length ? renderGiftsBlock(gifts) : ''}
    `;

    $('fy-select').addEventListener('change', (e) => { state.gainsFy = e.target.value; renderGains(); });
    $('export-112a').addEventListener('click', () => export112a(fyRows, state.gainsFy));
  }

  function renderGiftsBlock(gifts) {
    return `
      <div class="card" style="padding:0;overflow:hidden;margin-top:16px">
        <div class="gains-toolbar"><div><div class="gains-toolbar-title">Gift transfers</div><div class="gains-toolbar-sub">Informational only — not part of the gains totals above</div></div></div>
        <div class="table-scroll"><div class="gtable">
          <div class="gtable-head" style="display:grid;grid-template-columns:1.6fr 100px 108px 100px 116px 1fr;gap:8px">
            <span>Scheme</span><span>Direction</span><span>Date</span><span class="right">Units</span><span class="right">Value</span><span>Counterparty folio</span>
          </div>
          ${gifts.map(g => `
            <div class="grow" style="display:grid;grid-template-columns:1.6fr 100px 108px 100px 116px 1fr;gap:8px">
              <div style="font-weight:600">${esc(g.scheme)}</div>
              <span>${g.direction === 'IN' ? 'Received' : 'Given'}</span>
              <span class="num">${fdate(g.date)}</span>
              <span class="right">${units(g.units)}</span>
              <span class="right">${money(g.value)}</span>
              <span class="num" style="color:var(--ink-3)">${esc(g.counterparty_folio || '—')}</span>
            </div>`).join('')}
        </div></div>
      </div>`;
  }

  function export112a(rows, fy) {
    const head = ['Scheme', 'ISIN', 'Type', 'Acquired', 'Sold', 'Units', 'Cost of acquisition', 'Sale value', 'Gain/Loss', 'Term'];
    const lines = [head.join(',')];
    rows.forEach(g => lines.push([
      JSON.stringify(g.scheme), g.isin, g.gain_type === 'LTCG' ? 'LTCG' : 'STCG',
      g.purchase_date, g.sale_date, g.units, g.acquisition_value.toFixed(2), g.sale_value.toFixed(2), g.gain.toFixed(2), g.gain_type,
    ].join(',')));
    download(`casparser-gains-112a-${fy}.csv`, lines.join('\n'), 'text/csv');
  }

  // ---------- Demat mode ----------
  function renderDemat(data) {
    let dEq = 0, dMf = 0, dBo = 0;
    const accountsHtml = data.accounts.map(acc => {
      let ev = 0, mv = 0, bv = 0;
      const equities = acc.equities || [];
      const mfs = acc.mutual_funds || [];
      const bonds = acc.bonds || [];
      equities.forEach(e => ev += e.value);
      mfs.forEach(m => mv += m.value);
      bonds.forEach(b => bv += b.value);
      dEq += ev; dMf += mv; dBo += bv;
      const total = ev + mv + bv;

      const equitiesHtml = equities.length ? `
        <div class="card" style="padding:0;overflow:hidden">
          <div class="holdings-title">Equities</div>
          <div class="hhead h-equities"><span>Holding</span><span class="right">Qty</span><span class="right">Price</span><span class="right">Value</span></div>
          ${equities.map(e => `
            <div class="hrow h-equities">
              <div><div class="holding-name">${esc(e.name || e.isin)}</div><div class="holding-sub">${esc([e.symbol, e.exchange].filter(Boolean).join(' · '))}</div></div>
              <span class="right num">${units(e.num_shares)}</span>
              <span class="right num">${money(e.price, 2)}</span>
              <span class="right num" style="font-weight:600;font-family:var(--font-head)">${money(e.value)}</span>
            </div>`).join('')}
        </div>` : '';

      const mfsHtml = mfs.length ? `
        <div class="card" style="padding:0;overflow:hidden">
          <div class="holdings-title">Mutual funds</div>
          <div class="hhead h-mfs"><span>Scheme</span><span class="right">Units</span><span class="right">NAV</span><span class="right">Value</span><span class="right">Return</span></div>
          ${mfs.map(m => `
            <div class="hrow h-mfs">
              <div class="holding-name">${esc(m.name || m.isin)}</div>
              <span class="right num">${units(m.balance)}</span>
              <span class="right num">${money(m.nav, 2)}</span>
              <span class="right num" style="font-weight:600;font-family:var(--font-head)">${money(m.value)}</span>
              <span class="right num ${m.return != null ? gainClass(m.return) : ''}" style="font-weight:600">${m.return != null ? sign(m.return) + Math.abs(m.return).toFixed(1) + '%' : '—'}</span>
            </div>`).join('')}
        </div>` : '';

      const bondsHtml = bonds.length ? `
        <div class="card" style="padding:0;overflow:hidden">
          <div class="holdings-title">Bonds</div>
          <div class="hhead h-bonds"><span>Bond</span><span class="right">Qty</span><span class="right">Coupon</span><span class="right">Value</span><span class="right">Maturity</span></div>
          ${bonds.map(b => `
            <div class="hrow h-bonds">
              <div class="holding-name">${esc(b.name || b.isin)}</div>
              <span class="right num">${units(b.num_bonds)}</span>
              <span class="right num">${b.coupon_rate != null ? b.coupon_rate + '%' : '—'}</span>
              <span class="right num" style="font-weight:600;font-family:var(--font-head)">${money(b.value)}</span>
              <span class="right num">${b.maturity_date ? fdate(b.maturity_date) : '—'}</span>
            </div>`).join('')}
        </div>` : '';

      const owners = acc.owners || [];
      const ownersStr = owners.map(o => `${o.name} (${maskPan(o.PAN)})`).join(', ');

      return `
        <div class="demat-account">
          <div class="demat-account-head">
            <div>
              <div class="demat-account-name">${esc(acc.name)}</div>
              <div class="demat-account-meta">${esc(acc.type)} · DP ${esc(acc.dp_id || '—')} · Client ${esc(acc.client_id || '—')}</div>
              ${owners.length ? `<div class="demat-account-meta" style="margin-top:2px">${owners.length > 1 ? 'Joint holders' : 'Owner'} · ${esc(ownersStr)}</div>` : ''}
            </div>
            <div class="demat-account-value"><div class="v">${money(total)}</div><div class="l">Total valuation</div></div>
          </div>
          ${equitiesHtml}${mfsHtml}${bondsHtml}
        </div>`;
    }).join('');

    const npsHtml = data.nps ? `
      <div class="demat-account">
        <div class="demat-account-head">
          <div><div class="demat-account-name">National Pension System</div><div class="demat-account-meta">PRAN ${esc(data.nps.pran || '—')} · ${esc(data.nps.nps_sp || '')}</div></div>
          <div class="demat-account-value"><div class="v">${money(data.nps.value)}</div><div class="l">Total valuation</div></div>
        </div>
        <div class="card" style="padding:0;overflow:hidden">
          <div class="hhead" style="display:grid;grid-template-columns:1.6fr 1fr .6fr .6fr 1fr 1fr;gap:8px"><span>Scheme</span><span>Fund manager</span><span>Tier</span><span>Class</span><span class="right">Units</span><span class="right">Value</span></div>
          ${(data.nps.schemes || []).map(s => `
            <div class="hrow" style="display:grid;grid-template-columns:1.6fr 1fr .6fr .6fr 1fr 1fr;gap:8px">
              <div class="holding-name">${esc(s.scheme)}</div>
              <span class="num" style="color:var(--ink-2)">${esc(s.fund_manager || '—')}</span>
              <span>${esc(s.tier || '—')}</span>
              <span>${esc(s.asset_class || '—')}</span>
              <span class="right num">${units(s.units)}</span>
              <span class="right num" style="font-weight:600">${money(s.value)}</span>
            </div>`).join('')}
        </div>
      </div>` : '';

    // reuse the same band-stats strip, but for demat holdings
    $('band-stats').innerHTML = [
      { label: 'Equities', value: money(dEq) },
      { label: 'Mutual funds & bonds', value: money(dMf + dBo) },
      { label: 'Total valuation', value: money(dEq + dMf + dBo) },
    ].map(s => `<div class="band-stat"><div class="band-stat-label">${esc(s.label)}</div><div class="band-stat-value" style="color:var(--band-ink)">${s.value}</div></div>`).join('');

    $('demat-view').innerHTML = accountsHtml + npsHtml || `<div class="card"><div class="demat-empty">No holdings found in this statement.</div></div>`;
  }

  showScreen('upload');
})();
