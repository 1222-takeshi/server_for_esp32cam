from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.conftest import MINIMAL_JPEG


@pytest.mark.parametrize('age,status', [(0, 'healthy'), (600, 'healthy'),
                                          (601, 'stale'), (1800, 'stale'),
                                          (1801, 'offline'), (-30, 'healthy')])
def test_connection_status_boundaries(app, client, age, status):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    app.extensions['metadata_store'].record_heartbeat('cam', seen_at=now - timedelta(seconds=age))
    with patch('script.routes.datetime') as clock:
        clock.now.return_value = now
        response = client.get('/api/cameras/cam/connection')
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == status
    assert data['seconds_since_contact'] == max(0, age)
    assert data['expected_interval_seconds'] == 300
    assert data['device_server_base_url'] is None


@pytest.mark.parametrize('origin,expected', [
    ('http://raspi-server.local:8080/', 'http://raspi-server.local:8080'),
    ('https://pi.example', 'https://pi.example'),
    ('http://user:secret@pi:8080', None),
    ('http://pi:8080/?token=secret', None),
    ('http://pi:8080/#secret', None),
    ('http://pi:8080/upload', None),
    ('http://pi:bad', None),
    ('http://pi:0', None),
    ('javascript:alert(1)', None),
    ('http://pi with spaces', None),
    (None, None),
])
def test_connection_origin_validation(app, client, origin, expected):
    app.config['DEVICE_SERVER_BASE_URL'] = origin
    app.extensions['metadata_store'].record_heartbeat('cam')
    data = client.get('/api/cameras/cam/connection').get_json()
    assert data['device_server_base_url'] == expected
    assert 'secret' not in str(data)


@pytest.mark.parametrize('camera', ['unknown', 'bad!id'])
def test_connection_missing_or_invalid_camera(client, camera):
    response = client.get(f'/api/cameras/{camera}/connection')
    assert response.status_code == 404
    assert response.get_json() == {'message': 'Camera not found'}


def test_detail_explains_unknown_cause_and_uses_lan_origin(app, client):
    app.config['DEVICE_SERVER_BASE_URL'] = 'http://raspi-server.local:8080'
    app.extensions['metadata_store'].record_heartbeat(
        'cam', seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    response = client.get('/cameras/cam', base_url='http://100.108.195.27:8080')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '接続診断' in html
    assert 'http://raspi-server.local:8080' in html
    assert '考えられる原因・確認項目（未確認）' in html
    assert '電源断やWi-Fi切断を確定する表示ではありません' in html
    assert 'PiのIP変更後もカメラが古いIP' in html
    assert 'camera_connection.js' in html
    assert 'id="connection-checks" hidden' not in html


def test_healthy_detail_hides_suspected_causes_and_preserves_snapshot(client):
    response = client.post('/upload', data=MINIMAL_JPEG, content_type='image/jpeg',
                           headers={'X-Device-Id': 'cam', 'X-Captured-At': '2026-01-01T00:00:00Z'})
    assert response.status_code == 200
    html = client.get('/cameras/cam').get_data(as_text=True)
    assert 'id="connection-checks" hidden' in html
    assert '未指定（管理者による設定が必要）' in html
    assert 'id="latest-img"' in html
    assert len(client.get('/api/cameras/cam/snapshots').get_json()['snapshots']) == 1


def test_connection_explains_received_frames_without_saved_snapshot(app, client):
    app.config['KEEP_ONLY_DETECTED_SNAPSHOTS'] = True
    app.extensions['metadata_store'].record_heartbeat('cam')
    metrics = app.extensions['upload_metrics']
    metrics.record_frame('cam', 1000)
    response = client.get('/api/cameras/cam/connection')
    data = response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'
    assert data['websocket_frames_received'] == 1
    assert data['last_websocket_frame_at'] is not None
    assert data['keep_only_detected_snapshots'] is True
    html = client.get('/cameras/cam').get_data(as_text=True)
    assert '検知対象がない画像は保存しない設定です' in html
    assert 'WebSocket受信フレーム数：1 件' in html
    assert 'No snapshots' in html
    assert 'id="live-img" src="/stream/cameras/cam"' in html


@pytest.mark.parametrize('scenario', ['healthy', 'offline', 'degraded', 'network', 'invalid', 'http'])
def test_diagnostic_ui_refresh_and_failure_states(scenario):
    import json
    from pathlib import Path
    import shutil
    import subprocess

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the browser-script unit test')
    source = Path('static/camera_connection.js').read_text()
    harness = r'''
const assert = require('node:assert/strict');
const elements = Object.fromEntries(['connection-diagnostics', 'connection-status',
  'connection-checks', 'connection-server-health', 'connection-frames']
  .map(id => [id, {textContent: '', hidden: false, dataset: {url: '/contact'}}]));
global.document = {getElementById: id => elements[id]};
let refresh;
global.setInterval = fn => {refresh = fn;};
global.fetch = async url => {
  if (scenario === 'network') throw new Error('unreachable');
  if (url === '/contact') return {ok: scenario !== 'http', json: async () => ({
    status: scenario === 'offline' ? 'offline' : 'healthy',
    seconds_since_contact: scenario === 'invalid' ? 'broken' : 10,
    websocket_frames_received: 3, last_websocket_frame_at: null})};
  return {ok: scenario !== 'degraded', json: async () => ({
    status: scenario === 'degraded' ? 'degraded' : 'ready'})};
};
(async () => {
  eval(source);
  await new Promise(resolve => setImmediate(resolve));
  const failed = ['network', 'invalid', 'http'].includes(scenario);
  assert.equal(elements['connection-checks'].hidden, !failed && scenario !== 'offline');
  assert.ok(elements['connection-status'].textContent.includes(failed ? '取得できません' : '10 秒'));
  assert.ok(elements['connection-server-health'].textContent.includes(
    scenario === 'network' ? '確認不能' : scenario === 'degraded' ? '異常' : '正常'));
  assert.ok(elements['connection-frames'].textContent.includes(failed ? '確認不能' : '3 件'));
  await refresh();
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run(
        [node, '-e', f'const scenario={json.dumps(scenario)};const source={json.dumps(source)};'
         + harness], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
