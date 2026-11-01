"""All model providers, memory readings and client construction are fixtures."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock,MagicMock

from tools.staged_loading.policy import (
    Runtime,StartupPolicy,Model,Budget,Reading,Receipt,State,AdmissionDenied,LoadFailure,GIB,
)


class StagedLoadingTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self,policy=None):
        self.now=0
        models={'llm':Model('llm','qwen2.5:3b',remote=True),
                'asr':Model('asr','small',cooperative_cancel=True)}
        async def success(model,token):return Receipt(model.name,model.model_id,token,self.now,True,True)
        loaders={name:AsyncMock(side_effect=success) for name in models}
        budget=Budget(int(.5*GIB),int(.75*GIB),'fixture')
        return Runtime(policy or StartupPolicy(text_only=False),models,{name:budget for name in (*models,'client')},
                       lambda:Reading(6*GIB,6*GIB,self.now),loaders,MagicMock(return_value=object()),clock=lambda:self.now)

    async def test_preload_disabled_builds_client_without_loading_any_model(self):
        runtime=self.runtime()
        status=await runtime.start()
        runtime.client_factory.assert_called_once_with(eager_preload=False)
        for loader in runtime.loaders.values():loader.assert_not_called()
        self.assertFalse(status['text_model_ready']);self.assertFalse(status['asr_ready'])

    async def test_explicit_eager_setting_uses_serial_budgeted_path(self):
        runtime=self.runtime(StartupPolicy(eager_preload=True,text_only=False,asr_mode='eager'))
        status=await runtime.start()
        self.assertTrue(status['text_model_ready']);self.assertTrue(status['asr_ready'])
        for loader in runtime.loaders.values():loader.assert_awaited_once()

    async def test_model_state_transitions_are_not_client_construction(self):
        runtime=self.runtime();gate=asyncio.Event()
        async def load(model,token):
            await gate.wait();return Receipt(model.name,model.model_id,token,self.now,True,True)
        runtime.loaders['llm']=AsyncMock(side_effect=load)
        self.assertEqual(runtime.states['llm'],State.UNLOADED)
        task=asyncio.create_task(runtime.request('llm'));await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertEqual(runtime.states['llm'],State.LOADING)
        self.assertFalse(runtime.status()['text_model_ready'])
        gate.set();self.assertEqual(await task,State.READY)
        self.assertFalse(runtime.status()['conversation_verified'])

    async def test_insufficient_ram_refuses_without_loader_call(self):
        runtime=self.runtime();runtime.read_memory=lambda:Reading(2*GIB,6*GIB,self.now)
        with self.assertRaisesRegex(AdmissionDenied,'physical'):await runtime.request('llm')
        runtime.loaders['llm'].assert_not_called();self.assertEqual(runtime.states['llm'],State.UNLOADED)

    async def test_commit_exhaustion_is_distinct_from_available_ram(self):
        runtime=self.runtime();runtime.read_memory=lambda:Reading(6*GIB,int(.1*GIB),self.now)
        with self.assertRaisesRegex(AdmissionDenied,'commit'):await runtime.request('llm')

    async def test_unknown_model_budget_is_not_inferred_from_model_size(self):
        runtime=self.runtime();runtime.budgets['llm']=Budget()
        with self.assertRaisesRegex(AdmissionDenied,'budget_unknown'):await runtime.request('llm')
        runtime.loaders['llm'].assert_not_called()

    async def test_fixture_budget_cannot_be_admitted_as_real_measurement(self):
        runtime=self.runtime();runtime.offline=False
        with self.assertRaises(AdmissionDenied):await runtime.request('llm')

    async def test_stale_or_missing_memory_readings_fail_closed(self):
        for reading in (Reading(6*GIB,6*GIB,-3),Reading(None,6*GIB,0),Reading(6*GIB,None,0)):
            runtime=self.runtime();runtime.read_memory=lambda:reading
            with self.assertRaises(AdmissionDenied):await runtime.request('llm')

    async def test_duplicate_requests_share_one_load(self):
        runtime=self.runtime();gate=asyncio.Event();entered=asyncio.Event()
        async def load(model,token):
            entered.set()
            await gate.wait();return Receipt(model.name,model.model_id,token,self.now,True,True)
        runtime.loaders['llm']=AsyncMock(side_effect=load)
        tasks=[asyncio.create_task(runtime.request('llm')) for _ in range(3)]
        await asyncio.wait_for(entered.wait(),1)
        runtime.loaders['llm'].assert_awaited_once()
        gate.set();self.assertEqual(await asyncio.gather(*tasks),[State.READY]*3)

    async def test_different_models_cannot_load_concurrently(self):
        runtime=self.runtime();gate=asyncio.Event()
        async def load(model,token):
            await gate.wait();return Receipt(model.name,model.model_id,token,self.now,True,True)
        runtime.loaders['llm']=AsyncMock(side_effect=load)
        task=asyncio.create_task(runtime.request('llm'));await asyncio.sleep(0)
        with self.assertRaisesRegex(AdmissionDenied,'another_model'):await runtime.request('asr')
        runtime.loaders['asr'].assert_not_called();gate.set();await task

    async def test_failure_requires_explicit_reset_and_no_automatic_retry(self):
        runtime=self.runtime();runtime.loaders['asr']=AsyncMock(side_effect=LoadFailure('model_missing',may_continue=False))
        self.assertEqual(await runtime.request('asr'),State.FAILED)
        with self.assertRaisesRegex(AdmissionDenied,'reset'):await runtime.request('asr')
        runtime.loaders['asr'].assert_awaited_once();runtime.reset_failed('asr')
        self.assertEqual(runtime.states['asr'],State.UNLOADED)

    async def test_ollama_stopped_is_failed_not_ready(self):
        runtime=self.runtime();runtime.loaders['llm']=AsyncMock(side_effect=LoadFailure('connection_failed',may_continue=False))
        self.assertEqual(await runtime.request('llm'),State.FAILED)
        self.assertFalse(runtime.status()['text_model_ready'])

    async def test_remote_timeout_keeps_global_slot_uncertain(self):
        runtime=self.runtime();runtime.models['llm']=replace(runtime.models['llm'],timeout_seconds=.005)
        runtime.loaders['llm']=AsyncMock(side_effect=lambda *a:None)
        async def pending(*a):await asyncio.Event().wait()
        runtime.loaders['llm'].side_effect=pending
        self.assertEqual(await runtime.request('llm'),State.PENDING)
        with self.assertRaisesRegex(AdmissionDenied,'unconfirmed'):await runtime.request('asr')

    async def test_cancellation_of_cooperative_local_initialization_is_clean(self):
        runtime=self.runtime()
        async def pending(*a):await asyncio.Event().wait()
        runtime.loaders['asr']=AsyncMock(side_effect=pending)
        caller=asyncio.create_task(runtime.request('asr'));await asyncio.sleep(0);await asyncio.sleep(0)
        inner=runtime.active[1];caller.cancel()
        with self.assertRaises(asyncio.CancelledError):await caller
        await inner;await asyncio.sleep(0)
        self.assertEqual(runtime.states['asr'],State.UNLOADED);self.assertIsNone(runtime.active)

    async def test_remote_cancel_does_not_claim_server_operation_stopped(self):
        runtime=self.runtime();gate=asyncio.Event()
        async def pending(model,token):
            await gate.wait();return Receipt(model.name,model.model_id,token,self.now,True,True)
        runtime.loaders['llm']=AsyncMock(side_effect=pending)
        caller=asyncio.create_task(runtime.request('llm'));await asyncio.sleep(0);await asyncio.sleep(0)
        inner=runtime.active[1];caller.cancel()
        with self.assertRaises(asyncio.CancelledError):await caller
        self.assertEqual(runtime.states['llm'],State.PENDING)
        with self.assertRaises(AdmissionDenied):runtime.reconcile('llm',verified_idle=True,token=1,captured_at=self.now)
        gate.set();await inner;await asyncio.sleep(0)
        self.assertEqual(runtime.states['llm'],State.PENDING)
        with self.assertRaises(AdmissionDenied):await runtime.request('asr')
        runtime.reconcile('llm',verified_idle=True,token=1,captured_at=self.now)
        self.assertEqual(runtime.states['llm'],State.UNLOADED)

    async def test_noninterruptible_asr_cancel_also_blocks_other_model(self):
        runtime=self.runtime();runtime.models['asr']=replace(runtime.models['asr'],cooperative_cancel=False)
        gate=asyncio.Event()
        async def pending(model,token):
            await gate.wait();return Receipt(model.name,model.model_id,token,self.now,True,True)
        runtime.loaders['asr']=AsyncMock(side_effect=pending)
        caller=asyncio.create_task(runtime.request('asr'));await asyncio.sleep(0);await asyncio.sleep(0)
        inner=runtime.active[1];caller.cancel()
        with self.assertRaises(asyncio.CancelledError):await caller
        with self.assertRaises(AdmissionDenied):await runtime.request('llm')
        gate.set();await inner;await asyncio.sleep(0)
        self.assertEqual(runtime.states['asr'],State.PENDING)

    async def test_reconciliation_rejects_wrong_model_token_and_stale_evidence(self):
        runtime=self.runtime();runtime.loaders['llm']=AsyncMock(side_effect=TimeoutError())
        await runtime.request('llm');self.now=5
        for receipt in (Receipt('llm','wrong-model',1,5,True,True),Receipt('llm','qwen2.5:3b',99,5,True,True),
                        Receipt('llm','qwen2.5:3b',1,0,True,True)):
            with self.assertRaises(AdmissionDenied):runtime.reconcile('llm',receipt)
        runtime.reconcile('llm',Receipt('llm','qwen2.5:3b',1,5,True,True))
        self.assertEqual(runtime.states['llm'],State.READY)

    async def test_http_200_like_payload_is_not_completion_evidence(self):
        runtime=self.runtime();runtime.loaders['llm']=AsyncMock(return_value={'status':200})
        self.assertEqual(await runtime.request('llm'),State.PENDING)
        self.assertFalse(runtime.status()['text_model_ready'])

    async def test_memory_is_rechecked_between_sequential_eager_loads(self):
        runtime=self.runtime(StartupPolicy(eager_preload=True,text_only=False,asr_mode='eager'))
        readings=iter([Reading(6*GIB,6*GIB,0),Reading(6*GIB,6*GIB,0),Reading(2*GIB,6*GIB,0)])
        runtime.read_memory=lambda:next(readings)
        with self.assertRaises(AdmissionDenied):await runtime.start()
        self.assertEqual(runtime.states['llm'],State.READY)
        runtime.loaders['asr'].assert_not_called()

    async def test_mcp_unprepared_is_independent_of_client_and_llm(self):
        runtime=self.runtime();await runtime.start();await runtime.request('llm')
        runtime.update_mcp({},('weather','komugi-context'),enabled=True,command_center='stopped')
        status=runtime.status()
        self.assertTrue(status['text_model_ready']);self.assertFalse(status['required_mcp_ready'])
        self.assertFalse(status['life_information_available'])

    async def test_mcp_ready_does_not_hide_command_center_failure(self):
        runtime=self.runtime()
        runtime.update_mcp({'weather':'ready'},('weather',),enabled=True,command_center='stopped')
        self.assertTrue(runtime.status()['required_mcp_ready'])
        self.assertFalse(runtime.status()['life_information_available'])

    async def test_text_only_mode_never_reports_asr_ready(self):
        runtime=self.runtime(StartupPolicy(text_only=True,asr_mode='eager'))
        status=await runtime.start();self.assertFalse(status['asr_ready'])
        with self.assertRaises(AdmissionDenied):await runtime.request('asr')
        runtime.loaders['asr'].assert_not_called()

    async def test_failure_details_are_not_leaked(self):
        runtime=self.runtime();runtime.loaders['llm']=AsyncMock(side_effect=LoadFailure('SECRET=private',may_continue=False))
        await runtime.request('llm')
        self.assertEqual(runtime.status()['errors']['llm'],'load_failed')

    async def test_cancelled_before_execute_starts_releases_active_slot(self):
        runtime=self.runtime();runtime.tokens['asr']=1;runtime.states['asr']=State.LOADING
        async def never_started():await asyncio.sleep(0)
        task=asyncio.create_task(never_started());runtime.active=('asr',task);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        runtime.release('asr',1,task)
        self.assertEqual(runtime.states['asr'],State.UNLOADED);self.assertIsNone(runtime.active)

    async def test_client_only_startup_also_refuses_insufficient_ram(self):
        runtime=self.runtime();runtime.read_memory=lambda:Reading(int(.6*GIB),6*GIB,self.now)
        with self.assertRaises(AdmissionDenied):await runtime.start()
        runtime.client_factory.assert_not_called()
        for loader in runtime.loaders.values():loader.assert_not_called()

    async def test_truthy_unknown_completion_is_not_ready(self):
        runtime=self.runtime()
        runtime.loaders['llm']=AsyncMock(return_value=Receipt('llm','qwen2.5:3b',1,0,'unknown',True))
        self.assertEqual(await runtime.request('llm'),State.PENDING)
        self.assertFalse(runtime.status()['text_model_ready'])

    async def test_expired_remote_readiness_is_not_kept_ready(self):
        runtime=self.runtime();await runtime.request('llm');self.now=3
        status=runtime.status()
        self.assertFalse(status['text_model_ready'])
        self.assertEqual(status['states']['llm'],State.PENDING.value)

    async def test_expired_remote_evidence_blocks_next_model(self):
        runtime=self.runtime();await runtime.request('llm');self.now=3
        with self.assertRaises(AdmissionDenied):await runtime.request('asr')
        runtime.loaders['asr'].assert_not_called()
