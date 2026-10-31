"""No server/model/transport is started. SDK process contexts are mocked."""
import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import io
import json
from pathlib import Path
from types import SimpleNamespace as S
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.open_llm_vtuber.mcpp.owned_session import OwnedSession
from src.open_llm_vtuber.mcpp.mcp_client import MCPClient
from src.open_llm_vtuber.mcpp.server_registry import ServerRegistry
from src.open_llm_vtuber.mcpp.tool_adapter import ToolAdapter
from src.open_llm_vtuber.asr.local_model import resolve_local_model
from src.open_llm_vtuber.startup_status import conversation_status
from src.open_llm_vtuber.komugi_fast_path_client import KomugiFastPathClient, KomugiFastPathConfig

ROOT = Path(__file__).resolve().parents[1]


def isolated_class(file, name, **env):
    """Execute the real class methods without importing unrelated native engines."""
    tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"logger": MagicMock(), "json": json, "asyncio": asyncio, **env}
    exec(compile(module, str(file), "exec"), namespace)
    return namespace[name]


class AsyncStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_contexts_enter_and_exit_in_same_task(self):
        owners = []
        @asynccontextmanager
        async def transport():
            owners.append(asyncio.current_task())
            try: yield object(), object()
            finally: self.assertIs(owners[0], asyncio.current_task())
        @asynccontextmanager
        async def session(*args):
            yield S(initialize=AsyncMock(), list_tools=AsyncMock(return_value="tools"))
        owned = OwnedSession(transport, session, .1)
        await owned.ready
        self.assertEqual(await owned.list_tools(), "tools")
        await owned.aclose()
        await owned.aclose()
        self.assertTrue(owned.task.done())

    async def test_initialization_timeout_closes_mock_process(self):
        closed = []
        @asynccontextmanager
        async def transport():
            try: yield None, None
            finally: closed.append(True)
        @asynccontextmanager
        async def session(*args):
            yield S(initialize=lambda: asyncio.sleep(10))
        owned = OwnedSession(transport, session, .01)
        with self.assertRaises(TimeoutError): await owned.ready
        with self.assertRaises(TimeoutError): await owned.aclose()
        self.assertEqual(closed, [True])

    async def test_cancel_during_initialization_closes_mock_process(self):
        closed = []
        @asynccontextmanager
        async def transport():
            try: yield None, None
            finally: closed.append(True)
        @asynccontextmanager
        async def session(*args):
            yield S(initialize=lambda: asyncio.sleep(10))
        owned = OwnedSession(transport, session, 30)
        await asyncio.sleep(0)
        await owned.aclose()
        self.assertTrue(owned.task.done())
        self.assertEqual(closed, [True])

    async def test_close_during_request_does_not_leave_waiter_pending(self):
        entered = asyncio.Event()
        @asynccontextmanager
        async def transport(): yield None, None
        async def slow():
            entered.set(); await asyncio.sleep(10)
        @asynccontextmanager
        async def session(*args): yield S(initialize=AsyncMock(), list_tools=slow)
        owned = OwnedSession(transport, session, 30)
        await owned.ready
        request = asyncio.create_task(owned.list_tools())
        await entered.wait(); await owned.aclose()
        with self.assertRaises(RuntimeError): await request

    async def test_same_client_concurrent_requests_initialize_once(self):
        registry = ServerRegistry.__new__(ServerRegistry)
        registry.get_server = lambda name: S(timeout=timedelta(seconds=1), command="mock", args=[], env=None, cwd=None)
        entered = []
        @asynccontextmanager
        async def transport(*args):
            entered.append(True); yield None, None
        @asynccontextmanager
        async def session(*args, **kwargs):
            yield S(initialize=AsyncMock(), list_tools=AsyncMock(return_value=S(tools=[])))
        with patch('src.open_llm_vtuber.mcpp.mcp_client.stdio_client', transport), patch('src.open_llm_vtuber.mcpp.mcp_client.ClientSession', session):
            client = MCPClient(registry)
            await asyncio.gather(client.list_tools('weather'), client.list_tools('weather'))
            await client.aclose()
        self.assertEqual(len(entered), 1)

    async def test_discovery_shared_once_and_copy_isolation(self):
        adapter = ToolAdapter(S(servers={}))
        adapter.get_server_and_tool_info = AsyncMock(return_value=({}, {}))
        a, b = await asyncio.gather(adapter.get_tools(['weather']), adapter.get_tools(['weather']))
        await adapter.get_tools(['weather'])
        self.assertEqual(adapter.get_server_and_tool_info.await_count, 1)
        self.assertIsNot(a[1], b[1])

    async def test_discovery_timeout_isolated_from_required_weather(self):
        adapter = ToolAdapter(S(servers={'ddg-search':object(), 'weather':object()}))
        closed = []
        class Client:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): closed.append(True)
            async def list_tools(self, name):
                if name == 'ddg-search': raise TimeoutError()
                return [S(name='weather_tool', description='fixture', inputSchema={'properties':{}})]
        with patch('src.open_llm_vtuber.mcpp.tool_adapter.MCPClient', Client):
            result = await adapter.get_tools(['ddg-search','weather'])
        self.assertEqual(adapter.availability['ddg-search']['state'], 'timeout')
        self.assertEqual(adapter.availability['weather']['state'], 'ready')
        self.assertIn('weather_tool',result[3]); self.assertEqual(len(closed),2)

    async def test_required_failure_and_missing_server_are_visible(self):
        adapter = ToolAdapter(S(servers={'komugi-context':object()}))
        class Client:
            def __init__(self,*args):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def list_tools(self,*args):raise ConnectionError('fixture')
        with patch('src.open_llm_vtuber.mcpp.tool_adapter.MCPClient',Client):
            await adapter.get_tools(['komugi-context','weather'])
        self.assertTrue(adapter.availability['komugi-context']['required'])
        self.assertEqual(adapter.availability['komugi-context']['state'],'unavailable')
        self.assertEqual(adapter.availability['weather']['reason'],'not_configured')

    async def test_discovery_cancellation_does_not_cache_partial_success(self):
        adapter=ToolAdapter(S(servers={}))
        adapter.get_server_and_tool_info=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):await adapter.get_tools(['weather'])
        self.assertEqual(adapter._definitions,{})

    async def test_failed_discovery_is_cached_not_repeated_on_reconnect(self):
        adapter=ToolAdapter(S(servers={}))
        await adapter.get_tools(['ddg-search'])
        self.assertEqual(adapter.availability['ddg-search']['state'],'unavailable')
        adapter.get_server_and_tool_info=AsyncMock(side_effect=AssertionError('Unexpected rediscovery'))
        await adapter.get_tools(['ddg-search'])

    def context_class(self):
        class Client:
            def __init__(self, registry, send_text, uid):
                self.uid=uid;self.send=send_text;self.aclose=AsyncMock()
        return isolated_class('src/open_llm_vtuber/service_context.py','ServiceContext',
            MCPClient=Client, ServerRegistry=MagicMock(), ToolManager=lambda **kwargs:object(),
            ToolExecutor=lambda client,manager:S(client=client), json=json)

    async def test_first_reconnect_and_parallel_sessions_reuse_definitions_not_clients(self):
        context=self.context_class()
        adapter=ToolAdapter(S(servers={}))
        adapter.get_server_and_tool_info=AsyncMock(return_value=({},{}))
        class Agent:
            def for_session(self,*args):return S(memory=[],executor=args[1])
        agent=Agent()
        character=S(agent_config=S(agent_settings=S(basic_memory_agent=S(use_mcpp=True,mcp_enabled_servers=['weather']))))
        async def create(uid):
            c=context(); c.init_komugi_fast_path=lambda:None
            send=AsyncMock()
            await c.load_cache(object(),object(),character,object(),object(),object(),None,agent,None,
                               tool_adapter=adapter,send_text=send,client_uid=uid)
            return c
        a,b=await asyncio.gather(create('a'),create('b'))
        self.assertIsNot(a.mcp_client,b.mcp_client)
        self.assertEqual((a.mcp_client.uid,b.mcp_client.uid),('a','b'))
        self.assertIs(a.agent_engine.executor,a.tool_executor)
        a.agent_engine.memory.append('fixture')
        self.assertEqual(b.agent_engine.memory,[])
        await a.close()
        c=await create('c');await b.close();await c.close()
        self.assertEqual(adapter.get_server_and_tool_info.await_count,1)

    async def test_existing_client_closed_before_reinitialization(self):
        c=self.context_class()();old=S(aclose=AsyncMock());c.mcp_client=old
        await c._init_mcp_components(False,[])
        old.aclose.assert_awaited_once();self.assertIsNone(c.mcp_client)

    async def test_borrowed_agent_is_not_closed_on_disconnect(self):
        c=self.context_class()();c._owns_agent=False;c.agent_engine=S(close=AsyncMock())
        await c.close();c.agent_engine.close.assert_not_awaited()

    async def test_command_center_failure_is_not_fake_lifestyle_answer(self):
        c=KomugiFastPathClient(KomugiFastPathConfig(enabled=True))
        with patch('requests.post',side_effect=ConnectionError('fixture')):
            result=await c.quick_response('今日のゴミは？')
        self.assertTrue(result.handled);self.assertEqual(result.error,'command_center_unavailable')
        self.assertIn('取得できません',result.response)

    async def test_ordinary_conversation_still_falls_through_when_cc_down(self):
        c=KomugiFastPathClient(KomugiFastPathConfig(enabled=True))
        with patch('requests.post',side_effect=ConnectionError('fixture')):
            result=await c.quick_response('好きな季節を教えて')
        self.assertFalse(result.handled)

    async def test_websocket_initial_status_notice_and_start_marker(self):
        handler=isolated_class('src/open_llm_vtuber/websocket_handler.py','WebSocketHandler',ServiceContext=object)
        h=handler.__new__(handler);h.send_group_update=AsyncMock()
        context=S(asr_engine=object(),tts_engine=object(),agent_engine=object(),
                  tool_adapter=S(availability={'weather':{'state':'unavailable','required':True}}),
                  live2d_model=S(model_info={}),character_config=S(conf_name='Komugi',conf_uid='fixture'))
        messages=[]
        async def send(value):messages.append(json.loads(value))
        # The method globals use the real pure status helper, no engine imports.
        handler._send_initial_messages.__globals__['conversation_status']=conversation_status
        await h._send_initial_messages(S(send_text=send),'client-a',context)
        self.assertEqual(messages[0]['type'],'service-status')
        self.assertFalse(messages[0]['required_mcp_ready'])
        self.assertIn('weather',messages[1]['text'])
        self.assertEqual(messages[-1],{'type':'control','text':'start-mic'})

    async def test_failed_websocket_connection_closes_session_resources(self):
        handler=isolated_class('src/open_llm_vtuber/websocket_handler.py','WebSocketHandler',ServiceContext=object,message_handler=MagicMock())
        h=handler.__new__(handler)
        context=S(close=AsyncMock());h.client_connections={};h.client_contexts={'a':context}
        h.received_data_buffers={};h.current_conversation_tasks={};h.chat_group_manager=S(client_group_map={})
        await h._cleanup_failed_connection('a')
        context.close.assert_awaited_once()

    async def test_owned_client_init_failure_releases_connection_slot(self):
        registry=ServerRegistry.__new__(ServerRegistry)
        registry.get_server=lambda name:S(timeout=timedelta(seconds=.01),command='mock',args=[],env=None,cwd=None)
        @asynccontextmanager
        async def transport(*args):yield None,None
        @asynccontextmanager
        async def session(*args,**kwargs):yield S(initialize=lambda:asyncio.sleep(10))
        with patch('src.open_llm_vtuber.mcpp.mcp_client.stdio_client',transport),patch('src.open_llm_vtuber.mcpp.mcp_client.ClientSession',session):
            c=MCPClient(registry)
            with self.assertRaises(TimeoutError):await c.list_tools('ddg-search')
            self.assertEqual(c._connections,{})
            await c.aclose()


class StaticStartupTests(unittest.TestCase):
    def test_local_model_missing_and_incomplete_never_download(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):resolve_local_model('small',d)
            with self.assertRaises(FileNotFoundError):resolve_local_model(d,None)

    def test_local_cached_small_reference_and_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'models--Systran--faster-whisper-small'
            (root/'refs').mkdir(parents=True)
            ref='a'*40;(root/'refs/main').write_text(ref)
            files=root/'snapshots'/ref;files.mkdir(parents=True)
            for name in ['model.bin','config.json','tokenizer.json','vocabulary.txt']:(files/name).write_text('fixture')
            self.assertEqual(resolve_local_model('small',d),files.resolve())
            (root/'refs/main').write_text('../escape')
            with self.assertRaises(ValueError):resolve_local_model('small',d)

    def test_asr_init_uses_local_only_and_records_phase_metrics(self):
        from src.open_llm_vtuber.asr.faster_whisper_asr import VoiceRecognition
        with tempfile.TemporaryDirectory() as d:
            for name in ['model.bin','config.json','tokenizer.json','vocabulary.txt']:(Path(d)/name).write_text('fixture')
            fake=MagicMock()
            with patch.dict('sys.modules',{'faster_whisper':S(WhisperModel=fake)}):
                asr=VoiceRecognition(model_path=d,language='ja')
            self.assertTrue(fake.call_args.kwargs['local_files_only'])
            self.assertEqual(set(asr.initialization_metrics),{'resolve_ms','native_import_ms','model_construct_ms','total_ms','cpu_ms'})

    def test_registry_direct_local_search_and_missing_package(self):
        import importlib.metadata
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mcp.json';path.write_text(json.dumps({'mcp_servers':{'ddg-search':{'command':'uvx','args':['duckduckgo-mcp-server']},'weather':{'command':'python','args':['weather.py']}}}))
            package=S(entry_points=[S(name='duckduckgo-mcp-server',value='duckduckgo_mcp_server.server:main')])
            with patch('importlib.metadata.distribution',return_value=package):r=ServerRegistry(path)
            self.assertNotEqual(r.servers['ddg-search'].command,'uvx')
            self.assertIn('UV_OFFLINE',r.servers['weather'].env)
            with patch('importlib.metadata.distribution',side_effect=importlib.metadata.PackageNotFoundError):r=ServerRegistry(path)
            self.assertIn('ddg-search',r.unavailable);self.assertIn('weather',r.servers)

    def test_readiness_stages_do_not_claim_inference_or_speaker_success(self):
        c=S(asr_engine=None,tts_engine=object(),agent_engine=object(),tool_adapter=S(availability={'weather':{'state':'timeout','required':True}}))
        r=conversation_status(c);self.assertFalse(r['conversation_core_ready']);self.assertFalse(r['required_mcp_ready'])
        c.asr_engine=object();self.assertTrue(conversation_status(c)['conversation_core_ready'])
        self.assertEqual(r['speaker_output'],'not_tested');self.assertEqual(r['command_center'],'not_checked')

    def test_actual_basic_agent_clone_has_independent_memory_and_executor(self):
        agent=isolated_class('src/open_llm_vtuber/agent/agents/basic_memory_agent.py','BasicMemoryAgent',
            AgentInterface=object,StreamJSONDetector=object)
        agent._chat_function_factory=lambda self:object()
        agent.set_system=lambda self,s:setattr(self,'_system',s)
        base=agent(object(),'fixture',object())
        child=base.for_session(None,object(),'')
        self.assertIs(base._llm,child._llm);self.assertIsNot(base._memory,child._memory)
        self.assertIsNot(base._tool_executor,child._tool_executor)

    def test_config_and_voicevox_unchanged_from_base(self):
        import hashlib
        self.assertEqual(hashlib.sha256((ROOT/'characters/komugi.yaml').read_bytes()).hexdigest(),
                         '17b145123b972246d6260e8787e82341e84653f0d860a1640b2a559ddb22f5fc')
        text=(ROOT/'characters/komugi.yaml').read_text(encoding='utf-8')
        self.assertIn('speaker_id: 8',text)
        self.assertIn('http://127.0.0.1:50021',text)


if __name__=='__main__':unittest.main()
