"""One candidate start, bounded observation, and cleanup of its private Job only."""
import argparse
import asyncio
import ctypes
import http.client
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
from windows_owner import job,members,memory,cpu_ticks,identity,k,listeners,parents

p=argparse.ArgumentParser()
p.add_argument('--runtime',required=True)
p.add_argument('--production',required=True)
p.add_argument('--port',type=int,default=12394)
a=p.parse_args()
runtime=Path(a.runtime).resolve();root=Path(__file__).resolve().parents[2]
if runtime.exists():raise SystemExit('runtime_exists')
if memory()['available_bytes']<1.5*1024**3:raise SystemExit('RAM_gate_failed')
protected_ports=(18873,18874,18875,18876,18081,5005,11434,50021)
baseline_ports={port:pid for port,pid in listeners().items() if port in protected_ports}
if len(baseline_ports)!=len(protected_ports) or 12393 in listeners():raise SystemExit('protected_port_gate_failed')
baseline_files={str(Path(a.production)/name):hashlib.sha256((Path(a.production)/name).read_bytes()).hexdigest()
                for name in ('.git/index','conf.yaml','characters/komugi.yaml','mcp_servers.json','.python-version')}
with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
    probe.bind(('127.0.0.1',a.port))
runtime.mkdir(parents=True)
for directory in ('cache','live2d-models','backgrounds','avatars','web_tool','frontend','tmp','models','home'):
    (runtime/directory).mkdir()
shutil.copyfile(root/'model_dict.json',runtime/'model_dict.json')
result={'test_start_count':0,'port':a.port,'deadline_seconds':300,'before':memory(),
        'samples':[],'websocket':None,'http':None,'candidate_writes':'runtime_only',
        'text_inference_requests':0,'tts_requests':0,'microphone':False}
result['protected_ports_before']=baseline_ports
lock=threading.Lock();finished=threading.Event()
def websocket_once():
    async def connect():
        from websockets.asyncio.client import connect
        events=[];status=None
        async with connect(f'ws://127.0.0.1:{a.port}/client-ws',open_timeout=5,close_timeout=2) as ws:
            result['websocket_connected_s']=round(time.monotonic()-start,3)
            for _ in range(12):
                message=json.loads(await asyncio.wait_for(ws.recv(),8))
                events.append(message.get('type'))
                if message.get('type')=='service-status':
                    status={key:value for key,value in message.items() if key!='type'}
                if message.get('type')=='control' and message.get('text')=='start-mic':break
            result['websocket']={'events':events,'status':status,'sent_application_messages':0,
                                 'microphone_command_not_executed':True}
        result['websocket_closed_s']=round(time.monotonic()-start,3)
    try:asyncio.run(asyncio.wait_for(connect(),18))
    except BaseException as exc:result['websocket_error']=type(exc).__name__
    finally:finished.set()

env={name:value for name,value in os.environ.items() if not re.search(r'(?i)token|password|secret|api.?key|home_assistant|proxy',name)}
for name in ('TEMP','TMP','HF_HOME','MODELSCOPE_CACHE','XDG_CACHE_HOME','TORCH_HOME','HOME','USERPROFILE'):
    env[name]=str(runtime/('tmp' if name in ('TEMP','TMP') else 'home'))
env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',UV_OFFLINE='1',
           PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',HF_HUB_DISABLE_TELEMETRY='1')
handle=job();process=None;known={};start=time.monotonic();ticks=cpu_ticks();last=start;high=0;ws_thread=None
def snapshot():
    global ticks,last,high
    now=time.monotonic();new=cpu_ticks();total=new[1]-ticks[1]
    cpu=100*(1-(new[0]-ticks[0])/total) if total else 0
    ticks=new;last=now
    owned=[identity(pid) for pid in members(handle)]
    parent_map=parents()
    for proc in owned:proc['parent_pid']=parent_map.get(proc['pid'])
    for proc in owned:known[(proc['pid'],proc.get('created_filetime'))]=proc
    sample={'elapsed_s':round(now-start,3),'cpu_percent':round(cpu,1),**memory(),'owned':owned}
    result['samples'].append(sample)
    high=high+1 if cpu>=80 else 0
    if sample['available_bytes']<1.5*1024**3:result['aborted']='available_RAM_below_1.5GiB'
    if high>=3:result['aborted']='CPU_sustained_80_percent'
    allowed={str(Path(sys.executable).resolve()).lower(),str(Path(sys.base_prefix,'python.exe').resolve()).lower()}
    if any(proc.get('executable','').lower() not in allowed for proc in owned if not proc.get('exited_or_inaccessible')):
        result['aborted']='unexpected_owned_executable'
    if len(owned)>12:result['aborted']='unexpected_child_count'
    current=listeners()
    if any(current.get(port)!=pid for port,pid in baseline_ports.items()) or 12393 in current:
        result['aborted']='protected_port_changed'
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest for path,digest in baseline_files.items()):
        result['aborted']='production_file_changed'
    # Persist every sample and stop reason before teardown can encounter a race.
    (runtime/'checkpoint.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return sample

try:
    # Waiting gate ensures there are no children before private Job assignment.
    with (runtime/'stdout.safe.log').open('w',encoding='utf-8') as out:
        process=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).with_name('candidate.py')),
            '--runtime',str(runtime),'--production',a.production,'--port',str(a.port)],
            cwd=runtime,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        result['test_start_count']=1;result['candidate_identity']=identity(process.pid)
        if not k.AssignProcessToJobObject(handle,int(process._handle)):
            process.kill();process.wait();raise RuntimeError('candidate_Job_assignment_failed')
        (runtime/'owned.start').write_text('start',encoding='ascii')
        snapshot()
        while time.monotonic()-start<300:
            if time.monotonic()-last>=2:snapshot()
            if 'aborted' in result:break
            if process.poll() is not None:
                result['candidate_exit_code']=process.returncode;break
            if result['http'] is None:
                connection=http.client.HTTPConnection('127.0.0.1',a.port,timeout=.3)
                try:
                    connection.connect()
                    result.setdefault('tcp_ready_s',round(time.monotonic()-start,3))
                    connection.request('GET','/openapi.json');response=connection.getresponse()
                    body=json.loads(response.read(2*1024*1024))
                    result['http']={'status':response.status,'valid_openapi':isinstance(body.get('paths'),dict),
                                    'elapsed_s':round(time.monotonic()-start,3)}
                except (OSError,ValueError):pass
                finally:connection.close()
                if result['http']:
                    if result['http']['status']!=200:result['aborted']='http_failed';break
                    ws_thread=threading.Thread(target=websocket_once,daemon=True);ws_thread.start()
            if finished.is_set():break
            time.sleep(.3)
        else:result['aborted']='startup_deadline_300s'
        snapshot()
        result['owned_before_cleanup']=[identity(pid) for pid in members(handle)]
        (runtime/'pre-cleanup.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        # A cooperative request allows MCP context exits on the same event loop.
        (runtime/'stop').write_text('stop',encoding='ascii')
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:pass
        result['job_members_before_close']=members(handle)
finally:
    # Job membership proves ownership, independently of process names/PID reuse.
    k.CloseHandle(handle)
    if process:
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:result['cleanup_error']='candidate_did_not_exit'
    if ws_thread:ws_thread.join(timeout=3)
    time.sleep(.5)
    result['known_owned_identities']=list(known.values())
    result['remaining_owned']=[]
    result['identity_query_errors']=[]
    for old in known.values():
        try:
            current=identity(old['pid'])
            if not current.get('exited_or_inaccessible') and current.get('created_filetime')==old.get('created_filetime'):
                result['remaining_owned'].append(current)
        except OSError as exc:
            result['identity_query_errors'].append({'pid':old['pid'],'error':type(exc).__name__})
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        try:probe.bind(('127.0.0.1',a.port));result['test_port_released']=True
        except OSError:result['test_port_released']=False
    result['after']=memory();result['total_seconds']=round(time.monotonic()-start,3)
    result['protected_ports_after']={port:pid for port,pid in listeners().items() if port in protected_ports}
    result['production_hashes_unchanged']=all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in baseline_files.items())
    (runtime/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({key:result[key] for key in ('test_start_count','http','websocket','test_port_released','after')}
                     |{key:result[key] for key in ('aborted','candidate_exit_code','cleanup_error') if key in result}),flush=True)
