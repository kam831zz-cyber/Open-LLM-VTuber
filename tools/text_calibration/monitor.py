"""Mockable calibration monitor. Checkpoint and first stop precede all cleanup."""
from copy import deepcopy
import math
import time
from .policy import stop_causes


def summarize(data):
    """Boundary/interval samples, not a proof of attributable physical peak."""
    stack=[];phases=[];samples=data['samples']
    for event in data.get('phases',[]):
        if event['kind']=='begin':stack.append(event)
        elif event['kind']=='end' and stack:
            begin=stack.pop();span=samples[begin['sample_index']:event['sample_index']+1]
            def peak(name):
                values=[s.get(name) for s in span if type(s.get(name)) is int]
                return max(values) if values else None
            def change(name):
                first,last=span[0].get(name),span[-1].get(name)
                return last-first if type(first) is int and type(last) is int else None
            phases.append(dict(phase=event['phase'],work_elapsed_ns=event['work_elapsed_ns'],
                observed_system_available_change_bytes=change('available_bytes'),
                observed_commit_headroom_change_bytes=change('commit_headroom_bytes'),
                sampled_peak_WS_upper_bound_bytes=peak('working_set_upper_bound_bytes'),
                sampled_peak_private_bytes_sum=peak('private_bytes_sum'),
                job_lifetime_peak_commit_bytes=peak('job_peak_commit_bytes'),
                boundary_begin_sample=begin['sample_index'],boundary_end_sample=event['sample_index']))
    return dict(phases=phases,unfinished_phases=[s['phase'] for s in stack],
                phase_intervals_overlap=True,sampling_interval_seconds=.25,
                physical_attribution_verified=False,normal_budget_promoted=False)


def run(backend, journal, approval, *, clock=time.monotonic, sleep=time.sleep):
    data=journal.data; previous=None; high_since=None
    data.update(scope='calibration_asgi_one_session_no_listener',
                normal_budget_promoted=False,tcp='not_tested',http='not_tested',
                websocket='not_tested',conversation='not_tested')
    try:
        backend.preflight()
        journal.event('calibration_preflight_passed')
        if data['persistence_errors']:raise OSError('preflight_checkpoint_failed')
        def launched(pid):
            data.update(candidate_pid=pid,test_start_count=1,state='launched')
            journal.event('candidate_created_suspended',pid=pid)
            if data['persistence_errors']:raise OSError('identity_checkpoint_failed')
        backend.start(launched)
        while data['stop_reason'] is None:
            code=backend.poll()
            if code is not None:
                data['candidate_exit_code']=code
                journal.stop('candidate_exited_early' if code==0 else 'candidate_failed')
                break
            sample=backend.sample();sample['elapsed_s']=journal.elapsed()
            data['samples'].append(deepcopy(sample))
            if not journal.save():journal.stop('checkpoint_write_failed')
            for reason in stop_causes(approval,sample,baseline=backend.before,previous=previous):journal.stop(reason)
            cpu=sample.get('cpu_percent')
            if cpu is None and previous is not None:journal.stop('cpu_measurement_missing')
            elif cpu is not None and (type(cpu) not in (int,float) or not math.isfinite(cpu) or not 0<=cpu<=100):
                journal.stop('cpu_measurement_invalid')
            elif cpu is not None and cpu>=80:
                if high_since is None:high_since=clock()
                elif clock()-high_since>=2:journal.stop('cpu_sustained_80_percent')
            else:high_since=None
            for reason in sample.get('stop_causes',[]):journal.stop(reason)
            if data['persistence_errors']:journal.stop('persistence_failed')
            if data['stop_reason'] is not None:break
            progress=backend.progress(sample)
            if progress is not None:
                journal.event('phase_boundary',**progress)
                if data['persistence_errors']:raise OSError('phase_checkpoint_failed')
                backend.ack(progress['sequence']) # never allow next phase before persistence
                if progress.get('finished'):journal.stop('calibration_observation_complete')
            if journal.elapsed()>=approval.deadline_seconds:journal.stop('calibration_deadline')
            previous=sample
            if data['stop_reason'] is None:sleep(.25)
    except BaseException as exc:
        journal.error('calibration_supervision',exc);journal.stop('calibration_supervisor_exception')
    finally:
        data['state']='stopping';journal.save('pre-cleanup.json')
        for name in ('capture_owned','request_stop','close_job','wait','verify_cleanup'):
            try:data['cleanup'][name]=getattr(backend,name)()
            except BaseException as exc:
                journal.error('cleanup.'+name,exc);data['cleanup'][name]={'error_type':type(exc).__name__}
            journal.save()
        try:data['cleanup_exit_code']=backend.poll()
        except BaseException as exc:journal.error('cleanup.poll',exc)
        data.update(state='finished',total_seconds=journal.elapsed(),
                    process_exit_is_not_application_success=True)
        data['phase_summary']=summarize(data)
        journal.save('result.json')
    return data
