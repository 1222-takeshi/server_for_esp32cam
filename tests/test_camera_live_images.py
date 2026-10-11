import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import MINIMAL_JPEG


def _detail(app, client, discard):
    app.config['KEEP_ONLY_DETECTED_SNAPSHOTS'] = discard
    app.extensions['metadata_store'].record_heartbeat('cam')
    return client.get('/cameras/cam').get_data(as_text=True)


@pytest.mark.parametrize('discard', [False, True])
def test_saved_snapshot_script_url_matches_content_version(app, client, discard):
    script = Path(app.static_folder) / 'saved_snapshots.js'
    version = hashlib.sha256(script.read_bytes()).hexdigest()[:12]
    html = _detail(app, client, discard)
    assert f'<script src="/static/saved_snapshots.js?v={version}" defer></script>' in html


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


@pytest.mark.parametrize('discard,streaming,foreign', [
    (True, False, False),
    (False, True, False),
    (False, False, False),
    (False, False, True),
])
def test_upload_event_does_not_claim_image_was_saved(app, client, discard, streaming, foreign):
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
assert.equal(elements['snap-list'].children.length, 0);
assert.equal(elements['snapshot-count'].textContent, '1');
assert.equal(elements['latest-img'].src, initial);
assert.equal(elements['latest-filename'].textContent, 'saved.jpg');
'''
    prefix = (f'const SCRIPT={json.dumps(script)}, DISCARD={json.dumps(discard)}, '
              f'STREAMING={json.dumps(streaming)}, FOREIGN={json.dumps(foreign)};\n')
    result = subprocess.run([node, '-e', prefix + harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_saved_snapshot_polling_uses_only_retained_api_rows(app, client):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser polling regression checks')
    html = _detail(app, client, False)
    assert 'id="snapshot-count">確認中' in html
    assert 'static/saved_snapshots.js' in html
    assert 'id="latest-img"' in html
    assert 'id="saved-image-empty"' in html
    script = (app.static_folder and __import__('pathlib').Path(app.static_folder)
              / 'saved_snapshots.js').read_text()
    harness = r'''
const vm = require('vm'), assert = require('assert');
function element() {
  return {dataset:{},children:[],style:{},textContent:'',src:'',
    appendChild(child){child.parent=this;this.children.push(child)},
    append(...children){children.forEach(child=>this.appendChild(child))},
    replaceChildren(child){this.children = child.children},
    removeAttribute(attr){if(attr==='src')this.src=''},
    querySelectorAll(selector){return this.children.filter(child=>selector==='.bbox'&&child.className==='bbox')},
    remove(){this.removed=true;if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this)}};
}
const list = element();
list.dataset={apiUrl:'/api/cameras/cam/snapshots?limit=20',
 fileUrl:'/files/__filename__',thumbnailUrl:'/files/__filename__/thumbnail',keepOnlyDetected:'false'};
const count=element(), state=element(), img=element(), name=element(), ts=element();
const wrap=element(), meta=element(), empty=element(), badge=element(), status=element(), error=element();
wrap.style.display='none';empty.style.display='';badge.hidden=true;
const elements={'snap-list':list,'snapshot-count':count,'saved-snapshot-state':state,
 'latest-img':img,'latest-filename':name,'latest-ts':ts,'latest-img-wrap':wrap,
 'latest-meta':meta,'saved-image-empty':empty,'latest-badge':badge,
 'latest-status':status,'latest-inference-error':error};
let resolveFetch, rejectFetch, calls=0;
const timers=[];
const context={
  window:{CameraShared:{formatTimestamp:x=>x}},
  document:{hidden:false,getElementById:id=>elements[id]||null,
    createElement:element,createDocumentFragment:element,
    querySelectorAll:()=>wrap.querySelectorAll('.bbox')},
  AbortController:class {constructor(){this.signal={}} abort(){}},
  setTimeout:fn=>{timers.push(fn);return timers.length},clearTimeout:()=>{},
  fetch:(url,opts)=>{calls++;assert.equal(url,list.dataset.apiUrl);
    assert.equal(opts.cache,'no-store');
    return new Promise((resolve,reject)=>{resolveFetch=resolve;rejectFetch=reject})},
};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const row=(filename)=>({filename,stored_at:'2026-10-11T00:00:00Z',
 captured_at:null,inference_status:'succeeded',detections:[]});
(async()=>{
  vm.runInNewContext(SCRIPT,context);
  assert.equal(calls,1);
  // A slow fetch must not trigger a second concurrent request.
  assert.equal(timers.length,1); // abort timer only
  const detected=row('a<.jpg');detected.detections=[{label:'person',score:0.9,bbox:[0.1,0.2,0.5,0.7]}];
  resolveFetch({ok:true,json:async()=>({total:2,snapshots:[detected,row('b.jpg')]})});
  await tick();await tick();
  assert.equal(count.textContent,'2');
  assert.match(state.textContent,/最終保存/);
  assert.match(state.textContent,/検知 1件/);
  assert.equal(list.children.length,2);
  assert.equal(list.children[0].href,'/files/a%3C.jpg');
  assert.equal(list.children[0].children[1].children[0].textContent,'a<.jpg');
  assert.equal(img.src,'/files/a%3C.jpg');
  assert.equal(img.dataset.snapshotSrc,'/files/a%3C.jpg');
  assert.equal(wrap.style.display,'inline-block');
  assert.equal(empty.style.display,'none');
  assert.equal(wrap.querySelectorAll('.bbox').length,1);
  assert.equal(wrap.querySelectorAll('.bbox')[0].children[0].textContent,'person 90%');
  assert.equal(status.textContent,'succeeded');
  assert.equal(badge.hidden,false);

  img.src='http://pi/stream/cameras/cam';
  timers.pop()(); // next poll
  const failed=row('a<.jpg');failed.inference_status='failed';failed.inference_error='model unavailable';
  resolveFetch({ok:true,json:async()=>({total:1,snapshots:[failed]})});
  await tick();await tick();
  assert.equal(img.src,'http://pi/stream/cameras/cam');
  assert.equal(error.textContent,'model unavailable');
  // toggleStream restores the saved URL before calling restore().
  img.src=img.dataset.snapshotSrc;
  context.window.SavedSnapshotView.restore();
  assert.equal(wrap.querySelectorAll('.bbox').length,0);
  assert.equal(badge.hidden,false);
  assert.equal(status.textContent,'failed');

  timers.pop()();
  const next=row('new.jpg');next.detections=[{label:'dog',score:0.8,bbox:[0.2,0.3,0.6,0.9]}];
  resolveFetch({ok:true,json:async()=>({total:1,snapshots:[next]})});
  await tick();await tick();
  assert.equal(img.src,'/files/new.jpg');
  assert.equal(img.alt,'new.jpg');
  assert.equal(name.textContent,'new.jpg');
  assert.equal(error.textContent,'');
  assert.equal(status.textContent,'succeeded');
  assert.equal(wrap.querySelectorAll('.bbox').length,1);
  assert.equal(wrap.querySelectorAll('.bbox')[0].children[0].textContent,'dog 80%');

  img.src='http://pi/stream/cameras/cam';
  timers.pop()();
  resolveFetch({ok:true,json:async()=>({total:0,snapshots:[]})});
  await tick();await tick();
  assert.equal(count.textContent,'0');
  assert.equal(list.children.length,1);
  assert.equal(list.children[0].textContent,'保存済み画像はありません');
  assert.match(state.textContent,/最終保存: なし/);
  assert.equal(img.src,'http://pi/stream/cameras/cam');
  img.src='';context.window.SavedSnapshotView.restore();
  assert.equal(img.src,'');
  assert.equal(wrap.style.display,'none');
  assert.equal(empty.style.display,'');
  assert.equal(badge.hidden,true);

  timers.pop()();
  resolveFetch({ok:true,json:async()=>({total:'bad',snapshots:[]})});
  await tick();await tick();
  assert.equal(count.textContent,'0');
  assert.equal(list.children.length,1);
  assert.match(state.textContent,/前回確認時点/);

  timers.pop()();
  rejectFetch(new Error('network down'));
  await tick();await tick();
  assert.equal(count.textContent,'0');
  assert.match(state.textContent,/前回確認時点/);
  assert.equal(calls,6);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', f'const SCRIPT={json.dumps(script)};\n' + harness],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_dashboard_event_keeps_saved_thumbnail_until_reload(app, client):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser event regression checks')
    client.post('/upload', data=MINIMAL_JPEG, content_type='image/jpeg',
                headers={'X-Device-Id': 'cam', 'X-Captured-At': '2026-10-10T00:00:00Z'})
    html = client.get('/').get_data(as_text=True)
    script = next(s for s in re.findall(r'<script>(.*?)</script>', html, re.S)
                  if 'function onEvent' in s)
    harness = r'''
const vm=require('vm'),assert=require('assert');
const img={src:'/files/retained.jpg'},meta={textContent:'saved time'},count={textContent:'1'};
const badge={dataset:{status:'healthy'},classList:{remove(){},add(){}}};
const elements={'live-pill':{},'cam-img-cam':img,'cam-meta-cam':meta,
 'stat-snapshots':count,'cam-status-badge-cam':badge};
let handler;
class WebSocket {addEventListener(kind,fn){if(kind==='message')handler=fn}}
vm.runInNewContext(SCRIPT,{window:{CameraShared:{formatTimestamp:x=>x,formatFrameSize:x=>x},
 _setWsStatus(){}},location:{protocol:'http:',host:'pi'},WebSocket,
 setInterval(){},setTimeout(){},document:{getElementById:id=>elements[id]||null}});
handler({data:JSON.stringify({type:'snapshot.created',camera_id:'cam',
 snapshot:{url:'/files/deleted.jpg',thumbnail_url:'/files/deleted.jpg/thumbnail',
 stored_at:'2026-10-11T00:00:00Z'}})});
assert.equal(img.src,'/files/retained.jpg');
assert.equal(meta.textContent,'saved time');
assert.equal(count.textContent,'1');
'''
    result = subprocess.run([node, '-e', f'const SCRIPT={json.dumps(script)};\n' + harness],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('fail_initial', [False, True])
def test_hidden_detail_fetches_once_then_waits_until_visible(app, fail_initial):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser polling regression checks')
    script = (__import__('pathlib').Path(app.static_folder) / 'saved_snapshots.js').read_text()
    harness = r'''
const vm=require('vm'),assert=require('assert');
const list={dataset:{apiUrl:'/api/cameras/cam/snapshots?limit=20',keepOnlyDetected:'false'},
 replaceChildren(node){this.children=node.children}};
const count={textContent:'確認中'},state={textContent:'保存済み画像を確認中…'};
const document={hidden:true,getElementById:id=>({
 'snap-list':list,'snapshot-count':count,'saved-snapshot-state':state})[id]||null,
 createDocumentFragment:()=>({children:[],appendChild(child){this.children.push(child)}}),
 createElement:()=>({textContent:'',className:''})};
const timers=[];let calls=0;
const response=()=>({ok:true,json:async()=>({total:0,snapshots:[]})});
const context={document,window:{CameraShared:{formatTimestamp:x=>x}},
 AbortController:class{constructor(){this.signal={}}abort(){}},
 setTimeout:(fn,ms)=>{timers.push({fn,ms,used:false});return timers.length},
 clearTimeout:()=>{},
 fetch:(url,options)=>{calls++;assert.equal(url,list.dataset.apiUrl);
 assert.equal(options.cache,'no-store');
 return calls===1&&FAIL_INITIAL?Promise.reject(new Error('network')):Promise.resolve(response())}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function nextPoll(){const timer=timers.find(t=>t.ms===10000&&!t.used);
 assert.ok(timer,'next poll scheduled');timer.used=true;timer.fn()}
(async()=>{
 vm.runInNewContext(SCRIPT,context);
 assert.equal(calls,1,'hidden startup must fetch once');
 await tick();await tick();
 assert.equal(state.textContent,FAIL_INITIAL?
  '保存済み画像を確認できません（表示内容は前回確認時点）':'最終保存: なし');
 if(!FAIL_INITIAL)assert.equal(count.textContent,'0');
 nextPoll();
 assert.equal(calls,1,'hidden failure/success must not repeatedly fetch');
 document.hidden=false;
 nextPoll();
 assert.equal(calls,2,'visible tab resumes polling');
 await tick();await tick();
 assert.equal(count.textContent,'0');
 assert.equal(state.textContent,'最終保存: なし');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    prefix = f'const SCRIPT={json.dumps(script)},FAIL_INITIAL={json.dumps(fail_initial)};\n'
    result = subprocess.run([node, '-e', prefix + harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
