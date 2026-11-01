# Phase 1F-2D — model-free application integration candidate

Base: `2056e26c241319b2e9c35b2035ce8696f4f604b8`.
Branch: `feat/g10-model-free-staged-integration`.
Scope: isolated source, Mock/offline tests only. Not a production deployment or
proof of successful application startup/conversation/native ABI compatibility.

## Application path

An **opt-in** `system_config.staged_loading` object adds:

```yaml
staged_loading:
  enabled: true
  text_only: true
  asr_mode: disabled # disabled / lazy / eager
  eager_preload: false
```

This is documentation, not an edited production configuration. Existing settings
default to `enabled: false` and retain the legacy behavior, including legacy eager
initialization. The candidate does not make the legacy path memory-safe.

`ServiceContext.load_from_config()` creates one shared `StagedController`.
Disabled/lazy ASR does not invoke ASRFactory; text-only overrides eager ASR. VAD
and audio translation are not initialized in the candidate. Native ASR model
modules are not imported on this branch of initialization. Common array/SDK
imports are not claimed to disappear. Tests execute actual class methods with
injected dependencies; a full server import/start has **not** been tested.

AgentFactory restricts this candidate to BasicMemoryAgent + Ollama; other agents
and providers fail explicitly. TTS is restricted to the existing VOICEVOX
constructor (which may create a local cache directory, but does not synthesize).
No VOICEVOX/ACK implementation or event schema was changed.

Ollama client construction is separated from `preload_model()`. The factory
forces `eager_preload=False` and `unload_at_exit=False` for staged mode. Direct
preload calls cannot bypass its controller. Neither destruction nor session
disconnect unloads a shared server model. Normal chat generation is guarded
before invoking the existing transport, so an unready model cannot be implicitly
loaded by the first text request.

`load_cache()` shares only the model controller and existing stateless LLM;
MCPClient, ToolExecutor, history, callbacks and client_uid remain session-local.
Mode changes in an already constructed server context are rejected.

## Readiness and speech input

Client constructed, model ready, ASR ready, MCP ready, and functional conversation
verified remain separate. Unloaded LLM => `conversation_core_ready=false` even
when the agent/TTS wrapper exists. ASR disabled does not prevent a future verified
text-model readiness, but does prevent microphone readiness. No `start-mic` is
sent; unsolicited audio chunks and microphone end events are also discarded
before buffering/transcription. Text events retain the existing dispatcher.

MCP readiness is computed from the actual session's required-server evidence.
Command Center 18000 remains `not_checked`; life-information availability is
false until separately verified. 18081 is not substituted. LLM response, TTS and
speaker success remain `not_tested`. Optional ddg failure does not invent MCP
availability. Existing MCP ownership/discovery tests remain in the regression run.

## Memory and shared-Ollama safety

The original pure policy moved to `src/open_llm_vtuber/model_loading_policy.py`;
the tools path re-exports it, preserving the previous design tests without a
second policy implementation. Application and design tests now use the same gate.

- Physical reserve is at least **1.5 GiB**, plus the existing 0.25 GiB policy margin
  and a measured incremental peak. Commit headroom is checked separately.
- The Windows reader uses GlobalMemoryStatusEx and GetPerformanceInfo, with a
  monotonic capture time; missing values and readings older than 2 seconds fail.
- Real-runtime fixture budgets and unknown budgets fail. No model budget was
  promoted to measured in this phase.
- Existing Runtime serializes different models and coalesces duplicate requests.
  Cancellation/timeout with uncertain remote or native completion latches PENDING;
  no further model load is admitted until trustworthy reconciliation.
- An injected, fresh, exclusive Ollama ownership observation is required. A local
  lock, an empty `/api/ps`, or free RAM alone is **not** an exclusivity proof.
  Unknown ownership, another model, or stale ownership evidence rejects loading.
  Readiness is suppressed after ownership evidence is lost.

**Deliberate remaining barrier:** no real loader or Ollama-wide ownership adapter
is registered by default. Eager modes therefore fail closed rather than loading
without measured budgets. Lazy ASR is represented as unloaded; there is no new
automatic lazy-load-on-microphone endpoint. Future admitted ASR adapters must
return a verified instance as well as completion evidence. This candidate proves
model-free construction and admission wiring, not real deferred model loading.

## Unexplained runner investigation

Previous observation: qwen2.5:3b runner PID 11084 was created at 2026-11-01
08:59:53 JST under existing Ollama PID 13840, after the previous trial had stopped.
Its later disappearance was observed without this task unloading/killing it.

Read-only inspection in this phase found:

- Production OpenLLM retains constructor preload and chat paths.
- Command Center `backend/integrations/ollama.py` can POST `/api/chat`; its default
  model is qwen2.5:3b. A possible path is not attribution evidence.
- HomeAI tasks invoke existing PowerShell launchers; inspected task action
  metadata did not directly contain chat/generate requests. OpenLLM and Command
  Center tasks were Ready. No task was executed.
- Allowlisted metadata extracted from the existing Ollama stdout log showed
  GET `/api/ps` at 08:17:54, POST `/api/chat` status 499 at 08:19:35, and GET
  `/api/ps` at 08:20:03 and 09:05:26. These records do not identify the requester
  of the 08:59:53 runner. Request bodies, addresses, conversation data and
  environment values were neither output nor saved.

**Requester: UNKNOWN.** Parent PID identifies Ollama as runner owner, not the HTTP
caller. No API call was made for this investigation; no additional tracing or
request-body logging was enabled.

## Offline validation and protection

Run `tools/verify_startup_offline.py` with the existing isolated Python environment.
The harness blocks external socket connections, subprocess creation and native
model imports; model constructors/transports are Mocked. See `test-results.json`.
All **136 tests PASS** (107 existing + 29 new integration cases; no skips).
Python syntax validation passed for 12 changed Python files; diff whitespace and
credential-signature checks passed. These pattern checks are not a proof that all
possible secrets can be recognized.

Existing 107 cases are retained; new integration cases exercise actual class
methods/factories with dependencies injected. This is not a real WebSocket test.

No production source/settings/task/index, Room port, model or dependency was
changed. No real server/model/inference/TTS/microphone was started. Production
frontend HEAD, required gitlink, 226 staged deletions and parent index hash are
checked separately in `phase1f2d-preservation.json`.

## Next smallest separately approved trial

1. Review candidate and opt-in settings; recheck resource/port/Git preservation.
2. Permit one model-free isolated server instance with eager preload off, ASR
   disabled, VAD/translation off, existing VOICEVOX wrapper only. Use existing
   test ownership/Job and isolated writable directories; do not use run_server's
   config/submodule update path. Audit MCP launches before authorizing any.
3. Observe TCP, HTTP, one WebSocket status, no start-mic, unloaded model statuses,
   and zero Ollama/TTS requests. No text/microphone request is needed.
4. Stop only the proven trial Job; check child ownership, port release and
   protected services. Do not unload unrelated Ollama models.

Actual model loading remains blocked pending measured physical/commit budgets,
process-wide ownership coordination with other Ollama clients, a reviewed load
adapter and separately approved model trial. A cancelled HTTP request does not
prove the Ollama server stopped processing it.

Rollback: this is an independent branch/worktree only. Production needs no
rollback. Do not replace production source with this candidate; no merge/PR or
production startup is part of this phase.
