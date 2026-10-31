"""Model/process/network-free supervision scenarios and partial measurement tests."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/readiness_trial'))
from monitor import Journal,run_trial
from windows_backend import WindowsBackend,classify_owned


class Clock:
    def __init__(self):self.now=0
    def __call__(self):return self.now
    def sleep(self,amount):self.now+=amount


class FakeBackend:
    def __init__(self,clock,runtime,*,exit_at=None,exit_code=0,samples=None,throw_sample=None,throw_cleanup=None,
                 residual=False,start_error=False):
        self.clock=clock;self.runtime=runtime;self.exit_at=exit_at;self.code=exit_code
        self.samples=samples;self.throw_sample=throw_sample;self.throw_cleanup=throw_cleanup
        self.residual=residual;self.start_error=start_error;self.calls=[];self.count=0;self.closed=False
    def preflight(self):pass
    def start(self,callback):
        callback(42)
        if self.start_error:raise OSError('sensitive_token_must_not_be_logged')
    def poll(self):
        if self.closed:return 0
        return self.code if self.exit_at is not None and self.clock()>=self.exit_at else None
    def sample(self):
        self.count+=1
        if self.throw_sample==self.count:raise RuntimeError('sensitive_token_must_not_be_logged')
        if self.samples:return self.samples[min(self.count-1,len(self.samples)-1)]
        return {'cpu_percent':7.5,'available_bytes':3*1024**3,'owned':[{'pid':42,'created_filetime':7,
                'working_set_bytes':16*1024**2,'private_bytes':9*1024**2}],'errors':[]}
    def progress(self):return None
    def capture_owned(self):
        self.calls.append('capture_owned')
        state=json.loads((self.runtime/'checkpoint.json').read_text())
        assert state['stop_reason'] is not None
        assert (self.runtime/'pre-cleanup.json').exists()
        return {'members':[42]}
    def request_stop(self):
        self.calls.append('request_stop')
        if self.throw_cleanup=='request_stop':raise RuntimeError('sensitive_token_must_not_be_logged')
        return {'requested':True}
    def close_job(self):
        self.calls.append('close_job')
        if self.throw_cleanup=='close_job':raise OSError('sensitive_token_must_not_be_logged')
        self.closed=True
        return {'closed':True,'owned_only':[42]}
    def wait(self):self.calls.append('wait');return {'exit_code':0}
    def verify_cleanup(self):
        self.calls.append('verify_cleanup')
        return {'remaining_owned':[{'pid':42,'state':'unknown'}] if self.residual else [],
                'unrelated_process_untouched':True}


class MeterReliabilityTests(unittest.TestCase):
    def trial(self,**kwargs):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);clock=Clock();journal=Journal(runtime,clock)
            backend=FakeBackend(clock,runtime,**kwargs)
            result=run_trial(backend,journal,deadline=3,interval=1,clock=clock,sleep=clock.sleep)
            final=json.loads((runtime/'result.json').read_text())
            before=json.loads((runtime/'pre-cleanup.json').read_text())
            checkpoint=json.loads((runtime/'checkpoint.json').read_text())
            return result,backend,final,before,checkpoint

    def test_normal_exit_code_zero_is_preserved(self):
        result,_,final,_,_=self.trial(exit_at=1)
        self.assertEqual(result['stop_reason'],'candidate_exited')
        self.assertEqual(final['candidate_exit_code'],0)

    def test_immediate_exit_records_pid_code_without_fabricated_samples(self):
        result,_,final,_,_=self.trial(exit_at=0)
        self.assertEqual(final['candidate_pid'],42)
        self.assertEqual(final['samples'],[])
        self.assertEqual(final['test_start_count'],1)

    def test_abnormal_exit_retains_original_code_before_cleanup(self):
        result,_,final,before,_=self.trial(exit_at=0,exit_code=17)
        self.assertEqual(result['stop_reason'],'candidate_failed')
        self.assertEqual(before['candidate_exit_code'],17)
        self.assertEqual(final['candidate_exit_code'],17)

    def test_identity_error_retains_partial_measurement(self):
        sample={'cpu_percent':8,'available_bytes':3*1024**3,
                'errors':[{'phase':'identity','error_type':'OSError'}],'owned':[{'pid':42,'state':'unknown'}]}
        result,_,final,_,_=self.trial(samples=[sample])
        self.assertEqual(final['stop_reason'],'measurement_error')
        self.assertEqual(final['samples'][0]['cpu_percent'],8)
        self.assertEqual(final['samples'][0]['available_bytes'],3*1024**3)

    def test_job_cleanup_is_called_even_after_measurement_exception(self):
        result,backend,_,before,_=self.trial(throw_sample=1)
        self.assertEqual(result['stop_reason'],'supervisor_exception')
        self.assertEqual(before['errors'][0]['error_type'],'RuntimeError')
        self.assertEqual(backend.calls,['capture_owned','request_stop','close_job','wait','verify_cleanup'])

    def test_cpu_ram_and_working_set_are_separate(self):
        result,_,_,_,_=self.trial(exit_at=1)
        sample=result['samples'][0]
        self.assertEqual(sample['cpu_percent'],7.5)
        self.assertNotEqual(sample['available_bytes'],sample['owned'][0]['working_set_bytes'])
        self.assertNotEqual(sample['owned'][0]['private_bytes'],sample['owned'][0]['working_set_bytes'])

    def test_midrun_exception_keeps_previous_samples(self):
        result,_,_,before,_=self.trial(throw_sample=2)
        self.assertEqual(len(before['samples']),1)
        self.assertEqual(result['errors'][0]['phase'],'supervision')

    def test_deadline_is_not_extended(self):
        result,_,_,before,_=self.trial()
        self.assertEqual(result['stop_reason'],'startup_deadline')
        self.assertEqual(before['stop_elapsed_s'],3)

    def test_simulated_low_ram_is_not_labeled_normal(self):
        result,_,_,before,_=self.trial(samples=[{'available_bytes':1024**3,'cpu_percent':5,'errors':[]}])
        self.assertEqual(before['stop_reason'],'available_RAM_below_1.5GiB')

    def test_cleanup_exception_does_not_skip_job_close_or_final_result(self):
        result,backend,final,_,_=self.trial(exit_at=1,throw_cleanup='request_stop')
        self.assertTrue(backend.closed)
        self.assertEqual(final['state'],'finished')
        self.assertEqual(final['errors'][0]['phase'],'cleanup.request_stop')
        self.assertEqual(final['stop_reason'],'candidate_exited')

    def test_job_close_exception_is_recorded_without_claiming_cleanup_success(self):
        result,backend,final,_,_=self.trial(exit_at=1,throw_cleanup='close_job',residual=True)
        self.assertFalse(backend.closed)
        self.assertEqual(final['cleanup']['close_job']['error_type'],'OSError')
        self.assertEqual(final['errors'][0]['phase'],'cleanup.close_job')
        self.assertTrue(final['cleanup']['verify_cleanup']['remaining_owned'])

    def test_precleanup_checkpoint_contains_first_stop_reason_and_pid(self):
        _,_,_,before,checkpoint=self.trial(exit_at=1)
        self.assertEqual(before['candidate_pid'],42)
        self.assertEqual(before['stop_reason'],'candidate_exited')
        self.assertEqual(checkpoint['state'],'stopping')

    def test_result_generated_after_start_failure_and_logs_are_sanitized(self):
        result,_,final,_,_=self.trial(start_error=True)
        self.assertEqual(final['candidate_pid'],42)
        self.assertEqual(final['errors'][0]['error_type'],'OSError')
        self.assertNotIn('sensitive_token',json.dumps(final))

    def test_unknown_residual_is_not_reported_as_clean(self):
        result,_,_,_,_=self.trial(exit_at=1,residual=True)
        self.assertEqual(result['cleanup']['verify_cleanup']['remaining_owned'][0]['state'],'unknown')

    def test_first_stop_reason_is_not_overwritten(self):
        result,_,_,_,_=self.trial(samples=[{'available_bytes':1024**3,'errors':[],
                                          'stop_causes':['protected_port_changed']}])
        self.assertEqual(result['stop_reason'],'available_RAM_below_1.5GiB')
        self.assertIn('protected_port_changed',result['stop_causes'])

    def test_checkpoint_write_failure_is_recorded_and_final_result_still_written(self):
        with tempfile.TemporaryDirectory() as directory:
            clock=Clock();journal=Journal(directory,clock);atomic=journal.atomic
            def flaky(name):
                if name=='checkpoint.json':raise PermissionError(13,'do_not_log_me')
                return atomic(name)
            with patch.object(journal,'atomic',side_effect=flaky):
                result=run_trial(FakeBackend(clock,Path(directory)),journal,deadline=3,interval=1,clock=clock,sleep=clock.sleep)
            self.assertTrue(result['persistence_errors'])
            self.assertEqual(result['stop_reason'],'supervisor_exception')
            self.assertTrue((Path(directory)/'result.json').exists())

    def test_atomic_save_does_not_leave_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=Journal(directory);self.assertTrue(journal.save())
            self.assertEqual(list(Path(directory).glob('*.tmp')),[])
            self.assertIsNone(json.loads((Path(directory)/'checkpoint.json').read_text())['stop_reason'])

    def test_windows_sampler_preserves_cpu_ram_when_identity_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            api=types.SimpleNamespace(memory=lambda:{'available_bytes':3*1024**3},cpu_ticks=lambda:(120,1100),
                members=lambda handle:[42],parents=lambda:{42:7},identity=MagicMock(side_effect=OSError('no_path')),
                listeners=lambda:{18874:5})
            backend=WindowsBackend(directory,directory,journal=Journal(directory),api=api)
            backend.tick=(100,1000);backend.tick_time=0;backend.handle=1
            backend.baseline_ports={18874:5};backend.hashes={};backend.allowed=set()
            with patch('windows_backend.time.monotonic',return_value=1):sample=backend.sample()
            self.assertEqual(sample['cpu_percent'],80)
            self.assertEqual(sample['available_bytes'],3*1024**3)
            self.assertEqual(sample['errors'][0]['phase'],'identity')

    def test_pid_reuse_is_reported_without_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            api=types.SimpleNamespace(identity=lambda pid:{'pid':pid,'state':'running','created_filetime':9},
                                      listeners=lambda:{},memory=lambda:{'available_bytes':3*1024**3})
            backend=WindowsBackend(directory,directory,journal=Journal(directory),api=api)
            backend.known={(42,7):{'pid':42,'created_filetime':7}};backend.baseline_ports={};backend.hashes={}
            result=backend.verify_cleanup()
            self.assertEqual(result['owned_statuses'][0]['state'],'pid_reused')
            self.assertEqual(result['remaining_owned'],[])

    def test_console_host_requires_exact_path_parent_and_creation_order(self):
        allowed={'python.exe'};parent={42:{'created_filetime':7}}
        console={'pid':43,'executable':'system/conhost.exe','parent_pid':42,'created_filetime':8}
        self.assertEqual(classify_owned(console,parent,allowed,'system/conhost.exe',str),'windows_console_host')
        for override in ({'executable':'elsewhere/conhost.exe'},{'parent_pid':99},
                         {'created_filetime':6},{'created_filetime':None}):
            with self.subTest(override=override):
                self.assertEqual(classify_owned(console|override,parent,allowed,'system/conhost.exe',str),'unknown')

    def test_sampler_accepts_only_private_job_console_host(self):
        with tempfile.TemporaryDirectory() as directory:
            identities={42:{'pid':42,'state':'running','created_filetime':7,'executable':'python.exe'},
                        43:{'pid':43,'state':'running','created_filetime':8,'executable':'system/conhost.exe'}}
            api=types.SimpleNamespace(memory=lambda:{'available_bytes':3*1024**3},cpu_ticks=lambda:(120,1100),
                members=lambda handle:[42,43],parents=lambda:{42:1,43:42},identity=lambda pid:identities[pid],
                listeners=lambda:{18874:5})
            backend=WindowsBackend(directory,directory,dummy=True,journal=Journal(directory),api=api)
            backend.tick=(100,1000);backend.tick_time=0;backend.handle=1
            backend.baseline_ports={18874:5};backend.hashes={}
            backend.canonical=str;backend.allowed={'python.exe'};backend.console_path='system/conhost.exe'
            with patch('windows_backend.time.monotonic',return_value=1):sample=backend.sample()
            self.assertEqual(sample['stop_causes'],[])
            self.assertEqual(sample['owned'][1]['role'],'windows_console_host')
