// ===================== STATE =====================
const state = {
  versions: [],
  diffData: null,
  currentSpec: '23.501',
  expandedClauses: new Set(),
};

// ===================== UTILITIES =====================
const $ = id => document.getElementById(id);
const escapeHtml = str => {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
};

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

function fetchDiffWithProgress(spec, v1, v2, refresh) {
  return new Promise((resolve, reject) => {
    const steps = [`Parsing ${v1}`, `Parsing ${v2}`, 'Computing diff'];
    let stepIndex = 0;
    const startTime = Date.now();
    const MIN_DISPLAY_MS = 600;
    $('content').innerHTML = renderProgress(steps, 0);

    const url = `/api/diff-stream?spec=${spec}&v1=${v1}&v2=${v2}` + (refresh ? '&refresh=1' : '');
    const es = new EventSource(url);
    let pendingDone = null;

    es.addEventListener('progress', (e) => {
      if (stepIndex < steps.length - 1) {
        stepIndex++;
        $('content').innerHTML = renderProgress(steps, stepIndex);
      }
    });

    es.addEventListener('done', (e) => {
      es.close();
      try {
        pendingDone = JSON.parse(e.data);
      } catch (err) {
        reject(new Error('Invalid diff response'));
        return;
      }
      const elapsed = Date.now() - startTime;
      if (elapsed < MIN_DISPLAY_MS) {
        setTimeout(() => resolve(pendingDone), MIN_DISPLAY_MS - elapsed);
      } else {
        resolve(pendingDone);
      }
    });

    es.addEventListener('error', (e) => {
      es.close();
      const msg = e.data ? JSON.parse(e.data) : 'Stream error';
      reject(new Error(msg));
    });

    es.onerror = () => {
      es.close();
      reject(new Error('Connection lost'));
    };
  });
}

// ===================== LIGHTBOX =====================
function openLightbox(src, alt) {
  const lb = document.getElementById('lightbox');
  const img = document.getElementById('lightboxImg');
  img.src = src;
  img.alt = alt || '';
  lb.classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeLightbox() {
  const lb = document.getElementById('lightbox');
  lb.classList.remove('open');
  document.body.style.overflow = '';
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeLightbox();
});

// ===================== IMAGE THUMBNAILS =====================
function renderImageThumbnails(images, spec, version) {
  if (!images || images.length === 0) return '';
  let html = '<div class="clause-images">';
  for (const img of images) {
    const src = `/api/image/${spec}/${version}/${img.src}`;
    const alt = img.alt || '';
    html += `<div class="clause-image" onclick="event.stopPropagation();openLightbox('${src}','${escapeHtml(alt)}')">
      <img src="${src}" alt="${escapeHtml(alt)}" loading="lazy">
    </div>`;
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
      state.currentSpec = specs[0].id;
      $('specSelect').value = specs[0].id;
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
  if (!spec) { alert('Please enter a spec number'); return; }

  const btn = $('downloadBtn');
  const prog = $('downloadProgress');
  btn.disabled = true;
  btn.textContent = 'Starting...';
  prog.style.display = '';
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

    // Poll until complete
    while (true) {
      await new Promise(r => setTimeout(r, 1000));
      const sr = await fetch(`/api/download-status?spec=${spec}`);
      const st = await sr.json();
           if (st.status === 'listing')      prog.textContent = `[${elapsed()}] Listing releases...`;
      else if (st.status === 'downloading')  prog.textContent = `[${elapsed()}] Downloading ${st.done}/${st.total} releases...`;
      else if (st.status === 'completed')    { prog.textContent = `[${elapsed()}] Download complete!`; break; }
      else if (st.status === 'error')        throw new Error(st.error || 'Download failed');
      /* else 'not_found' — keep polling */
    }

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
        while (true) {
          await new Promise(r => setTimeout(r, 1500));
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
      }
    } catch (_) {}

    setTimeout(() => { prog.style.display = 'none'; }, 3000);
  } catch (err) {
    prog.textContent = `[${elapsed()}] Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Download';
  }
}

// ===================== TOC RENDER =====================
function renderToc(clauses) {
  const tree = $('tocTree');
  if (!clauses || clauses.length === 0) {
    tree.innerHTML = '<div class="toc-item" style="color:var(--text-secondary);padding:16px;">No clauses</div>';
    return;
  }

  let html = '';
  const flat = flattenClauseTree(clauses);

  for (const node of flat) {
    const indent = node._depth;
    const status = node.status || 'unchanged';
    const statusDot = status !== 'unchanged'
      ? `<span class="status-dot ${status}"></span>`
      : '<span class="status-dot" style="background:transparent"></span>';
    const id = node.id || '';
    const bodyText = ((node.body || '') + ' ' + (node.old_body || '') + ' ' + (node.new_body || '')).toLowerCase();

    html += `<div class="toc-item" style="--indent:${indent}" data-id="${escapeHtml(id)}" data-body="${escapeHtml(bodyText)}"
      onclick="scrollToClause('${escapeHtml(id)}')">
      ${statusDot}
      <span class="toc-id">${escapeHtml(id)}</span>
      <span class="toc-title">${escapeHtml(node.title || '')}</span>
    </div>`;
  }

  tree.innerHTML = html;
}

window.filterToc = function() {
  const input = $('tocSearchInput');
  const raw = input.value.trim().toLowerCase();
  const keywords = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
  const items = document.querySelectorAll('.toc-item');

  if (keywords.length === 0) {
    items.forEach(el => el.style.display = '');
    return;
  }

  items.forEach(el => {
    const id = (el.dataset.id || '').toLowerCase();
    const title = (el.querySelector('.toc-title')?.textContent || '').toLowerCase();
    const body = el.dataset.body || '';
    const match = keywords.some(kw => id.includes(kw) || title.includes(kw) || body.includes(kw));
    el.style.display = match ? '' : 'none';
  });
};

// ===================== DIFF RENDER =====================
let _showUnchanged = false;

function renderDiff(diffData) {
  const container = $('content');
  const stats = diffData.stats;

  const total = stats.added + stats.deleted + stats.modified + stats.unchanged;
  const toggleId = 'uncToggle';
  let toggleHtml = '';
  if (stats.unchanged > 0) {
    toggleHtml = `<label style="margin-left:auto;font-size:12px;display:flex;align-items:center;gap:4px;cursor:pointer;color:var(--text-secondary)">
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
  $('statsBar').style.display = 'flex';

  const uncCb = $(toggleId);
  if (uncCb) {
    uncCb.addEventListener('change', () => {
      _showUnchanged = uncCb.checked;
      document.querySelectorAll('.clause-diff.unchanged').forEach(el => {
        el.style.display = _showUnchanged ? '' : 'none';
      });
    });
  }

  // Render TOC
  renderToc(diffData.clauses);
  $('tocToggle').innerHTML = 'Table of Contents <span>&#9660;</span>';
  $('tocSearch').style.display = '';
  $('tocSearchInput').value = '';
  document.querySelectorAll('.toc-children').forEach(e => e.classList.add('open'));

  // First pass: render WITHOUT word-level diffs (fast)
  const flat = flattenClauseTree(diffData.clauses);
  let html = '';

  // Header
  html += `<div class="diff-header">
    <h2>${escapeHtml(diffData.title || '')}</h2>
    <div class="subtitle">
      <a class="spec-link" href="https://portal.3gpp.org/desktopmodules/Specifications/SpecificationDetails.aspx?specificationId=${diffData.spec.replace('.','')}" target="_blank">
        TS ${diffData.spec}
      </a>
      &mdash; Comparing <strong>v${diffData.old_version}</strong> (Rel-${diffData.old_release})
      vs <strong>v${diffData.new_version}</strong> (Rel-${diffData.new_release})
    </div>
  </div>`;

  const spec = diffData.spec;
  const oldVer = diffData.old_version;
  const newVer = diffData.new_version;
  for (const node of flat) {
    html += clauseDiffHtml(node, spec, oldVer, newVer, true /* skipWordDiff */);
  }

  container.innerHTML = html;

  // Apply unchanged visibility
  if (!_showUnchanged) {
    document.querySelectorAll('.clause-diff.unchanged').forEach(el => {
      el.style.display = 'none';
    });
  }

  // Second pass: compute word-level diffs progressively (does not block render)
  const modifiedNodes = flat.filter(n => n.status === 'modified');
  let wordIdx = 0;
  function processWordBatch() {
    const end = Math.min(wordIdx + 5, modifiedNodes.length);
    for (; wordIdx < end; wordIdx++) {
      const node = modifiedNodes[wordIdx];
      const el = document.getElementById('clause-' + (node.id || '').replace(/\./g, '-'));
      if (!el) continue;
      const cells = el.querySelectorAll('.diff-word-content');
      if (cells.length < 2) continue;
      const { leftHtml, rightHtml } = renderWordDiffHtml(node.old_body || '', node.new_body || '');
      cells[0].innerHTML = leftHtml || escapeHtml(node.old_body || '');
      cells[1].innerHTML = rightHtml || escapeHtml(node.new_body || '');
    }
    if (wordIdx < modifiedNodes.length) {
      requestAnimationFrame(processWordBatch);
    }
  }
  if (modifiedNodes.length > 0) {
    requestAnimationFrame(processWordBatch);
  }
}

function clauseDiffHtml(node, spec, oldVersion, newVersion, skipWordDiff) {
  const status = node.status || 'unchanged';
  const id = node.id || '';
  const title = node.title || '';
  const clauseId = 'clause-' + id.replace(/\./g, '-');
  const hidden = status === 'unchanged' && !_showUnchanged ? ' style="display:none"' : '';

  let bodyHtml = '';

  if (status === 'unchanged') {
    const imgs = renderImageThumbnails(node.images, spec, newVersion);
    const body = node.body || '';
    const collapsed = body.length > 300 ? ' collapsed' : '';
    bodyHtml = imgs + `<div class="clause-body${collapsed}">${escapeHtml(body || '(no content)')}</div>`;
    if (body.length > 300) {
      bodyHtml += `<button class="expand-btn" onclick="this.previousElementSibling.classList.toggle('collapsed');this.textContent=this.previousElementSibling.classList.contains('collapsed')?'Show more':'Show less';">Show more</button>`;
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

  return `<div class="clause-diff ${status}" id="${clauseId}" data-clause-id="${escapeHtml(id)}"${hidden}>
    <div class="clause-diff-header">
      <span class="clause-id">${escapeHtml(id)}</span>
      <span>${escapeHtml(title)}</span>
      <span class="status-badge ${status}">${status}</span>
    </div>
    ${bodyHtml}
  </div>`;
}

// ===================== WORD-LEVEL DIFF =====================
function renderWordDiffHtml(oldText, newText) {
  const tokenize = (text) => text.match(/\w+|[^\w\s]|\s+/g) || [];
  const a = tokenize(oldText);
  const b = tokenize(newText);
  const n = a.length, m = b.length;

  const dp = Array.from({length: n + 1}, () => new Array(m + 1).fill(0));
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

  let leftHtml = '', rightHtml = '';
  for (const [op, oldStart, oldEnd, newStart, newEnd] of merged) {
    if (op === 'equal') {
      for (let k = oldStart; k < oldEnd; k++) {
        const t = escapeHtml(a[k]);
        leftHtml += t;
        rightHtml += t;
      }
    } else if (op === 'delete') {
      for (let k = oldStart; k < oldEnd; k++) {
        leftHtml += `<span class="word-del">${escapeHtml(a[k])}</span>`;
      }
    } else if (op === 'insert') {
      for (let k = newStart; k < newEnd; k++) {
        rightHtml += `<span class="word-add">${escapeHtml(b[k])}</span>`;
      }
    }
  }

  return { leftHtml, rightHtml };
}

// ===================== SCROLL TO CLAUSE =====================
window.scrollToClause = function(clauseId) {
  const el = document.getElementById('clause-' + clauseId.replace(/\./g, '-'));
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    document.querySelectorAll('.toc-item').forEach(e => e.classList.remove('active'));
    document.querySelector(`.toc-item[data-id="${CSS.escape(clauseId)}"]`)?.classList.add('active');
  }
};

// ===================== MAIN FLOW =====================
window.runDiff = async function(refresh) {
  const v1 = $('v1Select').value;
  const v2 = $('v2Select').value;

  if (!v1 || !v2) {
    alert('Please select both versions');
    return;
  }

  if (v1 === v2) {
    alert('Please select two different versions');
    return;
  }

  const p1 = v1.split('.').map(Number);
  const p2 = v2.split('.').map(Number);
  for (let i = 0; i < Math.max(p1.length, p2.length); i++) {
    const a = p1[i] || 0, b = p2[i] || 0;
    if (a > b) {
      alert('Old version must be earlier than new version');
      return;
    }
    if (a < b) break;
  }

  $('diffBtn').disabled = true;
  $('diffBtn').textContent = 'Loading...';
  $('refreshBtn').style.display = 'none';

  try {
    const diff = await fetchDiffWithProgress(state.currentSpec, v1, v2, refresh);
    state.diffData = diff;
    renderDiff(diff);
    $('refreshBtn').style.display = '';
  } catch (err) {
    $('content').innerHTML = `<div class="error-msg">Error: ${escapeHtml(err.message)}</div>`;
  } finally {
    $('diffBtn').disabled = false;
    $('diffBtn').textContent = 'Compare';
  }
};

window.openLightbox = openLightbox;
window.closeLightbox = closeLightbox;

// ===================== EVENT BINDING =====================
$('specSelect').addEventListener('change', loadVersions);
$('diffBtn').addEventListener('click', () => window.runDiff());
$('downloadBtn').addEventListener('click', downloadSpec);
$('specInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') downloadSpec();
});

// TOC toggle
$('tocToggle').addEventListener('click', () => {
  const tree = $('tocTree');
  const search = $('tocSearch');
  const isHidden = tree.style.display === 'none';
  tree.style.display = isHidden ? 'block' : 'none';
  search.style.display = isHidden ? '' : 'none';
  $('tocToggle').innerHTML = isHidden ? 'Table of Contents <span>&#9660;</span>' : 'Table of Contents <span>&#9654;</span>';
});

// Keyboard shortcut
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.target.tagName === 'SELECT' || e.target.tagName === 'BUTTON')) {
    if (e.target === $('diffBtn') || e.target.id === 'v1Select' || e.target.id === 'v2Select') {
      window.runDiff();
    }
  }
});

// ===================== INIT =====================
loadSpecs();
