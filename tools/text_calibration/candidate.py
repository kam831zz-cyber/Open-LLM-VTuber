"""Calibration worker. Only supervisor-owned execution; no listen/API/model stage."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time


class Phases:
    def __init__(self,runtime):self.runtime=Path(runtime);self.sequence=0

    def boundary(self,phase,kind,**fields):
        self.sequence+=1
        record=dict(sequence=self.sequence,phase=phase,kind=kind,monotonic_ns=time.monotonic_ns(),**fields)
        temporary=self.runtime/'stage.tmp'
        with temporary.open('w',encoding='utf-8') as handle:
            json.dump(record,handle);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,self.runtime/'stage.json')
        deadline=time.monotonic()+5
        while not (self.runtime/f'ack-{self.sequence}').exists():
            if (self.runtime/'stop').exists() or time.monotonic()>deadline:
                raise RuntimeError('calibration_boundary_not_acknowledged')
            time.sleep(.02)

    @contextmanager
    def phase(self,name):
        self.boundary(name,'begin')
        start=time.monotonic_ns()
        yield
        self.boundary(name,'end',work_elapsed_ns=time.monotonic_ns()-start)


async def collect(root, phases):
    # Imports intentionally occur inside the measured phase after all guards.
    with phases.phase('python_application_imports'):
        import yaml
        from open_llm_vtuber.config_manager.utils import Config
        from open_llm_vtuber.service_context import ServiceContext
        from open_llm_vtuber.server import WebSocketServer
        from open_llm_vtuber.websocket_handler import WebSocketHandler
        from open_llm_vtuber.agent.agent_factory import AgentFactory
        from open_llm_vtuber.startup_status import conversation_status
        from tools.text_only_trial.preparation import settings

    with phases.phase('configuration'):
        data=yaml.safe_load((root/'config_templates/conf.default.yaml').read_text(encoding='utf-8'))
        # Public fixture only: no production files, env substitution or secrets.
        config=Config(**settings(data,12394))
        config.system_config.tool_prompts={}

    factory=AgentFactory.create_agent
    def measured_agent(*args,**kwargs):
        with phases.phase('agent_construction'):return factory(*args,**kwargs)
    AgentFactory.create_agent=staticmethod(measured_agent)
    context=ServiceContext();session=None
    try:
        with phases.phase('context_initialization'):
            await context.load_from_config(config)
        if any(getattr(context,name) is not None for name in
               ('asr_engine','tts_engine','vad_engine','live2d_model','mcp_client','tool_adapter')):
            raise RuntimeError('forbidden_engine_constructed')
        with phases.phase('asgi_construction'):
            server=WebSocketServer(config,default_context_cache=context)
        with phases.phase('one_inprocess_session'):
            handler=WebSocketHandler(context)
            async def discard(_):pass
            session=await handler._init_service_context(discard,'calibration-session')
            status=conversation_status(session)
            if (status['text_model_ready'] or status['asr_ready'] or status['required_mcp_ready']
                    or status['text_requests_enabled'] or status['audio_enabled']):
                raise RuntimeError('calibration_readiness_invalid')
        phases.boundary('calibration_complete','complete')
    finally:
        if session is not None:await session.close()
        await context.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',required=True)
    parser.add_argument('--approval-sha256',required=True);args=parser.parse_args()
    runtime=Path(args.runtime).resolve();root=Path(__file__).resolve().parents[2]
    sys.path[:0]=[str(root),str(root/'src')]
    os.chdir(runtime)
    # Parent creates the marker only after suspended creation and Job assignment.
    timeout=time.monotonic()+5
    while not (runtime/'owned.start').exists():
        if time.monotonic()>timeout:raise SystemExit('ownership_marker_missing')
        time.sleep(.02)
    raw=(runtime/'approval.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.approval_sha256:raise SystemExit('approval_hash_mismatch')
    marker=json.loads((runtime/'owned.start').read_text(encoding='utf-8'))
    from tools.text_calibration.windows_limits import assert_current_job
    assert_current_job(marker['job_name'])
    from tools.text_calibration.policy import Approval
    # The worker rechecks scope/expiry; source cleanliness was pinned by supervisor.
    Approval.parse(json.loads(raw),source_sha=marker['source_sha'])
    from tools.model_free_trial.guard import NativeModels,audit
    from tools.readiness_trial.guard import install
    install(runtime,port=None,ollama=False) # no listener and no allowed child argv
    sys.addaudithook(audit);sys.meta_path.insert(0,NativeModels())
    from loguru import logger
    logger.remove()
    import asyncio
    phases=Phases(runtime)
    with phases.phase('python_baseline'):pass
    try:asyncio.run(collect(root,phases))
    except BaseException as exc:
        (runtime/'candidate-error.json').write_text(json.dumps({'error_type':type(exc).__name__}),encoding='utf-8')
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
