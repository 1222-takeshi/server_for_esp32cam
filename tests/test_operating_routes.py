import io
import tempfile
from pathlib import Path
from threading import Thread

import pytest
from werkzeug.test import EnvironBuilder

from conftest import MINIMAL_JPEG
from script.operating_rpc import ControlServer
from script.operating_runtime import OperatingRuntime

BASE = '/api/cameras/cam/operating'
AUTH = {'Authorization': 'Bearer admin-private'}


@pytest.fixture
def native(app, tmp_path):
    with tempfile.TemporaryDirectory(prefix='ec-ui-', dir='/tmp') as directory:
        socket = Path(directory) / 'ipc/control.sock'
        app.config.update(OPERATING_SOCKET=str(socket), OPERATING_CAMERA_ID='cam', ADMIN_TOKEN='admin-private')
        app.extensions['metadata_store'].ensure_camera('cam')
        runtime = OperatingRuntime(app, object(), 'cam', 'device-private', tmp_path / 'diag',
            clock=lambda: 100, sampler=lambda *_: {'free': 5*1024**3, 'total': 10*1024**3, 'db': 0, 'wal': 0, 'write_bytes': 0},
            hardware=lambda: (45, 0))
        runtime.maintain()
        runtime.sample()
        runtime.mode.hello(7, 0)
        runtime.mode.acknowledge(7, runtime.mode.version, 'eco', 1)
        runtime.latest = (7, runtime.mode.version, 1, MINIMAL_JPEG, 100)
        with ControlServer(socket, runtime.dispatch) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            yield runtime
            server.shutdown()
            thread.join(timeout=2)
        runtime.close()


def test_status_mode_ack_and_ram_preview_without_disk_writes(native, client):
    before = client.get(BASE)
    assert before.status_code == 200 and before.headers['Cache-Control'] == 'no-store'
    assert before.json['state'] == 'pending' and before.json['effective_mode'] is None
    image = client.get(BASE + '/frame')
    assert image.data == MINIMAL_JPEG and image.mimetype == 'image/jpeg'
    assert image.headers['Cache-Control'] == 'no-store'
    changed = client.put(BASE, headers=AUTH, json={'mode': 'tracking', 'expected_version': before.json['version']})
    assert changed.status_code == 200 and changed.json['requested_mode'] == 'tracking'
    assert changed.json['state'] == 'pending' and changed.json['actuation_state'] == 'hold'
    assert client.get(BASE + '/frame').status_code == 204
    assert client.put(BASE, headers=AUTH, json={'mode': 'eco', 'expected_version': before.json['version']}).status_code == 409
    assert native.app.extensions['metadata_store'].total_snapshot_count() == 0


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer device-private'}, {'X-API-Key': 'admin-private'}])
def test_mutation_requires_admin_not_device_or_query_token(native, client, headers):
    version = native.mode.version
    response = client.put(BASE + '?token=admin-private', headers=headers, json={'mode': 'tracking', 'expected_version': version})
    assert response.status_code == 401 and response.headers['WWW-Authenticate'].startswith('Bearer')
    assert native.mode.version == version


def test_unconfigured_admin_and_legacy_camera_refused(native, app, client):
    app.config['ADMIN_TOKEN'] = ''
    assert client.put(BASE, json={}).status_code == 503
    assert client.put('/api/cameras/legacy/operating', headers=AUTH, json={}).status_code == 404


@pytest.mark.parametrize('payload', [[], {}, {'op': 'status'}, {'camera_id': 'other'}, {'mode': [], 'expected_version': 1},
                                     {'mode': 'tracking', 'expected_version': True}])
def test_invalid_or_injected_fields_rejected(native, client, payload):
    response = client.put(BASE, headers=AUTH, json=payload)
    assert response.status_code == 400


def test_body_boundary_chunked_sentinel_and_malformed(native, client):
    import json
    body = json.dumps({'mode': 'eco', 'expected_version': native.mode.version}).encode()
    assert client.put(BASE, headers=AUTH, data=body.ljust(1024), content_type='application/json').status_code == 200
    for length in (1025, 1026):
        env = EnvironBuilder(path=BASE, method='PUT', headers=AUTH, content_type='application/json').get_environ()
        env.pop('CONTENT_LENGTH', None)
        env['wsgi.input'] = io.BytesIO(body.ljust(length))
        env['wsgi.input_terminated'] = True
        assert client.open(env).status_code == 413
    assert client.put(BASE, headers=AUTH, data=b'{', content_type='application/json').status_code == 400


def test_resume_and_diagnostic_are_explicit_bounded_operations(native, client):
    assert client.post(BASE+'/resume', headers=AUTH, json={'expected_version': 1}).status_code == 409
    assert client.post(BASE+'/diagnostic', headers=AUTH, json={'duration_seconds': 601}).status_code == 400
    assert client.post(BASE+'/diagnostic', headers=AUTH, json={'duration_seconds': 1}).json['diagnostic']['state'] == 'running'
    assert client.delete(BASE+'/diagnostic', headers=AUTH, json={}).json['diagnostic']['state'] == 'closed'


def test_unavailable_worker_clears_effective_state_and_preview(native, app, client):
    app.config['OPERATING_SOCKET'] += '.missing'
    result = client.get(BASE)
    assert result.status_code == 503 and result.json['state'] == 'unknown'
    assert result.json['effective_mode'] is None and result.json['actuation_state'] == 'hold'
    assert client.get(BASE+'/frame').status_code == 503
    assert client.put(BASE, headers=AUTH, json={'mode': 'eco', 'expected_version': 1}).status_code == 503


def test_connection_and_detail_use_ram_instead_of_archive_heartbeat(native, client):
    native.mode.frames.append((100, 100000000, 1))
    response = client.get('/api/cameras/cam/connection')
    assert response.json['seconds_since_contact'] == 0 and response.json['status'] == 'healthy'
    assert response.json['operating']['connected'] is True
    for keep in (True, False):
        native.app.config['KEEP_ONLY_DETECTED_SNAPSHOTS'] = keep
        html = client.get('/cameras/cam').get_data(as_text=True)
        assert 'operating-panel' in html and 'operating.js' in html
        assert '管理者トークン' in html and 'モーター停止' in html
        assert 'src="/stream/' not in html and 'id="stream-toggle-btn"' not in html
        assert 'admin-private' not in html and 'device-private' not in html


def test_mismatched_native_camera_is_unavailable_not_a_jpeg(native, app, client):
    app.config['OPERATING_CAMERA_ID'] = 'other'
    path = BASE.replace('/cam/', '/other/')
    assert client.get(path).status_code == 503
    assert client.get(path+'/frame').status_code == 503


def test_ui_fences_delayed_preview_and_keeps_token_out_of_storage():
    import json
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for UI behavior test')
    source = Path('static/operating.js').read_text()
    harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
 const elements={},timers=[],requests=[],revoked=[];
 const element=id=>elements[id]||= {dataset:{},textContent:'',value:'',removeAttribute(){this.src=''},addEventListener(e,cb){this[e]=cb}};
 const buttons=[{dataset:{mode:'eco'},addEventListener(e,cb){this[e]=cb}},{dataset:{mode:'tracking'},addEventListener(e,cb){this[e]=cb}}];
 const panel=element('operating-panel');panel.dataset.url='/api/cameras/cam/operating';panel.querySelectorAll=()=>buttons;
 const initial={state:'pending',version:1,boot_id:7,connected:true,acknowledged:true,requested_mode:'eco',resources:{},vitals:{thermal_ok:true},retention_ok:true};
 let resolveFrame,resolveStatus,getCount=0;
 const response=data=>({ok:true,json:async()=>data});
 const fetch=async(url,options)=>{
  requests.push({url,options});
  if(url.endsWith('/frame'))return {status:200,blob:()=>new Promise(resolve=>resolveFrame=resolve)};
  if(options.method)return response({...initial,version:2,acknowledged:false,requested_mode:'tracking'});
  if(++getCount===2)return new Promise(resolve=>resolveStatus=resolve);
  return response(initial);
 };
 const context={document:{getElementById:element,hidden:false},window:{addEventListener(){}},fetch,
  URL:{createObjectURL(){throw Error('old preview was published')},revokeObjectURL(x){revoked.push(x)}},
  AbortSignal:{timeout:()=>null},setTimeout:(fn,delay)=>{timers.push({fn,delay})}};
 vm.runInNewContext(SCRIPT,context);
 const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};await flush();
 assert.equal(buttons[1].disabled,false);
 const preview=timers.find(t=>t.delay===500);assert.ok(preview);preview.fn();await flush();
 assert.equal(typeof resolveFrame,'function');
 const oldStatus=timers.find(t=>t.delay===1000);oldStatus.fn();await flush();
 assert.equal(typeof resolveStatus,'function');
 element('operating-token').value='admin-private';buttons[1].click();await flush();
 assert.equal(requests.at(-1).options.headers.Authorization,'Bearer admin-private');
 const body=JSON.parse(requests.at(-1).options.body);assert.equal(body.expected_version,1);assert.equal(body.mode,'tracking');
 assert.ok(element('operating-state').textContent.includes('切替・周期確認中'));
 resolveStatus(response(initial));await flush();
 assert.ok(element('operating-state').textContent.includes('要求：追跡準備'));
 resolveFrame({size:100});await flush();assert.ok(!element('live-img').src);
 const poll=timers.filter(t=>t.delay===1000).at(-1);
 context.fetch=async()=>({ok:false,json:async()=>({state:'unknown',message:'認識サービスに接続できません'})});
 poll.fn();await flush();assert.ok(element('operating-message').textContent.includes('接続できません'));
 context.fetch=async()=>response(initial);timers.filter(t=>t.delay===1000).at(-1).fn();await flush();
 assert.equal(element('operating-message').textContent,'');
 assert.ok(!SCRIPT.includes('localStorage')&&!SCRIPT.includes('sessionStorage'));
 assert.ok(timers.filter(t=>t.delay===500).length===2);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', 'const SCRIPT='+json.dumps(source)+';\n'+harness], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
