"""Review-only by default. Real calibration additionally needs separate human approval."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from tools.text_calibration.policy import Approval
from tools.readiness_trial.monitor import Journal
from tools.text_calibration.monitor import run


def load_approval(path, digest, source_sha):
    if path.stat().st_size>8192:raise ValueError('approval_oversized')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('approval_hash_mismatch')
    return Approval.parse(json.loads(raw),source_sha=source_sha),raw


def reserve_once(path, approval, digest, *, ledger_root=None):
    # Public per-run receipt, never a measured-budget record. Exclusive creation.
    folder=Path(ledger_root) if ledger_root is not None else path.parent
    folder.mkdir(parents=True,exist_ok=True)
    ledger=folder/('.calibration-used-'+approval.run_id+'.json')
    with ledger.open('x',encoding='utf-8') as handle:
        json.dump({'run_id':approval.run_id,'approval_sha256':digest,'attempt_reserved':True},handle)
        handle.flush();os.fsync(handle.fileno())


def source_identity():
    env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'}
    head=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True,env=env).strip()
    dirty=subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain'],text=True,env=env)
    if dirty:raise ValueError('calibration_source_dirty')
    return head


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute-calibration',action='store_true')
    parser.add_argument('--approval');parser.add_argument('--approval-sha256')
    parser.add_argument('--runtime');parser.add_argument('--production')
    args=parser.parse_args(argv)
    if not args.execute_calibration:
        print(json.dumps({'state':'review_only','started':False,'approval_required':True}))
        return 0
    if not all((args.approval,args.approval_sha256,args.runtime,args.production)):
        raise ValueError('explicit_calibration_arguments_required')
    if (sys.version_info[:3]!=(3,11,9) or sys.prefix==sys.base_prefix
            or Path(sys.base_prefix).name.lower()!='python3119'
            or Path(sys.prefix).name.lower()!='openllm-venv3119'):
        raise ValueError('verified_python_environment_required')
    approval_path=Path(args.approval).resolve();runtime=Path(args.runtime).resolve()
    production=Path(args.production).resolve();diagnostics=production.parent/'diagnostics'
    if (not runtime.is_relative_to(diagnostics) or runtime==diagnostics or runtime.exists()
            or not approval_path.is_relative_to(diagnostics)
            or runtime.is_relative_to(ROOT) or approval_path.is_relative_to(ROOT)):
        raise ValueError('calibration_paths_invalid_or_used')
    approval,raw=load_approval(approval_path,args.approval_sha256,source_identity())
    runtime.mkdir(parents=True,exist_ok=False)
    journal=Journal(runtime)
    try:
        from tools.text_calibration.backend import CalibrationBackend
        backend=CalibrationBackend(runtime,production,approval=approval,approval_bytes=raw,journal=journal,
            reserve=lambda:reserve_once(approval_path,approval,args.approval_sha256,
                                         ledger_root=diagnostics/'.komugi-text-calibration-ledger'))
        result=run(backend,journal,approval)
    except BaseException as exc:
        journal.error('calibration_setup',exc);journal.stop('calibration_setup_failed')
        journal.save('pre-cleanup.json');journal.save('result.json')
        return 1
    print(json.dumps({'stop_reason':result['stop_reason'],'started':bool(result['test_start_count']),
                      'normal_budget_promoted':False}))
    cleanup=result['cleanup'].get('verify_cleanup',{})
    return int(result['stop_reason']!='calibration_observation_complete' or bool(result['errors'])
               or bool(result['persistence_errors']) or bool(cleanup.get('remaining_owned'))
               or cleanup.get('protected_ports_unchanged') is not True
               or cleanup.get('production_hashes_unchanged') is not True)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'state':'refused','error_type':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(1)
