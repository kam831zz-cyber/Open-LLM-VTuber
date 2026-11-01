"""Fail-closed trial restrictions; policy methods can be tested without hooks."""
import importlib.abc
import json
from pathlib import Path
import sys


class NativeModels(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'faster_whisper','ctranslate2','torch','onnxruntime','av'}:
            raise ImportError('model_free_native_model_import_forbidden')


def audit(event, args):
    if event in ('subprocess.Popen','os.system'):
        raise PermissionError('model_free_child_process_forbidden')
    if event == 'socket.connect':
        frame = sys._getframe()
        while frame:
            if frame.f_code.co_name == 'socketpair' and Path(frame.f_code.co_filename).name == 'socket.py':
                return
            frame = frame.f_back
        raise PermissionError('model_free_outbound_connection_forbidden')


def install(runtime, port):
    # Reuse existing write/loopback bind containment; add a stricter child rule.
    from tools.readiness_trial.guard import install as install_containment
    install_containment(runtime, port=port, ollama=False)
    sys.addaudithook(audit)
    sys.meta_path.insert(0, NativeModels())


class ReadinessOnly:
    """ASGI barrier: no API side effects or conversation messages in first trial."""
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            if scope.get('method') != 'GET' or scope.get('path') != '/openapi.json':
                await send({'type':'http.response.start','status':403,'headers':[]})
                await send({'type':'http.response.body','body':b'trial_readiness_only'})
                return
        if scope['type'] == 'websocket':
            if scope.get('path') != '/client-ws':
                await send({'type':'websocket.close','code':1008}); return
            async def limited_receive():
                event = await receive()
                if event['type'] == 'websocket.receive':
                    # No text, microphone, config switch, tools or generation events.
                    await send({'type':'websocket.close','code':1008})
                    return {'type':'websocket.disconnect','code':1008}
                return event
            await self.app(scope, limited_receive, send)
            return
        await self.app(scope, receive, send)
