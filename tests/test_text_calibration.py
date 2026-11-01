"""T1 is Mock-only: not even the calibration worker or a real Job is started."""
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace as S
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from tools.text_calibration.policy import Approval, SCOPE, admit_calibration, stop_causes
from tools.text_calibration.monitor import run, summarize
from tools.text_calibration.launch import main, load_approval, reserve_once
from tools.readiness_trial.monitor import Journal
from tools.model_free_trial.preflight import Observation, ServerBudget, admit_server, TEXT_PROFILE
from open_llm_vtuber.model_loading_policy import GIB, Reading, AdmissionDenied

NOW=datetime(2026,11,1,tzinfo=timezone.utc)


def data(**changes):
    return dict(dict(authorized=True,scope=SCOPE,max_runs=1,budget_evidence='provisional_not_measured',
        run_id='a'*32,source_sha='b'*40,physical_cap_bytes=64*1024**2,
        job_commit_cap_bytes=128*1024**2,rapid_drop_bytes=32*1024**2,
        cpu_cap_percent=20,deadline_seconds=30,rationale='Offline fixture; not a real approved limit.',
        expires_at=(NOW+timedelta(minutes=10)).isoformat()),**changes)


def approved():return Approval.parse(data(),source_sha='b'*40,now=NOW)


def sample(**changes):
    return dict(dict(available_bytes=6*GIB,commit_headroom_bytes=6*GIB,
        working_set_upper_bound_bytes=8*1024**2,private_bytes_sum=8*1024**2,
        job_peak_commit_bytes=8*1024**2,monotonic_ns=250_000_000,
        cpu_percent=10,protection_ok=True,runner_pids=(),errors=[],stop_causes=[]),**changes)


class FakeBackend:
    def __init__(self,samples=None):
        self.before={'available_bytes':6*GIB};self.samples=list(samples or [sample()])
        self.calls=[];self.done=False
    def preflight(self):self.calls.append('preflight')
    def start(self,callback):self.calls.append('start');callback(123)
    def poll(self):return None
    def sample(self):
        if len(self.samples)>1:return self.samples.pop(0)
        return self.samples[0]
    def progress(self,s):return dict(sequence=1,phase='calibration_complete',kind='complete',finished=True)
    def ack(self,seq):self.calls.append('ack')
    def capture_owned(self):self.calls.append('capture');return {'owned':[123]}
    def request_stop(self):self.calls.append('stop');return {'cooperative_grace_seconds':0}
    def close_job(self):self.calls.append('close');return {'closed':True}
    def wait(self):self.calls.append('wait');return {'exit_code':0}
    def verify_cleanup(self):
        self.calls.append('verify');return dict(remaining_owned=[],protected_ports_unchanged=True,
                                              production_hashes_unchanged=True)


class CalibrationTests(unittest.TestCase):
    def test_approval_template_is_not_executable(self):
        template=json.loads((Path(__file__).resolve().parents[1]/
                            'tools/text_calibration/approval.template.json').read_text())
        with self.assertRaises(AdmissionDenied):Approval.parse(template,source_sha='b'*40,now=NOW)

    def test_explicit_valid_approval_preserves_provisional_evidence(self):
        a=approved();self.assertEqual(a.physical_cap_bytes,64*1024**2)
        self.assertEqual(a.rationale,data()['rationale'])

    def test_scope_sha_identity_and_repeat_authorization_rejected(self):
        for change in ({'authorized':False},{'scope':'other'},{'source_sha':'c'*40},
                       {'run_id':'../outside'},{'max_runs':2},{'budget_evidence':'measured'}):
            with self.assertRaises(AdmissionDenied):Approval.parse(data(**change),source_sha='b'*40,now=NOW)

    def test_caps_cannot_be_missing_or_invalid(self):
        for change in ({'physical_cap_bytes':None},{'job_commit_cap_bytes':0},
                       {'rapid_drop_bytes':True},{'cpu_cap_percent':80},{'deadline_seconds':121},
                       {'rapid_drop_bytes':GIB},{'rationale':''}):
            with self.assertRaises(AdmissionDenied):Approval.parse(data(**change),source_sha='b'*40,now=NOW)

    def test_expiry_missing_expired_and_long_approval_rejected(self):
        for value in ('invalid',NOW.isoformat(),(NOW+timedelta(hours=2)).isoformat(),
                      NOW.replace(tzinfo=None).isoformat()):
            with self.assertRaises(AdmissionDenied):Approval.parse(data(expires_at=value),source_sha='b'*40,now=NOW)

    def test_calibration_does_not_admit_normal_startup(self):
        o=Observation(Reading(6*GIB,6*GIB,0),(),0)
        result=admit_calibration(approved(),o,clock=lambda:0)
        self.assertFalse(result['normal_startup_admitted'])
        with self.assertRaises(AdmissionDenied):admit_server(ServerBudget(profile=TEXT_PROFILE),o,clock=lambda:0)

    def test_ram_cap_is_added_to_175_reserve(self):
        a=approved();threshold=int(1.75*GIB)+a.physical_cap_bytes
        with self.assertRaisesRegex(AdmissionDenied,'physical'):
            admit_calibration(a,Observation(Reading(threshold-1,6*GIB,0),(),0),clock=lambda:0)
        admit_calibration(a,Observation(Reading(threshold,6*GIB,0),(),0),clock=lambda:0)

    def test_commit_cap_is_separate(self):
        with self.assertRaisesRegex(AdmissionDenied,'commit'):
            admit_calibration(approved(),Observation(Reading(6*GIB,int(.25*GIB),0),(),0),clock=lambda:0)

    def test_stale_runner_and_unknown_memory_refuse(self):
        for o in (Observation(Reading(6*GIB,6*GIB,0),(),-3),
                  Observation(Reading(6*GIB,6*GIB,0),None,0),
                  Observation(Reading(6*GIB,6*GIB,0),(999,),0),
                  Observation(Reading(None,6*GIB,0),(),0)):
            with self.assertRaises(AdmissionDenied):admit_calibration(approved(),o,clock=lambda:0)

    def test_rapid_drop_and_monitor_gap_stop(self):
        previous=sample();current=sample(available_bytes=6*GIB-40*1024**2,monotonic_ns=500_000_000)
        reasons=stop_causes(approved(),current,baseline={'available_bytes':6*GIB},previous=previous)
        self.assertIn('rapid_available_memory_drop',reasons)
        reasons=stop_causes(approved(),sample(monotonic_ns=2_000_000_000),
                           baseline={'available_bytes':6*GIB},previous=previous)
        self.assertIn('monitor_gap_or_clock_error',reasons)

    def test_commit_physical_reserve_and_caps_stop(self):
        for change,reason in (({'available_bytes':GIB},'physical_reserve_reached'),
            ({'commit_headroom_bytes':0},'commit_reserve_reached'),
            ({'working_set_upper_bound_bytes':GIB},'provisional_physical_cap_reached'),
            ({'job_peak_commit_bytes':GIB},'provisional_job_commit_cap_reached')):
            self.assertIn(reason,stop_causes(approved(),sample(**change),baseline={'available_bytes':6*GIB}))

    def run_fake(self,backend):
        with tempfile.TemporaryDirectory() as folder:
            journal=Journal(folder,clock=lambda:0)
            result=run(backend,journal,approved(),clock=lambda:0,sleep=lambda _:None)
            self.assertTrue((Path(folder)/'pre-cleanup.json').exists())
            self.assertTrue((Path(folder)/'result.json').exists())
            return result

    def test_complete_measurement_does_not_claim_tcp_http_ws_or_chat(self):
        backend=FakeBackend();r=self.run_fake(backend)
        self.assertEqual(r['stop_reason'],'calibration_observation_complete')
        for key in ('tcp','http','websocket','conversation'):self.assertEqual(r[key],'not_tested')
        self.assertFalse(r['normal_budget_promoted']);self.assertIn('close',backend.calls)

    def test_runner_appearance_protection_change_and_failure_stop_before_ack(self):
        for change,reason in (({'runner_pids':(99,)},'runner_unknown_or_appeared'),
                             ({'protection_ok':False},'protected_state_changed_or_unknown'),
                             ({'errors':[{'error_type':'fixture'}]},'measurement_failed'),
                             ({'available_bytes':None},'measurement_missing')):
            backend=FakeBackend([sample(**change)]);r=self.run_fake(backend)
            self.assertEqual(r['stop_reason'],reason);self.assertNotIn('ack',backend.calls)

    def test_monitor_exception_is_saved_before_cleanup(self):
        backend=FakeBackend();backend.sample=MagicMock(side_effect=OSError('private fixture'))
        r=self.run_fake(backend)
        self.assertEqual(r['stop_reason'],'calibration_supervisor_exception')
        self.assertEqual(r['errors'][0]['error_type'],'OSError')
        self.assertNotIn('private fixture',json.dumps(r))

    def test_early_exit_retains_code_without_invented_samples(self):
        backend=FakeBackend();backend.poll=lambda:7;r=self.run_fake(backend)
        self.assertEqual(r['candidate_exit_code'],7);self.assertEqual(r['samples'],[])

    def test_cleanup_failure_does_not_skip_other_steps(self):
        backend=FakeBackend();backend.request_stop=MagicMock(side_effect=OSError('fixture'))
        r=self.run_fake(backend)
        self.assertIn('close',backend.calls);self.assertIn('verify',backend.calls)
        self.assertTrue(any(e['phase']=='cleanup.request_stop' for e in r['errors']))

    def test_preflight_failure_never_starts_worker(self):
        backend=FakeBackend();backend.preflight=MagicMock(side_effect=AdmissionDenied('fixture'))
        r=self.run_fake(backend);self.assertNotIn('start',backend.calls);self.assertEqual(r['test_start_count'],0)

    def test_review_only_launcher_never_creates_process(self):
        with patch('sys.stdout',new=io.StringIO()) as output:
            self.assertEqual(main([]),0)
        self.assertFalse(json.loads(output.getvalue())['started'])

    def test_single_use_reservation_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'ledger';path=Path(folder)/'approval.json'
            reserve_once(path,approved(),'fixture',ledger_root=root)
            with self.assertRaises(FileExistsError):
                reserve_once(Path(folder)/'another-copy/approval.json',approved(),'fixture',ledger_root=root)

    def test_approval_hash_mismatch_does_not_parse_or_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'approval.json';path.write_text('{}')
            with self.assertRaises(ValueError):load_approval(path,'invalid','b'*40)

    def test_nested_phase_summary_is_not_summed_as_physical_attribution(self):
        events=[dict(kind='begin',phase='context_initialization',sample_index=0),
                dict(kind='begin',phase='agent_construction',sample_index=0),
                dict(kind='end',phase='agent_construction',sample_index=1,work_elapsed_ns=5),
                dict(kind='end',phase='context_initialization',sample_index=2,work_elapsed_ns=10)]
        summary=summarize({'samples':[sample(),sample(),sample()],'phases':events})
        self.assertEqual(len(summary['phases']),2);self.assertTrue(summary['phase_intervals_overlap'])
        self.assertFalse(summary['physical_attribution_verified'])

    def test_fake_job_sets_hard_limits_and_closes_on_setup_failure(self):
        from tools.readiness_trial import windows_owner
        from tools.text_calibration.windows_limits import create
        api=S(Limits=windows_owner.Limits,k=MagicMock())
        api.k.CreateJobObjectW.return_value=101;api.k.SetInformationJobObject.return_value=True
        with patch('ctypes.get_last_error',return_value=0):
            self.assertEqual(create(api,approved(),'fixture'),101)
        calls=api.k.SetInformationJobObject.call_args_list
        self.assertEqual([c.args[1] for c in calls],[9,15])
        limits=calls[0].args[2]._obj;cpu=calls[1].args[2]._obj
        self.assertEqual(limits.basic.active,4)
        self.assertEqual(limits.basic.flags,0x2000|0x100|0x200|0x8)
        self.assertEqual(limits.job_memory,approved().job_commit_cap_bytes)
        self.assertEqual(cpu.rate,approved().cpu_cap_percent*100)
        api.k.SetInformationJobObject.return_value=False
        with patch('ctypes.get_last_error',return_value=0):
            with self.assertRaises(OSError):create(api,approved(),'fixture')
        api.k.CloseHandle.assert_called_with(101)

    def timed_run(self, *, cpu=10, deadline=30):
        from dataclasses import replace
        clock=[0.0];backend=FakeBackend()
        backend.progress=lambda s:None
        backend.sample=lambda:sample(monotonic_ns=int((clock[0]+.25)*1e9),cpu_percent=cpu)
        with tempfile.TemporaryDirectory() as folder:
            journal=Journal(folder,clock=lambda:clock[0])
            return run(backend,journal,replace(approved(),deadline_seconds=deadline),
                       clock=lambda:clock[0],sleep=lambda amount:clock.__setitem__(0,clock[0]+amount))

    def test_deadline_never_extends_for_missing_phase(self):
        self.assertEqual(self.timed_run(deadline=1)['stop_reason'],'calibration_deadline')

    def test_sustained_high_cpu_stops_without_real_load(self):
        self.assertEqual(self.timed_run(cpu=85)['stop_reason'],'cpu_sustained_80_percent')

    def test_persistence_failure_prevents_process_creation(self):
        backend=FakeBackend()
        with tempfile.TemporaryDirectory() as folder:
            journal=Journal(folder,clock=lambda:0);save=journal.atomic;counter=[0]
            def fail_once(name):
                counter[0]+=1
                if counter[0]==1:raise OSError('fixture')
                return save(name)
            journal.atomic=fail_once
            result=run(backend,journal,approved(),clock=lambda:0,sleep=lambda _:None)
            self.assertNotIn('start',backend.calls)
            self.assertTrue(result['persistence_errors'])

    def test_phase_protocol_requires_every_ordered_boundary(self):
        from tools.text_calibration.backend import CalibrationBackend,PHASE_PLAN
        with tempfile.TemporaryDirectory() as folder:
            backend=CalibrationBackend.__new__(CalibrationBackend)
            backend.runtime=Path(folder);backend.phase_sequence=0;backend.phase_stack=[]
            backend.journal=Journal(folder);backend.journal.data.update(phases=[],samples=[sample()])
            for seq,(phase,kind) in enumerate(PHASE_PLAN,1):
                stage=dict(sequence=seq,phase=phase,kind=kind,monotonic_ns=seq,work_elapsed_ns=1)
                (Path(folder)/'stage.json').write_text(json.dumps(stage))
                result=backend.progress(sample());backend.ack(seq)
                self.assertEqual(result['sequence'],seq)
                self.assertTrue((Path(folder)/f'ack-{seq}').exists())
            self.assertTrue(result['finished'])
            self.assertEqual(backend.phase_stack,[])

    def test_completion_cannot_skip_import_config_agent_and_session(self):
        from tools.text_calibration.backend import CalibrationBackend
        with tempfile.TemporaryDirectory() as folder:
            backend=CalibrationBackend.__new__(CalibrationBackend)
            backend.runtime=Path(folder);backend.phase_sequence=0;backend.phase_stack=[]
            (Path(folder)/'stage.json').write_text(json.dumps(dict(sequence=1,phase='calibration_complete',
                                                               kind='complete',monotonic_ns=1)))
            with self.assertRaises(RuntimeError):backend.progress(sample())
