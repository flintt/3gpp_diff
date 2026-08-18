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
const INITIAL_CONTENT_HTML = $('content').innerHTML;
const INITIAL_TOC_HTML = $('tocTree').innerHTML;
const compareVersions = (left, right) => {
  const a = String(left).split('.').map(Number);
  const b = String(right).split('.').map(Number);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const difference = (a[i] || 0) - (b[i] || 0);
    if (difference) return difference;
  }
  return 0;
};

function isValidVersionPair(v1 = $('v1Select').value, v2 = $('v2Select').value) {
  return Boolean(v1 && v2 && v1 !== v2 && compareVersions(v1, v2) < 0);
}

function comparisonPairLabel(oldVersion, newVersion) {
  const releases = new Map();
  for (const version of state.versions) {
    releases.set(version.release, (releases.get(version.release) || 0) + 1);
  }
  const endpointLabel = version => releases.get(version.release) > 1
    ? `v${version.version}`
    : `Rel-${version.release}`;
  return `${endpointLabel(oldVersion)} → ${endpointLabel(newVersion)}`;
}

function nearbyComparisonPairs(currentOldVersion, currentNewVersion) {
  const versions = [...state.versions].sort(
    (left, right) => compareVersions(left.version, right.version),
  );
  const oldIndex = versions.findIndex(version => version.version === currentOldVersion);
  const newIndex = versions.findIndex(version => version.version === currentNewVersion);
  if (oldIndex < 0 || newIndex <= oldIndex) return [];

  const pairs = [];
  const seen = new Set([`${currentOldVersion}|${currentNewVersion}`]);
  const addPair = (leftIndex, rightIndex) => {
    if (leftIndex < 0 || rightIndex >= versions.length || leftIndex >= rightIndex) return;
    const oldVersion = versions[leftIndex];
    const newVersion = versions[rightIndex];
    const key = `${oldVersion.version}|${newVersion.version}`;
    if (seen.has(key)) return;
    seen.add(key);
    pairs.push({oldVersion, newVersion});
  };

  // For an adjacent pair, offer the previous/next window and a wider range.
  // For a wider range, offer its two nearest contractions instead.
  if (oldIndex > 0) {
    addPair(oldIndex - 1, oldIndex);
    addPair(oldIndex - 1, newIndex);
  }
  if (newIndex - oldIndex > 1) {
    addPair(oldIndex, newIndex - 1);
    addPair(oldIndex + 1, newIndex);
  } else if (newIndex < versions.length - 1) {
    addPair(newIndex, newIndex + 1);
    addPair(oldIndex, newIndex + 1);
  }
  return pairs;
}

const THEME_STORAGE_KEY = '3gpp-delta-theme';

function applyTheme(theme, persist = false) {
  const nextTheme = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = nextTheme;
  $('themeColorMeta').setAttribute('content', nextTheme === 'light' ? '#f6f8fc' : '#0b1220');
  const toggle = $('themeToggle');
  const targetTheme = nextTheme === 'light' ? 'dark' : 'light';
  toggle.setAttribute('aria-label', `Switch to ${targetTheme} theme`);
  toggle.setAttribute('title', `Switch to ${targetTheme} theme`);
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, nextTheme); } catch (_) {}
  }
}

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
  const annex = title.match(/^(Annex\s+[A-Z0-9]+(?:\s+\([^)]+\))?)\s*:\s*(.+)$/i);
  if (annex && annex[1].toLowerCase().startsWith(id.toLowerCase())) {
    id = annex[1];
    title = annex[2];
  } else if (id === title) {
    title = '';
  } else if (id && title.toLowerCase().startsWith(id.toLowerCase())) {
    const remainder = title.slice(id.length);
    if (/^[\s.:\-–—]/.test(remainder)) title = remainder.replace(/^[\s.:\-–—]+/, '');
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
let _diffEventSource = null;
let _versionLoadGeneration = 0;

async function fetchDiffWithProgress(spec, v1, v2, refresh) {
  const steps = [`Parse v${v1}`, `Parse v${v2}`, 'Compare clauses', 'Load workspace'];
  $('content').innerHTML = renderProgress(steps, 0);

  _diffAbortController?.abort();
  _diffEventSource?.close();
  _diffAbortController = new AbortController();
  const signal = _diffAbortController.signal;
  const params = new URLSearchParams({spec, v1, v2});
  if (refresh) params.set('refresh', '1');

  let usedProgressStream = false;
  if ('EventSource' in window) {
    const streamParams = new URLSearchParams(params);
    streamParams.set('compact', '1');
    try {
      await new Promise((resolve, reject) => {
        const source = new EventSource(`/api/diff-stream?${streamParams}`);
        _diffEventSource = source;
        let settled = false;

        const finish = callback => {
          if (settled) return;
          settled = true;
          source.close();
          if (_diffEventSource === source) _diffEventSource = null;
          signal.removeEventListener('abort', onAbort);
          callback();
        };
        const onAbort = () => finish(() => reject(new DOMException('Comparison cancelled', 'AbortError')));

        source.addEventListener('progress', event => {
          try {
            const progress = JSON.parse(event.data);
            const step = Math.max(0, Math.min(2, Number(progress.step) || 0));
            $('content').innerHTML = renderProgress(steps, step);
          } catch (_) {}
        });
        source.addEventListener('done', () => finish(resolve));
        source.addEventListener('error', event => {
          let message = 'Progress connection failed';
          const error = new Error(message);
          if (event.data) {
            try { message = JSON.parse(event.data).message || message; } catch (_) {}
            error.message = message;
            error.name = 'ComparisonError';
          } else {
            error.name = 'ProgressConnectionError';
          }
          finish(() => reject(error));
        });
        signal.addEventListener('abort', onAbort, {once: true});
      });
      usedProgressStream = true;
    } catch (error) {
      if (error.name !== 'ProgressConnectionError') throw error;
      // Some reverse proxies disable SSE. The regular endpoint remains fully
      // functional, so continue without live progress instead of failing.
    }
  }

  $('content').innerHTML = renderProgress(steps, 3);
  // The progress request has already performed a forced refresh. Fetch the
  // compact, pre-serialized result without asking the backend to recompute it.
  const resultParams = new URLSearchParams({spec, v1, v2});
  resultParams.set('view', 'changes');
  if (refresh && !usedProgressStream) resultParams.set('refresh', '1');
  const response = await fetch(`/api/diff?${resultParams}`, {
    signal,
    headers: {'Accept': 'application/json'},
    cache: refresh ? 'reload' : 'default',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || `Comparison failed (${response.status})`);
  }

  const result = await response.json();
  if (result.error) throw new Error(result.error);
  return result;
}

// ===================== LIGHTBOX =====================
let _lbZoom = 1;
let _lbPan = { x: 0, y: 0, dragging: false, startX: 0, startY: 0 };

function openLightbox(src, alt, originalSrc = '') {
  const lb = document.getElementById('lightbox');
  const img = document.getElementById('lightboxImg');
  const caption = document.getElementById('lightboxCaption');
  const original = document.getElementById('lightboxOriginal');
  img.src = src;
  img.alt = alt || '';
  caption.textContent = alt || '';
  original.hidden = !originalSrc;
  original.href = originalSrc || '';
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
  const original = document.getElementById('lightboxOriginal');
  original.hidden = true;
  original.href = '';
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
    const originalSrc = img.original_src
      ? `/api/image/${spec}/${version}/${img.original_src}?download=1`
      : '';
    const alt = img.alt || '';
    html += `<button class="clause-image" type="button" data-image-src="${escapeHtml(src)}" data-image-original="${escapeHtml(originalSrc)}" data-image-alt="${escapeHtml(alt)}" aria-label="Open figure ${escapeHtml(alt)}">
      <img data-src="${escapeHtml(src)}" alt="${escapeHtml(alt)}" loading="lazy">
    </button>`;
  }
  html += '</div>';
  return html;
}

// ===================== SPEC & VERSION LOADING =====================

async function loadSpecs() {
  try {
    const resp = await fetch('/api/specs');
    if (!resp.ok) throw new Error(`Unable to load specs (${resp.status})`);
    const specs = await resp.json();
    $('specSelect').innerHTML = specs.map(s =>
      `<option value="${escapeHtml(s.id)}">${escapeHtml(s.title || 'TS ' + s.id)}</option>`
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
  if (!spec) return false;
  const loadGeneration = ++_versionLoadGeneration;
  state.currentSpec = spec;

  $('v1Select').innerHTML = '<option value="">Loading...</option>';
  $('v2Select').innerHTML = '<option value="">Loading...</option>';
  $('diffBtn').disabled = true;

  try {
    const resp = await fetch(`/api/versions?${new URLSearchParams({spec})}`);
    if (!resp.ok) throw new Error(`Unable to load versions (${resp.status})`);
    const versions = await resp.json();
    if (loadGeneration !== _versionLoadGeneration || $('specSelect').value !== spec) {
      return false;
    }
    state.versions = versions;

    if (versions.length === 0) {
      $('v1Select').innerHTML = '<option value="">No versions cached</option>';
      $('v2Select').innerHTML = '<option value="">No versions cached</option>';
      $('diffBtn').disabled = true;
      return false;
    }

    const opts = versions.map(v =>
      `<option value="${escapeHtml(v.version)}">${escapeHtml(v.label || 'Rel-' + v.release + ' (' + v.version + ')')}</option>`
    ).join('');

    $('v1Select').innerHTML = '<option value="">Select older version...</option>' + opts;
    $('v2Select').innerHTML = '<option value="">Select newer version...</option>' + opts;

    // Auto-select latest two releases
    if (versions.length >= 2) {
      const sorted = [...versions].sort((a, b) => compareVersions(b.version, a.version));
      $('v2Select').value = sorted[0].version;
      const prevRelease = sorted[0].release - 1;
      const prev = sorted.find(v => v.release === prevRelease && v.version.endsWith('.0.0'));
      if (prev) {
        $('v1Select').value = prev.version;
      } else if (sorted.length > 1) {
        $('v1Select').value = sorted[1].version;
      }
    }

    $('diffBtn').disabled = !($('v1Select').value && $('v2Select').value);
    return true;

  } catch (err) {
    if (loadGeneration !== _versionLoadGeneration || $('specSelect').value !== spec) {
      return false;
    }
    $('v1Select').innerHTML = `<option value="">Error: ${err.message}</option>`;
    $('v2Select').innerHTML = `<option value="">Error: ${err.message}</option>`;
    $('diffBtn').disabled = true;
    return false;
  }
}

// ===================== DOWNLOAD =====================

async function downloadSpec() {
  const spec = $('specInput').value.trim();
  if (!spec) { showToast('Please enter a spec number', 'error'); return; }
  if (!/^\d{2,3}\.\d{3}$/.test(spec)) {
    showToast('Use a spec number such as 23.501', 'error');
    return;
  }

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
    if (!resp.ok || data.error) throw new Error(data.error || `Download failed (${resp.status})`);
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
      const sr = await fetch(`/api/download-status?${new URLSearchParams({spec})}`);
      const st = await sr.json();
           if (st.status === 'listing')      prog.textContent = `[${elapsed()}] Listing releases...`;
      else if (st.status === 'downloading')  prog.textContent = `[${elapsed()}] Downloading ${st.done}/${st.total} releases...`;
      else if (st.status === 'completed')    {
        const unavailable = Number(st.failed) || 0;
        prog.textContent = unavailable
          ? `[${elapsed()}] Downloaded ${st.available}/${st.total} releases (${unavailable} unavailable)`
          : `[${elapsed()}] Download complete!`;
        break;
      }
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
          const sr = await fetch(`/api/precompute-status?${new URLSearchParams({spec})}`);
          const st = await sr.json();
          if (st.status === 'computing') {
            prog.textContent = `[${elapsed()}] Checked ${st.processed}/${st.total} pairs; ${st.done} ready...`;
          } else if (st.status === 'completed') {
            prog.textContent = `[${elapsed()}] All diffs ready!`;
            break;
          } else if (st.status === 'partial') {
            prog.textContent = `[${elapsed()}] ${st.done}/${st.total} diffs ready; ${st.failed} failed`;
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
let _activeTocIndex = -1;
let _tocSearchAbortController = null;

function updateTocSearchSummary(matchCount = null, searching = false) {
  const summary = $('tocSearchSummary');
  if (!summary) return;
  summary.classList.toggle('searching', searching);
  if (searching) {
    summary.textContent = 'Searching full clause text';
    summary.hidden = false;
  } else if (matchCount === null) {
    summary.textContent = '';
    summary.hidden = true;
  } else {
    summary.textContent = matchCount === 1 ? '1 matching clause' : `${matchCount} matching clauses`;
    summary.hidden = false;
  }
}

function applyTocFilter(predicate) {
  let matches = 0;
  _tocRecords.forEach((record, index) => {
    const match = predicate(record, index);
    record.element.hidden = !match;
    if (match) matches += 1;
  });
  return matches;
}

function renderToc(clauses, preparedFlat = null) {
  const tree = $('tocTree');
  if (!clauses || clauses.length === 0) {
    tree.innerHTML = '<div class="toc-no-results">No clauses found</div>';
    _tocRecords = [];
    return;
  }

  let html = '';
  const flat = preparedFlat || flattenClauseTree(clauses);
  flat.forEach((node, index) => { node._flatIndex = index; });

  for (let index = 0; index < flat.length; index++) {
    const node = flat[index];
    const display = getClauseDisplayParts(node);
    const indent = node._depth;
    const status = node.status || 'unchanged';
    const id = node.id || '';

    const active = index === _activeTocIndex ? ' active' : '';
    html += `<button class="toc-item status-${status}${active}" type="button" style="--indent:${indent}" data-id="${escapeHtml(id)}" data-clause-index="${index}">
      <span class="toc-id">${escapeHtml(display.id)}</span>
      <span class="toc-title">${escapeHtml(display.title)}</span>
    </button>`;
  }

  tree.innerHTML = html;
  const items = tree.querySelectorAll('.toc-item');
  _tocRecords = flat.map((node, index) => ({
    element: items[index],
    node,
    heading: [node.id, node.title, node.old_id, node.old_title]
      .filter(Boolean)
      .join(' ')
      .toLowerCase(),
  }));

  return flat;
}

window.filterToc = async function() {
  const input = $('tocSearchInput');
  const raw = input.value.trim().toLowerCase();
  const keywords = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];

  if (keywords.length === 0) {
    _tocSearchAbortController?.abort();
    _tocSearchAbortController = null;
    $('tocSearch').removeAttribute('aria-busy');
    _tocRecords.forEach(record => { record.element.hidden = false; });
    updateTocSearchSummary();
    return;
  }

  if (state.diffData?.view !== 'changes') {
    const count = applyTocFilter(record => {
      const fields = [
        record.heading,
        record.node.body,
        record.node.old_body,
        record.node.new_body,
      ];
      return keywords.some(keyword =>
        fields.some(value => value && value.toLowerCase().includes(keyword))
      );
    });
    updateTocSearchSummary(count);
    return;
  }

  // Give immediate heading results while the server searches unchanged bodies
  // that were intentionally omitted from the lightweight initial response.
  const headingCount = applyTocFilter(record =>
    keywords.some(keyword => record.heading.includes(keyword))
  );
  updateTocSearchSummary(headingCount, true);

  _tocSearchAbortController?.abort();
  const controller = new AbortController();
  _tocSearchAbortController = controller;
  const comparison = state.diffData;
  const generation = _renderGeneration;
  const params = new URLSearchParams({
    spec: comparison.spec,
    v1: comparison.old_version,
    v2: comparison.new_version,
    q: raw,
  });
  $('tocSearch').setAttribute('aria-busy', 'true');
  try {
    const response = await fetch(`/api/diff-search?${params}`, {
      signal: controller.signal,
      headers: {'Accept': 'application/json'},
      cache: 'no-store',
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `Search failed (${response.status})`);
    }
    const result = await response.json();
    if (!Array.isArray(result.matches)) throw new Error('Invalid search response');
    if (
      controller.signal.aborted
      || generation !== _renderGeneration
      || input.value.trim().toLowerCase() !== raw
    ) return;
    const matchedIndexes = new Set(result.matches);
    const count = applyTocFilter((_record, index) => matchedIndexes.has(index));
    updateTocSearchSummary(count);
  } catch (error) {
    if (error.name !== 'AbortError') {
      updateTocSearchSummary(headingCount);
      showToast(`Unable to search full text: ${error.message}`, 'error');
    }
  } finally {
    if (_tocSearchAbortController === controller) {
      _tocSearchAbortController = null;
      $('tocSearch').removeAttribute('aria-busy');
    }
  }
};

// ===================== DIFF RENDER =====================
let _showUnchanged = false;
let _diffLayout = 'split';
const CLAUSE_BATCH_SIZE = 36;
let _allClauseNodes = [];
let _renderNodes = [];
let _renderPositionByFlatIndex = new Map();
let _renderedClauseCount = 0;
let _renderGeneration = 0;
let _clauseObserver = null;
let _wordDiffObserver = null;
let _imageObserver = null;
let _fullDiffPromise = null;

const scheduleIdle = window.requestIdleCallback
  ? callback => window.requestIdleCallback(callback, {timeout: 350})
  : callback => window.setTimeout(callback, 0);

function prepareComparisonLoading() {
  _renderGeneration += 1;
  _clauseObserver?.disconnect();
  _wordDiffObserver?.disconnect();
  _imageObserver?.disconnect();
  _tocSearchAbortController?.abort();
  _tocSearchAbortController = null;
  state.diffData = null;
  _showUnchanged = false;
  _allClauseNodes = [];
  _renderNodes = [];
  _renderPositionByFlatIndex = new Map();
  _renderedClauseCount = 0;
  _tocRecords = [];
  _activeTocIndex = -1;
  _changedIndexes = [];
  _navIndex = -1;
  _fullDiffPromise = null;
  $('statsBar').replaceChildren();
  $('statsBar').hidden = true;
  $('tocTree').innerHTML = '<div class="toc-empty"><p>Preparing comparison…</p></div>';
  $('tocSearch').hidden = true;
  updateTocSearchSummary();
  $('clauseNav').classList.remove('visible');
}

async function ensureFullDiffData() {
  const partial = state.diffData;
  if (!partial || partial.view !== 'changes') return partial;
  if (_fullDiffPromise) return _fullDiffPromise;

  const generation = _renderGeneration;
  const params = new URLSearchParams({
    spec: partial.spec,
    v1: partial.old_version,
    v2: partial.new_version,
  });
  const promise = (async () => {
    const response = await fetch(`/api/diff?${params}`, {
      signal: _diffAbortController?.signal,
      headers: {'Accept': 'application/json'},
      cache: 'no-cache',
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `Unable to load full comparison (${response.status})`);
    }
    const full = await response.json();
    if (generation !== _renderGeneration || state.diffData !== partial) {
      return state.diffData;
    }

    const fullFlat = flattenClauseTree(full.clauses || []);
    if (fullFlat.length !== _allClauseNodes.length) {
      throw new Error('Comparison changed while full text was loading');
    }
    for (let index = 0; index < fullFlat.length; index++) {
      const target = _allClauseNodes[index];
      const source = fullFlat[index];
      if (target.id !== source.id || target.status !== source.status) {
        throw new Error('Comparison changed while full text was loading');
      }
      const depth = target._depth;
      Object.assign(target, source, {_depth: depth, _flatIndex: index});
      if (_tocRecords[index]) _tocRecords[index].node = target;
    }
    full.view = 'full';
    state.diffData = full;
    return full;
  })();
  _fullDiffPromise = promise;
  try {
    return await promise;
  } finally {
    if (_fullDiffPromise === promise) _fullDiffPromise = null;
  }
}

async function setUnchangedVisibility(show, options = {}) {
  const checkbox = $('uncToggle');
  const shouldShow = Boolean(show);
  if (!checkbox) return false;

  if (shouldShow && state.diffData?.view === 'changes') {
    checkbox.disabled = true;
    try {
      await ensureFullDiffData();
    } catch (error) {
      if (checkbox.isConnected) checkbox.checked = false;
      if (error.name !== 'AbortError') {
        showToast(`Unable to load unchanged clauses: ${error.message}`, 'error');
      }
      return false;
    } finally {
      if (checkbox.isConnected) checkbox.disabled = false;
    }
  }

  if (!checkbox.isConnected) return false;
  checkbox.checked = shouldShow;
  _showUnchanged = shouldShow;
  startClauseRendering(shouldShow);
  if (options.scroll !== false) $('content').scrollTo({top: 0});
  return true;
}

async function setDiffLayout(layout, options = {}) {
  const nextLayout = layout === 'inline' ? 'inline' : 'split';
  if (_diffLayout === nextLayout || !state.diffData) return;

  const content = $('content');
  const contentTop = content.getBoundingClientRect().top;
  const anchor = [...content.querySelectorAll('.clause-diff')].find(element => (
    element.getBoundingClientRect().bottom > contentTop + 1
  ));
  const anchorIndex = anchor ? Number(anchor.dataset.clauseIndex) : null;
  const anchorOffset = anchor ? anchor.getBoundingClientRect().top - contentTop : 0;

  _diffLayout = nextLayout;
  document.querySelectorAll('[data-diff-layout]').forEach(button => {
    const active = button.dataset.diffLayout === nextLayout;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  startClauseRendering(_showUnchanged);

  if (anchorIndex !== null) {
    const anchorPosition = _renderPositionByFlatIndex.get(anchorIndex);
    if (anchorPosition !== undefined && _renderedClauseCount <= anchorPosition) {
      appendClauseBatch(anchorPosition + 1);
    }
    await new Promise(resolve => requestAnimationFrame(resolve));
    const restoredAnchor = document.getElementById(`clause-${anchorIndex}`);
    if (restoredAnchor) {
      content.scrollTop += restoredAnchor.getBoundingClientRect().top - contentTop - anchorOffset;
    }
  }

  if ($('tocSearchInput').value.trim() && _tocRecords.length > 0) window.filterToc();
  if (options.updateURL !== false) updateCurrentComparisonURL(true);
}

function addInlineVersionTooltips(element, oldVersion, newVersion) {
  for (const deletion of element.querySelectorAll('.word-del')) {
    deletion.title = `Only in v${oldVersion} (not in v${newVersion})`;
  }
  for (const addition of element.querySelectorAll('.word-add')) {
    addition.title = `Only in v${newVersion} (not in v${oldVersion})`;
  }
}

function queueWordDiff(element, node, generation) {
  if (element.dataset.wordDiffState) return;
  element.dataset.wordDiffState = 'queued';
  scheduleIdle(() => {
    if (generation !== _renderGeneration || !element.isConnected) return;
    const cells = element.querySelectorAll('.diff-word-content');
    const {leftHtml, rightHtml, inlineHtml} = renderBodyDiffHtml(
      node.old_body || '',
      node.new_body || '',
    );
    if (_diffLayout === 'inline') {
      if (cells.length < 1) return;
      cells[0].innerHTML = inlineHtml || renderBodyContentHtml(
        node.new_body || node.old_body || '',
      );
      addInlineVersionTooltips(
        cells[0],
        state.diffData?.old_version || '',
        state.diffData?.new_version || '',
      );
    } else {
      if (cells.length < 2) return;
      cells[0].innerHTML = leftHtml || renderBodyContentHtml(node.old_body || '');
      cells[1].innerHTML = rightHtml || renderBodyContentHtml(node.new_body || '');
    }
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
  for (const image of list.querySelectorAll('img[data-src]:not([data-image-observed])')) {
    image.dataset.imageObserved = 'true';
    if (_imageObserver) {
      _imageObserver.observe(image);
    } else {
      image.src = image.dataset.src;
      image.removeAttribute('data-src');
    }
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
  _imageObserver?.disconnect();

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

  _imageObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      _imageObserver.unobserve(entry.target);
      entry.target.src = entry.target.dataset.src;
      entry.target.removeAttribute('data-src');
    }
  }, {root: $('content'), rootMargin: '450px 0px'});

  _clauseObserver = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) appendClauseBatch();
  }, {root: $('content'), rootMargin: '1000px 0px'});
  _clauseObserver.observe($('renderSentinel'));
  appendClauseBatch();
}

function renderDiff(diffData) {
  _fullDiffPromise = null;
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
  const currentOldVersion = state.versions.find(
    version => version.version === diffData.old_version,
  );
  const currentNewVersion = state.versions.find(
    version => version.version === diffData.new_version,
  );
  const nearbyPairs = nearbyComparisonPairs(diffData.old_version, diffData.new_version);
  const layoutToggleHtml = `<div class="diff-layout-toggle" role="group" aria-label="Diff layout">
      <span>View</span>
      <button type="button" data-diff-layout="split" aria-pressed="${_diffLayout === 'split'}" class="${_diffLayout === 'split' ? 'active' : ''}">Split</button>
      <button type="button" data-diff-layout="inline" aria-pressed="${_diffLayout === 'inline'}" class="${_diffLayout === 'inline' ? 'active' : ''}">Inline</button>
    </div>`;
  const pairSwitcherHtml = currentOldVersion && currentNewVersion && nearbyPairs.length > 0
    ? `<div class="comparison-switcher" role="group" aria-label="Nearby comparisons">
        <span>Quick switch</span>
        <strong class="current-version-pair" title="Current comparison">Current · ${escapeHtml(comparisonPairLabel(currentOldVersion, currentNewVersion))}</strong>
        ${nearbyPairs.map(pair => `
          <button class="pair-shortcut-btn" type="button"
            data-old-version="${escapeHtml(pair.oldVersion.version)}"
            data-new-version="${escapeHtml(pair.newVersion.version)}"
            title="Compare v${escapeHtml(pair.oldVersion.version)} with v${escapeHtml(pair.newVersion.version)}">
            ${escapeHtml(comparisonPairLabel(pair.oldVersion, pair.newVersion))}
          </button>`).join('')}
      </div>`
    : '';
  $('statsBar').innerHTML = `
    <span class="stat stat-total" id="statTotal">${total} total clauses</span>
    <span class="stat stat-added" id="statAdded">+${stats.added} added</span>
    <span class="stat stat-deleted" id="statDeleted">-${stats.deleted} deleted</span>
    <span class="stat stat-modified" id="statModified">~${stats.modified} modified</span>
    ${layoutToggleHtml}
    ${pairSwitcherHtml}
    ${toggleHtml}
  `;
  $('statsBar').hidden = false;

  $('statsBar').querySelector('.diff-layout-toggle')?.addEventListener('click', event => {
    const button = event.target.closest('[data-diff-layout]');
    if (button) setDiffLayout(button.dataset.diffLayout);
  });

  $('statsBar').querySelector('.comparison-switcher')?.addEventListener('click', event => {
    const button = event.target.closest('.pair-shortcut-btn');
    if (!button) return;
    $('v1Select').value = button.dataset.oldVersion;
    $('v2Select').value = button.dataset.newVersion;
    window.runDiff();
  });

  const uncCb = $(toggleId);
  if (uncCb) {
    uncCb.addEventListener('change', async () => {
      await setUnchangedVisibility(uncCb.checked);
      updateCurrentComparisonURL(true);
    });
  }

  // Flatten immediately for the content pane, but defer the thousands of TOC
  // elements until after the first clause batch can paint.
  const flat = flattenClauseTree(diffData.clauses);
  flat.forEach((node, index) => { node._flatIndex = index; });
  _tocRecords = [];
  _activeTocIndex = -1;
  $('tocTree').innerHTML = '<div class="toc-empty"><p>Building clause index…</p></div>';
  $('tocSearch').hidden = true;
  updateTocSearchSummary();
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
  // Content rendering may restart immediately when a deep link opens an
  // unchanged clause. The flattened node array remains the identity of this
  // comparison, so use it to reject stale work without cancelling the TOC for
  // a valid view-mode change.
  const tocNodes = flat;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    scheduleIdle(() => {
      if (_allClauseNodes !== tocNodes) return;
      renderToc(diffData.clauses, flat);
      $('tocSearch').hidden = false;
      if ($('tocSearchInput').value.trim()) window.filterToc();
    });
  }));
}

function parsePipeTableRow(line) {
  const trimmed = String(line || '').trim();
  if (!trimmed.startsWith('|') || !trimmed.endsWith('|')) return null;

  const cells = [];
  let value = '';
  let escaped = false;
  for (const character of trimmed.slice(1, -1)) {
    if (escaped) {
      value += character === 'n'
        ? '\n'
        : character === 't'
          ? '\t'
          : character === '|' || character === '\\'
            ? character
            : `\\${character}`;
      escaped = false;
    } else if (character === '\\') {
      escaped = true;
    } else if (character === '|') {
      cells.push(value.trim());
      value = '';
    } else {
      value += character;
    }
  }
  if (escaped) value += '\\';
  cells.push(value.trim());
  return cells.length >= 1 ? cells : null;
}

function parseBodyBlocks(text) {
  const lines = String(text || '').split('\n');
  const blocks = [];
  let textLines = [];
  const flushText = () => {
    if (textLines.length) blocks.push({type: 'text', text: textLines.join('\n')});
    textLines = [];
  };

  for (let index = 0; index < lines.length;) {
    const firstRow = parsePipeTableRow(lines[index]);
    if (!firstRow) {
      textLines.push(lines[index]);
      index++;
      continue;
    }

    flushText();
    const rows = [firstRow];
    index++;
    while (index < lines.length) {
      const row = parsePipeTableRow(lines[index]);
      if (!row) break;
      rows.push(row);
      index++;
    }
    blocks.push({type: 'table', rows});
  }
  flushText();
  return blocks;
}

function renderDocumentTable(rows, cellRenderer = value => escapeHtml(value), rowClass = () => '') {
  const columnCount = Math.max(0, ...rows.map(row => row.length));
  const hasHeader = rows.length > 1;
  const body = rows.map((row, rowIndex) => {
    const cells = [...row, ...Array(Math.max(0, columnCount - row.length)).fill('')];
    const cellTag = hasHeader && rowIndex === 0 ? 'th' : 'td';
    const scope = hasHeader && rowIndex === 0 ? ' scope="col"' : '';
    const cellsHtml = cells.map((value, columnIndex) => {
      const content = cellRenderer(value, rowIndex, columnIndex) || '&nbsp;';
      return `<${cellTag}${scope}>${content}</${cellTag}>`;
    }).join('');
    const className = rowClass(rowIndex);
    return `<tr${className ? ` class="${className}"` : ''}>${cellsHtml}</tr>`;
  }).join('');
  return `<div class="doc-table-wrap"><table class="doc-table">${body}</table></div>`;
}

function renderBodyBlock(block) {
  if (!block) return '';
  if (block.type === 'table') return renderDocumentTable(block.rows);
  return block.text
    ? `<div class="body-text-block">${escapeHtml(block.text)}</div>`
    : '';
}

function renderBodyContentHtml(text) {
  const value = String(text || '');
  if (!value) return '(no content)';
  return parseBodyBlocks(value).map(renderBodyBlock).join('');
}

function renderComparedTable(oldRows, newRows) {
  const valueAt = (rows, rowIndex, columnIndex) => rows[rowIndex]?.[columnIndex] || '';
  const diffCell = (oldValue, newValue, side) => {
    const result = renderWordDiffHtml(oldValue, newValue);
    const html = side === 'old'
      ? result.leftHtml
      : side === 'new'
        ? result.rightHtml
        : result.inlineHtml;
    return html || escapeHtml(side === 'old' ? oldValue : newValue);
  };

  const leftHtml = renderDocumentTable(
    oldRows,
    (value, rowIndex, columnIndex) => diffCell(
      value,
      valueAt(newRows, rowIndex, columnIndex),
      'old',
    ),
    rowIndex => rowIndex >= newRows.length ? 'doc-table-row-removed' : '',
  );
  const rightHtml = renderDocumentTable(
    newRows,
    (value, rowIndex, columnIndex) => diffCell(
      valueAt(oldRows, rowIndex, columnIndex),
      value,
      'new',
    ),
    rowIndex => rowIndex >= oldRows.length ? 'doc-table-row-added' : '',
  );

  const rowCount = Math.max(oldRows.length, newRows.length);
  const columnCount = Math.max(
    0,
    ...oldRows.map(row => row.length),
    ...newRows.map(row => row.length),
  );
  const inlineRows = Array.from(
    {length: rowCount},
    () => Array(columnCount).fill(''),
  );
  const inlineHtml = renderDocumentTable(
    inlineRows,
    (_value, rowIndex, columnIndex) => diffCell(
      valueAt(oldRows, rowIndex, columnIndex),
      valueAt(newRows, rowIndex, columnIndex),
      'inline',
    ),
    rowIndex => rowIndex >= oldRows.length
      ? 'doc-table-row-added'
      : rowIndex >= newRows.length
        ? 'doc-table-row-removed'
        : '',
  );
  return {leftHtml, rightHtml, inlineHtml};
}

function renderBodyDiffHtml(oldText, newText) {
  const oldBlocks = parseBodyBlocks(oldText);
  const newBlocks = parseBodyBlocks(newText);
  const hasTable = [...oldBlocks, ...newBlocks].some(block => block.type === 'table');
  if (!hasTable) return renderWordDiffHtml(oldText, newText);

  let leftHtml = '';
  let rightHtml = '';
  let inlineHtml = '';
  const blockCount = Math.max(oldBlocks.length, newBlocks.length);
  for (let index = 0; index < blockCount; index++) {
    const oldBlock = oldBlocks[index];
    const newBlock = newBlocks[index];
    if (oldBlock?.type === 'text' && newBlock?.type === 'text') {
      const result = renderWordDiffHtml(oldBlock.text, newBlock.text);
      leftHtml += `<div class="body-text-block">${result.leftHtml}</div>`;
      rightHtml += `<div class="body-text-block">${result.rightHtml}</div>`;
      inlineHtml += `<div class="body-text-block">${result.inlineHtml}</div>`;
    } else if (oldBlock?.type === 'table' && newBlock?.type === 'table') {
      const result = renderComparedTable(oldBlock.rows, newBlock.rows);
      leftHtml += result.leftHtml;
      rightHtml += result.rightHtml;
      inlineHtml += result.inlineHtml;
    } else {
      if (oldBlock) {
        const oldHtml = renderBodyBlock(oldBlock);
        leftHtml += oldHtml;
        inlineHtml += `<div class="doc-block-removed">${oldHtml}</div>`;
      }
      if (newBlock) {
        const newHtml = renderBodyBlock(newBlock);
        rightHtml += newHtml;
        inlineHtml += `<div class="doc-block-added">${newHtml}</div>`;
      }
    }
  }
  return {leftHtml, rightHtml, inlineHtml};
}

function clauseDiffHtml(node, spec, oldVersion, newVersion, skipWordDiff) {
  const status = node.status || 'unchanged';
  const isMoved = status === 'modified' && node.change_type === 'moved';
  const statusStyle = isMoved ? 'moved' : status;
  const id = node.id || '';
  const display = getClauseDisplayParts(node);
  const oldDisplay = node.old_title
    ? getClauseDisplayParts({id: node.old_id || node.id, title: node.old_title})
    : null;
  const clauseId = `clause-${node._flatIndex}`;
  const idHtml = node.old_id
    ? `<span class="clause-id clause-id-change" title="v${escapeHtml(oldVersion)}: ${escapeHtml(oldDisplay.id)}; v${escapeHtml(newVersion)}: ${escapeHtml(display.id)}">
        <span class="id-old">${escapeHtml(oldDisplay.id)}</span>
        <span aria-hidden="true">→</span>
        <span class="id-new">${escapeHtml(display.id)}</span>
      </span>`
    : `<span class="clause-id">${escapeHtml(display.id)}</span>`;
  const titleChanged = oldDisplay &&
    oldDisplay.title.replace(/\s+/g, ' ').trim() !== display.title.replace(/\s+/g, ' ').trim();
  const titleHtml = titleChanged
    ? `<span class="clause-title title-change" title="v${escapeHtml(oldVersion)}: ${escapeHtml(oldDisplay.title)}; v${escapeHtml(newVersion)}: ${escapeHtml(display.title)}">
        <span class="title-old">${escapeHtml(oldDisplay.title)}</span>
        <span class="title-change-arrow" aria-hidden="true">→</span>
        <span class="title-new">${escapeHtml(display.title)}</span>
      </span>`
    : `<span class="clause-title">${escapeHtml(display.title)}</span>`;

  let bodyHtml = '';
  let versionContextHtml = '';

  if (status === 'modified') {
    if (isMoved) {
      const moveDescription = `Moved in v${newVersion}: clause ${oldDisplay?.id || node.old_id || ''} → ${display.id}`;
      versionContextHtml = `<span class="clause-version-context" aria-label="${escapeHtml(moveDescription)}" title="${escapeHtml(moveDescription)}">
        <span class="version-chip moved">Moved in v${escapeHtml(newVersion || 'New')}</span>
      </span>`;
    } else {
      versionContextHtml = `<span class="clause-version-context" aria-label="Comparing version ${escapeHtml(oldVersion)} with ${escapeHtml(newVersion)}">
        <span class="version-chip old">v${escapeHtml(oldVersion || 'Old')}</span>
        <span class="version-arrow-small" aria-hidden="true">→</span>
        <span class="version-chip new">v${escapeHtml(newVersion || 'New')}</span>
      </span>`;
    }
  } else if (status === 'added') {
    versionContextHtml = `<span class="clause-version-context"><span class="version-chip new">Added in v${escapeHtml(newVersion || 'New')}</span></span>`;
  } else if (status === 'deleted') {
    versionContextHtml = `<span class="clause-version-context"><span class="version-chip old">Removed after v${escapeHtml(oldVersion || 'Old')}</span></span>`;
  }

  if (status === 'unchanged') {
    const imgs = renderImageThumbnails(node.images, spec, newVersion);
    const body = node.body || '';
    const collapsed = body.length > 300 ? ' collapsed' : '';
    bodyHtml = imgs + `<div class="clause-body${collapsed}">${renderBodyContentHtml(body)}</div>`;
    if (body.length > 300) {
      bodyHtml += '<button class="expand-btn" type="button" data-action="expand-clause">Show more</button>';
    }
  } else if (status === 'added') {
    const imgs = renderImageThumbnails(node.images, spec, newVersion);
    const body = node.body || '';
    bodyHtml = _diffLayout === 'inline'
      ? `<div class="diff-view diff-view-inline">
        <div class="diff-pane diff-pane-inline" aria-label="Added in version ${escapeHtml(newVersion)}">
          ${imgs}
          <div class="diff-line diff-line-inline add" title="Only in v${escapeHtml(newVersion)} (not in v${escapeHtml(oldVersion)})">
            <div class="diff-line-content">${renderBodyContentHtml(body)}</div>
          </div>
        </div>
      </div>`
      : `<div class="diff-view">
        <div class="diff-pane diff-pane-old" aria-label="Previous version">
          <div class="diff-empty">Clause did not exist in the old version</div>
        </div>
        <div class="diff-pane diff-pane-new" aria-label="Version ${escapeHtml(newVersion)}">
          ${imgs}
          <div class="diff-line add">
            <span class="diff-line-num"></span>
            <div class="diff-line-content">${renderBodyContentHtml(body)}</div>
          </div>
        </div>
      </div>`;
  } else if (status === 'deleted') {
    const imgs = renderImageThumbnails(node.images, spec, oldVersion);
    const body = node.body || '';
    bodyHtml = _diffLayout === 'inline'
      ? `<div class="diff-view diff-view-inline">
        <div class="diff-pane diff-pane-inline" aria-label="Removed after version ${escapeHtml(oldVersion)}">
          ${imgs}
          <div class="diff-line diff-line-inline del" title="Only in v${escapeHtml(oldVersion)} (not in v${escapeHtml(newVersion)})">
            <div class="diff-line-content">${renderBodyContentHtml(body)}</div>
          </div>
        </div>
      </div>`
      : `<div class="diff-view">
        <div class="diff-pane diff-pane-old" aria-label="Version ${escapeHtml(oldVersion)}">
          ${imgs}
          <div class="diff-line del">
            <span class="diff-line-num"></span>
            <div class="diff-line-content">${renderBodyContentHtml(body)}</div>
          </div>
        </div>
        <div class="diff-pane diff-pane-new" aria-label="New version">
          <div class="diff-empty">Clause removed in the new version</div>
        </div>
      </div>`;
  } else if (status === 'modified') {
    const oldImgs = renderImageThumbnails(node.old_images, spec, oldVersion);
    const newImgs = renderImageThumbnails(node.new_images, spec, newVersion);
    const oldText = node.old_body || '';
    const newText = node.new_body || '';

    let wordLeft, wordRight, wordInline;
    if (skipWordDiff) {
      wordLeft = renderBodyContentHtml(oldText);
      wordRight = renderBodyContentHtml(newText);
      wordInline = renderBodyContentHtml(newText || oldText);
    } else {
      const result = renderBodyDiffHtml(oldText, newText);
      wordLeft = result.leftHtml;
      wordRight = result.rightHtml;
      wordInline = result.inlineHtml;
    }

    if (_diffLayout === 'inline') {
      const oldImageGroup = oldImgs
        ? `<div class="inline-image-version" title="Figures in v${escapeHtml(oldVersion)}">
            <span>Figures in v${escapeHtml(oldVersion)}</span>${oldImgs}
          </div>`
        : '';
      const newImageGroup = newImgs
        ? `<div class="inline-image-version" title="Figures in v${escapeHtml(newVersion)}">
            <span>Figures in v${escapeHtml(newVersion)}</span>${newImgs}
          </div>`
        : '';
      bodyHtml = `<div class="diff-view diff-view-inline">
        <div class="diff-pane diff-pane-inline" aria-label="Inline comparison of version ${escapeHtml(oldVersion)} with ${escapeHtml(newVersion)}">
          ${oldImageGroup}${newImageGroup}
          <div class="diff-line"><div class="diff-word-content inline-word-content">${wordInline || escapeHtml(newText || oldText) || '(no content)'}</div></div>
        </div>
      </div>`;
    } else {
      bodyHtml = `<div class="diff-view">
        <div class="diff-pane diff-pane-old" aria-label="Version ${escapeHtml(oldVersion)}">
          ${oldImgs}
          <div class="diff-line"><div class="diff-word-content">${wordLeft || escapeHtml(oldText) || '(no content)'}</div></div>
        </div>
        <div class="diff-pane diff-pane-new" aria-label="Version ${escapeHtml(newVersion)}">
          ${newImgs}
          <div class="diff-line"><div class="diff-word-content">${wordRight || escapeHtml(newText) || '(no content)'}</div></div>
        </div>
      </div>`;
    }
  }

  return `<article class="clause-diff ${status}${isMoved ? ' change-moved' : ''}" id="${clauseId}" data-clause-id="${escapeHtml(id)}" data-clause-index="${node._flatIndex}">
    <div class="clause-diff-header">
      ${idHtml}
      ${versionContextHtml}
      ${titleHtml}
      <span class="status-badge ${statusStyle}">${statusStyle}</span>
    </div>
    ${bodyHtml}
  </article>`;
}

// ===================== WORD-LEVEL DIFF =====================
const MAX_LCS_CELLS = 300_000;
const MAX_MYERS_EDIT_DISTANCE = 512;
const tokenizeDiffText = text => text.match(/\w+|[^\w\s]|\s+/g) || [];
const diffSegmenters = [
  text => text.match(/[^\n]*\n|[^\n]+$/g) || [],
  text => text.match(/.*?(?:[.!?](?:["')\]]+)?\s+|[;:]\s+|\n+)|.+$/gs) || [],
  text => text.match(/.*?(?:[,;:]\s+|[.!?](?:["')\]]+)?\s+|\n+)|.+$/gs) || [],
];

function renderWordDiffHtml(oldText, newText) {
  return _renderHierarchicalDiff(oldText, newText, 0);
}

function _renderHierarchicalDiff(oldText, newText, level) {
  // Try an exact word diff first. On deeper levels, avoid retrying Myers on
  // the same very large block until segmentation has made it smaller.
  const allowMyers = level === 0 || oldText.length + newText.length < 12_000;
  const exact = _renderExactTokenDiff(oldText, newText, allowMyers);
  if (exact) return exact;

  const segmenter = diffSegmenters[level];
  if (!segmenter) return _renderCoarseDiff(oldText, newText);

  const oldSegments = segmenter(oldText);
  const newSegments = segmenter(newText);
  if (oldSegments.length <= 1 && newSegments.length <= 1) {
    return _renderHierarchicalDiff(oldText, newText, level + 1);
  }

  const ops = _sequenceDiff(oldSegments, newSegments);
  if (!ops) return _renderHierarchicalDiff(oldText, newText, level + 1);

  let leftHtml = '';
  let rightHtml = '';
  let inlineHtml = '';
  let oldChanged = [];
  let newChanged = [];

  const flushChangedBlock = () => {
    if (oldChanged.length === 0 && newChanged.length === 0) return;
    const nested = _renderHierarchicalDiff(oldChanged.join(''), newChanged.join(''), level + 1);
    leftHtml += nested.leftHtml;
    rightHtml += nested.rightHtml;
    inlineHtml += nested.inlineHtml;
    oldChanged = [];
    newChanged = [];
  };

  for (const [op, oldStart, oldEnd, newStart, newEnd] of ops) {
    if (op === 'equal') {
      flushChangedBlock();
      const equalHtml = escapeHtml(oldSegments.slice(oldStart, oldEnd).join(''));
      leftHtml += equalHtml;
      rightHtml += equalHtml;
      inlineHtml += equalHtml;
    } else if (op === 'delete') {
      oldChanged.push(...oldSegments.slice(oldStart, oldEnd));
    } else if (op === 'insert') {
      newChanged.push(...newSegments.slice(newStart, newEnd));
    }
  }
  flushChangedBlock();
  return {leftHtml, rightHtml, inlineHtml};
}

function _renderExactTokenDiff(oldText, newText, allowMyers) {
  const a = tokenizeDiffText(oldText);
  const b = tokenizeDiffText(newText);
  const n = a.length, m = b.length;

  if (n === 0 && m === 0) return {leftHtml: '', rightHtml: '', inlineHtml: ''};

  // Trim common prefix and suffix to reduce problem size
  let prefixLen = 0;
  while (prefixLen < n && prefixLen < m && a[prefixLen] === b[prefixLen]) prefixLen++;
  let suffixLen = 0;
  while (suffixLen < n - prefixLen && suffixLen < m - prefixLen && a[n - 1 - suffixLen] === b[m - 1 - suffixLen]) suffixLen++;

  // Slice to the changed middle section
  const aMid = a.slice(prefixLen, n - suffixLen);
  const bMid = b.slice(prefixLen, m - suffixLen);

  let ops;
  if (aMid.length * bMid.length <= MAX_LCS_CELLS) {
    ops = _lcsDiff(aMid, bMid);
  } else if (allowMyers) {
    ops = _myersDiff(aMid, bMid, MAX_MYERS_EDIT_DISTANCE);
  }
  if (!ops) return null;

  // Build HTML: prefix + diffed middle + suffix
  let leftHtml = '', rightHtml = '', inlineHtml = '';

  const prefixHtml = escapeHtml(a.slice(0, prefixLen).join(''));
  leftHtml += prefixHtml;
  rightHtml += prefixHtml;
  inlineHtml += prefixHtml;

  // Middle (diffed)
  for (const [op, s1, e1, s2, e2] of ops) {
    if (op === 'equal') {
      const equalHtml = escapeHtml(aMid.slice(s1, e1).join(''));
      leftHtml += equalHtml;
      rightHtml += equalHtml;
      inlineHtml += equalHtml;
    } else if (op === 'delete') {
      const deletedHtml = `<span class="word-del">${escapeHtml(aMid.slice(s1, e1).join(''))}</span>`;
      leftHtml += deletedHtml;
      inlineHtml += deletedHtml;
    } else if (op === 'insert') {
      const addedHtml = `<span class="word-add">${escapeHtml(bMid.slice(s2, e2).join(''))}</span>`;
      rightHtml += addedHtml;
      inlineHtml += addedHtml;
    }
  }

  const suffixHtml = escapeHtml(a.slice(n - suffixLen).join(''));
  leftHtml += suffixHtml;
  rightHtml += suffixHtml;
  inlineHtml += suffixHtml;

  return {leftHtml, rightHtml, inlineHtml};
}

function _renderCoarseDiff(oldText, newText) {
  const leftHtml = oldText
    ? `<span class="word-del word-diff-coarse">${escapeHtml(oldText)}</span>`
    : '';
  const rightHtml = newText
    ? `<span class="word-add word-diff-coarse">${escapeHtml(newText)}</span>`
    : '';
  return {
    leftHtml,
    rightHtml,
    inlineHtml: leftHtml + rightHtml,
  };
}

function _sequenceDiff(a, b) {
  if (a.length * b.length <= MAX_LCS_CELLS) return _lcsDiff(a, b);
  return _myersDiff(a, b, MAX_MYERS_EDIT_DISTANCE);
}

// Dynamic-programming LCS for small change regions.
function _lcsDiff(a, b) {
  const n = a.length, m = b.length;
  if (n === 0 && m === 0) return [];
  if (n === 0) return [['insert', 0, 0, 0, m]];
  if (m === 0) return [['delete', 0, n, 0, 0]];

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

  return _mergeDiffOps(ops);
}

// Myers finds sparse edits in long clauses without allocating an n*m matrix.
// The edit-distance limit bounds worst-case work for genuine rewrites.
function _myersDiff(a, b, editDistanceLimit) {
  const n = a.length;
  const m = b.length;
  if (n === 0) return m === 0 ? [] : [['insert', 0, 0, 0, m]];
  if (m === 0) return [['delete', 0, n, 0, 0]];

  const maxDistance = Math.min(n + m, editDistanceLimit);
  let frontier = new Map([[1, 0]]);
  const trace = [];

  for (let distance = 0; distance <= maxDistance; distance++) {
    const next = new Map();
    for (let diagonal = -distance; diagonal <= distance; diagonal += 2) {
      const left = frontier.get(diagonal - 1) ?? -Infinity;
      const right = frontier.get(diagonal + 1) ?? -Infinity;
      let x;
      if (diagonal === -distance || (diagonal !== distance && left < right)) {
        x = Math.max(0, right);
      } else {
        x = left + 1;
      }
      let y = x - diagonal;
      while (x < n && y < m && a[x] === b[y]) {
        x++;
        y++;
      }
      next.set(diagonal, x);
      if (x >= n && y >= m) {
        trace.push(next);
        return _backtrackMyers(trace, a, b);
      }
    }
    trace.push(next);
    frontier = next;
  }
  return null;
}

function _backtrackMyers(trace, a, b) {
  let x = a.length;
  let y = b.length;
  const ops = [];

  for (let distance = trace.length - 1; distance > 0; distance--) {
    const previous = trace[distance - 1];
    const diagonal = x - y;
    const left = previous.get(diagonal - 1) ?? -Infinity;
    const right = previous.get(diagonal + 1) ?? -Infinity;
    const previousDiagonal = (
      diagonal === -distance || (diagonal !== distance && left < right)
    ) ? diagonal + 1 : diagonal - 1;
    const previousX = previous.get(previousDiagonal) ?? 0;
    const previousY = previousX - previousDiagonal;

    while (x > previousX && y > previousY) {
      ops.push(['equal', x - 1, x, y - 1, y]);
      x--;
      y--;
    }
    if (x === previousX) {
      ops.push(['insert', previousX, previousX, previousY, previousY + 1]);
    } else {
      ops.push(['delete', previousX, previousX + 1, previousY, previousY]);
    }
    x = previousX;
    y = previousY;
  }

  while (x > 0 && y > 0) {
    ops.push(['equal', x - 1, x, y - 1, y]);
    x--;
    y--;
  }
  while (x > 0) {
    ops.push(['delete', x - 1, x, 0, 0]);
    x--;
  }
  while (y > 0) {
    ops.push(['insert', 0, 0, y - 1, y]);
    y--;
  }

  ops.reverse();
  return _mergeDiffOps(ops);
}

function _mergeDiffOps(ops) {
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
  let node = _allClauseNodes[flatIndex];
  if (!node) return null;

  if (node.status === 'unchanged' && !_showUnchanged) {
    if (state.diffData?.view === 'changes') {
      try {
        await ensureFullDiffData();
      } catch (error) {
        if (error.name !== 'AbortError') showToast(`Unable to load clause: ${error.message}`, 'error');
        return null;
      }
      node = _allClauseNodes[flatIndex];
    }
    _showUnchanged = true;
    const checkbox = $('uncToggle');
    if (checkbox) checkbox.checked = true;
    startClauseRendering(true);
  }

  const targetPosition = _renderPositionByFlatIndex.get(flatIndex);
  if (targetPosition === undefined) return null;
  if (_renderedClauseCount <= targetPosition) {
    // Build one fragment up to the requested clause. Repeated 36-item DOM
    // insertions make deep TOC jumps progressively slower on long specs.
    appendClauseBatch(targetPosition + 1);
    await new Promise(resolve => requestAnimationFrame(resolve));
    await new Promise(resolve => requestAnimationFrame(resolve));
  }

  return document.getElementById(`clause-${flatIndex}`);
}

window.scrollToClause = async function(clauseReference, options = {}) {
  let flatIndex;
  if (typeof clauseReference === 'number') {
    flatIndex = Number(clauseReference);
  } else {
    flatIndex = _allClauseNodes.findIndex(node => node.id === String(clauseReference));
  }

  const element = await ensureClauseRendered(flatIndex);
  if (!element) return;
  const content = $('content');
  const distance = Math.abs(
    element.getBoundingClientRect().top - content.getBoundingClientRect().top
  );
  element.scrollIntoView({
    behavior: distance > content.clientHeight * 2 ? 'auto' : 'smooth',
    block: 'start',
  });
  _activeTocIndex = flatIndex;
  document.querySelectorAll('.toc-item.active').forEach(item => item.classList.remove('active'));
  document.querySelector(`.toc-item[data-clause-index="${flatIndex}"]`)?.classList.add('active');
  if (options.updateURL !== false) {
    const node = _allClauseNodes[flatIndex];
    _updateURL(
      state.currentSpec,
      state.diffData?.old_version,
      state.diffData?.new_version,
      node?.id,
      true,
    );
  }
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
function _updateURL(spec, v1, v2, clause = '', replace = false, viewState = {}) {
  const params = new URLSearchParams();
  const filterQuery = viewState.filterQuery ?? $('tocSearchInput')?.value.trim() ?? '';
  const showUnchanged = viewState.showUnchanged ?? _showUnchanged;
  const diffLayout = viewState.diffLayout ?? _diffLayout;
  if (spec) params.set('spec', spec);
  if (v1) params.set('v1', v1);
  if (v2) params.set('v2', v2);
  if (clause) params.set('clause', clause);
  if (filterQuery) params.set('q', filterQuery);
  if (showUnchanged) params.set('unchanged', '1');
  if (diffLayout === 'inline') params.set('layout', 'inline');
  const qs = params.toString();
  const url = qs ? `${location.pathname}?${qs}` : location.pathname;
  if (`${location.pathname}${location.search}` === url) return;
  history[replace ? 'replaceState' : 'pushState'](null, '', url);
}

function updateCurrentComparisonURL(replace = true) {
  if (!state.diffData) return;
  const clause = new URLSearchParams(location.search).get('clause') || '';
  _updateURL(
    state.diffData.spec,
    state.diffData.old_version,
    state.diffData.new_version,
    clause,
    replace,
  );
}

function _resetWorkspace() {
  clearTimeout(_versionSwitchTimer);
  _versionSwitchTimer = null;
  _comparisonActive = false;
  _isComparing = false;
  _runGeneration += 1;
  _versionLoadGeneration += 1;
  _renderGeneration += 1;
  _diffAbortController?.abort();
  _diffEventSource?.close();
  _diffAbortController = null;
  _diffEventSource = null;
  _clauseObserver?.disconnect();
  _wordDiffObserver?.disconnect();
  _imageObserver?.disconnect();
  _tocSearchAbortController?.abort();
  _tocSearchAbortController = null;
  state.diffData = null;
  _showUnchanged = false;
  _diffLayout = 'split';
  _allClauseNodes = [];
  _renderNodes = [];
  _renderPositionByFlatIndex = new Map();
  _renderedClauseCount = 0;
  _tocRecords = [];
  _activeTocIndex = -1;
  _changedIndexes = [];
  _navIndex = -1;
  _fullDiffPromise = null;
  $('content').innerHTML = INITIAL_CONTENT_HTML;
  $('content').scrollTop = 0;
  $('statsBar').replaceChildren();
  $('statsBar').hidden = true;
  $('tocTree').innerHTML = INITIAL_TOC_HTML;
  $('tocSearch').hidden = true;
  updateTocSearchSummary();
  $('tocSearchInput').value = '';
  $('refreshBtn').hidden = true;
  $('clauseNav').classList.remove('visible');
  $('diffBtn').disabled = !isValidVersionPair();
  $('diffBtn').querySelector('span').textContent = 'Compare';
  setMobileToc(false);
}

function _resetForSelectionChange() {
  const hadComparisonURL = Boolean(location.search);
  _resetWorkspace();
  if (hadComparisonURL) history.pushState(null, '', location.pathname);
}

async function _restoreFromURL() {
  const params = new URLSearchParams(location.search);
  const spec = params.get('spec');
  const v1 = params.get('v1');
  const v2 = params.get('v2');
  const filterQuery = params.get('q') || '';
  const showUnchanged = params.get('unchanged') === '1';
  const diffLayout = params.get('layout') === 'inline' ? 'inline' : 'split';
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
  $('tocSearchInput').value = filterQuery;

  if (v1 && v2 && v1 !== v2) {
    window.runDiff(false, {showUnchanged, diffLayout});
  }
}

window.addEventListener('popstate', () => {
  const params = new URLSearchParams(location.search);
  const spec = params.get('spec');
  const v1 = params.get('v1');
  const v2 = params.get('v2');
  const filterQuery = params.get('q') || '';
  const showUnchanged = params.get('unchanged') === '1';
  const diffLayout = params.get('layout') === 'inline' ? 'inline' : 'split';
  _resetWorkspace();
  if (spec && v1 && v2) {
    $('specSelect').value = spec;
    state.currentSpec = spec;
    $('tocSearchInput').value = filterQuery;
    loadVersions().then(loaded => {
      const current = new URLSearchParams(location.search);
      if (!loaded || current.get('spec') !== spec || current.get('v1') !== v1 || current.get('v2') !== v2) {
        return;
      }
      $('v1Select').value = v1;
      $('v2Select').value = v2;
      window.runDiff(false, {showUnchanged, diffLayout});
    });
  }
});

// ===================== MAIN FLOW =====================
let _runGeneration = 0;
let _comparisonActive = false;
let _isComparing = false;
let _versionSwitchTimer = null;

window.runDiff = async function(refresh = false, options = {}) {
  clearTimeout(_versionSwitchTimer);
  _versionSwitchTimer = null;
  const spec = state.currentSpec;
  const v1 = $('v1Select').value;
  const v2 = $('v2Select').value;
  const currentParams = new URLSearchParams(location.search);
  const requestedClause = options.requestedClause ?? currentParams.get('clause') ?? '';
  const filterQuery = options.filterQuery ?? $('tocSearchInput').value.trim();
  const restoreUnchanged = options.showUnchanged ?? _showUnchanged;
  const restoreDiffLayout = options.diffLayout === 'inline' || (
    options.diffLayout === undefined && _diffLayout === 'inline'
  ) ? 'inline' : 'split';

  if (!v1 || !v2) {
    showToast('Please select both versions', 'error');
    return;
  }

  if (v1 === v2) {
    showToast('Please select two different versions', 'error');
    return;
  }

  if (compareVersions(v1, v2) >= 0) {
    showToast('Old version must be earlier than new version', 'error');
    return;
  }

  _comparisonActive = true;
  _isComparing = true;
  $('tocSearchInput').value = filterQuery;
  $('diffBtn').disabled = true;
  $('refreshBtn').hidden = true;
  $('diffBtn').querySelector('span').textContent = 'Loading…';
  const runGeneration = ++_runGeneration;
  prepareComparisonLoading();

  try {
    const diff = await fetchDiffWithProgress(spec, v1, v2, refresh);
    if (runGeneration !== _runGeneration) return;
    state.diffData = diff;
    _diffLayout = restoreDiffLayout;
    renderDiff(diff);
    $('refreshBtn').hidden = false;
    if (restoreUnchanged) {
      await setUnchangedVisibility(true, {scroll: false});
      if (runGeneration !== _runGeneration) return;
    }
    _updateURL(spec, v1, v2, requestedClause, false, {
      filterQuery,
      showUnchanged: _showUnchanged,
      diffLayout: _diffLayout,
    });
    if (requestedClause) {
      await window.scrollToClause(requestedClause, {updateURL: false});
    }
  } catch (err) {
    if (runGeneration !== _runGeneration || err.name === 'AbortError') return;
    $('content').innerHTML = `<div class="error-msg">Error: ${escapeHtml(err.message)}</div>`;
  } finally {
    if (runGeneration === _runGeneration) {
      _isComparing = false;
      $('diffBtn').disabled = !isValidVersionPair();
      $('diffBtn').querySelector('span').textContent = 'Compare';
    }
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
      _filterTimer = setTimeout(() => {
        window.filterToc();
        updateCurrentComparisonURL(true);
      }, 200);
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
    openLightbox(
      imageButton.dataset.imageSrc,
      imageButton.dataset.imageAlt,
      imageButton.dataset.imageOriginal,
    );
    return;
  }

  const expandButton = event.target.closest('[data-action="expand-clause"]');
  if (expandButton) {
    const body = expandButton.previousElementSibling;
    body.classList.toggle('collapsed');
    expandButton.textContent = body.classList.contains('collapsed') ? 'Show more' : 'Show less';
  }
});

$('specSelect').addEventListener('change', () => {
  _resetForSelectionChange();
  loadVersions();
});

function handleVersionSelectionChange() {
  clearTimeout(_versionSwitchTimer);
  _versionSwitchTimer = null;
  const validPair = isValidVersionPair();
  $('diffBtn').disabled = _isComparing || !validPair;
  if (!_comparisonActive || !validPair) return;

  const v1 = $('v1Select').value;
  const v2 = $('v2Select').value;
  // Let users change both selectors without eagerly loading the intermediate
  // pair; a single-selector change still switches the comparison promptly.
  _versionSwitchTimer = setTimeout(() => {
    if ($('v1Select').value !== v1 || $('v2Select').value !== v2) return;
    window.runDiff();
  }, 250);
}

$('v1Select').addEventListener('change', handleVersionSelectionChange);
$('v2Select').addEventListener('change', handleVersionSelectionChange);
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

const systemThemeQuery = window.matchMedia('(prefers-color-scheme: light)');
applyTheme(document.documentElement.dataset.theme, false);
$('themeToggle').addEventListener('click', () => {
  const nextTheme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  applyTheme(nextTheme, true);
});
systemThemeQuery.addEventListener('change', event => {
  try {
    if (!localStorage.getItem(THEME_STORAGE_KEY)) applyTheme(event.matches ? 'light' : 'dark');
  } catch (_) {}
});

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
