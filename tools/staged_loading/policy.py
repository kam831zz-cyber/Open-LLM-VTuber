"""Dependency-injected model admission/lifecycle proposal. No model/API imports.

The installed application does not use this module. A future adapter must provide
trusted resource readings and explicit completion evidence, not just HTTP 200.
"""
import asyncio
from dataclasses import dataclass
from enum import Enum
import time


GIB=1024**3


class State(str,Enum):
    UNLOADED='unloaded'
    LOADING='loading'
    READY='ready'
    FAILED='failed'
    PENDING='pending_external_or_noninterruptible'


@dataclass(frozen=True)
class StartupPolicy:
    eager_preload:bool=False
    asr_mode:str='lazy'  # disabled / lazy / eager
    text_only:bool=True
    physical_reserve_bytes:int=int(1.5*GIB)
    extra_margin_bytes:int=int(.25*GIB)  # provisional policy margin, not measured model usage
    max_reading_age_seconds:float=2


@dataclass(frozen=True)
class Reading:
    available_bytes:int|None
    commit_headroom_bytes:int|None
    captured_at:float


@dataclass(frozen=True)
class Budget:
    physical_peak_increment_bytes:int|None=None
    commit_peak_increment_bytes:int|None=None
    evidence:str='unknown'  # measured / fixture / unknown; model size is not evidence


@dataclass(frozen=True)
class Model:
    name:str
    model_id:str
    remote:bool=False
    cooperative_cancel:bool=False  # never true for an uninterruptible native constructor
    timeout_seconds:float=10


@dataclass(frozen=True)
class Receipt:
    name:str
    model_id:str
    token:int
    captured_at:float
    verified_available:bool
    operation_complete:bool


class LoadFailure(Exception):
    """Adapter must explicitly distinguish rejected connection from unknown completion."""
    def __init__(self,code,*,may_continue=True):
        super().__init__(code)
        self.code=code
        self.may_continue=may_continue


class AdmissionDenied(Exception):pass


class Runtime:
    def __init__(self,policy,models,budgets,read_memory,loaders,client_factory,*,clock=time.monotonic,offline=True):
        if policy.asr_mode not in ('disabled','lazy','eager'):raise ValueError('invalid_asr_mode')
        if policy.physical_reserve_bytes<1.5*GIB or policy.extra_margin_bytes<0:
            raise ValueError('unsafe_reserve')
        self.policy=policy;self.models=models;self.budgets=budgets;self.read_memory=read_memory
        self.loaders=loaders;self.client_factory=client_factory;self.clock=clock;self.offline=offline
        self.states={name:State.UNLOADED for name in models}
        self.errors={};self.values={};self.tokens={};self.started_at={};self.sequence=0
        self.active=None;self.uncertain=None;self.client=None
        self.mcp_enabled=None;self.required_mcp=();self.mcp={};self.command_center='unconfirmed'

    def admit(self,name):
        budget=self.budgets.get(name,Budget())
        if budget.evidence not in ('measured','fixture') or (budget.evidence=='fixture' and not self.offline):
            raise AdmissionDenied('budget_unknown_or_not_measured')
        if (budget.physical_peak_increment_bytes is None or budget.commit_peak_increment_bytes is None
                or budget.physical_peak_increment_bytes<=0 or budget.commit_peak_increment_bytes<=0):
            raise AdmissionDenied('budget_incomplete')
        reading=self.read_memory()
        age=self.clock()-reading.captured_at
        if not 0<=age<=self.policy.max_reading_age_seconds:raise AdmissionDenied('reading_stale')
        if reading.available_bytes is None or reading.commit_headroom_bytes is None:
            raise AdmissionDenied('memory_unknown')
        physical=self.policy.physical_reserve_bytes+self.policy.extra_margin_bytes+budget.physical_peak_increment_bytes
        commit=self.policy.extra_margin_bytes+budget.commit_peak_increment_bytes
        if reading.available_bytes<physical:raise AdmissionDenied('insufficient_physical_headroom')
        if reading.commit_headroom_bytes<commit:raise AdmissionDenied('insufficient_commit_headroom')

    async def start(self):
        # A real OllamaLLM adapter needs a constructor change before this is usable.
        if self.active or self.uncertain:raise AdmissionDenied('operation_in_progress')
        if self.client is None:
            self.admit('client')  # also budget imports/client construction before any model
            self.client=self.client_factory(eager_preload=False)
        if self.policy.eager_preload:
            result=await self.request('llm')
            if result!=State.READY:return self.status()
        if not self.policy.text_only and self.policy.asr_mode=='eager':
            await self.request('asr')
        return self.status()

    async def request(self,name):
        if name not in self.models:raise ValueError('unknown_model')
        if name=='asr' and (self.policy.text_only or self.policy.asr_mode=='disabled'):
            raise AdmissionDenied('asr_disabled_for_mode')
        self.expire_remote_evidence()
        if self.uncertain is not None:raise AdmissionDenied('previous_operation_unconfirmed')
        if self.states[name]==State.READY:return State.READY
        if self.states[name]==State.FAILED:raise AdmissionDenied('explicit_reset_required')
        if self.active is not None:
            active_name,task=self.active
            if active_name!=name:raise AdmissionDenied('another_model_loading')
        else:
            self.admit(name)  # no state/model side effects if admission is denied
            self.sequence+=1;token=self.sequence
            self.tokens[name]=token;self.started_at[name]=self.clock()
            self.states[name]=State.LOADING
            task=asyncio.create_task(self.execute(name,token))
            self.active=(name,task)
            task.add_done_callback(lambda completed:self.release(name,token,completed))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            model=self.models[name]
            if model.cooperative_cancel and not model.remote:
                task.cancel()
            else:
                self.states[name]=State.PENDING;self.errors[name]='caller_cancelled_completion_unknown'
                self.uncertain=name
            raise

    def release(self,name,token,task):
        # A task cancelled before its first instruction never enters execute/finally.
        if self.tokens.get(name)==token and task.cancelled() and self.states[name]==State.LOADING:
            model=self.models[name]
            if model.cooperative_cancel and not model.remote:
                self.states[name]=State.UNLOADED;self.errors[name]='cancelled_cleanly'
            else:
                self.states[name]=State.PENDING;self.errors[name]='completion_unknown';self.uncertain=name
        if self.active==(name,task):self.active=None
        if not task.cancelled():task.exception()

    def valid_receipt(self,name,token,receipt):
        return (isinstance(receipt,Receipt) and receipt.name==name and receipt.model_id==self.models[name].model_id
                and receipt.token==token and receipt.operation_complete is True and receipt.verified_available is True
                and self.started_at[name]<=receipt.captured_at<=self.clock()
                and self.clock()-receipt.captured_at<=self.policy.max_reading_age_seconds)

    async def execute(self,name,token):
        model=self.models[name]
        try:
            receipt=await asyncio.wait_for(self.loaders[name](model,token),model.timeout_seconds)
            if not self.valid_receipt(name,token,receipt):raise LoadFailure('completion_not_verified')
            if self.uncertain==name:return State.PENDING
            self.values[name]=receipt;self.states[name]=State.READY
            return State.READY
        except asyncio.CancelledError:
            if model.cooperative_cancel and not model.remote:
                self.states[name]=State.UNLOADED;self.errors[name]='cancelled_cleanly'
            else:
                self.states[name]=State.PENDING;self.errors[name]='completion_unknown';self.uncertain=name
            return self.states[name]
        except Exception as exc:
            uncertain=(getattr(exc,'may_continue',True) and (model.remote or not model.cooperative_cancel))
            self.states[name]=State.PENDING if uncertain else State.FAILED
            allowed_codes={'completion_not_verified','connection_failed','model_missing','load_rejected','load_failed'}
            self.errors[name]=(exc.code if exc.code in allowed_codes else 'load_failed') if isinstance(exc,LoadFailure) else type(exc).__name__
            if uncertain:self.uncertain=name
            return self.states[name]
        finally:
            if self.active and self.active[0]==name:self.active=None

    def reset_failed(self,name):
        if self.active or self.uncertain or self.states[name]!=State.FAILED:
            raise AdmissionDenied('reset_not_safe')
        self.states[name]=State.UNLOADED;self.errors.pop(name,None)

    def reconcile(self,name,receipt=None,*,verified_idle=False,token=None,captured_at=None):
        """No API call here. A future trusted adapter must supply fresh, model-scoped evidence."""
        if self.active is not None or self.uncertain!=name:raise AdmissionDenied('operation_still_owned_or_not_pending')
        expected=self.tokens[name]
        if receipt is not None and self.valid_receipt(name,expected,receipt):
            self.states[name]=State.READY;self.values[name]=receipt
        elif (verified_idle and token==expected and captured_at is not None
              and self.started_at[name]<=captured_at<=self.clock()
              and self.clock()-captured_at<=self.policy.max_reading_age_seconds):
            self.states[name]=State.UNLOADED
        else:raise AdmissionDenied('fresh_completion_or_idle_evidence_required')
        self.uncertain=None;self.errors.pop(name,None)

    def update_mcp(self,availability,required,*,enabled,command_center):
        self.mcp=dict(availability);self.required_mcp=tuple(required)
        self.mcp_enabled=enabled;self.command_center=command_center

    def expire_remote_evidence(self):
        for name,model in self.models.items():
            if (model.remote and self.states[name]==State.READY
                    and not self.valid_receipt(name,self.tokens[name],self.values.get(name))):
                self.states[name]=State.PENDING;self.errors[name]='readiness_evidence_stale'
                self.uncertain=name

    def status(self):
        self.expire_remote_evidence()
        required_ready=(self.mcp_enabled is True and bool(self.required_mcp)
                        and all(self.mcp.get(name)=='ready' for name in self.required_mcp))
        return {'scope':'offline_design_prototype' if self.offline else 'unwired_adapter_candidate',
                'states':{name:state.value for name,state in self.states.items()},'errors':dict(self.errors),
                'text_client_constructed':self.client is not None,
                'text_model_ready':self.states.get('llm')==State.READY and self.uncertain is None,
                'asr_ready':not self.policy.text_only and self.states.get('asr')==State.READY,
                'required_mcp_ready':required_ready,'mcp_state':'disabled' if self.mcp_enabled is False else 'ready' if required_ready else 'unconfirmed',
                'command_center':self.command_center,
                'life_information_available':required_ready and self.command_center=='available',
                'background_model_operation':self.uncertain or (self.active[0] if self.active else None),
                'conversation_verified':False,'llm_response':'not_tested','tts_audio':'not_tested',
                'speaker_output':'not_tested'}
