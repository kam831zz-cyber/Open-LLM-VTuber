"""Model-free supervision state machine and durable, sanitized journals."""
import copy
import json
import os
from pathlib import Path
import sys
import time


class Journal:
    def __init__(self, runtime, clock=time.monotonic):
        self.runtime=Path(runtime)
        self.clock=clock
        self.started=clock()
        self.data={'schema':2,'test_start_count':0,'state':'created','stop_reason':None,
                   'stop_causes':[],'errors':[],'samples':[],'events':[],
                   'candidate_pid':None,'candidate_exit_code':None,'cleanup':{},
                   'persistence_errors':[]}

    def elapsed(self):
        return round(self.clock()-self.started,6)

    def atomic(self, name):
        path=self.runtime/name
        temporary=self.runtime/(name+'.tmp')
        with temporary.open('w',encoding='utf-8',newline='\n') as handle:
            json.dump(self.data,handle,ensure_ascii=True,indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)

    def save(self,name='checkpoint.json'):
        try:
            self.atomic(name)
            return True
        except Exception as exc:
            self.data['persistence_errors'].append({'file':name,'error_type':type(exc).__name__,
                                                    'errno':getattr(exc,'errno',None),'elapsed_s':self.elapsed()})
            return False

    def event(self,name,**fields):
        self.data['events'].append({'event':name,'elapsed_s':self.elapsed(),**fields})
        self.save()

    def error(self,phase,exc):
        # No exception text, source lines, locals, environment or credentials.
        self.data['errors'].append({'phase':phase,'error_type':type(exc).__name__,
                                   'errno':getattr(exc,'errno',None),'elapsed_s':self.elapsed()})
        self.save()

    def stop(self,reason):
        if reason not in self.data['stop_causes']:self.data['stop_causes'].append(reason)
        if self.data['stop_reason'] is None:
            self.data['stop_reason']=reason
            self.data['stop_elapsed_s']=self.elapsed()
        self.save()


def run_trial(backend,journal,*,deadline=300,interval=2,clock=time.monotonic,sleep=time.sleep):
    """Backend is injectable. Every failure path reaches independent cleanup steps."""
    result=journal.data
    result['deadline_seconds']=deadline
    high_seconds=0
    last_cpu_at=None
    try:
        backend.preflight()
        journal.event('preflight_passed')
        # Backend invokes this immediately after Popen, before identity/Job queries.
        def launched(pid):
            result.update(candidate_pid=pid,test_start_count=1,state='launched')
            journal.event('candidate_created',pid=pid)
            if result['persistence_errors']:
                raise OSError('candidate_identity_not_persisted')
        backend.start(launched)
        result['state']='monitoring'
        journal.event('ownership_verified')
        while result['stop_reason'] is None:
            code=backend.poll()
            if code is not None:
                result['candidate_exit_code']=code
                journal.stop('candidate_exited' if code==0 else 'candidate_failed')
                break
            sample=backend.sample()
            sample['elapsed_s']=journal.elapsed()
            result['samples'].append(copy.deepcopy(sample))
            if not journal.save():
                journal.stop('checkpoint_write_failed')
            if sample.get('errors'):
                journal.stop('measurement_error')
            if sample.get('available_bytes') is not None and sample['available_bytes']<1.5*1024**3:
                journal.stop('available_RAM_below_1.5GiB')
            cpu=sample.get('cpu_percent')
            if cpu is not None:
                now=clock()
                if cpu>=80 and last_cpu_at is not None:high_seconds+=now-last_cpu_at
                else:high_seconds=0
                last_cpu_at=now
                if high_seconds>=6:journal.stop('CPU_sustained_80_percent')
            for reason in sample.get('stop_causes',[]):journal.stop(reason)
            if result['stop_reason'] is not None:break
            progress=backend.progress()
            if progress:
                result['progress']=copy.deepcopy(progress)
                if progress.get('finished'):journal.stop(progress['reason'])
            if journal.elapsed()>=deadline:journal.stop('startup_deadline')
            if result['stop_reason'] is None:sleep(interval)
    except BaseException as exc:
        journal.error('supervision',exc)
        journal.stop('supervisor_exception')
    finally:
        result['state']='stopping'
        journal.event('before_cleanup')
        journal.save('pre-cleanup.json')
        for name in ('capture_owned','request_stop','close_job','wait','verify_cleanup'):
            try:
                result['cleanup'][name]=getattr(backend,name)()
            except BaseException as exc:
                journal.error('cleanup.'+name,exc)
                result['cleanup'][name]={'error_type':type(exc).__name__}
            journal.event('cleanup_step',step=name)
        # A valid exit code can be zero; never use truthiness as an exit test.
        try:
            code=backend.poll()
            result['cleanup_exit_code']=code
            if result['candidate_exit_code'] is None:result['candidate_exit_code']=code
        except BaseException as exc:journal.error('final_poll',exc)
        result['state']='finished'
        result['total_seconds']=journal.elapsed()
        final_saved=journal.save('result.json')
        if not final_saved:
            # If all storage fails, retain a sanitized terminal record as a last resort.
            print(json.dumps({'state':'result_write_failed','candidate_pid':result['candidate_pid'],
                              'stop_reason':result['stop_reason'],'errors':result['errors'],
                              'persistence_errors':result['persistence_errors']}),file=sys.stderr,flush=True)
    return result
