"""One isolated backend; read production config in memory, never run run_server."""
import argparse
import asyncio
import builtins
import json
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--runtime', required=True)
parser.add_argument('--production', required=True)
parser.add_argument('--port', type=int, required=True)
args = parser.parse_args()
runtime = Path(args.runtime).resolve()
root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root), str(root / 'src'), str(Path(__file__).parent)]
os.chdir(runtime)
while not (runtime / 'owned.start').exists():
    time.sleep(.05)
started = time.monotonic()


def emit(stage, **fields):
    record = {'stage': stage, 'elapsed_s': round(time.monotonic()-started, 3),
              'monotonic_ns':time.monotonic_ns(), **fields}
    with (runtime / 'stages.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=True) + '\n')


from guard import install
install(runtime, port=args.port, ollama=True)
from loguru import logger
logger.remove()  # Never serialize application config, prompts or exception locals.


async def main():
    emit('python_native_import_begin')
    from src.open_llm_vtuber.server import WebSocketServer
    from src.open_llm_vtuber.config_manager import read_yaml, validate_config
    from src.open_llm_vtuber.service_context import deep_merge
    from src.open_llm_vtuber.mcpp.server_registry import ServerRegistry
    from src.open_llm_vtuber.mcpp.tool_adapter import ToolAdapter
    from src.open_llm_vtuber.startup_status import conversation_status
    import src.open_llm_vtuber.asr.faster_whisper_asr as asr
    import requests
    import uvicorn
    emit('python_native_import_complete')

    data = read_yaml(str(Path(args.production) / 'conf.yaml'))
    selected = data['system_config'].get('default_character_config')
    if selected and selected != 'conf.yaml':
        assert Path(selected).name == selected
        alt = read_yaml(str(Path(args.production) / 'characters' / selected))
        data['character_config'] = deep_merge(data['character_config'], alt['character_config'])
    data['system_config'].update(host='127.0.0.1', port=args.port,
                                default_character_config='', config_alts_dir=str(root/'characters'), enable_proxy=False)
    config = validate_config(data)
    fw = config.character_config.asr_config.faster_whisper
    fw.download_root = str(Path(args.production) / 'models' / 'whisper')
    assert config.character_config.asr_config.asr_model == 'faster_whisper'
    assert fw.model_path == 'small'
    ollama = config.character_config.agent_config.llm_configs.ollama_llm
    assert ollama.model == 'qwen2.5:3b'
    assert ollama.base_url.rstrip('/') in ('http://localhost:11434/v1', 'http://127.0.0.1:11434/v1')
    # Finite candidate-only lease, no unload of a service-owned model on teardown.
    ollama.base_url = 'http://127.0.0.1:11434/v1'
    ollama.keep_alive = 60
    ollama.unload_at_exit = False
    emit('configuration_validated', asr='small', llm='qwen2.5:3b', model_lease_seconds=60)

    original_request = requests.sessions.Session.request
    preloads = 0
    def guarded_request(session, method, url, **kw):
        nonlocal preloads
        payload = kw.get('json', {})
        if not (method.upper() == 'POST' and url == 'http://127.0.0.1:11434/api/chat'
                and set(payload) == {'model', 'keep_alive'} and payload['model'] == 'qwen2.5:3b'
                and payload['keep_alive'] == 60 and preloads == 0):
            raise PermissionError('trial_http_request_forbidden')
        preloads += 1
        emit('ollama_preload_begin')
        response = original_request(session, method, url, **kw)
        emit('ollama_preload_complete', status=response.status_code)
        response.raise_for_status()
        return response
    requests.sessions.Session.request = guarded_request

    resolve = asr.resolve_local_model
    def timed_resolve(*a, **kw):
        emit('asr_file_resolution_begin')
        value = resolve(*a, **kw)
        emit('asr_file_resolution_complete')
        return value
    asr.resolve_local_model = timed_resolve
    asr_init=asr.VoiceRecognition.__init__
    def measured_asr_init(self,*a,**kw):
        asr_init(self,*a,**kw)
        # Persist immediately: a later blocking Ollama preload must not hide ASR timings.
        emit('asr_model_construct_complete',**self.initialization_metrics)
    asr.VoiceRecognition.__init__=measured_asr_init
    original_import = builtins.__import__
    def timed_import(name, *a, **kw):
        if name == 'faster_whisper':
            emit('asr_native_import_begin')
            value = original_import(name, *a, **kw)
            emit('asr_native_import_complete')
            return value
        return original_import(name, *a, **kw)
    builtins.__import__ = timed_import

    registry_init = ServerRegistry.__init__
    def trial_registry(self, *a, **kw):
        registry_init(self, root / 'mcp_servers.json')
        for name, server in self.servers.items():
            if name == 'ddg-search':
                kind, target = 'ddg', '-'
            else:
                assert name in ('weather', 'komugi-context', 'komugi-weather-ai')
                assert len(server.args) == 1
                kind, target = 'script', str((root / server.args[0]).resolve())
                assert Path(target).is_relative_to(root / 'scripts')
            server.command = sys.executable
            server.args = ['-I', '-B', str(Path(__file__).with_name('guard.py')), '--child', str(runtime), kind, target]
            server.cwd = str(runtime)
    ServerRegistry.__init__ = trial_registry
    fetch = ToolAdapter.get_server_and_tool_info
    discoveries = 0
    async def timed_fetch(self, *a, **kw):
        nonlocal discoveries
        discoveries += 1
        emit('mcp_discovery_begin', count=discoveries)
        result = await fetch(self, *a, **kw)
        emit('mcp_discovery_complete', count=discoveries, availability=self.availability)
        return result
    ToolAdapter.get_server_and_tool_info = timed_fetch
    server = WebSocketServer(config)
    await server.initialize()
    ctx = server.default_context_cache
    emit('asr_metrics', **ctx.asr_engine.initialization_metrics)
    emit('context_initialized', **conversation_status(ctx))
    runner = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=args.port,
                                          log_config=None, access_log=False))
    async def stop_watch():
        while not (runtime / 'stop').exists():
            await asyncio.sleep(.2)
        runner.should_exit = True
    watcher = asyncio.create_task(stop_watch())
    try:
        emit('uvicorn_begin')
        await runner.serve()
    finally:
        watcher.cancel()
        await ctx.close()
        emit('candidate_stopped', discovery_count=discoveries, preloads=preloads)


try:
    asyncio.run(main())
except BaseException as exc:
    # Class and source location only; never exception text/config/locals.
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    emit('candidate_error', error_type=type(exc).__name__,
         locations=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in frames[-8:]])
    raise SystemExit(1)
