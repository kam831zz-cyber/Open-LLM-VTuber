"""No server, native model, subprocess, or real HTTP request is executed."""
import copy
import json
from types import SimpleNamespace as S
import unittest
from unittest.mock import AsyncMock, MagicMock
from test_startup_isolation import isolated_class
from open_llm_vtuber.model_loading_policy import Reading, GIB, AdmissionDenied
from open_llm_vtuber.staged_loading import StagedController
from tools.model_free_trial.preflight import ServerBudget, Observation, admit_server, PROFILE
from tools.model_free_trial.candidate import isolated_settings, prepare
from tools.model_free_trial.guard import audit, NativeModels, ReadinessOnly


def original():
    return {'system_config': {'default_character_config':'fixture.yaml'},
        'character_config': {'agent_config': {'conversation_agent_choice':'basic_memory_agent',
            'agent_settings':{'basic_memory_agent':{'llm_provider':'ollama_llm','use_mcpp':True,
                                                   'mcp_enabled_servers':['fixture']}},
            'llm_configs':{'ollama_llm':{'eager_preload':True,'unload_at_exit':True}}},
            'tts_config':{'tts_model':'voicevox_tts'}, 'vad_config':{'vad_model':'fixture'}}}


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    def client(self, post=None, **kwargs):
        class Base:
            def __init__(self, **fields): self.__dict__.update(fields)
        self.http = MagicMock()
        self.http.post = post or MagicMock(return_value=S(raise_for_status=MagicMock(),
                                                        json=lambda:{'done':True}))
        cls = isolated_class('src/open_llm_vtuber/agent/stateless_llm/ollama_llm.py',
                             'OllamaLLM', AsyncLLM=Base, requests=self.http, atexit=MagicMock())
        return cls(model='fixture-model', base_url='http://fixture.invalid/v1/',
                   keep_alive=42, unload_at_exit=False, **kwargs)

    def observation(self, *, available=6*GIB, commit=6*GIB, runners=(), at=10):
        return Observation(Reading(available,commit,at),runners,at)

    def budget(self, **kwargs):
        return ServerBudget(**dict(dict(physical_peak_bytes=GIB,commit_peak_bytes=GIB,
                                       evidence='fixture',evidence_id='offline-case'),**kwargs))

    def admit(self, budget=None, observation=None, **kw):
        return admit_server(budget or self.budget(), observation or self.observation(),
                            clock=lambda:10, offline=True, **kw)

    async def test_legacy_preload_uses_instance_fields_and_checks_completion(self):
        client=self.client()
        self.http.post.assert_called_once_with('http://fixture.invalid/api/chat',
            json={'model':'fixture-model','keep_alive':42},timeout=(3,30))
        self.assertTrue(client.preload_succeeded)

    async def test_legacy_connection_exception_propagates(self):
        with self.assertRaisesRegex(RuntimeError,'ollama_preload_failed'):
            self.client(post=MagicMock(side_effect=ConnectionError('private fixture')))

    async def test_legacy_http_error_propagates(self):
        response=S(raise_for_status=MagicMock(side_effect=RuntimeError('private fixture')))
        with self.assertRaisesRegex(RuntimeError,'ollama_preload_failed'):
            self.client(post=MagicMock(return_value=response))

    async def test_legacy_200_error_payload_is_not_success(self):
        response=S(raise_for_status=MagicMock(),json=lambda:{'error':'private fixture'})
        with self.assertRaises(RuntimeError):self.client(post=MagicMock(return_value=response))

    async def test_legacy_incomplete_or_invalid_json_is_not_success(self):
        for payload in ({'done':False}, [], None):
            response=S(raise_for_status=MagicMock(),json=lambda:payload)
            with self.assertRaises(RuntimeError):self.client(post=MagicMock(return_value=response))

    async def test_legacy_json_parse_failure_is_not_success(self):
        response=S(raise_for_status=MagicMock(),json=MagicMock(side_effect=ValueError('private fixture')))
        with self.assertRaises(RuntimeError):self.client(post=MagicMock(return_value=response))

    async def test_staged_construct_and_cleanup_send_zero_posts(self):
        c=StagedController(S(text_only=True,asr_mode='disabled',eager_preload=False))
        client=self.client(staged_controller=c)
        client.cleanup(); client.__del__()
        self.http.post.assert_not_called()
        self.assertFalse(c.status()['text_model_ready'])

    async def test_isolated_configuration_does_not_mutate_input(self):
        source=original(); saved=copy.deepcopy(source); config=isolated_settings(source,12394)
        self.assertEqual(source,saved)
        self.assertEqual(config['system_config']['staged_loading'],
            dict(enabled=True,text_only=True,asr_mode='disabled',eager_preload=False))
        self.assertFalse(config['character_config']['agent_config']['agent_settings']['basic_memory_agent']['use_mcpp'])
        self.assertFalse(config['system_config']['komugi_fast_path']['enabled'])

    async def test_protected_port_is_not_accepted(self):
        for port in (12393,18000,18081,18873,18874,18875,18876):
            with self.assertRaises(ValueError):isolated_settings(original(),port)

    async def test_admission_allows_fixture_only_offline(self):
        self.assertTrue(self.admit()['admitted'])
        with self.assertRaises(AdmissionDenied):
            admit_server(self.budget(),self.observation(),clock=lambda:10)

    async def test_unknown_server_budget_refuses_before_factory(self):
        factory=MagicMock()
        with self.assertRaisesRegex(AdmissionDenied,'unmeasured'):
            await prepare(original(),12394,ServerBudget(),self.observation,factory,
                          clock=lambda:10,offline=True)
        factory.assert_not_called()

    async def test_low_physical_ram_refuses_before_factory(self):
        factory=MagicMock()
        with self.assertRaisesRegex(AdmissionDenied,'physical'):
            await prepare(original(),12394,self.budget(),
                lambda:self.observation(available=int(1.7*GIB)),factory,clock=lambda:10,offline=True)
        factory.assert_not_called()

    async def test_commit_headroom_is_separate(self):
        with self.assertRaisesRegex(AdmissionDenied,'commit'):
            self.admit(observation=self.observation(commit=int(.5*GIB)))

    async def test_stale_observation_refuses(self):
        with self.assertRaisesRegex(AdmissionDenied,'stale'):self.admit(observation=self.observation(at=7))

    async def test_runner_unknown_and_present_both_refuse(self):
        for runners in (None,(999,)):
            with self.assertRaises(AdmissionDenied):self.admit(observation=self.observation(runners=runners))

    async def test_wrong_budget_profile_refuses(self):
        with self.assertRaises(AdmissionDenied):self.admit(budget=self.budget(profile='asr-plus-llm'))

    async def test_nan_or_bool_budget_and_memory_refuse(self):
        for value in (float('nan'),True,None,-1):
            with self.assertRaises(AdmissionDenied):self.admit(budget=self.budget(physical_peak_bytes=value))
        with self.assertRaises(AdmissionDenied):self.admit(observation=self.observation(available=float('nan')))

    async def test_model_free_factory_receives_only_isolated_configuration(self):
        server=S(initialize=AsyncMock(),app=AsyncMock(),run=MagicMock())
        factory=MagicMock(return_value=server)
        result,admission=await prepare(original(),12394,self.budget(),self.observation,factory,
                                       clock=lambda:10,offline=True)
        server.initialize.assert_awaited_once();server.run.assert_not_called()
        self.assertIsInstance(result.app,ReadinessOnly)
        self.assertFalse(admission['models_admitted']);self.assertFalse(admission['mcp_children_admitted'])

    async def test_no_external_socket_or_subprocess_is_permitted(self):
        for event,args in (('socket.connect',(None,('127.0.0.1',11434))),
                           ('subprocess.Popen',('fixture',)),('os.system',('fixture',))):
            with self.assertRaises(PermissionError):audit(event,args)

    async def test_native_model_imports_are_rejected(self):
        for name in ('faster_whisper','torch','ctranslate2','onnxruntime','av'):
            with self.assertRaises(ImportError):NativeModels().find_spec(name)

    async def test_only_openapi_get_is_forwarded(self):
        app=AsyncMock(); middleware=ReadinessOnly(app); send=AsyncMock(); receive=AsyncMock()
        await middleware({'type':'http','method':'GET','path':'/openapi.json'},receive,send)
        app.assert_awaited_once()
        app.reset_mock()
        for method,path in (('POST','/api/chat'),('GET','/api/tts'),('GET','/')):
            await middleware({'type':'http','method':method,'path':path},receive,send)
        app.assert_not_awaited()

    async def test_websocket_text_request_is_closed_before_dispatch(self):
        captured=[]
        async def app(scope,receive,send):captured.append(await receive())
        middleware=ReadinessOnly(app); send=AsyncMock()
        await middleware({'type':'websocket','path':'/client-ws'},
            AsyncMock(return_value={'type':'websocket.receive','text':json.dumps({'type':'text-input'})}),send)
        self.assertEqual(captured,[{'type':'websocket.disconnect','code':1008}])
        send.assert_awaited_once_with({'type':'websocket.close','code':1008})

    async def test_public_template_can_form_typed_model_free_settings(self):
        import yaml
        from pathlib import Path
        from open_llm_vtuber.config_manager.utils import Config
        # Public template only; no read_yaml/environment substitution or production config.
        source=yaml.safe_load((Path(__file__).resolve().parents[1]/
                              'config_templates/conf.default.yaml').read_text(encoding='utf-8'))
        source['character_config']['tts_config'].update(tts_model='voicevox_tts',voicevox_tts={})
        configured=Config(**isolated_settings(source,12394))
        self.assertTrue(configured.system_config.staged_loading.enabled)
        self.assertFalse(configured.system_config.staged_loading.eager_preload)
        self.assertEqual(configured.system_config.staged_loading.asr_mode,'disabled')
        self.assertFalse(configured.character_config.agent_config.agent_settings.basic_memory_agent.use_mcpp)

    async def test_websocket_initial_status_is_unloaded_and_no_mic(self):
        from open_llm_vtuber.startup_status import conversation_status
        c=StagedController(S(text_only=True,asr_mode='disabled',eager_preload=False))
        context=S(staged_controller=c,asr_engine=None,agent_engine=object(),tts_engine=object(),
                  _mcp_enabled=False,_mcp_expected_servers=(),
                  live2d_model=S(model_info={}),character_config=S(conf_name='fixture',conf_uid='fixture'))
        cls=isolated_class('src/open_llm_vtuber/websocket_handler.py','WebSocketHandler',
                           conversation_status=conversation_status)
        h=cls.__new__(cls);h.send_group_update=AsyncMock();ws=S(send_text=AsyncMock())
        await h._send_initial_messages(ws,'fixture',context)
        messages=[json.loads(call.args[0]) for call in ws.send_text.await_args_list]
        status=messages[0]
        self.assertFalse(status['conversation_core_ready']);self.assertFalse(status['text_model_ready'])
        self.assertFalse(status['asr_ready']);self.assertFalse(status['required_mcp_ready'])
        self.assertEqual(status['required_mcp_state'],'disabled')
        self.assertEqual(status['states'],{'llm':'unloaded','asr':'unloaded'})
        self.assertFalse(any(m.get('text')=='start-mic' for m in messages))
