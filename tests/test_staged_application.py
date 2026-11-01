"""Actual application methods with injected engines/transports; no server startup."""
import asyncio
import json
from types import SimpleNamespace as S
import unittest
from unittest.mock import AsyncMock, MagicMock
from test_startup_isolation import isolated_class
from open_llm_vtuber.staged_loading import StagedController, Occupancy
from open_llm_vtuber.model_loading_policy import AdmissionDenied, Budget, Reading, Receipt, State, GIB
from open_llm_vtuber.startup_status import conversation_status


def settings(**kw):
    return S(**dict(dict(enabled=True, text_only=True, asr_mode='disabled', eager_preload=False), **kw))


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, **kw):
        return StagedController(settings(**kw))

    def ollama(self, **kw):
        class Base:
            def __init__(self, **config): self.__dict__.update(config)
            async def chat_completion(self, *args, **kwargs):
                yield 'mock'
        self.requests = MagicMock()
        cls = isolated_class('src/open_llm_vtuber/agent/stateless_llm/ollama_llm.py',
                             'OllamaLLM', AsyncLLM=Base, requests=self.requests, atexit=MagicMock())
        return cls(model='fixture-model', base_url='http://fixture.invalid/v1', **kw)

    async def test_construct_client_without_preload_or_cleanup_request(self):
        client = self.ollama(eager_preload=False)
        client.cleanup(); client.cleanup()
        self.requests.post.assert_not_called()

    async def test_staged_constructor_overrides_legacy_eager_and_unload(self):
        c = self.controller(); client = self.ollama(staged_controller=c)
        self.assertIs(c.runtime.client, client)
        client.cleanup(); self.requests.post.assert_not_called()

    async def test_unloaded_chat_does_not_implicitly_load_model(self):
        client = self.ollama(staged_controller=self.controller())
        with self.assertRaisesRegex(AdmissionDenied, 'not_ready'):
            await anext(client.chat_completion([]))
        self.requests.post.assert_not_called()

    async def test_direct_preload_cannot_bypass_controller(self):
        client = self.ollama(staged_controller=self.controller())
        with self.assertRaisesRegex(RuntimeError, 'verified_adapter'): client.preload_model()
        self.requests.post.assert_not_called()

    async def test_disabled_and_lazy_asr_never_call_factory(self):
        for mode in ('disabled', 'lazy'):
            c = self.controller(text_only=False, asr_mode=mode)
            context = S(asr_engine=object())
            await c.prepare_asr(context, S(asr_model='fixture-asr'))
            self.assertIsNone(context.asr_engine)
            self.assertFalse(c.mic_ready())

    async def test_text_only_overrides_eager_asr(self):
        c = self.controller(asr_mode='eager')
        await c.prepare_asr(S(), S(asr_model='fixture-asr'))
        self.assertEqual(c.runtime.states['asr'], State.UNLOADED)

    async def test_eager_without_ownership_proof_refused(self):
        c = self.controller(text_only=False, asr_mode='eager')
        with self.assertRaisesRegex(AdmissionDenied, 'ownership'):
            await c.prepare_asr(S(), S(asr_model='fixture-asr'))

    async def test_unknown_budget_refused_even_with_exclusive_evidence(self):
        c = self.controller(); c.occupancy = lambda: Occupancy(c.runtime.clock(), True)
        with self.assertRaisesRegex(AdmissionDenied, 'budget_unknown'): await c.request_model('llm')

    async def test_external_model_occupation_refused(self):
        c = self.controller(); c.occupancy = lambda: Occupancy(c.runtime.clock(), True, ('other-model',))
        with self.assertRaisesRegex(AdmissionDenied, 'ownership'): await c.request_model('asr')

    async def test_stale_ownership_refused(self):
        c = self.controller(); c.occupancy = lambda: Occupancy(c.runtime.clock()-3, True)
        with self.assertRaises(AdmissionDenied): await c.request_model('llm')

    async def test_production_adapter_rejects_fixture_budget(self):
        c = self.controller(); c.occupancy = lambda: Occupancy(c.runtime.clock(), True)
        c.runtime.budgets['llm'] = Budget(GIB, GIB, 'fixture')
        with self.assertRaises(AdmissionDenied): await c.request_model('llm')

    async def test_real_loader_is_not_registered_by_default(self):
        c = self.controller(); c.occupancy = lambda: Occupancy(c.runtime.clock(), True)
        c.runtime.budgets['llm'] = Budget(GIB, GIB, 'measured')
        c.runtime.read_memory = lambda: Reading(6*GIB, 6*GIB, c.runtime.clock())
        with self.assertRaisesRegex(AdmissionDenied, 'not_registered'): await c.request_model('llm')

    async def test_cannot_change_mode_in_live_context(self):
        c = self.controller()
        with self.assertRaises(AdmissionDenied): c.validate_mode(settings(text_only=False))

    async def test_actual_context_model_free_initialization(self):
        cls = isolated_class('src/open_llm_vtuber/service_context.py', 'ServiceContext',
                             __package__='open_llm_vtuber', __name__='open_llm_vtuber.service_context')
        ctx = cls()
        for name in ('init_live2d', 'init_tts', 'init_translate', 'init_komugi_fast_path'):
            setattr(ctx, name, MagicMock())
        ctx.init_asr = MagicMock(side_effect=AssertionError('ASR/native factory called'))
        ctx.init_vad = MagicMock(side_effect=AssertionError('VAD/native factory called'))
        ctx._init_mcp_components = AsyncMock()
        ctx.init_agent = AsyncMock()
        char = S(live2d_model_name='fixture', asr_config=S(asr_model='faster_whisper'),
                 tts_config=S(), vad_config=S(), persona_prompt='',
                 agent_config=S(agent_settings=S(basic_memory_agent=S(use_mcpp=False, mcp_enabled_servers=[]))),
                 tts_preprocessor_config=S(translator_config=S()))
        await ctx.load_from_config(S(system_config=S(staged_loading=settings()), character_config=char))
        ctx.init_asr.assert_not_called(); ctx.init_vad.assert_not_called()
        ctx.init_agent.assert_awaited_once()
        self.assertEqual(ctx.staged_controller.status()['states'], {'llm':'unloaded','asr':'unloaded'})

    async def test_actual_websocket_omits_mic_keeps_config_events(self):
        cls = isolated_class('src/open_llm_vtuber/websocket_handler.py', 'WebSocketHandler',
                             conversation_status=conversation_status)
        h = cls.__new__(cls); h.send_group_update = AsyncMock()
        ws = S(send_text=AsyncMock())
        ctx = S(staged_controller=self.controller(), asr_engine=None, tts_engine=object(), agent_engine=object(),
                live2d_model=S(model_info={}), character_config=S(conf_name='fixture',conf_uid='fixture'))
        await h._send_initial_messages(ws, 'fixture', ctx)
        messages = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
        self.assertFalse(any(m.get('text') == 'start-mic' for m in messages))
        self.assertTrue(any(m['type']=='set-model-and-conf' for m in messages))
        self.assertFalse(messages[0]['conversation_core_ready'])
        self.assertFalse(messages[0]['required_mcp_ready'])
        self.assertFalse(messages[0]['life_information_available'])

    async def test_context_session_retains_shared_controller_not_session_callbacks(self):
        from test_startup_isolation import AsyncStartupTests
        cls = AsyncStartupTests().context_class()
        c = self.controller(); ctx = cls(); ctx.init_komugi_fast_path = MagicMock()
        ctx._init_mcp_components = AsyncMock()
        agent = S(for_session=MagicMock(return_value=object()))
        char = S(agent_config=S(agent_settings=S(basic_memory_agent=S(use_mcpp=False,mcp_enabled_servers=[]))))
        await ctx.load_cache(object(), object(), char, None, None, None, None, agent, None,
                             send_text='callback', client_uid='session', staged_controller=c)
        self.assertIs(ctx.staged_controller, c); self.assertEqual(ctx.client_uid, 'session')
        await ctx.close()
        self.assertIs(ctx.staged_controller, c)

    async def test_offline_loader_uses_actual_gate(self):
        c = StagedController(settings(), offline=True, clock=lambda:0,
            occupancy=lambda:Occupancy(0,True), read_memory=lambda:Reading(6*GIB,6*GIB,0),
            budgets={'llm':Budget(GIB,GIB,'fixture')})
        async def load(model, token): return Receipt('llm',model.model_id,token,0,True,True)
        c.runtime.loaders['llm'] = AsyncMock(side_effect=load)
        self.assertEqual(await c.request_model('llm'), State.READY)
        c.runtime.loaders['llm'].assert_awaited_once()

    async def test_eager_preload_setting_is_admitted_not_constructor_side_effect(self):
        c = self.controller(eager_preload=True)
        client = self.ollama(staged_controller=c)
        self.requests.post.assert_not_called()
        with self.assertRaises(AdmissionDenied): await c.finish_startup()
        client.cleanup(); self.requests.post.assert_not_called()

    async def test_agent_factory_forces_nonpreloading_ollama(self):
        factory = MagicMock(); factory.create_llm.return_value = object()
        cls = isolated_class('src/open_llm_vtuber/agent/agent_factory.py', 'AgentFactory',
            StatelessLLMFactory=factory, BasicMemoryAgent=MagicMock(),
            _komugi_fast_path_base_url=lambda config:'http://fixture.invalid')
        c = self.controller()
        cls.create_agent('basic_memory_agent', {'basic_memory_agent':{'llm_provider':'ollama_llm'}},
                         {'ollama_llm':{'model':'fixture'}}, '', staged_controller=c)
        kwargs = factory.create_llm.call_args.kwargs
        self.assertIs(kwargs['staged_controller'], c)
        self.assertFalse(kwargs['eager_preload']); self.assertFalse(kwargs['unload_at_exit'])

    async def test_non_ollama_provider_rejected_in_staged_mode(self):
        cls = isolated_class('src/open_llm_vtuber/agent/agent_factory.py', 'AgentFactory')
        with self.assertRaisesRegex(ValueError,'provider_not_supported'):
            cls.create_agent('basic_memory_agent', {'basic_memory_agent':{'llm_provider':'other'}},
                {'other':{'model':'fixture'}}, '', staged_controller=self.controller())

    async def test_nonbasic_agent_rejected_before_initialization(self):
        cls = isolated_class('src/open_llm_vtuber/agent/agent_factory.py', 'AgentFactory')
        with self.assertRaisesRegex(ValueError,'agent_not_supported'):
            cls.create_agent('other', {}, {}, '', staged_controller=self.controller())

    async def test_direct_asr_factory_bypass_rejected(self):
        cls = isolated_class('src/open_llm_vtuber/service_context.py', 'ServiceContext')
        ctx = cls(); ctx.staged_controller = self.controller()
        with self.assertRaisesRegex(RuntimeError,'verified_adapter'): ctx.init_asr(S())

    async def test_voicevox_constructor_path_kept_without_generation(self):
        engine = object(); factory = S(get_tts_engine=MagicMock(return_value=engine))
        cls = isolated_class('src/open_llm_vtuber/service_context.py', 'ServiceContext', TTSFactory=factory)
        ctx = cls(); ctx.staged_controller = self.controller(); ctx.character_config = S(tts_config=None)
        config = S(tts_model='voicevox_tts', voicevox_tts=S(model_dump=lambda:{'speaker_id':8}))
        ctx.init_tts(config)
        factory.get_tts_engine.assert_called_once_with('voicevox_tts',speaker_id=8)
        self.assertIs(ctx.tts_engine,engine)

    async def test_unvalidated_native_tts_provider_rejected(self):
        cls = isolated_class('src/open_llm_vtuber/service_context.py', 'ServiceContext')
        ctx = cls(); ctx.staged_controller = self.controller()
        with self.assertRaisesRegex(RuntimeError,'provider_not_supported'):
            ctx.init_tts(S(tts_model='native-fixture'))

    async def test_settings_validate_modes_and_keep_legacy_default(self):
        from open_llm_vtuber.config_manager.system import StagedLoadingSettings
        self.assertFalse(StagedLoadingSettings().enabled)
        with self.assertRaises(ValueError): StagedLoadingSettings(asr_mode='invalid')

    async def test_ownership_loss_not_reported_as_ready(self):
        c = self.controller(); r = c.runtime; now = r.clock()
        r.tokens['llm']=1; r.started_at['llm']=now; r.states['llm']=State.READY
        r.values['llm']=Receipt('llm','unbound',1,now,True,True)
        self.assertFalse(c.status()['text_model_ready'])

    async def test_unrequested_audio_is_rejected_without_asr_or_buffering(self):
        cls = isolated_class('src/open_llm_vtuber/websocket_handler.py', 'WebSocketHandler')
        h = cls.__new__(cls); h.client_contexts={'fixture':S(staged_controller=self.controller())}
        for method, data in (('_handle_audio_data', {'audio':[1]}),
                             ('_handle_raw_audio_data', {'audio':[1]}),
                             ('_handle_conversation_trigger', {'type':'mic-audio-end'})):
            await getattr(h,method)(None, 'fixture', data)

    async def test_text_event_still_uses_existing_conversation_dispatch(self):
        dispatch = AsyncMock()
        cls = isolated_class('src/open_llm_vtuber/websocket_handler.py', 'WebSocketHandler',
                             handle_conversation_trigger=dispatch)
        h=cls.__new__(cls); h.client_contexts={'fixture':S(staged_controller=self.controller())}
        for name in ('client_connections','chat_group_manager','received_data_buffers',
                     'current_conversation_tasks','broadcast_to_group'):setattr(h,name,object())
        await h._handle_conversation_trigger(None,'fixture',{'type':'text-input'})
        dispatch.assert_awaited_once()

    async def test_model_identity_cannot_be_replaced_while_reusing_controller(self):
        c = self.controller(); c.bind_llm(S(model='first'))
        with self.assertRaises(AdmissionDenied): c.bind_llm(S(model='second'))
        c.runtime.states['asr'] = State.READY
        with self.assertRaises(AdmissionDenied):
            await c.prepare_asr(S(),S(asr_model='different'))
