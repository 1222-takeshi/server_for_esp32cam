(() => {
  const panel = document.getElementById('connection-diagnostics');
  if (!panel) return;
  const status = document.getElementById('connection-status');
  const checks = document.getElementById('connection-checks');
  const health = document.getElementById('connection-server-health');
  const frames = document.getElementById('connection-frames');
  let pending = false;
  async function refresh() {
    if (pending) return;
    pending = true;
    try {
      await Promise.all([
        (async () => {
          try {
            const response = await fetch(panel.dataset.url, {cache: 'no-store', signal: AbortSignal.timeout(8000)});
            if (!response.ok) throw new Error('contact unavailable');
            const data = await response.json();
            if (!['healthy', 'stale', 'offline'].includes(data.status) ||
                !Number.isFinite(data.seconds_since_contact) || data.seconds_since_contact < 0) {
              throw new Error('invalid contact data');
            }
            status.textContent = `サーバーへの最終通信から ${data.seconds_since_contact} 秒（${data.status}）`;
            checks.hidden = data.status === 'healthy';
            frames.textContent = `WebSocket受信フレーム数：${data.websocket_frames_received} 件（再起動等でリセット）。最終受信：${data.last_websocket_frame_at || '記録なし'}`;
          } catch (_) {
            status.textContent = '通信情報を取得できません。表示済み画像が最新とは限りません。';
            checks.hidden = false;
            frames.textContent = '画像の受信状況は確認不能です。';
          }
        })(),
        (async () => {
          try {
            const response = await fetch('/healthz/ready', {cache: 'no-store', signal: AbortSignal.timeout(8000)});
            const data = await response.json();
            health.textContent = response.ok && data.status === 'ready' ? '正常（ready）' : '異常（degraded）';
          } catch (_) {
            health.textContent = '到達できない、または確認不能';
          }
        })(),
      ]);
    } finally {
      pending = false;
    }
  }
  refresh();
  setInterval(refresh, 30000);
})();
