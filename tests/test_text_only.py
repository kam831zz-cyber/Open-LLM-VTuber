"""Text-first candidate; all SDKs, models, memory readings and sends are Mock."""
import asyncio
import json
from types import SimpleNamespace as S
import unittest
from unittest.mock import MagicMock, AsyncMock
from test_startup_isolation import isolated_class
from open_llm_vtuber.model_loading_policy import State, Reading, Receipt, Budget, GIB, AdmissionDenied
from open_llm_vtuber.staged_loading import StagedController, Occupancy
from open_llm_vtuber.startup_status import conversation_status
from open_llm_vtuber.text_conversation import run_text_turn, init_text_routes
from tools.model_free_trial.candidate import isolated_settings
from tools.model_free_trial.preflight import ServerBudget, TEXT_PROFILE, admit_server, Observation
import test_model_free_preflight as preflight_fixtures


class Stream:
    def __init__(self, chunks):self.chunks=chunks;self.close=AsyncMock()
    async def __aiter__(self):
        for text,finish in self.chunks:
            yield S(choices=[S(delta=S(content=text,tool_calls=None),finish_reason=finish)])


class TextOnlyTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, *, ready=True, enabled=True):
        c=StagedController(S(text_only=True,asr_mode='disabled',eager_preload=False,
                            audio_enabled=False,text_requests_enabled=enabled),
            offline=True,clock=lambda:0,read_memory=lambda:Reading(6*GIB,6*GIB,0),
            occupancy=lambda:Occupancy(0,True,('fixture',)),
            budgets={'text_request':Budget(GIB,GIB,'fixture')})
        if ready:
            c.runtime.models['llm']=S(model_id='fixture',remote=True)
            c.runtime.tokens['llm']=1;c.runtime.started_at['llm']=0
            c.runtime.states['llm']=State.READY
            c.runtime.values['llm']=Receipt('llm','fixture',1,0,True,True)
        return c

    def llm(self, controller, stream):
        class Base:
            def __init__(self,**fields):self.__dict__.update(fields)
        self.requests=MagicMock()
        cls=isolated_class('src/open_llm_vtuber/agent/stateless_llm/ollama_llm.py',
            'OllamaLLM',AsyncLLM=Base,requests=self.requests,atexit=MagicMock(),
            __package__='open_llm_vtuber.agent.stateless_llm',__name__='open_llm_vtuber.agent.stateless_llm.ollama_llm')
        client=cls(model='fixture',base_url='http://fixture.invalid/v1',staged_controller=controller)
        self.sdk=S(chat=S(completions=S(create=AsyncMock(return_value=stream))))
        client.client=S(with_options=MagicMock(return_value=self.sdk))
        return client

    def agent(self,llm):
        cls=isolated_class('src/open_llm_vtuber/agent/agents/basic_memory_agent.py','BasicMemoryAgent',AgentInterface=object)
        agent=cls.__new__(cls);agent._memory=[];agent._system='fixture';agent._llm=llm
        return agent

    async def test_tts_factory_is_not_needed(self):
        cls=isolated_class('src/open_llm_vtuber/service_context.py','ServiceContext')
        ctx=cls();ctx.staged_controller=self.controller(ready=False)
        ctx.init_tts(S(tts_model='not-installed'))
        self.assertIsNone(ctx.tts_engine)

    async def test_audio_disabled_readiness_constructed_without_tts_but_not_model_ready(self):
        ctx=S(staged_controller=self.controller(ready=False),agent_engine=object(),tts_engine=None,asr_engine=None)
        status=conversation_status(ctx)
        self.assertTrue(status['conversation_core_constructed'])
        self.assertFalse(status['conversation_core_ready']);self.assertFalse(status['text_conversation_ready'])
        self.assertEqual(status['tts_audio'],'disabled');self.assertEqual(status['audio_mode'],'disabled')

    async def test_text_ready_does_not_claim_real_conversation_test(self):
        ctx=S(staged_controller=self.controller(),agent_engine=object(),tts_engine=None,asr_engine=None)
        status=conversation_status(ctx)
        self.assertTrue(status['text_conversation_ready'])
        self.assertFalse(status['conversation_verified']);self.assertEqual(status['llm_response'],'not_tested')

    async def test_pure_text_profile_omits_voicevox_requirement(self):
        source=preflight_fixtures.original();source['character_config']['tts_config']['tts_model']='not-installed'
        config=isolated_settings(source,12394,audio_enabled=False)
        self.assertFalse(config['system_config']['staged_loading']['audio_enabled'])
        self.assertFalse(config['system_config']['staged_loading']['text_requests_enabled'])
        self.assertEqual(source['system_config'],{'default_character_config':'fixture.yaml'})

    async def test_voicevox_budget_cannot_admit_text_profile(self):
        budget=ServerBudget(GIB,GIB,'fixture','fixture')
        with self.assertRaises(AdmissionDenied):
            admit_server(budget,Observation(Reading(6*GIB,6*GIB,0),(),0),clock=lambda:0,
                         offline=True,profile=TEXT_PROFILE)

    async def test_invalid_audio_disabled_eager_mode_rejected(self):
        with self.assertRaises(ValueError):
            StagedController(S(text_only=True,asr_mode='disabled',eager_preload=True,audio_enabled=False))

    async def test_text_sdk_request_has_no_audio_tools_or_retry(self):
        stream=Stream([('hello','stop')]);c=self.controller();llm=self.llm(c,stream)
        self.assertEqual([x async for x in llm.text_completion([], 'fixture')],['hello'])
        llm.client.with_options.assert_called_once_with(max_retries=0,timeout=30)
        kwargs=self.sdk.chat.completions.create.call_args.kwargs
        self.assertNotIn('tools',kwargs);self.assertNotIn('audio',kwargs)
        self.assertEqual(kwargs['model'],'fixture');stream.close.assert_awaited_once()
        llm.cleanup();self.requests.post.assert_not_called()

    async def test_text_model_unloaded_sends_no_sdk_request(self):
        c=self.controller(ready=False);llm=self.llm(c,Stream([]))
        with self.assertRaises(AdmissionDenied):await anext(llm.text_completion([],''))
        self.sdk.chat.completions.create.assert_not_awaited()

    async def test_text_requests_disabled_sends_no_sdk_request(self):
        llm=self.llm(self.controller(enabled=False),Stream([]))
        with self.assertRaises(AdmissionDenied):await anext(llm.text_completion([],''))
        self.sdk.chat.completions.create.assert_not_awaited()

    async def test_inference_budget_unknown_refuses(self):
        c=self.controller();c.runtime.budgets={};llm=self.llm(c,Stream([]))
        with self.assertRaises(AdmissionDenied):await anext(llm.text_completion([],''))
        self.sdk.chat.completions.create.assert_not_awaited()

    async def test_low_ram_refuses_inference_without_request(self):
        c=self.controller();c.runtime.read_memory=lambda:Reading(GIB,6*GIB,0)
        llm=self.llm(c,Stream([]))
        with self.assertRaises(AdmissionDenied):await anext(llm.text_completion([],''))
        self.sdk.chat.completions.create.assert_not_awaited()

    async def test_busy_shared_llm_is_not_queued(self):
        c=self.controller();c.text_inflight=True;llm=self.llm(c,Stream([]))
        with self.assertRaisesRegex(AdmissionDenied,'busy'):await anext(llm.text_completion([],''))

    async def test_connection_failure_latches_uncertain_completion(self):
        c=self.controller();llm=self.llm(c,Stream([]))
        self.sdk.chat.completions.create.side_effect=ConnectionError('private fixture')
        with self.assertRaises(ConnectionError):await anext(llm.text_completion([],''))
        self.assertEqual(c.runtime.uncertain,'llm');self.assertEqual(c.runtime.states['llm'],State.PENDING)
        self.assertFalse(c.text_inflight)

    async def test_truncated_stream_is_not_success(self):
        c=self.controller();stream=Stream([('partial',None)]);llm=self.llm(c,stream)
        with self.assertRaises(RuntimeError):[x async for x in llm.text_completion([],'')]
        self.assertEqual(c.runtime.states['llm'],State.PENDING);stream.close.assert_awaited_once()

    async def test_agent_text_chat_preserves_session_memory_without_prefetch(self):
        llm=self.llm(self.controller(),Stream([('hello','stop')]))
        agent=self.agent(llm)
        self.assertEqual([x async for x in agent.text_chat('fixture')],['hello'])
        self.assertEqual([m['role'] for m in agent._memory],['user','assistant'])

    async def test_failed_agent_text_chat_does_not_commit_memory(self):
        llm=self.llm(self.controller(),Stream([('partial',None)]));agent=self.agent(llm)
        with self.assertRaises(RuntimeError):[x async for x in agent.text_chat('fixture')]
        self.assertEqual(agent._memory,[])

    async def test_text_turn_has_no_audio_events_or_ack_wait(self):
        c=self.controller();llm=self.llm(c,Stream([('hello','stop')]))
        ctx=S(staged_controller=c,agent_engine=self.agent(llm));send=AsyncMock()
        result=await run_text_turn(ctx,'fixture',send)
        self.assertTrue(result['ok']);self.assertEqual(result['audio'],'disabled')
        events=[json.loads(call.args[0]) for call in send.await_args_list]
        self.assertTrue(any(e['type']=='full-text' for e in events))
        self.assertFalse(any(e['type'] in ('audio','backend-synth-complete') for e in events))
        self.assertFalse(ctx._text_turn_active)

    async def test_not_ready_turn_rejects_before_agent(self):
        ctx=S(staged_controller=self.controller(ready=False),agent_engine=S(text_chat=MagicMock()))
        result=await run_text_turn(ctx,'fixture',AsyncMock())
        self.assertEqual(result['code'],'text_not_ready');ctx.agent_engine.text_chat.assert_not_called()

    async def test_invalid_text_does_not_start_agent(self):
        ctx=S(staged_controller=self.controller(),agent_engine=S(text_chat=MagicMock()))
        for text in (None,'',' '*2,'x'*4097):
            self.assertFalse((await run_text_turn(ctx,text,AsyncMock()))['ok'])
        ctx.agent_engine.text_chat.assert_not_called()

    async def test_http_status_and_disabled_post_without_socket(self):
        ctx=S(staged_controller=self.controller(ready=False,enabled=False),agent_engine=object(),tts_engine=None,asr_engine=None)
        routes=init_text_routes(ctx).routes
        status=await next(r.endpoint for r in routes if r.path=='/api/text/status')()
        self.assertFalse(status['text_model_ready'])
        response=await next(r.endpoint for r in routes if r.path=='/api/text/chat')(S())
        self.assertEqual(response.status_code,503)

    async def test_http_cross_origin_rejected_before_agent(self):
        ctx=S(staged_controller=self.controller(),agent_engine=S(for_session=MagicMock()))
        route=next(r.endpoint for r in init_text_routes(ctx).routes if r.path=='/api/text/chat')
        response=await route(S(client=S(host='127.0.0.1'),headers={'origin':'http://untrusted.invalid'},
                               base_url='http://127.0.0.1:12394/'))
        self.assertEqual(response.status_code,403);ctx.agent_engine.for_session.assert_not_called()

    async def test_text_server_requires_no_static_frontend_or_audio_routes(self):
        app=MagicMock();webtool=MagicMock(side_effect=AssertionError('audio routes registered'))
        static=MagicMock(side_effect=AssertionError('static asset dependency'))
        cls=isolated_class('src/open_llm_vtuber/server.py','WebSocketServer',
            FastAPI=MagicMock(return_value=app),ServiceContext=lambda:S(),
            CORSMiddleware=object,init_client_ws_route=MagicMock(return_value='client-ws'),
            init_webtool_routes=webtool,CORSStaticFiles=static,
            __package__='open_llm_vtuber',__name__='open_llm_vtuber.server')
        server=cls(S(system_config=S(staged_loading=S(enabled=True,audio_enabled=False),enable_proxy=True)))
        self.assertIs(server.app,app);webtool.assert_not_called();static.assert_not_called()
        app.mount.assert_not_called()

    async def test_text_mode_rejects_audio_group_and_config_events(self):
        cls=isolated_class('src/open_llm_vtuber/websocket_handler.py','WebSocketHandler')
        h=cls.__new__(cls);h.client_contexts={'fixture':S(staged_controller=self.controller())}
        h._message_handlers={'ai-speak-signal':AsyncMock()};ws=S(send_text=AsyncMock())
        for event in ('ai-speak-signal','raw-audio-data','switch-config','add-client-to-group'):
            await h._route_message(ws,'fixture',{'type':event})
        h._message_handlers['ai-speak-signal'].assert_not_awaited()

    async def test_request_cancellation_blocks_later_model_operations(self):
        c=self.controller();llm=self.llm(c,Stream([]));entered=asyncio.Event()
        async def waiting(**kwargs):entered.set();await asyncio.Event().wait()
        self.sdk.chat.completions.create.side_effect=waiting
        task=asyncio.create_task(anext(llm.text_completion([],'')))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(c.runtime.uncertain,'llm');self.assertFalse(c.text_inflight)

    async def test_http_chat_uses_ephemeral_agent_and_mock_text_only(self):
        c=self.controller();llm=self.llm(c,Stream([('hello','stop')]))
        child=self.agent(llm);default=S(for_session=MagicMock(return_value=child))
        ctx=S(staged_controller=c,agent_engine=default,system_config=S(host='127.0.0.1',port=12394))
        async def body():yield b'{"text":"fixture"}'
        request=S(client=S(host='127.0.0.1'),headers={'origin':'http://127.0.0.1:12394'},
                  base_url='http://127.0.0.1:12394/',stream=body)
        route=next(r.endpoint for r in init_text_routes(ctx).routes if r.path=='/api/text/chat')
        result=await route(request)
        self.assertEqual(result.status_code,200);self.assertEqual(json.loads(result.body)['text'],'hello')
        default.for_session.assert_called_once_with(None,None,'')

    async def test_text_preparation_defaults_are_unmeasured_and_audio_free(self):
        from tools.text_only_trial.preparation import settings, unmeasured_budget
        config=settings(preflight_fixtures.original(),12394)
        self.assertFalse(config['system_config']['staged_loading']['audio_enabled'])
        budget=unmeasured_budget()
        self.assertEqual(budget.profile,TEXT_PROFILE);self.assertEqual(budget.evidence,'unknown')

    async def test_first_trial_allows_text_status_but_not_chat_post(self):
        from tools.model_free_trial.guard import ReadinessOnly
        app=AsyncMock();guard=ReadinessOnly(app,text_status=True);send=AsyncMock()
        await guard({'type':'http','method':'GET','path':'/api/text/status'},AsyncMock(),send)
        app.assert_awaited_once();app.reset_mock()
        await guard({'type':'http','method':'POST','path':'/api/text/chat'},AsyncMock(),send)
        app.assert_not_awaited()

    async def test_origin_is_checked_against_configuration_not_host_header(self):
        from open_llm_vtuber.text_conversation import text_origin_allowed
        ctx=S(system_config=S(host='127.0.0.1',port=12394))
        self.assertTrue(text_origin_allowed(ctx,'127.0.0.1','http://127.0.0.1:12394'))
        self.assertFalse(text_origin_allowed(ctx,'127.0.0.1','http://untrusted.invalid'))
        self.assertFalse(text_origin_allowed(ctx,'192.0.2.1','http://127.0.0.1:12394'))
