(() => {
  const panel = document.getElementById('operating-panel');
  if (!panel) return;
  const get = id => document.getElementById(id);
  const image = get('live-img');
  const message = get('operating-message');
  let latest = null, busy = false, objectUrl = null, mutationEpoch = 0, statusError = false;
  const names = {eco: '省エネ', tracking: '追跡準備'};
  const states = {active: '反映確認済み', pending: '切替・周期確認中', degraded: '保護停止', unknown: '不明'};
  function clearPreview() {
    image.removeAttribute('src');
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
  function render(data) {
    if (latest?.version !== data.version || latest?.boot_id !== data.boot_id) clearPreview();
    latest = data;
    const state = data.eco_auto_recovery_pending ? '省エネの自動復帰待ち' : data.eco_warning ? '省エネの周期揺れを確認中' : states[data.state] || '不明';
    get('operating-state').textContent = `要求：${names[data.requested_mode] || '不明'} ／ 実効：${names[data.effective_mode] || '未確認'} ／ ${state} ／ 認識 ${(data.recognition_hz || 0).toFixed(2)} Hz ／ モーター停止`;
    const resources = data.resources || {};
    const free = resources.sample?.free;
    get('operating-resources').textContent = `温度 ${data.vitals?.temperature_c ?? '—'}℃ ／ 空き容量 ${free == null ? '—' : (free / 1024 ** 3).toFixed(2) + ' GiB'} ／ 保存 ${data.writer?.saved ?? 0}枚 ／ 診断 ${data.diagnostic?.state || 'off'} ／ 保護理由 ${data.reason || resources.reason || 'なし'}`;
    panel.querySelectorAll('[data-mode]').forEach(button => {
      button.disabled = busy || !Number.isSafeInteger(data.version) || (button.dataset.mode === 'tracking' && (!data.connected || resources.paused || !data.vitals?.thermal_ok || data.retention_ok !== true));
    });
    get('operating-resume').disabled = busy || !resources.resume_ready || !data.vitals?.thermal_ok || data.retention_ok !== true;
    const age = data.last_result_age_seconds;
    get('camera-last-seen').textContent = age == null ? '認識結果を未確認' : `${age.toFixed(1)}秒前（RAM認識結果）`;
    get('camera-live-interval').textContent = data.effective_mode === 'tracking' ? '約0.19s' : data.effective_mode === 'eco' ? '約5s' : '未確認';
    const label = get('cam-status-text');
    if (label) label.textContent = data.connected ? states[data.state] || '不明' : '未接続';
    if (!data.connected || !data.acknowledged) clearPreview();
  }
  async function refresh() {
    const epoch = mutationEpoch;
    try {
      if (busy) return;
      const response = await fetch(panel.dataset.url, {cache: 'no-store', signal: AbortSignal.timeout(2500)});
      const data = await response.json();
      if (epoch !== mutationEpoch) return;
      render(data);
      if (statusError && response.ok) message.textContent = '';
      statusError = !response.ok;
      if (statusError) message.textContent = data.message || '状態を取得できません';
    } catch (_) {
      if (epoch !== mutationEpoch) return;
      render({state: 'unknown'});
      statusError = true;
      message.textContent = '認識サービスに接続できません';
    } finally {
      setTimeout(refresh, document.hidden ? 5000 : 1000);
    }
  }
  async function preview() {
    try {
      if (!document.hidden && latest?.connected && latest?.acknowledged) {
        const version = latest.version, boot = latest.boot_id;
        const response = await fetch(panel.dataset.url + '/frame', {cache: 'no-store', signal: AbortSignal.timeout(2000)});
        if (response.status !== 200) {
          clearPreview();
          get('live-image-state').textContent = '鮮度を満たす新しい画像を待っています';
        } else {
          const blob = await response.blob();
          if (latest?.version !== version || latest?.boot_id !== boot || !latest?.acknowledged) return;
          const next = URL.createObjectURL(blob);
          get('camera-frame-size').textContent = (blob.size / 1024).toFixed(1) + ' KB';
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          objectUrl = next;
          image.src = next;
        }
      }
    } catch (_) { clearPreview(); }
    finally { setTimeout(preview, 500); }
  }
  async function mutate(suffix, method, payload) {
    const token = get('operating-token').value.trim();
    if (!token) { message.textContent = '切替には管理者トークンを入力してください'; return; }
    if (busy) return;
    mutationEpoch += 1;  // Discard status responses issued before this operation.
    statusError = false;
    busy = true;
    render(latest || {state: 'unknown'});
    try {
      const response = await fetch(panel.dataset.url + suffix, {method, cache: 'no-store', signal: AbortSignal.timeout(2500),
        headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, body: JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || data.error || '操作を受け付けられません');
      render(data);
      message.textContent = '要求を受け付けました。ACKと新しい画像の周期を確認しています';
    } catch (error) { message.textContent = error.message; }
    finally { busy = false; render(latest || {state: 'unknown'}); }
  }
  panel.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () =>
    mutate('', 'PUT', {mode: button.dataset.mode, expected_version: latest?.version})));
  get('operating-resume').addEventListener('click', () => mutate('/resume', 'POST', {expected_version: latest?.version}));
  get('operating-diagnostic').addEventListener('click', () => mutate('/diagnostic', 'POST', {duration_seconds: 60}));
  get('operating-diagnostic-stop').addEventListener('click', () => mutate('/diagnostic', 'DELETE', {}));
  window.addEventListener('pagehide', () => { clearPreview(); get('operating-token').value = ''; });
  refresh();
  preview();
})();
