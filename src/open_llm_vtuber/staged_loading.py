"""Opt-in model-free application adapter. No transport or model imports.

Real load adapters are deliberately unregistered until measured budgets and an
Ollama-wide ownership mechanism have been validated in a separately approved trial.
"""
from dataclasses import dataclass
import time
from .model_loading_policy import (
    Runtime, StartupPolicy, Model, Budget, Reading, State, AdmissionDenied,
)


@dataclass(frozen=True)
class Occupancy:
    captured_at: float
    exclusive: bool = False
    model_ids: tuple = ()


def windows_memory_reading():
    """Read physical availability and system commit headroom separately."""
    import ctypes
    from ctypes import wintypes

    class Memory(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in
            ('total', 'available', 'page_total', 'page_available', 'virtual_total',
             'virtual_available', 'extended')]

    class Performance(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in
            ('commit_total', 'commit_limit', 'commit_peak', 'physical_total',
             'physical_available', 'system_cache', 'kernel_total', 'kernel_paged',
             'kernel_nonpaged', 'page_size')] + [
            (name, wintypes.DWORD) for name in ('handles', 'processes', 'threads')]

    memory = Memory(); memory.length = ctypes.sizeof(memory)
    performance = Performance(); performance.size = ctypes.sizeof(performance)
    if not (ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
            and ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(performance), performance.size)):
        raise AdmissionDenied('memory_measurement_failed')
    return Reading(memory.available,
                   (performance.commit_limit - performance.commit_total) * performance.page_size,
                   time.monotonic())


class StagedController:
    def __init__(self, settings, *, read_memory=windows_memory_reading,
                 occupancy=lambda: None, budgets=None, loaders=None,
                 clock=time.monotonic, offline=False):
        self.policy = StartupPolicy(text_only=settings.text_only, asr_mode=settings.asr_mode,
                                    eager_preload=settings.eager_preload)
        self.audio_enabled = getattr(settings, "audio_enabled", True)
        self.text_requests_enabled = getattr(settings, "text_requests_enabled", False)
        if not self.audio_enabled and (not self.policy.text_only or self.policy.asr_mode != 'disabled'
                                       or self.policy.eager_preload):
            raise ValueError('invalid_text_only_startup_mode')
        self.text_inflight = False
        self.occupancy = occupancy
        self.runtime = Runtime(self.policy,
            {'llm': Model('llm', 'unbound', remote=True),
             'asr': Model('asr', 'unbound')},
            budgets or {}, read_memory, loaders or {}, lambda **kw: None,
            clock=clock, offline=offline)

    def validate_mode(self, settings):
        if (settings is None or not settings.enabled
                or getattr(settings, 'audio_enabled', True) != self.audio_enabled
                or getattr(settings, 'text_requests_enabled', False) != self.text_requests_enabled):
            raise AdmissionDenied('startup_mode_change_requires_new_context')
        if (
                settings.text_only, settings.asr_mode, settings.eager_preload) != (
                self.policy.text_only, self.policy.asr_mode, self.policy.eager_preload):
            raise AdmissionDenied('startup_mode_change_requires_new_context')

    def bind_llm(self, client):
        if self.runtime.client is not None and self.runtime.client is not client:
            raise AdmissionDenied('client_replacement_requires_new_context')
        self.runtime.models['llm'] = Model('llm', client.model, remote=True)
        self.runtime.client = client  # constructed SDK is not a loaded model

    def check_occupancy(self, *, ready=False):
        evidence = self.occupancy()
        if (not isinstance(evidence, Occupancy) or evidence.exclusive is not True
                or (evidence.model_ids != (self.runtime.models["llm"].model_id,) if ready else bool(evidence.model_ids))
                or not 0 <= self.runtime.clock() - evidence.captured_at <= 2):
            raise AdmissionDenied('ollama_exclusive_ownership_unconfirmed')

    async def request_model(self, name):
        # A local lock alone cannot prevent another Ollama client loading a model.
        self.check_occupancy()
        self.runtime.admit(name)
        if name not in self.runtime.loaders:
            raise AdmissionDenied('verified_loader_not_registered')
        return await self.runtime.request(name)

    async def prepare_asr(self, context, config):
        selected = getattr(config, config.asr_model, None)
        identity = getattr(selected, 'model_path', None) or config.asr_model
        if (self.runtime.models['asr'].model_id != str(identity)
                and self.runtime.states['asr'] != State.UNLOADED):
            raise AdmissionDenied('asr_replacement_requires_new_context')
        context.asr_engine = None
        self.runtime.models['asr'] = Model('asr', str(identity))
        if self.audio_enabled and not self.policy.text_only and self.policy.asr_mode == 'eager':
            await self.request_model('asr')
            # No implicit call to ASRFactory: the admitted adapter owns construction.
            context.asr_engine = getattr(self, 'asr_instance', None)
            if context.asr_engine is None:
                raise AdmissionDenied('asr_instance_not_verified')

    async def finish_startup(self):
        if self.policy.eager_preload:
            await self.request_model('llm')

    def require_llm_ready(self):
        self.runtime.expire_remote_evidence()
        if not self.runtime.status()['text_model_ready']:
            raise AdmissionDenied('llm_model_not_ready')
        # Evidence must remain fresh; no automatic network probe or model request.
        self.check_occupancy(ready=True)

    def mic_ready(self):
        return (self.audio_enabled and not self.policy.text_only and self.policy.asr_mode != 'disabled'
                and self.runtime.states['asr'] == State.READY
                and self.runtime.uncertain is None)

    def status(self):
        status = self.runtime.status()
        if status['text_model_ready']:
            try:
                self.check_occupancy(ready=True)
            except AdmissionDenied:
                status['text_model_ready'] = False
                status['ollama_ownership'] = 'unconfirmed'
        return {**status, 'audio_enabled': self.audio_enabled,
                'text_requests_enabled': self.text_requests_enabled, 'startup_mode':
                'text_only' if self.policy.text_only else self.policy.asr_mode,
                'microphone_ready': self.mic_ready()}
