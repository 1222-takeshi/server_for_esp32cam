import json
import re
import shutil
import subprocess

import pytest

from tests.conftest import MINIMAL_JPEG


def _detail(app, client, discard):
    app.config['KEEP_ONLY_DETECTED_SNAPSHOTS'] = discard
    app.extensions['metadata_store'].record_heartbeat('cam')
    return client.get('/cameras/cam').get_data(as_text=True)


def test_discard_mode_shows_live_image_without_saved_snapshot(app, client):
    html = _detail(app, client, True)
    assert 'id="live-img" src="/stream/cameras/cam"' in html
    assert 'ライブ画像の受信待ち' in html
    assert 'ライブ画像を取得できません' in html
    assert 'id="latest-img"' not in html
    assert 'onclick="toggleStream()"' not in html
    assert '保存済みの画像' in html


def test_normal_mode_preserves_saved_snapshot(app, client):
    client.post('/upload', data=MINIMAL_JPEG, content_type='image/jpeg',
                headers={'X-Device-Id': 'cam', 'X-Captured-At': '2026-10-10T00:00:00Z'})
    html = _detail(app, client, False)
    assert 'id="latest-img" src="/files/' in html
    assert 'onclick="toggleStream()"' in html
    assert 'id="live-img"' not in html


@pytest.mark.parametrize('discard,streaming,foreign,expected', [
    (True, False, False, 0),
    (False, True, False, 1),
    (False, False, False, 1),
    (False, False, True, 0),
])
def test_upload_event_does_not_break_live_image(app, client, discard, streaming, foreign, expected):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser event regression checks')
    html = _detail(app, client, discard)
    script = next(s for s in re.findall(r'<script>(.*?)</script>', html, re.S)
                  if 'function onEvent' in s)
    harness = r'''
const vm = require('vm'), assert = require('assert');
const initial = STREAMING ? 'http://pi/stream/cameras/cam' : '/files/saved.jpg';
const elements = {
 'latest-img': {src:initial,dataset:{}}, 'stream-toggle-btn':{}, 'latest-ts':{textContent:'saved time'},
 'latest-filename':{textContent:'saved.jpg'},
 'snapshot-count':{textContent:'1'}, 'snap-list':{children:[],insertBefore(a){this.children.push(a)}},
 'camera-status-badge':{dataset:{status:'offline'},classList:{remove(){},add(){}}},
 'camera-status-text':{}, 'camera-last-seen':{}, 'camera-frame-size':{},
};
let handler;
class WebSocket {addEventListener(kind, fn) {if(kind==='message')handler=fn;}}
const context = {window:{CameraShared:{formatTimestamp:x=>x,formatFrameSize:x=>String(x)}},
 location:{protocol:'http:',host:'pi'}, WebSocket,setInterval(){},setTimeout(){},
 document:{getElementById:id=>elements[id]||null,querySelectorAll:()=>[],createElement:()=>({style:{}})}};
vm.runInNewContext(SCRIPT,context);
if (!DISCARD && !STREAMING) {
 context.toggleStream();
 assert.equal(elements['latest-img'].src, '/stream/cameras/cam');
 context.toggleStream();
 assert.equal(elements['latest-img'].src, initial);
}
handler({data:'invalid json'});
handler({data:JSON.stringify({type:'snapshot.created',camera_id:FOREIGN?'other':'cam',
 snapshot:{url:'/files/new.jpg',filename:'new.jpg',size:123,captured_at:'new time'}})});
assert.equal(elements['snap-list'].children.length, EXPECTED);
assert.equal(elements['snapshot-count'].textContent, EXPECTED ? '2':'1');
assert.equal(elements['latest-img'].src, DISCARD||STREAMING||FOREIGN ? initial:'/files/new.jpg');
assert.equal(elements['latest-filename'].textContent, DISCARD||STREAMING||FOREIGN ? 'saved.jpg':'new.jpg');
'''
    prefix = (f'const SCRIPT={json.dumps(script)}, DISCARD={json.dumps(discard)}, '
              f'STREAMING={json.dumps(streaming)}, FOREIGN={json.dumps(foreign)}, EXPECTED={expected};\n')
    result = subprocess.run([node, '-e', prefix + harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
