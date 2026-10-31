"""Owned Windows trial backend. Dummy mode never imports application code."""
import asyncio
import hashlib
import http.client
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


class WindowsBackend:
    def __init__(self,runtime,production,*,dummy=False,port=12394,journal,api=None):
        if api is None:import windows_owner as api
        self.api=api
        self.runtime=Path(runtime);self.production=Path(production)
        self.root=Path(__file__).resolve().parents[2]
        self.dummy=dummy;self.port=port;self.journal=journal
        self.handle=None;self.process=None;self.assigned=False;self.known={}
        self.tick=None;self.tick_time=None;self.ws_result=None;self.ws_thread=None
        self.finished=threading.Event();self.http=None

    @staticmethod
    def canonical(path):return os.path.normcase(str(Path(path).resolve()))

    def preflight(self):
        self.before=self.api.memory()
        if self.before['available_bytes']<1.5*1024**3:raise RuntimeError('RAM_gate_failed')
        ports=self.api.listeners()
        self.protected=(18873,18874,18875,18876,18081,5005,11434,50021)
        self.baseline_ports={port:ports[port] for port in self.protected if port in ports}
        if len(self.baseline_ports)!=len(self.protected) or 12393 in ports:
            raise RuntimeError('protected_port_gate_failed')
        self.hashes={str(self.production/name):hashlib.sha256((self.production/name).read_bytes()).hexdigest()
                     for name in ('.git/index','conf.yaml','characters/komugi.yaml','mcp_servers.json','.python-version')}
        if not self.dummy:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                probe.bind(('127.0.0.1',self.port))
        self.journal.data.update(before=self.before,protected_ports_before=self.baseline_ports,
                                 mode='dummy' if self.dummy else 'candidate')
        self.journal.save()

    def start(self,launched):
        self.handle=self.api.job()
        self.journal.event('private_job_created')
        env={name:value for name,value in os.environ.items()
             if not re.search(r'(?i)token|password|secret|api.?key|home_assistant|proxy',name)}
        for name in ('TEMP','TMP','HF_HOME','MODELSCOPE_CACHE','XDG_CACHE_HOME','TORCH_HOME','HOME','USERPROFILE'):
            folder=self.runtime/('tmp' if name in ('TEMP','TMP') else 'home')
            folder.mkdir(exist_ok=True);env[name]=str(folder)
        env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',UV_OFFLINE='1',
                   PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',HF_HUB_DISABLE_TELEMETRY='1')
        if self.dummy:
            # Base executable avoids the venv redirector: exactly one OS worker.
            python=str(Path(sys._base_executable).resolve())
            command=[python,'-I','-B',str(Path(__file__).with_name('dummy.py')),'--runtime',str(self.runtime)]
        else:
            for directory in ('cache','live2d-models','backgrounds','avatars','web_tool','frontend','models'):
                (self.runtime/directory).mkdir()
            shutil.copyfile(self.root/'model_dict.json',self.runtime/'model_dict.json')
            command=[sys.executable,'-I','-B',str(Path(__file__).with_name('candidate.py')),
                     '--runtime',str(self.runtime),'--production',str(self.production),'--port',str(self.port)]
        self.allowed={self.canonical(command[0]),self.canonical(Path(sys.base_prefix)/'python.exe')}
        self.console_path=self.canonical(self.api.console_host_path())
        self.journal.data['candidate_command']=command  # Only fixed flags/paths, never configuration values.
        self.process=subprocess.Popen(command,cwd=self.runtime,env=env,stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW|4)  # suspended before Job assignment
        launched(self.process.pid)
        if not self.api.k.AssignProcessToJobObject(self.handle,int(self.process._handle)):
            # The retained Popen handle owns this still-suspended child; it has no descendants.
            self.process.terminate();self.process.wait(timeout=3)
            raise RuntimeError('candidate_job_assignment_failed')
        self.assigned=True
        self.journal.data['candidate_identity']=self.api.identity(self.process.pid)
        self.journal.data['job_members_at_assignment']=self.api.members(self.handle)
        self.journal.event('assigned_suspended_candidate')
        self.tick=self.api.cpu_ticks();self.tick_time=time.monotonic()
        self.api.resume(self.process.pid)
        (self.runtime/'owned.start').write_text('start',encoding='ascii')
        self.journal.event('candidate_resumed')

    def poll(self):return self.process.poll() if self.process else None

    def sample(self):
        sample={'errors':[],'owned':[],'stop_causes':[],'monotonic_ns':time.monotonic_ns()}
        def attempt(phase,call):
            try:return call()
            except Exception as exc:
                sample['errors'].append({'phase':phase,'error_type':type(exc).__name__,
                                          'errno':getattr(exc,'errno',None)})
                return None
        current=attempt('memory',self.api.memory)
        if current:sample.update(current)
        ticks=attempt('cpu',self.api.cpu_ticks);now=time.monotonic()
        if ticks and self.tick:
            total=ticks[1]-self.tick[1]
            sample['cpu_interval_s']=round(now-self.tick_time,6)
            sample['cpu_percent']=round(max(0,min(100,100*(1-(ticks[0]-self.tick[0])/total))),2) if total>0 and now-self.tick_time>=.1 else None
            self.tick=ticks;self.tick_time=now
        pids=attempt('job_members',lambda:self.api.members(self.handle))
        parent_map=attempt('parents',self.api.parents) or {}
        for pid in pids or ():
            proc=attempt('identity',lambda:self.api.identity(pid))
            if proc is None:
                sample['owned'].append({'pid':pid,'state':'unknown'})
                continue
            proc['parent_pid']=parent_map.get(pid)
            sample['owned'].append(proc)
            if proc.get('created_filetime'):
                self.known[(pid,proc['created_filetime'])]=proc
            if proc.get('state')=='running':
                if not proc.get('memory_query_succeeded',True):
                    sample['errors'].append({'phase':'process_memory','error_type':'MemoryQueryFailed'})
        # Console hosts are not arbitrary executables allowed by name. They must
        # be in this private Job and descend from a measured Python identity.
        python_records={proc['pid']:proc for proc in sample['owned'] if proc.get('state')=='running'
                        and self.canonical(proc.get('executable','')) in self.allowed}
        support_count=0
        for proc in sample['owned']:
            if proc.get('state')!='running':continue
            proc['role']=classify_owned(proc,python_records,self.allowed,getattr(self,'console_path',None),self.canonical)
            if proc['role']=='unknown':sample['stop_causes'].append('unexpected_owned_executable')
            if proc['role']=='windows_console_host':support_count+=1
        if self.dummy and len(python_records)>1:sample['stop_causes'].append('unexpected_dummy_worker_count')
        if support_count>len(python_records):sample['stop_causes'].append('unexpected_console_host_count')
        if pids is not None and len(pids)>(2 if self.dummy else 12):
            sample['stop_causes'].append('unexpected_child_count')
        ports=attempt('listeners',self.api.listeners)
        if ports is not None and (any(ports.get(port)!=pid for port,pid in self.baseline_ports.items()) or 12393 in ports):
            sample['stop_causes'].append('protected_port_changed')
        unchanged=attempt('production_hashes',lambda:all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in self.hashes.items()))
        if unchanged is False:sample['stop_causes'].append('production_file_changed')
        return sample

    def progress(self):
        if self.dummy:
            if self.journal.elapsed()>=1.5 and (self.runtime/'dummy-started.json').exists():
                return {'finished':True,'reason':'dummy_observation_complete'}
            return None
        if self.http is None:
            connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=.3)
            try:
                connection.connect()
                self.journal.data.setdefault('tcp_ready_s',self.journal.elapsed())
                connection.request('GET','/openapi.json');response=connection.getresponse()
                body=json.loads(response.read(2*1024*1024))
                self.http={'status':response.status,'valid_openapi':isinstance(body.get('paths'),dict),
                           'elapsed_s':self.journal.elapsed()}
            except (OSError,ValueError):return None
            finally:connection.close()
            self.journal.data['http']=self.http
            if self.http['status']!=200 or not self.http['valid_openapi']:
                return {'finished':True,'reason':'http_failed'}
            self.ws_thread=threading.Thread(target=self.websocket_once,daemon=True);self.ws_thread.start()
        if self.finished.is_set():
            return {'finished':True,'reason':'websocket_observed' if 'status' in self.ws_result else 'websocket_failed',
                    'websocket':self.ws_result}
        return None

    def websocket_once(self):
        async def connect_once():
            from websockets.asyncio.client import connect
            events=[];status=None
            async with connect(f'ws://127.0.0.1:{self.port}/client-ws',open_timeout=5,close_timeout=2) as ws:
                for _ in range(12):
                    message=json.loads(await asyncio.wait_for(ws.recv(),8))
                    events.append(message.get('type'))
                    if message.get('type')=='service-status':status={k:v for k,v in message.items() if k!='type'}
                    if message.get('type')=='control' and message.get('text')=='start-mic':break
            return {'events':events,'status':status,'sent_application_messages':0}
        try:self.ws_result=asyncio.run(asyncio.wait_for(connect_once(),18))
        except BaseException as exc:self.ws_result={'error_type':type(exc).__name__}
        finally:self.finished.set()

    def capture_owned(self):
        if self.handle is None:return {'assigned':False,'members':[]}
        pids=self.api.members(self.handle)
        records=[]
        for pid in pids:
            record=self.api.identity(pid);records.append(record)
            if record.get('created_filetime'):self.known[(pid,record['created_filetime'])]=record
        return {'assigned':self.assigned,'members':pids,'identities':records}

    def request_stop(self):
        if self.process and self.poll() is None:
            if not self.assigned:
                # Still suspended; only this retained creation handle is targeted.
                self.process.terminate()
                self.process.wait(timeout=3)
                return {'unassigned_suspended_candidate_terminated':True}
            (self.runtime/'stop').write_text('stop',encoding='ascii')
            if self.journal.data.get('stop_reason') in (
                'available_RAM_below_1.5GiB','CPU_sustained_80_percent','measurement_error',
                'protected_port_changed','production_file_changed','unexpected_owned_executable'):
                # Release owned memory promptly; do not wait 3s while a model loads.
                return {'exit_code_before_job_close':self.poll(),'cooperative_grace_seconds':0,
                        'reason':'urgent_stop_no_grace'}
            try:self.process.wait(timeout=.2 if self.dummy else 3)
            except subprocess.TimeoutExpired:pass
        return {'exit_code_before_job_close':self.poll()}

    def close_job(self):
        if self.handle is None:return {'closed':False,'not_created':True}
        pending=self.poll() is None
        if not self.api.k.CloseHandle(self.handle):raise OSError('job_close_failed')
        self.handle=None
        return {'closed':True,'candidate_pending_before_close':pending,'kill_on_close':True}

    def wait(self):
        code=self.process.wait(timeout=5) if self.process else None
        if self.ws_thread:self.ws_thread.join(timeout=3)
        return {'exit_code':code}

    def verify_cleanup(self):
        statuses=[]
        for old in self.known.values():
            try:
                current=self.api.identity(old['pid'])
                if current.get('state')=='missing':state='gone'
                elif current.get('created_filetime')!=old['created_filetime']:state='pid_reused'
                elif current.get('state')=='exited':state='exited'
                else:state='remaining'
                statuses.append({'pid':old['pid'],'state':state})
            except Exception as exc:statuses.append({'pid':old['pid'],'state':'unknown','error_type':type(exc).__name__})
        ports=self.api.listeners()
        return {'owned_statuses':statuses,'remaining_owned':[s for s in statuses if s['state'] in ('remaining','unknown')],
                'after':self.api.memory(),'protected_ports_unchanged':all(ports.get(p)==v for p,v in self.baseline_ports.items()),
                'production_hashes_unchanged':all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in self.hashes.items()),
                'test_port_released':self.port not in ports,'production_port_unused':12393 not in ports}


def classify_owned(proc,python_records,allowed,console_path,canonical):
    """Only call with identities measured from private Job membership."""
    if canonical(proc.get('executable','')) in allowed:return 'python_worker'
    parent=python_records.get(proc.get('parent_pid'))
    if (console_path and canonical(proc.get('executable',''))==console_path and parent
            and isinstance(proc.get('created_filetime'),int)
            and isinstance(parent.get('created_filetime'),int)
            and proc['created_filetime']>=parent['created_filetime']):
        return 'windows_console_host'
    return 'unknown'
