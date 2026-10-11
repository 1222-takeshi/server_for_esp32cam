// A snapshot.created event precedes inference and retention. Only the API
// confirms which images are still saved on disk.
(function () {
  const list = document.getElementById('snap-list');
  if (!list) return;
  const count = document.getElementById('snapshot-count');
  const state = document.getElementById('saved-snapshot-state');
  const keepOnlyDetected = list.dataset.keepOnlyDetected === 'true';
  const INTERVAL_MS = 10000;
  let pending = false;
  let latestSnapshot = null;
  let latestSignature = '';

  function fileUrl(template, filename) {
    return template.replace('__filename__', encodeURIComponent(filename));
  }

  function latestLabel(snapshot) {
    const formatted = window.CameraShared.formatTimestamp(snapshot.stored_at);
    if (!formatted) return '最終保存時刻: 不明';
    const ageMs = Date.now() - Date.parse(snapshot.stored_at);
    const age = Number.isFinite(ageMs) && ageMs >= 0
      ? ` (${Math.floor(ageMs / 60000)}分前)` : '';
    return `最終保存: ${formatted}${age}`;
  }

  function savedReason(snapshot) {
    if (snapshot.detections.length) return `検知 ${snapshot.detections.length}件`;
    const reasons = {
      pending: '推論待ち', processing: '推論中', failed: '推論失敗',
      skipped: '推論スキップ', succeeded: '検知なし',
    };
    return reasons[snapshot.inference_status] || '保存理由を確認できません';
  }

  function renderBoxes(snapshot) {
    const wrap = document.getElementById('latest-img-wrap');
    if (!wrap) return;
    wrap.querySelectorAll('.bbox').forEach(box => box.remove());
    snapshot.detections.forEach(detection => {
      const bbox = detection.bbox;
      if (!Array.isArray(bbox) || bbox.length !== 4 ||
          !bbox.every(value => typeof value === 'number' && Number.isFinite(value)) ||
          bbox.some(value => value < 0 || value > 1) ||
          bbox[2] <= bbox[0] || bbox[3] <= bbox[1]) return;
      const box = document.createElement('div');
      box.className = 'bbox';
      box.style.left = `${bbox[0] * 100}%`;
      box.style.top = `${bbox[1] * 100}%`;
      box.style.width = `${(bbox[2] - bbox[0]) * 100}%`;
      box.style.height = `${(bbox[3] - bbox[1]) * 100}%`;
      const label = document.createElement('div');
      label.className = 'bbox-label';
      const score = Number.isFinite(detection.score) ? `${Math.round(detection.score * 100)}%` : '';
      label.textContent = `${detection.label || 'object'} ${score}`;
      box.appendChild(label);
      wrap.appendChild(box);
    });
  }

  function showLatest(snapshot) {
    const img = document.getElementById('latest-img');
    if (!img) return;
    latestSnapshot = snapshot;
    const url = fileUrl(list.dataset.fileUrl, snapshot.filename);
    const signature = JSON.stringify([
      snapshot.filename, snapshot.inference_status, snapshot.inference_error, snapshot.detections,
    ]);
    const changed = latestSignature !== signature;
    latestSignature = signature;
    img.dataset.savedFilename = snapshot.filename;
    img.dataset.snapshotSrc = url;
    const streaming = img.src.includes('/stream/');
    document.getElementById('latest-img-wrap').style.display = 'inline-block';
    document.getElementById('latest-meta').style.display = 'flex';
    document.getElementById('saved-image-empty').style.display = 'none';
    const name = document.getElementById('latest-filename');
    const timestamp = document.getElementById('latest-ts');
    const error = document.getElementById('latest-inference-error');
    const badge = document.getElementById('latest-badge');
    const status = document.getElementById('latest-status');
    if (name) name.textContent = snapshot.filename;
    if (timestamp) timestamp.textContent = window.CameraShared.formatTimestamp(
      snapshot.captured_at || snapshot.stored_at
    ) || '時刻不明';
    if (error) error.textContent = snapshot.inference_error || '';
    if (badge) {
      const known = ['pending', 'processing', 'succeeded', 'failed', 'skipped'];
      badge.className = `badge ${known.includes(snapshot.inference_status) ? snapshot.inference_status : ''}`;
      badge.hidden = streaming;
      badge.style.display = streaming ? 'none' : 'inline-flex';
    }
    if (status) status.textContent = snapshot.inference_status || 'unknown';
    if (!streaming && changed) {
      renderBoxes(snapshot);
      img.src = url;
      img.alt = snapshot.filename;
    }
  }

  function showEmpty() {
    latestSnapshot = null;
    latestSignature = '';
    const img = document.getElementById('latest-img');
    if (!img) return;
    document.getElementById('latest-meta').style.display = 'none';
    document.getElementById('latest-badge').hidden = true;
    document.getElementById('latest-badge').style.display = 'none';
    img.dataset.snapshotSrc = '';
    if (img.src.includes('/stream/')) return;
    img.removeAttribute('src');
    img.dataset.savedFilename = '';
    document.getElementById('latest-img-wrap').style.display = 'none';
    document.getElementById('saved-image-empty').style.display = '';
    document.getElementById('latest-img-wrap').querySelectorAll('.bbox').forEach(box => box.remove());
  }

  window.SavedSnapshotView = {
    restore() {
      if (!latestSnapshot) { showEmpty(); return; }
      showLatest(latestSnapshot);
      renderBoxes(latestSnapshot);
    },
  };

  function renderRow(snapshot) {
    const a = document.createElement('a');
    a.href = fileUrl(list.dataset.fileUrl, snapshot.filename);
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.style.cssText = 'display:flex;gap:10px;align-items:center;padding:8px;border-radius:6px;text-decoration:none;';
    const thumb = document.createElement('div');
    thumb.style.cssText = 'width:56px;height:42px;border-radius:4px;overflow:hidden;background:#12131a;flex-shrink:0;';
    const img = document.createElement('img');
    img.src = fileUrl(list.dataset.thumbnailUrl, snapshot.filename);
    img.alt = '';
    img.style.cssText = 'width:100%;height:100%;object-fit:cover;';
    thumb.appendChild(img);
    const info = document.createElement('div');
    info.style.cssText = 'flex:1;min-width:0;';
    const name = document.createElement('div');
    name.style.cssText = 'font-size:11px;color:var(--text-1);font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    name.textContent = snapshot.filename;
    const meta = document.createElement('div');
    meta.style.cssText = 'font-size:11px;color:var(--text-4);margin-top:2px;';
    meta.textContent = window.CameraShared.formatTimestamp(snapshot.stored_at) || '時刻不明';
    if (snapshot.detections.length) {
      const detected = document.createElement('span');
      detected.style.color = 'var(--green)';
      detected.textContent = ` · ${snapshot.detections.length} det`;
      meta.appendChild(detected);
    }
    info.append(name, meta);
    a.append(thumb, info);
    return a;
  }

  function render(body) {
    if (!body || !Number.isSafeInteger(body.total) || body.total < 0 ||
        !Array.isArray(body.snapshots) || body.snapshots.length > 20 ||
        body.snapshots.some(s => !s || typeof s.filename !== 'string' ||
          !s.filename || typeof s.stored_at !== 'string' || !Array.isArray(s.detections) ||
          s.detections.some(d => !d || typeof d !== 'object'))) {
      throw new Error('Invalid snapshots response');
    }
    const rows = document.createDocumentFragment();
    body.snapshots.forEach(snapshot => rows.appendChild(renderRow(snapshot)));
    if (!body.snapshots.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = '保存済み画像はありません';
      rows.appendChild(empty);
    }
    list.replaceChildren(rows);
    if (count) count.textContent = String(body.total);
    if (state) {
      const last = body.snapshots[0];
      state.textContent = last ? `${latestLabel(last)} · ${savedReason(last)}` : '最終保存: なし';
      if (keepOnlyDetected) state.textContent += ' · 検知対象がない画像は保存しません';
    }
    if (body.snapshots.length) showLatest(body.snapshots[0]);
    else showEmpty();
  }

  async function refresh() {
    if (pending) return;
    if (document.hidden) { setTimeout(refresh, INTERVAL_MS); return; }
    pending = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(list.dataset.apiUrl, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('Snapshots request failed');
      render(await response.json());
    } catch {
      if (state) state.textContent = '保存済み画像を確認できません（表示内容は前回確認時点）';
    } finally {
      clearTimeout(timeout);
      pending = false;
      setTimeout(refresh, INTERVAL_MS);
    }
  }

  refresh();
})();
