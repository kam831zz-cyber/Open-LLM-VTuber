"""Explicit text turns; no audio, tools, background jobs, or content logging."""
import asyncio
from contextlib import aclosing
import json
from .model_loading_policy import AdmissionDenied


def text_origin_allowed(context, host, origin):
    system = getattr(context, 'system_config', None)
    port = getattr(system, 'port', None)
    return (host in ('127.0.0.1','::1') and type(port) is int
            and getattr(system, 'host', None) == '127.0.0.1'
            and origin == f'http://127.0.0.1:{port}')


async def run_text_turn(context, text, send):
    async def emit(event): await send(json.dumps(event, ensure_ascii=False))
    controller = getattr(context, 'staged_controller', None)
    if (controller is None or controller.audio_enabled or not controller.text_requests_enabled):
        await emit({'type':'error','code':'text_requests_disabled'})
        return {'ok':False,'code':'text_requests_disabled'}
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        await emit({'type':'error','code':'invalid_text'})
        return {'ok':False,'code':'invalid_text'}
    if getattr(context, '_text_turn_active', False):
        await emit({'type':'error','code':'text_session_busy'})
        return {'ok':False,'code':'text_session_busy'}
    context._text_turn_active = True
    answer = ''
    try:
        # Reject before agent/memory/transport if the model is not verified ready.
        controller.require_llm_ready()
        await emit({'type':'control','text':'conversation-chain-start'})
        async with asyncio.timeout(60):
            async with aclosing(context.agent_engine.text_chat(text)) as stream:
                async for chunk in stream:
                    if not isinstance(chunk,str): raise RuntimeError('invalid_text_chunk')
                    answer += chunk
                    if len(answer) > 16384: raise RuntimeError('text_response_too_large')
                    await emit({'type':'text-delta','text':chunk})
        if not answer.strip(): raise RuntimeError('empty_text_response')
        await emit({'type':'full-text','text':answer})
        return {'ok':True,'text':answer,'llm_response':'verified','audio':'disabled'}
    except AdmissionDenied:
        await emit({'type':'error','code':'text_not_ready'})
        return {'ok':False,'code':'text_not_ready'}
    except asyncio.CancelledError:
        raise
    except Exception:
        # Do not expose raw exceptions, request bodies, URLs or auth fields.
        await emit({'type':'error','code':'text_generation_failed'})
        return {'ok':False,'code':'text_generation_failed'}
    finally:
        context._text_turn_active = False
        await emit({'type':'model-status', **controller.status()})
        await emit({'type':'control','text':'conversation-chain-end'})


def init_text_routes(default_context):
    """Loopback/same-origin HTTP alternative; default opt-in remains disabled."""
    from copy import copy
    from fastapi import APIRouter, Request
    from starlette.responses import JSONResponse
    from .startup_status import conversation_status

    router=APIRouter()

    @router.get('/api/text/status')
    async def status():
        return conversation_status(default_context)

    @router.post('/api/text/chat')
    async def chat(request: Request):
        controller=getattr(default_context,'staged_controller',None)
        if controller is None or controller.audio_enabled or not controller.text_requests_enabled:
            return JSONResponse({'ok':False,'code':'text_requests_disabled'},status_code=503)
        if request.client is None or not text_origin_allowed(
                default_context, request.client.host, request.headers.get('origin')):
            return JSONResponse({'ok':False,'code':'same_origin_required'},status_code=403)
        body=b''
        async for chunk in request.stream():
            body+=chunk
            if len(body)>8192:
                return JSONResponse({'ok':False,'code':'request_too_large'},status_code=413)
        try:
            payload=json.loads(body)
            if not isinstance(payload,dict) or set(payload)!={'text'}:raise ValueError()
        except (ValueError,UnicodeError):
            return JSONResponse({'ok':False,'code':'invalid_text'},status_code=400)
        context=copy(default_context)
        context._text_turn_active=False
        # HTTP has no persistent session/history yet; never share default memory.
        context.agent_engine=default_context.agent_engine.for_session(None,None,'')
        async def discard(event): pass
        result=await run_text_turn(context,payload['text'],discard)
        return JSONResponse(result,status_code=200 if result['ok'] else 503)

    return router
