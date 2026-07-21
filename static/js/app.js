// ===================== STATE =====================
const state = {
  versions: [],
  diffData: null,
  currentSpec: '23.501',
  expandedClauses: new Set(),
};

// ===================== UTILITIES =====================
const $ = id => document.getElementById(id);
const _escDiv = document.createElement('div');
const escapeHtml = str => { _escDiv.textContent = str; return _escDiv.innerHTML; };

// ===================== TOAST NOTIFICATIONS =====================
function showToast(msg, type = 'info') {
  const container = $('toastContainer');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add('toast-out');
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

function flattenClauseTree(clauses, depth = 0) {
  let result = [];
  for (const c of clauses) {
    result.push({ ...c, _depth: depth });
    if (c.children && c.children.length > 0) {
      result = result.concat(flattenClauseTree(c.children, depth + 1));
    }
  }
  return result;
}

function countClauses(clauses) {
  let n = 0;
  for (const c of clauses) {
    n++;
    if (c.children) n += countClauses(c.children);
  }
  return n;
}

function getClauseDisplayParts(node) {
  let id = (node.id || '').trim();
  let title = (node.title || '').trim();
  if (id === title) {
    const annex = id.match(/^(Annex\s+[A-Z0-9]+(?:\s+\([^)]+\))?)\s*:\s*(.+)$/i);
    if (annex) {
      id = annex[1];
      title = annex[2];
    } else {
      title = '';
    }
  }
  return {id, title};
}

// ===================== API =====================
function renderProgress(steps, currentStep) {
  let html = '<div class="diff-progress">';
  for (let i = 0; i < steps.length; i++) {
    const icon = i < currentStep ? '&#10003;' : (i === currentStep ? '&#8987;' : '&#9675;');
    const cls = i < currentStep ? 'done' : (i === currentStep ? 'active' : 'pending');
    html += `<div class="progress-step ${cls}"><span class="progress-icon">${icon}</span> ${escapeHtml(steps[i])}</div>`;
  }
  html += '</div>';
  return html;
}

let _diffAbortController = null;

async function fetchDiffWithProgress(spec, v1, v2, refresh) {
  const steps = ['Loading release data', 'Reading clause changes', 'Preparing workspace'];
  const startTime = performance.now();
  const minimumDisplayMs = 220;
  $('content').innerHTML = renderProgress(steps, 0);

  _diffAbortController?.abort();
  _diffAbortController = new AbortController();
  const params = new URLSearchParams({spec, v1, v2});
  if (refresh) params.set('refresh', '1');

  const response = await fetch(`/api/diff?${params}`, {
    signal: _diffAbortController.signal,
    headers: {'Accept': 'application/json'},
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || `Comparison failed (${response.status})`);
  }

  $('content').innerHTML = renderProgress(steps, 1);
  const result = await response.json();
  if (result.error) throw new Error(result.error);
  $('content').innerHTML = renderProgress(steps, 2);

  const remaining = minimumDisplayMs - (performance.now() - startTime);
  if (remaining > 0) await new Promise(resolve => setTimeout(resolve, remaining));
  return result;
}

// ===================== LIGHTBOX =====================
let _lbZoom = 1;
let _lbPan = { x: 0, y: 0, dragging: false, startX: 0, startY: 0 };

function openLightbox(src, alt) {
  const lb = document.getElementById('lightbox');
  const img = document.getElementById('lightboxImg');
  const caption = document.getElementById('lightboxCaption');
  img.src = src;
  img.alt = alt || '';
  caption.textContent = alt || '';
  lb.classList.add('open');
  document.body.style.overflow = 'hidden';
  _lbZoom = 1;
  _lbPan = { x: 0, y: 0, dragging: false, startX: 0, startY: 0 };
  img.classList.remove('zoomed');
  img.style.transform = '';
}

function closeLightbox() {
  const lb = document.getElementById('lightbox');
  lb.classList.remove('open');
  document.body.style.overflow = '';
  document.getElementById('lightboxImg').src = '';
}

window.zoomLightbox = function(dir) {
  const img = document.getElementById('lightboxImg');
  if (dir === 0) {
    _lbZoom = 1;
    _lbPan.x = 0;
    _lbPan.y = 0;
  } else if (dir > 0) {
    _lbZoom = Math.min(_lbZoom * 1.3, 8);
  } else {
    _lbZoom = Math.max(_lbZoom / 1.3, 0.5);
  }
  if (_lbZoom > 1) {
    img.classList.add('zoomed');
    img.style.transform = `scale(${_lbZoom}) translate(${_lbPan.x}px, ${_lbPan.y}px)`;
  } else {
    img.classList.remove('zoomed');
    img.style.transform = '';
    _lbPan.x = 0;
    _lbPan.y = 0;
  }
};

// Pan support for zoomed lightbox image
document.getElementById('lightboxImg').addEventListener('mousedown', e => {
  if (_lbZoom > 1) {
    e.preventDefault();
    _lbPan.dragging = true;
    _lbPan.startX = e.clientX - _lbPan.x;
    _lbPan.startY = e.clientY - _lbPan.y;
  }
});
document.addEventListener('mousemove', e => {
  if (_lbPan.dragging) {
    _lbPan.x = e.clientX - _lbPan.startX;
    _lbPan.y = e.clientY - _lbPan.startY;
    const img = document.getElementById('lightboxImg');
    img.style.transform = `scale(${_lbZoom}) translate(${_lbPan.x}px, ${_lbPan.y}px)`;
  }
});
document.addEventListener('mouseup', () => { _lbPan.dragging = false; });

// Wheel zoom in lightbox
document.getElementById('lightbox').addEventListener('wheel', e => {
  e.preventDefault();
  window.zoomLightbox(e.deltaY < 0 ? 1 : -1);
}, { passive: false });


// ===================== IMAGE THUMBNAILS =====================
function renderImageThumbnails(images, spec, version) {
  if (!images || images.length === 0) return '';
  let html = '<div class="clause-images">';
  for (const img of images) {
    const src = `/api/image/${spec}/${version}/${img.src}`;
    const alt = img.alt || '';
    html += `<button class="clause-image" type="button" data-image-src="${escapeHtml(src)}" data-image-alt="${escapeHtml(alt)}" aria-label="Open figure ${escapeHtml(alt)}">
      <img src="${src}" alt="${escapeHtml(alt)}" loading="lazy">
    </button>`;
  }
  html += '</div>';
  return html;
}

// ===================== SPEC & VERSION LOADING =====================

async function loadSpecs() {
  try {
    const resp = await fetch('/api/specs');
    const specs = await resp.json();
    $('specSelect').innerHTML = specs.map(s =>
      `<option value="${s.id}">${s.title || 'TS ' + s.id}</option>`
    ).join('') || '<option value="">No specs downloaded</option>';

    if (specs.length > 0) {
      const preferredSpec = specs.find(spec => spec.id === state.currentSpec)?.id || specs[0].id;
      state.currentSpec = preferredSpec;
      $('specSelect').value = preferredSpec;
      await loadVersions();
    } else {
      $('v1Select').innerHTML = '<option value="">Download a spec first</option>';
      $('v2Select').innerHTML = '<option value="">Download a spec first</option>';
      $('diffBtn').disabled = true;
    }
  } catch (err) {
    console.error('loadSpecs:', err);
  }
}

async function loadVersions() {
  const spec = $('specSelect').value;
  if (!spec) return;
  state.currentSpec = spec;

  $('v1Select').innerHTML = '<option value="">Loading...</option>';
  $('v2Select').innerHTML = '<option value="">Loading...</option>';
  $('diffBtn').disabled = true;

  try {
    const resp = await fetch(`/api/versions?spec=${spec}`);
    const versions = await resp.json();
    state.versions = versions;

    if (versions.length === 0) {
      $('v1Select').innerHTML = '<option value="">No versions cached</option>';
      $('v2Select').innerHTML = '<option value="">No versions cached</option>';
      $('diffBtn').disabled = true;
      return;
    }

    const opts = versions.map(v =>
      `<option value="${v.version}">${v.label || 'Rel-' + v.release + ' (' + v.version + ')'}</option>`
    ).join('');

    $('v1Select').innerHTML = '<option value="">Select older version...</option>' + opts;
    $('v2Select').innerHTML = '<option value="">Select newer version...</option>' + opts;

    // Auto-select latest two releases
    if (versions.length >= 2) {
      const sorted = [...versions].sort((a, b) => b.release - a.release);
      $('v2Select').value = sorted[0].version;
      const prevRelease = sorted[0].release - 1;
      const prev = sorted.find(v => v.release === prevRelease && v.version.endsWith('.0.0'));
      if (prev) {
        $('v1Select').value = prev.version;
      } else if (sorted.length > 1) {
        $('v1Select').value = sorted[1].version;
      }
    }

    $('diffBtn').disabled = false;

  } catch (err) {
    $('v1Select').innerHTML = `<option value="">Error: ${err.message}</option>`;
    $('v2Select').innerHTML = `<option value="">Error: ${err.message}</option>`;
  }
}

// ===================== DOWNLOAD =====================

async function downloadSpec() {
  const spec = $('specInput').value.trim();
  if (!spec) { showToast('Please enter a spec number', 'error'); return; }

  const btn = $('downloadBtn');
  const prog = $('downloadProgress');
  btn.disabled = true;
  btn.textContent = 'Starting...';
  prog.hidden = false;
  prog.textContent = 'Starting...';

  const startTime = Date.now();
  function elapsed() {
    const s = Math.round((Date.now() - startTime) / 1000);
    return s < 60 ? `${s}s` : `${Math.floor(s/60)}m${s%60}s`;
  }

  try {
    const resp = await fetch('/api/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({spec}),
    });
    const data = await resp.json();
    if (data.status === 'already_running') {
      prog.textContent = `[${elapsed()}] Already downloading...`;
    }

    // Poll until complete (with timeout + backoff)
    let pollInterval = 1000;
    let pollCount = 0;
    const MAX_POLLS = 600;
    while (pollCount < MAX_POLLS) {
      await new Promise(r => setTimeout(r, pollInterval));
      pollCount++;
      if (pollCount > 30) pollInterval = Math.min(pollInterval * 1.5, 5000);
      const sr = await fetch(`/api/download-status?spec=${spec}`);
      const st = await sr.json();
           if (st.status === 'listing')      prog.textContent = `[${elapsed()}] Listing releases...`;
      else if (st.status === 'downloading')  prog.textContent = `[${elapsed()}] Downloading ${st.done}/${st.total} releases...`;
      else if (st.status === 'completed')    { prog.textContent = `[${elapsed()}] Download complete!`; break; }
      else if (st.status === 'error')        throw new Error(st.error || 'Download failed');
      /* else 'not_found' — keep polling */
    }
    if (pollCount >= MAX_POLLS) throw new Error('Download timed out');

    await loadSpecs();
    const sel = $('specSelect');
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === spec) { sel.selectedIndex = i; break; }
    }
    await loadVersions();

    try {
      const pr = await fetch('/api/precompute', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({spec}),
      });
      const pd = await pr.json();
      if (pd.status === 'started' || pd.status === 'already_running') {
        prog.textContent = `[${elapsed()}] Computing diffs...`;
        let pcInterval = 1500;
        let pcCount = 0;
        const PC_MAX = 400;
        while (pcCount < PC_MAX) {
          await new Promise(r => setTimeout(r, pcInterval));
          pcCount++;
          if (pcCount > 20) pcInterval = Math.min(pcInterval * 1.5, 5000);
          const sr = await fetch(`/api/precompute-status?spec=${spec}`);
          const st = await sr.json();
          if (st.status === 'computing') {
            prog.textContent = `[${elapsed()}] Computing diffs ${st.done}/${st.total}...`;
          } else if (st.status === 'completed') {
            prog.textContent = `[${elapsed()}] All diffs ready!`;
            break;
          } else if (st.status === 'error') {
            prog.textContent = `[${elapsed()}] Diff compute error: ${st.error || 'unknown'}`;
            break;
          }
        }
        if (pcCount >= PC_MAX) prog.textContent = `[${elapsed()}] Diff compute timed out`;
      }
    } catch (_) {}

    setTimeout(() => { prog.hidden = true; }, 3000);
  } catch (err) {
    prog.textContent = `[${elapsed()}] Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Download';
  }
}

// ===================== TOC RENDER =====================
let _tocRecords = [];

function renderToc(clauses) {
  const tree = $('tocTree');
  if (!clauses || clauses.length === 0) {
    tree.innerHTML = '<div class="toc-no-results">No clauses found</div>';
    _tocRecords = [];
    return;
  }

  let html = '';
  const flat = flattenClauseTree(clauses);
  flat.forEach((node, index) => { node._flatIndex = index; });

  for (let index = 0; index < flat.length; index++) {
    const node = flat[index];
    const display = getClauseDisplayParts(node);
    const indent = node._depth;
    const status = node.status || 'unchanged';
    const statusDot = status !== 'unchanged'
      ? `<span class="status-dot ${status}"></span>`
      : '<span class="status-dot unchanged"></span>';
    const id = node.id || '';

    html += `<button class="toc-item" type="button" style="--indent:${indent}" data-id="${escapeHtml(id)}" data-clause-index="${index}">
      ${statusDot}
      <span class="toc-id">${escapeHtml(display.id)}</span>
      <span class="toc-title">${escapeHtml(display.title)}</span>
    </button>`;
  }

  tree.innerHTML = html;
  const items = tree.querySelectorAll('.toc-item');
  _tocRecords = flat.map((node, index) => ({
    element: items[index],
    node,
    heading: `${node.id || ''} ${node.title || ''}`.toLowerCase(),
  }));

  return flat;
}

window.filterToc = function() {
  const input = $('tocSearchInput');
  const raw = input.value.trim().toLowerCase();
  const keywords = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];

  if (keywords.length === 0) {
    _tocRecords.forEach(record => { record.element.hidden = false; });
    return;
  }

  _tocRecords.forEach(record => {
    const bodyFields = [record.node.body, record.node.old_body, record.node.new_body];
    const match = keywords.some(keyword =>
      record.heading.includes(keyword) || bodyFields.some(body => body && body.toLowerCase().includes(keyword))
    );
    record.element.hidden = !match;
  });
};

// ===================== DIFF RENDER =====================
let _showUnchanged = false;
const CLAUSE_BATCH_SIZE = 36;
let _allClauseNodes = [];
let _renderNodes = [];
let _renderPositionByFlatIndex = new Map();
let _renderedClauseCount = 0;
let _renderGeneration = 0;
let _clauseObserver = null;
let _wordDiffObserver = null;

const scheduleIdle = window.requestIdleCallback
  ? callback => window.requestIdleCallback(callback, {timeout: 350})
  : callback => window.setTimeout(callback, 0);

function queueWordDiff(element, node, generation) {
  if (element.dataset.wordDiffState) return;
  element.dataset.wordDiffState = 'queued';
  scheduleIdle(() => {
    if (generation !== _renderGeneration || !element.isConnected) return;
    const cells = element.querySelectorAll('.diff-word-content');
    if (cells.length < 2) return;
    const {leftHtml, rightHtml} = renderWordDiffHtml(node.old_body || '', node.new_body || '');
    cells[0].innerHTML = leftHtml || escapeHtml(node.old_body || '');
    cells[1].innerHTML = rightHtml || escapeHtml(node.new_body || '');
    element.dataset.wordDiffState = 'done';
  });
}

function appendClauseBatch(minimumEnd = 0) {
  const list = $('clauseList');
  if (!list || _renderedClauseCount >= _renderNodes.length) return;

  const start = _renderedClauseCount;
  const end = Math.min(
    _renderNodes.length,
    Math.max(start + CLAUSE_BATCH_SIZE, minimumEnd),
  );
  const spec = state.diffData.spec;
  const oldVersion = state.diffData.old_version;
  const newVersion = state.diffData.new_version;
  let html = '';
  for (let index = start; index < end; index++) {
    const node = _renderNodes[index];
    html += clauseDiffHtml(node, spec, oldVersion, newVersion, true);
  }
  list.insertAdjacentHTML('beforeend', html);
  _renderedClauseCount = end;

  for (let index = start; index < end; index++) {
    const node = _renderNodes[index];
    if (node.status !== 'modified') continue;
    const element = document.getElementById(`clause-${node._flatIndex}`);
    if (element) _wordDiffObserver?.observe(element);
  }

  const sentinel = $('renderSentinel');
  if (sentinel) {
    const remaining = _renderNodes.length - end;
    sentinel.hidden = remaining === 0;
    sentinel.querySelector('span').textContent = remaining ? `${remaining} more clauses` : '';
  }
}

function startClauseRendering(showUnchanged) {
  _renderGeneration += 1;
  const generation = _renderGeneration;
  _clauseObserver?.disconnect();
  _wordDiffObserver?.disconnect();

  _renderNodes = showUnchanged
    ? _allClauseNodes
    : _allClauseNodes.filter(node => node.status !== 'unchanged');
  _renderPositionByFlatIndex = new Map(
    _renderNodes.map((node, position) => [node._flatIndex, position]),
  );
  _renderedClauseCount = 0;
  $('clauseList').replaceChildren();

  _wordDiffObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      _wordDiffObserver.unobserve(entry.target);
      const flatIndex = Number(entry.target.dataset.clauseIndex);
      const node = _allClauseNodes[flatIndex];
      if (node) queueWordDiff(entry.target, node, generation);
    }
  }, {root: $('content'), rootMargin: '650px 0px'});

  _clauseObserver = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) appendClauseBatch();
  }, {root: $('content'), rootMargin: '1000px 0px'});
  _clauseObserver.observe($('renderSentinel'));
  appendClauseBatch();
}

function renderDiff(diffData) {
  _showUnchanged = false;
  const container = $('content');
  const stats = diffData.stats;

  const total = stats.added + stats.deleted + stats.modified + stats.unchanged;
  const toggleId = 'uncToggle';
  let toggleHtml = '';
  if (stats.unchanged > 0) {
    toggleHtml = `<label class="unchanged-toggle">
      <input type="checkbox" id="${toggleId}" ${_showUnchanged ? 'checked' : ''}> Show ${stats.unchanged} unchanged
    </label>`;
  }
  $('statsBar').innerHTML = `
    <span class="stat stat-total" id="statTotal">${total} total clauses</span>
    <span class="stat stat-added" id="statAdded">+${stats.added} added</span>
    <span class="stat stat-deleted" id="statDeleted">-${stats.deleted} deleted</span>
    <span class="stat stat-modified" id="statModified">~${stats.modified} modified</span>
    ${toggleHtml}
  `;
  $('statsBar').hidden = false;

  const uncCb = $(toggleId);
  if (uncCb) {
    uncCb.addEventListener('change', () => {
      _showUnchanged = uncCb.checked;
      startClauseRendering(_showUnchanged);
      $('content').scrollTo({top: 0});
    });
  }

  // Render TOC (returns flattened list to avoid duplicate traversal)
  const flat = renderToc(diffData.clauses) || [];
  $('tocSearch').hidden = false;
  $('tocSearchInput').value = '';
  document.querySelectorAll('.toc-children').forEach(e => e.classList.add('open'));
  const html = `<div class="diff-header">
    <h2>${escapeHtml(diffData.title || '')}</h2>
    <div class="subtitle">
      <a class="spec-link" href="https://portal.3gpp.org/desktopmodules/Specifications/SpecificationDetails.aspx?specificationId=${diffData.spec.replace('.','')}" target="_blank">
        TS ${diffData.spec}
      </a>
      &mdash; Comparing <strong>v${diffData.old_version}</strong> (Rel-${diffData.old_release})
      vs <strong>v${diffData.new_version}</strong> (Rel-${diffData.new_release})
    </div>
  </div>
  <div class="clause-list" id="clauseList"></div>
  <div class="render-sentinel" id="renderSentinel" role="status"><i></i><span></span></div>`;
  container.innerHTML = html;
  _allClauseNodes = flat;
  startClauseRendering(false);
  _updateNavState(flat);
}

function clauseDiffHtml(node, spec, oldVersion, newVersion, skipWordDiff) {
  const status = node.status || 'unchanged';
  const id = node.id || '';
  const display = getClauseDisplayParts(node);
  const clauseId = `clause-${node._flatIndex}`;

  let bodyHtml = '';

  if (status === 'unchanged') {
    const imgs = renderImageThumbnails(node.images, spec, newVersion);
    const body = node.body || '';
    const collapsed = body.length > 300 ? ' collapsed' : '';
    bodyHtml = imgs + `<div class="clause-body${collapsed}">${escapeHtml(body || '(no content)')}</div>`;
    if (body.length > 300) {
      bodyHtml += '<button class="expand-btn" type="button" data-action="expand-clause">Show more</button>';
    }
  } else if (status === 'added') {
    const imgs = renderImageThumbnails(node.images, spec, newVersion);
    const body = node.body || '';
    bodyHtml = `<div class="diff-view">
      <div class="diff-pane">
        <div class="diff-pane-header old">(empty)</div>
        <div class="diff-empty">Clause did not exist in the old version</div>
      </div>
      <div class="diff-pane">
        <div class="diff-pane-header new">New</div>
        ${imgs}
        <div class="diff-line add">
          <span class="diff-line-num"></span>
          <span class="diff-line-content">${escapeHtml(body || '(no content)')}</span>
        </div>
      </div>
    </div>`;
  } else if (status === 'deleted') {
    const imgs = renderImageThumbnails(node.images, spec, oldVersion);
    const body = node.body || '';
    bodyHtml = `<div class="diff-view">
      <div class="diff-pane">
        <div class="diff-pane-header old">Removed</div>
        ${imgs}
        <div class="diff-line del">
          <span class="diff-line-num"></span>
          <span class="diff-line-content">${escapeHtml(body || '(no content)')}</span>
        </div>
      </div>
      <div class="diff-pane">
        <div class="diff-pane-header new">(empty)</div>
        <div class="diff-empty">Clause removed in the new version</div>
      </div>
    </div>`;
  } else if (status === 'modified') {
    const oldImgs = renderImageThumbnails(node.old_images, spec, oldVersion);
    const newImgs = renderImageThumbnails(node.new_images, spec, newVersion);
    const oldText = node.old_body || '';
    const newText = node.new_body || '';

    let wordLeft, wordRight;
    if (skipWordDiff) {
      wordLeft = wordRight = '';
    } else {
      const result = renderWordDiffHtml(oldText, newText);
      wordLeft = result.leftHtml;
      wordRight = result.rightHtml;
    }

    bodyHtml = `<div class="diff-view">
      <div class="diff-pane">
        <div class="diff-pane-header old">v${oldVersion || 'Old'}</div>
        ${oldImgs}
        <div class="diff-line"><div class="diff-word-content">${wordLeft || escapeHtml(oldText) || '(no content)'}</div></div>
      </div>
      <div class="diff-pane">
        <div class="diff-pane-header new">v${newVersion || 'New'}</div>
        ${newImgs}
        <div class="diff-line"><div class="diff-word-content">${wordRight || escapeHtml(newText) || '(no content)'}</div></div>
      </div>
    </div>`;
  }

  return `<article class="clause-diff ${status}" id="${clauseId}" data-clause-id="${escapeHtml(id)}" data-clause-index="${node._flatIndex}">
    <div class="clause-diff-header">
      <span class="clause-id">${escapeHtml(display.id)}</span>
      <span class="clause-title">${escapeHtml(display.title)}</span>
      <span class="status-badge ${status}">${status}</span>
    </div>
    ${bodyHtml}
  </article>`;
}

// ===================== WORD-LEVEL DIFF =====================
function renderWordDiffHtml(oldText, newText) {
  const tokenize = (text) => text.match(/\w+|[^\w\s]|\s+/g) || [];
  const a = tokenize(oldText);
  const b = tokenize(newText);
  const n = a.length, m = b.length;

  if (n === 0 && m === 0) return { leftHtml: '', rightHtml: '' };

  // Trim common prefix and suffix to reduce problem size
  let prefixLen = 0;
  while (prefixLen < n && prefixLen < m && a[prefixLen] === b[prefixLen]) prefixLen++;
  let suffixLen = 0;
  while (suffixLen < n - prefixLen && suffixLen < m - prefixLen && a[n - 1 - suffixLen] === b[m - 1 - suffixLen]) suffixLen++;

  // Slice to the changed middle section
  const aMid = a.slice(prefixLen, n - suffixLen);
  const bMid = b.slice(prefixLen, m - suffixLen);

  // Run LCS-based diff on the middle section only
  const ops = _lcsDiff(aMid, bMid);

  // Build HTML: prefix + diffed middle + suffix
  let leftHtml = '', rightHtml = '';

  const prefixHtml = escapeHtml(a.slice(0, prefixLen).join(''));
  leftHtml += prefixHtml;
  rightHtml += prefixHtml;

  // Middle (diffed)
  for (const [op, s1, e1, s2, e2] of ops) {
    if (op === 'equal') {
      const equalHtml = escapeHtml(aMid.slice(s1, e1).join(''));
      leftHtml += equalHtml;
      rightHtml += equalHtml;
    } else if (op === 'delete') {
      leftHtml += `<span class="word-del">${escapeHtml(aMid.slice(s1, e1).join(''))}</span>`;
    } else if (op === 'insert') {
      rightHtml += `<span class="word-add">${escapeHtml(bMid.slice(s2, e2).join(''))}</span>`;
    }
  }

  const suffixHtml = escapeHtml(a.slice(n - suffixLen).join(''));
  leftHtml += suffixHtml;
  rightHtml += suffixHtml;

  return { leftHtml, rightHtml };
}

// Core LCS diff — used on the trimmed middle section.
// Prefix/suffix trimming in renderWordDiffHtml already removes the bulk;
// the middle section is typically small enough for direct DP.
function _lcsDiff(a, b) {
  const n = a.length, m = b.length;
  if (n === 0 && m === 0) return [];
  if (n === 0) return [['insert', 0, 0, 0, m]];
  if (m === 0) return [['delete', 0, n, 0, 0]];

  // Avoid quadratic stalls on clauses that were rewritten wholesale.
  // Prefix/suffix trimming still preserves the unchanged context around them.
  if (n * m > 300_000) {
    return [
      ['delete', 0, n, 0, 0],
      ['insert', n, n, 0, m],
    ];
  }

  const dp = Array.from({length: n + 1}, () => new Uint32Array(m + 1));
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      if (a[i - 1] === b[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }

  const ops = [];
  let i = n, j = m;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      ops.push(['equal', i - 1, i, j - 1, j]);
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.push(['insert', i, i, j - 1, j]);
      j--;
    } else {
      ops.push(['delete', i - 1, i, j, j]);
      i--;
    }
  }
  ops.reverse();

  // Merge adjacent same-type ops
  const merged = [];
  for (const op of ops) {
    if (merged.length > 0 && merged[merged.length - 1][0] === op[0]) {
      const last = merged[merged.length - 1];
      last[2] = op[2];
      last[4] = op[4];
    } else {
      merged.push([...op]);
    }
  }
  return merged;
}

// ===================== SCROLL TO CLAUSE =====================
async function ensureClauseRendered(flatIndex) {
  const node = _allClauseNodes[flatIndex];
  if (!node) return null;

  if (node.status === 'unchanged' && !_showUnchanged) {
    _showUnchanged = true;
    const checkbox = $('uncToggle');
    if (checkbox) checkbox.checked = true;
    startClauseRendering(true);
  }

  const targetPosition = _renderPositionByFlatIndex.get(flatIndex);
  if (targetPosition === undefined) return null;
  while (_renderedClauseCount <= targetPosition) {
    appendClauseBatch();
    await new Promise(resolve => requestAnimationFrame(resolve));
  }

  return document.getElementById(`clause-${flatIndex}`);
}

window.scrollToClause = async function(clauseReference) {
  let flatIndex;
  if (typeof clauseReference === 'number' || /^\d+$/.test(String(clauseReference))) {
    flatIndex = Number(clauseReference);
  } else {
    flatIndex = _allClauseNodes.findIndex(node => node.id === clauseReference);
  }

  const element = await ensureClauseRendered(flatIndex);
  if (!element) return;
  element.scrollIntoView({behavior: 'smooth', block: 'start'});
  document.querySelectorAll('.toc-item.active').forEach(item => item.classList.remove('active'));
  document.querySelector(`.toc-item[data-clause-index="${flatIndex}"]`)?.classList.add('active');
  if (mobileTocQuery.matches) setMobileToc(false);
};

// ===================== CHANGED CLAUSE NAVIGATION =====================
let _changedIndexes = [];
let _navIndex = -1;

function _updateNavState(flat) {
  _changedIndexes = flat
    .filter(node => node.status !== 'unchanged')
    .map(node => node._flatIndex);
  _navIndex = -1;
  const nav = $('clauseNav');
  const count = $('navCount');
  if (_changedIndexes.length > 0) {
    nav.classList.add('visible');
    count.textContent = `0 / ${_changedIndexes.length}`;
  } else {
    nav.classList.remove('visible');
  }
}

window.navChanged = function(dir) {
  if (_changedIndexes.length === 0) return;
  _navIndex += dir;
  if (_navIndex < 0) _navIndex = _changedIndexes.length - 1;
  if (_navIndex >= _changedIndexes.length) _navIndex = 0;
  window.scrollToClause(_changedIndexes[_navIndex]);
  $('navCount').textContent = `${_navIndex + 1} / ${_changedIndexes.length}`;
};

// ===================== URL DEEP LINKING =====================
function _updateURL(spec, v1, v2) {
  const params = new URLSearchParams();
  if (spec) params.set('spec', spec);
  if (v1) params.set('v1', v1);
  if (v2) params.set('v2', v2);
  const qs = params.toString();
  const url = qs ? `${location.pathname}?${qs}` : location.pathname;
  history.pushState(null, '', url);
}

async function _restoreFromURL() {
  const params = new URLSearchParams(location.search);
  const spec = params.get('spec');
  const v1 = params.get('v1');
  const v2 = params.get('v2');
  if (!spec) {
    await loadSpecs();
    return;
  }

  // Let the initial spec load also populate the matching versions.
  state.currentSpec = spec;
  await loadSpecs();
  const sel = $('specSelect');
  if (sel.value !== spec) return;

  if (v1) $('v1Select').value = v1;
  if (v2) $('v2Select').value = v2;

  if (v1 && v2 && v1 !== v2) {
    window.runDiff();
  }
}

window.addEventListener('popstate', () => {
  const params = new URLSearchParams(location.search);
  const spec = params.get('spec');
  const v1 = params.get('v1');
  const v2 = params.get('v2');
  if (spec && v1 && v2) {
    $('specSelect').value = spec;
    state.currentSpec = spec;
    loadVersions().then(() => {
      $('v1Select').value = v1;
      $('v2Select').value = v2;
      window.runDiff();
    });
  }
});

// ===================== MAIN FLOW =====================
window.runDiff = async function(refresh) {
  const v1 = $('v1Select').value;
  const v2 = $('v2Select').value;

  if (!v1 || !v2) {
    showToast('Please select both versions', 'error');
    return;
  }

  if (v1 === v2) {
    showToast('Please select two different versions', 'error');
    return;
  }

  const p1 = v1.split('.').map(Number);
  const p2 = v2.split('.').map(Number);
  for (let i = 0; i < Math.max(p1.length, p2.length); i++) {
    const a = p1[i] || 0, b = p2[i] || 0;
    if (a > b) {
      showToast('Old version must be earlier than new version', 'error');
      return;
    }
    if (a < b) break;
  }

  $('diffBtn').disabled = true;
  $('refreshBtn').hidden = true;
  $('diffBtn').querySelector('span').textContent = 'Loading…';

  try {
    const diff = await fetchDiffWithProgress(state.currentSpec, v1, v2, refresh);
    state.diffData = diff;
    renderDiff(diff);
    $('refreshBtn').hidden = false;
    _updateURL(state.currentSpec, v1, v2);
  } catch (err) {
    $('content').innerHTML = `<div class="error-msg">Error: ${escapeHtml(err.message)}</div>`;
  } finally {
    $('diffBtn').disabled = false;
    $('diffBtn').querySelector('span').textContent = 'Compare';
  }
};

window.openLightbox = openLightbox;
window.closeLightbox = closeLightbox;

// ===================== EVENT BINDING =====================
// Debounce TOC search input
{
  let _filterTimer = null;
  const searchInput = $('tocSearchInput');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(_filterTimer);
      _filterTimer = setTimeout(window.filterToc, 200);
    });
  }
}

$('tocTree').addEventListener('click', event => {
  const item = event.target.closest('.toc-item[data-clause-index]');
  if (item) window.scrollToClause(Number(item.dataset.clauseIndex));
});

$('content').addEventListener('click', event => {
  const imageButton = event.target.closest('[data-image-src]');
  if (imageButton) {
    openLightbox(imageButton.dataset.imageSrc, imageButton.dataset.imageAlt);
    return;
  }

  const expandButton = event.target.closest('[data-action="expand-clause"]');
  if (expandButton) {
    const body = expandButton.previousElementSibling;
    body.classList.toggle('collapsed');
    expandButton.textContent = body.classList.contains('collapsed') ? 'Show more' : 'Show less';
  }
});

$('specSelect').addEventListener('change', loadVersions);
$('diffBtn').addEventListener('click', () => window.runDiff());
$('refreshBtn').addEventListener('click', () => window.runDiff(true));
$('downloadBtn').addEventListener('click', downloadSpec);
$('specInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') downloadSpec();
});

// TOC controls: collapsible rail on desktop, off-canvas drawer on mobile.
const mobileTocQuery = window.matchMedia('(max-width: 760px)');

function setMobileToc(open) {
  document.body.classList.toggle('toc-open', open);
  $('mobileTocBtn').setAttribute('aria-expanded', String(open));
}

$('mobileTocBtn').addEventListener('click', () => setMobileToc(true));
$('tocBackdrop').addEventListener('click', () => setMobileToc(false));
$('tocToggle').addEventListener('click', () => {
  if (mobileTocQuery.matches) {
    setMobileToc(false);
    return;
  }
  const collapsed = document.body.classList.toggle('toc-collapsed');
  $('tocToggle').setAttribute('aria-expanded', String(!collapsed));
  $('tocToggle').setAttribute('aria-label', collapsed ? 'Expand table of contents' : 'Collapse table of contents');
});

mobileTocQuery.addEventListener('change', event => {
  setMobileToc(false);
  if (event.matches) document.body.classList.remove('toc-collapsed');
});

// Lightbox controls
$('lightboxClose').addEventListener('click', closeLightbox);
$('lightbox').addEventListener('click', event => {
  if (event.target === $('lightbox')) closeLightbox();
});
document.querySelector('.lightbox-controls').addEventListener('click', event => {
  const button = event.target.closest('[data-lightbox-zoom]');
  if (button) window.zoomLightbox(Number(button.dataset.lightboxZoom));
});

$('navPrevious').addEventListener('click', () => window.navChanged(-1));
$('navNext').addEventListener('click', () => window.navChanged(1));

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if ($('lightbox').classList.contains('open')) closeLightbox();
    else setMobileToc(false);
    return;
  }

  // Don't capture shortcuts while typing in inputs
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

  // Lightbox-open shortcuts (zoom +/- 0)
  if (document.getElementById('lightbox').classList.contains('open')) {
    if (e.key === '+' || e.key === '=') window.zoomLightbox(1);
    else if (e.key === '-') window.zoomLightbox(-1);
    else if (e.key === '0') window.zoomLightbox(0);
    return; // don't process other shortcuts while lightbox open
  }

  // n = next changed clause, Shift+N = previous
  if (e.key === 'n' || e.key === 'N') {
    window.navChanged(e.shiftKey ? -1 : 1);
    return;
  }

  if (e.key === '/') {
    e.preventDefault();
    if (mobileTocQuery.matches) setMobileToc(true);
    $('tocSearchInput').focus();
    return;
  }

  if (e.key === 'Enter' && e.target.tagName === 'BUTTON') {
    if (e.target === $('diffBtn')) window.runDiff();
  }
});

// ===================== INIT =====================
_restoreFromURL().catch(() => loadSpecs());
