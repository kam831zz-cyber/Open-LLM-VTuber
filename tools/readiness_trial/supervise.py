"""Bounded trial CLI. --dummy runs one inert worker, never OpenLLM."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from monitor import Journal,run_trial


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',required=True)
    parser.add_argument('--production',required=True)
    parser.add_argument('--port',type=int,default=12394)
    parser.add_argument('--dummy',action='store_true')
    args=parser.parse_args()
    runtime=Path(args.runtime).resolve()
    if runtime.exists():raise SystemExit('runtime_exists')
    runtime.mkdir(parents=True)
    journal=Journal(runtime)
    journal.event('journal_created')
    # Import/setup failure is recorded too; no application imports occur here.
    try:
        from windows_backend import WindowsBackend
        backend=WindowsBackend(runtime,args.production,dummy=args.dummy,port=args.port,journal=journal)
    except BaseException as exc:
        journal.error('backend_setup',exc);journal.stop('backend_setup_failed')
        journal.data['state']='finished';journal.save('pre-cleanup.json');journal.save('result.json')
        return 1
    result=run_trial(backend,journal,deadline=5 if args.dummy else 300,
                     interval=.25 if args.dummy else 2)
    print(json.dumps({key:result.get(key) for key in
        ('mode','state','candidate_pid','candidate_exit_code','stop_reason','stop_causes','total_seconds')}
        |{'samples':len(result['samples']),'errors':len(result['errors']),
          'persistence_errors':len(result['persistence_errors'])}),flush=True)
    verified=result.get('cleanup',{}).get('verify_cleanup',{})
    return int(bool(result['errors'] or result['persistence_errors'] or verified.get('remaining_owned')
                    or not verified.get('protected_ports_unchanged',False)
                    or not verified.get('production_hashes_unchanged',False)
                    or result['stop_reason']!=('dummy_observation_complete' if args.dummy else 'websocket_observed')))


if __name__=='__main__':raise SystemExit(main())
