import json
import re
import shutil
import subprocess
from datetime import datetime,timezone

import pytest


@pytest.fixture
def summary(app,monkeypatch):
    app.config.update(OPERATING_SOCKET='/private/control.sock',OPERATING_CAMERA_ID='cam')
    old=datetime(2026,1,1,tzinfo=timezone.utc)
    store=app.extensions['metadata_store']
    store.record_heartbeat('cam',seen_at=old)
    store.record_heartbeat('legacy',seen_at=old)
    native={'state':'active','effective_mode':'tracking','connected':True,'last_result_age_seconds':.1}
    monkeypatch.setattr('script.operating_routes.status',lambda _:dict(native))
    return native,store,old


@pytest.mark.parametrize('query',['','?limit=10&offset=0'])
def test_summaries_use_native_frames_without_sql_heartbeat(summary,client,query):
    native,store,old=summary
    rows={r['camera_id']:r for r in client.get('/api/cameras'+query).json['cameras']}
    assert rows['cam']['status']=='healthy' and rows['cam']['expected_interval_seconds']==300
    assert datetime.fromisoformat(rows['cam']['last_seen_at'])>old
    assert rows['legacy']['status']=='offline' and rows['legacy']['last_seen_at']==old.isoformat()
    for path in ('/','/cameras'):
        html=client.get(path).get_data(as_text=True)
        assert 'badge healthy' in html
    assert '0.19s' in client.get('/cameras').get_data(as_text=True)
    assert store.get_camera('cam').last_seen_at==old
    assert store.get_camera('cam').expected_interval_seconds==300
    assert store.total_snapshot_count()==0


@pytest.mark.parametrize('mode,age,connected,status', [('eco',6,True,'healthy'),('eco',7.1,True,'unknown'),
    ('tracking',.6,True,'unknown'),('tracking',.1,False,'unknown'),(None,None,False,'unknown')])
def test_stale_or_unavailable_native_is_unknown_not_archive_health(summary,client,mode,age,connected,status):
    native,_,_=summary
    native.update(effective_mode=mode,last_result_age_seconds=age,connected=connected)
    rows={r['camera_id']:r for r in client.get('/api/cameras').json['cameras']}
    assert rows['cam']['status']==status
    connection=client.get('/api/cameras/cam/connection').json
    assert connection['status']==status


def test_dashboard_native_poll_replaces_saved_health_and_ignores_legacy_events(summary,client):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node is required for dashboard behavior check')
    html=client.get('/').get_data(as_text=True)
    script=next(s for s in re.findall(r'<script>(.*?)</script>',html,re.S) if 'function onEvent' in s)
    harness=r'''
const vm=require('vm'),assert=require('assert/strict');
(async()=>{
 const badge={dataset:{status:'healthy'},classList:{remove(){},add(){}}};
 const elements={'cam-status-badge-cam':badge,'cam-status-text-cam':{},'stat-online':{textContent:'1'},'live-pill':{}};
 const timers=[];let message,healthCheck,online=false,calls=0;
 class WebSocket {addEventListener(kind,fn){if(kind==='message')message=fn}}
 const context={window:{CameraShared:{formatTimestamp:x=>x,formatFrameSize:x=>x},_setWsStatus(){}},
  location:{protocol:'http:',host:'pi'},WebSocket,AbortSignal:{timeout:()=>null},
  fetch:async()=>{calls++;return {ok:online,json:async()=>({connected:online,effective_mode:'tracking',last_result_age_seconds:online?.1:null})}},
  setTimeout:fn=>timers.push(fn),setInterval:fn=>healthCheck=fn,
  document:{hidden:false,getElementById:id=>elements[id]||null,querySelectorAll:()=>[{id:'cam-interval-cam',dataset:{lastSeen:'2000-01-01'}}]}};
 vm.runInNewContext(SCRIPT,context);const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve()};await flush();
 assert.equal(badge.dataset.status,'unknown');assert.equal(elements['stat-online'].textContent,'0');
 message({data:JSON.stringify({type:'snapshot.created',camera_id:'cam',snapshot:{stored_at:new Date().toISOString()}})});
 assert.equal(badge.dataset.status,'unknown');
 online=true;timers.shift()();await flush();assert.equal(badge.dataset.status,'healthy');
 assert.equal(elements['stat-online'].textContent,'1');healthCheck();assert.equal(badge.dataset.status,'healthy');
 assert.equal(calls,2);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result=subprocess.run([node,'-e','const SCRIPT='+json.dumps(script)+';\n'+harness],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
