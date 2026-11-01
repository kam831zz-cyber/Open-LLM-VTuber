"""Windows adapter for the reviewed, one-shot, no-listener calibration worker."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid
from tools.readiness_trial.windows_backend import WindowsBackend
from tools.model_free_trial.preflight import observe_windows
from .policy import admit_calibration
from . import windows_limits

PHASES={'python_baseline','python_application_imports','configuration','context_initialization',
        'agent_construction','asgi_construction','one_inprocess_session','calibration_complete'}
PHASE_PLAN=[('python_baseline','begin'),('python_baseline','end'),
    ('python_application_imports','begin'),('python_application_imports','end'),
    ('configuration','begin'),('configuration','end'),
    ('context_initialization','begin'),('agent_construction','begin'),
    ('agent_construction','end'),('context_initialization','end'),
    ('asgi_construction','begin'),('asgi_construction','end'),
    ('one_inprocess_session','begin'),('one_inprocess_session','end'),
    ('calibration_complete','complete')]


class CalibrationBackend(WindowsBackend):
    def __init__(self, runtime, production, *, approval, approval_bytes, journal, reserve, api=None):
        if api is None:
            from tools.readiness_trial import windows_owner as api
        super().__init__(runtime,production,dummy=False,journal=journal,api=api)
        self.approval=approval;self.approval_bytes=approval_bytes
        self.reserve=reserve
        self.job_name='Local\\KomugiTextCalibration-'+uuid.uuid4().hex
        self.phase_sequence=0;self.phase_stack=[];self.previous_process={}

    def preflight(self):
        # Existing protection hashes/ports, but never its legacy candidate/start path.
        super().preflight()
        self.front_index=Path(subprocess.check_output(['git','-C',str(self.production/'frontend'),
            'rev-parse','--path-format=absolute','--git-path','index'],
            text=True,env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'}).strip())
        self.hashes[str(self.front_index)]=hashlib.sha256(self.front_index.read_bytes()).hexdigest()
        ports=self.api.listeners()
        self.baseline_ports.update({port:ports.get(port) for port in (5000,5001,5002,5050,18000)})
        self.service_identities={}
        for pid in set(self.baseline_ports.values())-{None}:
            identity=self.api.identity(pid)
            if identity.get('state')!='running':raise RuntimeError('protected_service_identity_unknown')
            self.service_identities[pid]=identity['created_filetime']
        self.git_metadata={}
        for repository in (self.production,self.production/'frontend'):
            gitdir=Path(subprocess.check_output(['git','-C',str(repository),'rev-parse','--absolute-git-dir'],
                text=True,env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'}).strip())
            head=gitdir/'HEAD';paths=[head,gitdir/'packed-refs']
            text=head.read_text(encoding='utf-8').strip()
            if text.startswith('ref: '):paths.append(gitdir/text[5:])
            for path in paths:
                self.git_metadata[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        initial=observe_windows();admission=admit_calibration(self.approval,initial)
        self.before=dict(available_bytes=initial.memory.available_bytes,
                         commit_headroom_bytes=initial.memory.commit_headroom_bytes)
        # Protect existing VM-related process identities, without touching VM APIs.
        self.vm_identities=windows_limits.vm_identities(self.api)
        if not self.vm_identities:raise RuntimeError('protected_vm_process_not_confirmed')
        cpu=[];ticks=self.api.cpu_ticks()
        for _ in range(2):
            time.sleep(.25);new=self.api.cpu_ticks();total=new[1]-ticks[1]
            if total<=0:raise RuntimeError('preflight_cpu_measurement_failed')
            cpu.append(100*(1-(new[0]-ticks[0])/total));ticks=new
        if all(value>=80 for value in cpu):raise RuntimeError('preflight_cpu_sustained_high')
        self.journal.data.update(mode='text_calibration',before=self.before,admission=admission,
            provisional_limits=self.approval.__dict__,phases=[],preflight_cpu_percent=cpu,
            protected_ports_before=self.baseline_ports,protected_service_created_times=self.service_identities,
            cap_semantics={'physical':'monitored_available_drop_and_conservative_WS_upper_bound',
                          'job_memory':'hard_Job_private_commit_limit',
                          'cpu':'hard_Job_CPU_rate_cap','active_processes':4})
        # All-zero values are not substituted for an unavailable process/measurement.
        self.journal.save()

    def start(self, launched):
        # Second observation immediately before suspended process creation.
        from .policy import Approval
        Approval.parse(json.loads(self.approval_bytes),source_sha=self.approval.source_sha)
        admit_calibration(self.approval,observe_windows())
        self.reserve() # exclusive single-use lease before Job/process creation
        self.handle=windows_limits.create(self.api,self.approval,self.job_name)
        for name in ('tmp','home'):(self.runtime/name).mkdir(exist_ok=True)
        env={name:os.environ[name] for name in ('SYSTEMROOT','WINDIR','COMSPEC','PATHEXT',
              'NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE') if name in os.environ}
        env.update(TEMP=str(self.runtime/'tmp'),TMP=str(self.runtime/'tmp'),
                   HOME=str(self.runtime/'home'),USERPROFILE=str(self.runtime/'home'),
                   HF_HOME=str(self.runtime/'home'),XDG_CACHE_HOME=str(self.runtime/'home'),
                   TORCH_HOME=str(self.runtime/'home'),MODELSCOPE_CACHE=str(self.runtime/'home'),
                   HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',UV_OFFLINE='1',
                   HF_HUB_DISABLE_TELEMETRY='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
        (self.runtime/'approval.json').write_bytes(self.approval_bytes)
        digest=hashlib.sha256(self.approval_bytes).hexdigest()
        command=[sys.executable,'-I','-B',str(Path(__file__).with_name('candidate.py')),
                 '--runtime',str(self.runtime),'--approval-sha256',digest]
        self.allowed={self.canonical(sys.executable),self.canonical(Path(sys.base_prefix)/'python.exe')}
        self.console_path=self.canonical(self.api.console_host_path())
        self.process=subprocess.Popen(command,cwd=self.runtime,env=env,stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW|4)
        launched(self.process.pid)
        if not self.api.k.AssignProcessToJobObject(self.handle,int(self.process._handle)):
            self.process.terminate();self.process.wait(timeout=3)
            raise RuntimeError('calibration_job_assignment_failed')
        self.assigned=True
        identity=self.api.identity(self.process.pid)
        if self.process.pid not in self.api.members(self.handle) or identity.get('state')!='running':
            raise RuntimeError('suspended_candidate_ownership_unverified')
        self.known[(identity['pid'],identity['created_filetime'])]=identity
        self.journal.data['candidate_identity']=identity
        self.journal.event('calibration_suspended_assigned')
        if self.journal.data['persistence_errors']:raise OSError('ownership_checkpoint_failed')
        self.tick=self.api.cpu_ticks();self.tick_time=time.monotonic()
        (self.runtime/'owned.start').write_text(json.dumps(dict(job_name=self.job_name,
            source_sha=self.approval.source_sha)),encoding='utf-8')
        self.api.resume(self.process.pid)

    def sample(self):
        sample=super().sample()
        observation=observe_windows()
        sample.update(available_bytes=observation.memory.available_bytes,
            commit_headroom_bytes=observation.memory.commit_headroom_bytes,
            runner_pids=observation.runner_pids)
        current=sample['owned']
        if not current or any(p.get('state')!='running' or p.get('working_set_bytes') is None
                              or p.get('private_bytes') is None for p in current):
            raise RuntimeError('owned_process_memory_incomplete')
        sample['working_set_upper_bound_bytes']=sum(p['working_set_bytes'] for p in current)
        sample['private_bytes_sum']=sum(p['private_bytes'] for p in current)
        sample['job_peak_commit_bytes']=windows_limits.peak_commit(self.api,self.handle)
        vm_now={pid:self.api.identity(pid) for pid in self.vm_identities}
        vm_ok=all(vm_now[pid].get('state')=='running' and vm_now[pid].get('created_filetime')==stamp
                  for pid,stamp in self.vm_identities.items())
        services={pid:self.api.identity(pid) for pid in self.service_identities}
        services_ok=all(services[pid].get('state')=='running' and services[pid].get('created_filetime')==stamp
                        for pid,stamp in self.service_identities.items())
        sample['protection_ok']=not sample['stop_causes'] and vm_ok and services_ok and self.git_unchanged()
        # Monitor process costs are separate, never attributed to candidate imports.
        monitor=self.api.identity(os.getpid())
        if monitor.get('working_set_bytes') is None or monitor.get('private_bytes') is None:
            raise RuntimeError('monitor_memory_unavailable')
        sample['monitor_memory']={k:monitor.get(k) for k in ('working_set_bytes','private_bytes')}
        now=time.monotonic_ns()
        for proc in current:
            key=(proc['pid'],proc['created_filetime']);old=self.previous_process.get(key)
            proc['cpu_percent_all_cores']=(100*(proc['cpu_ticks']-old[0])/1e7/((now-old[1])/1e9)
                                           /os.cpu_count()) if old is not None else None
            self.previous_process[key]=(proc['cpu_ticks'],now)
        if len(current)>4:sample['stop_causes'].append('calibration_child_limit_exceeded')
        python=[p for p in current if self.canonical(p.get('executable','')) in self.allowed]
        if (len(python)>2 or any(p['pid']!=self.process.pid and p.get('parent_pid')!=self.process.pid for p in python)
                or (len(python)==2 and len({self.canonical(p['executable']) for p in python})!=2)):
            sample['stop_causes'].append('unexpected_calibration_python_descendant')
        if time.monotonic_ns()-sample['monotonic_ns']>1_000_000_000:
            sample['stop_causes'].append('monitor_sample_too_slow')
        return sample

    def poll(self):
        path=self.runtime/'candidate-error.json'
        if path.exists():
            if path.stat().st_size>512:raise RuntimeError('candidate_error_record_oversized')
            error=json.loads(path.read_text(encoding='utf-8')).get('error_type')
            if not isinstance(error,str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,79}',error):
                raise RuntimeError('candidate_error_record_invalid')
            self.journal.data['candidate_error_type']=error
        return super().poll()

    def progress(self,sample):
        path=self.runtime/'stage.json'
        if not path.exists():return None
        if path.stat().st_size>2048:raise RuntimeError('stage_record_oversized')
        stage=json.loads(path.read_text(encoding='utf-8'))
        seq=stage.get('sequence')
        if type(seq) is not int:raise RuntimeError('stage_sequence_invalid')
        if seq==self.phase_sequence:return None
        if seq!=self.phase_sequence+1 or stage.get('phase') not in PHASES:
            raise RuntimeError('stage_gap_or_unknown_phase')
        if (type(stage.get('monotonic_ns')) is not int
                or (stage.get('kind')=='end' and (type(stage.get('work_elapsed_ns')) is not int
                                                 or stage['work_elapsed_ns']<0))):
            raise RuntimeError('stage_clock_invalid')
        phase,kind=stage['phase'],stage['kind']
        if seq>len(PHASE_PLAN) or (phase,kind)!=PHASE_PLAN[seq-1]:
            raise RuntimeError('calibration_phase_order_invalid')
        if kind=='begin':self.phase_stack.append(phase)
        elif kind=='end':
            if not self.phase_stack or self.phase_stack.pop()!=phase:raise RuntimeError('stage_pair_mismatch')
        elif kind=='complete':
            if phase!='calibration_complete' or self.phase_stack:raise RuntimeError('stage_completion_invalid')
        else:raise RuntimeError('stage_kind_invalid')
        self.phase_sequence=seq
        record={**stage,'parent_observed_ns':time.monotonic_ns(),
                'sample_index':len(self.journal.data['samples'])-1,
                'finished':kind=='complete'}
        self.journal.data['phases'].append(record)
        return record

    def ack(self,sequence):
        (self.runtime/f'ack-{sequence}').write_text('ack',encoding='ascii')

    def request_stop(self):
        # Calibration stops close the owned Job immediately; never wait while RAM drops.
        if self.process and self.poll() is None:
            if not self.assigned:
                self.process.terminate();self.process.wait(timeout=3)
                return {'unassigned_suspended_process_terminated':True}
            (self.runtime/'stop').write_text('stop',encoding='ascii')
        return {'cooperative_grace_seconds':0,'job_close_required':self.assigned}

    def verify_cleanup(self):
        if not self.process:
            return {'candidate_not_started':True,'remaining_owned':[]}
        result=super().verify_cleanup()
        result['production_hashes_unchanged'] &= self.git_unchanged()
        return result

    def git_unchanged(self):
        return all((hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None)==digest
                   for path,digest in self.git_metadata.items())
