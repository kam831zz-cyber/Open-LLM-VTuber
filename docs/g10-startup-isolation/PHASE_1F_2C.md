# Phase 1F-2C — memory budget and staged-loading design

Base: 8150dbf8aa26023e9614bacad34c4f8a281eb89a.
Branch: design/g10-staged-model-memory-budget.
No OpenLLM start, model load/inference, speech generation or microphone operation
was performed by this work. No production code, configuration, service or VM was
changed. Implementation is an **unwired offline design prototype** under
tools/staged_loading, not an application rollout.

## Actual G10 memory inventory

Installed DIMMs: 16.00 GiB. OS-visible: 12.9477 GiB. Difference: 3.0523 GiB.
The Radeon adapter reports 3 GiB, consistent with a substantial UMA reservation,
but this does not prove that the entire installed/visible difference is UMA.
No BIOS/GPU/Windows setting was inspected invasively or changed.

At the initial 08:56 observation, Available RAM was about 2.4-2.5 GiB and commit
about 15.31/19.70 GiB. At the saved 09:03:34 observation:

| Counter | Value |
|---|---:|
| Available RAM | 0.571 GiB |
| Committed / commit limit | 17.023 / 19.698 GiB |
| Commit headroom | 2.675 GiB |
| System cache counter | 0.211 GiB |
| Paged / nonpaged pool | 0.449 / 0.548 GiB |
| Pagefile allocated / used / peak | 6912 / 959 / 2654 MiB |

The system cache counter is not a complete measurement of standby/cached RAM.
These counters and per-process readings are short sequential snapshots, not an
atomic OS-wide accounting. Commit is virtual memory backing demand; it is not
resident physical use. Shared Working Set pages can occur in multiple processes.
Never add WS + Private Bytes + commit, or sum process WS as exact physical use.

| Process at saved observation | PID | WS MiB | Private Bytes MiB |
|---|---:|---:|---:|
| llama-server | 11084 | 1972.3 | 2195.3 |
| ChatGPT | 2880 | 1071.0 | 1247.2 |
| ChatGPT | 13576 | 335.9 | 1102.8 |
| ChatGPT | 14244 | 596.8 | 676.5 |
| Codex | 8120 | 327.0 | 288.9 |
| VOICEVOX port owner | 13636 | 5.8 | 293.2 |
| Ollama service (not its runner) | 13840 | 38.2 | 60.7 |
| 18081 port owner | 8548 | 31.7 | 46.2 |
| Japan Monitor port owner | 14196 | 3.1 | 33.5 |

The full inventory includes Chrome, OS/security processes, service owners and
VirtualBox. Home Assistant VM is running, configured for 4096 MiB and two CPUs.
VBoxHeadless PID 5796 has WS about 53.7 MiB / private about 47.7 MiB, with additional
VBox processes recorded separately. Neither that small process footprint nor the
4096-MiB setting establishes total host physical RAM used by the VM. No VM RAM was
reassigned and no process was closed to make room.

### Concurrent Ollama activity discovered during this task

The initial process observation did not contain llama-server. Later PID 11084
appeared, created at 08:59:53, parent Ollama PID 13840. This task sent no model-load
or inference request. The calling origin was not identified; conversation logs,
command lines and credentials were not collected. One read-only GET /api/ps at
09:05:26 showed qwen2.5:3b, reported size 2164166490 bytes, VRAM size 0 and expiry
09:09:22. This metadata is not a measured physical allocation or transient peak.
At 09:10 the runner was no longer found and free RAM was again about 2.49 GiB.
No unload/kill was issued. Expiry timing is consistent with automatic release,
but the termination cause was not independently established.

This observation is separate from the previous startup experiment. It demonstrates
that another client can change the available budget while this candidate is idle.
Per-process serialization cannot control all clients of a shared Ollama service.

## Previous Phase 1F-2B: what can be attributed

The raw resource series and application stage series are retained in
phase1f2c-previous-memory-analysis.json. The clocks had different origins, so exact
phase-aligned deltas are unavailable. Approximate sequence, not isolated costs:

| Controller time | Available GiB | Backend WS / private MiB |
|---|---:|---:|
| 0.625 s | 2.367 | backend not yet sampled |
| 9.953 s | 2.239 | 128.8 / 337.8 |
| 24.078 s | 2.104 | 276.6 / 503.4 |
| 26.406 s | 1.891 | 585.1 / 816.8 |
| 31.062 s | 1.843 | 545.2 / 770.3 |
| 33.406 s | 1.139 | 545.2 / 770.3 |
| cleanup observation | 0.068 | processes being released; no matching WS sample |

Application-relative imports lasted 9.734 s; ASR native import 13.172 s; local file
resolution 0.015 s. ASR construction completed before discovery, but its independent
memory increment and exact constructor time were not saved. The Python process
contains shared native imports, ASR, TTS/VAD, registry and client setup; its whole
footprint cannot be assigned entirely to ASR. MCP discovery was rejected by the
then-broken Windows audit guard, so a normal MCP-process memory budget is unknown.

Ollama preload began at candidate +26.360 s. No successful completion was recorded.
The backend Python WS was roughly stable in the last samples while system
Available RAM dropped markedly. This is consistent with allocations outside that
Python process, including the separately owned Ollama service, but no old runner
series proves an Ollama-only delta. The old model file size cannot substitute for
runtime peak measurement. After stop, a three-second cooperative grace remained;
the separate service can continue processing an already received request even
after the candidate is cancelled. These explain possible continued pressure,
not a fully measured allocation breakdown. Phase 1F-2B shortened urgent grace;
it still cannot cancel independent server work or guarantee a global RAM floor.

## Admission budget: current answer is fail closed

There is no verified standalone peak increment for client-only startup, ASR or
cold Ollama load. All three real budgets remain null/unknown in
phase1f2c-policy-proposal.json; **no real load is admitted** by that proposal.
Evidence available for planning, not certified caps:

* Entire old backend: peak sampled WS about 585 MiB, Private Bytes about 817 MiB.
  This is not the isolated ASR increment and not a continuous peak.
* Current already-resident qwen runner: WS about 1.93 GiB, Private Bytes about
  2.14 GiB, separate API-reported size about 2.02 GiB. Cold-load, KV/context and
  temporary allocations are not bounded by those steady-state values.
* MCP's normal successful process footprint and live text-only import footprint
  have not been measured in isolation.

Proposed rule: Available >= 1.5 GiB reserve + approved physical peak increment +
0.25 GiB provisional margin; commit headroom >= approved commit peak increment +
margin. Check fresh data before client/import startup and every model transition.
For example ONLY: an approved 1-GiB incremental cap requires >=2.75 GiB Available;
a 3-GiB cap requires >=4.75 GiB. These example caps are not measured ASR/LLM budgets
and are not installed as settings. Merely exceeding the 1.5-GiB floor is insufficient.

At 0.57 GiB, even model-free client startup must be rejected. At about 2.5 GiB,
simultaneous ASR + local LLM with the reserve is still not demonstrated feasible.
Sequential loading reduces overlap of transient allocations; it does not eliminate
the combined resident memory if both models must stay loaded.

## A/B/C comparison

| Option | RAM and benefit | Constraints / difficulty |
|---|---|---|
| A: eager ASR + Ollama | requires core + both resident models + transient peaks + reserve; exact requirement unknown | previous attempt exhausted headroom; minimal code changes but not acceptable unchanged |
| B: staged, text-first | can avoid ASR/import cost and constructor preload; rechecks each transition, refuses unknown budget | moderate/high integration work; eventual dual residency still must fit; other Ollama clients bypass its lock |
| C: remote LLM | G10 needs client + requested ASR/TTS/service budgets rather than a local LLM increment | host/network/auth/timeout operations required; G10 ASR still needs measurement; local resident models do not disappear automatically |

Recommend B's admission/lifecycle controls as the immediate safety foundation and
an ASR-disabled text-first integration candidate. For sustained ASR + LLM on this
already-busy G10, prefer evaluating C rather than repeating A or weakening gates.
Neither B nor C authorizes changing the currently shared service or unloading its
models. Keep all existing services intact until a separately approved resource plan.

## Prototype and integration boundary

tools/staged_loading/policy.py has only injected memory/client/model providers.
It imports no application engine, model library, HTTP client or subprocess module.
StartupPolicy controls eager_preload and disabled/lazy/eager ASR. Default text-only
startup never invokes a model loader and still requires a client/import budget.
Each component has unloaded/loading/ready/failed states, plus pending-unknown for
noninterruptible or remote work. Missing budgets, stale/unknown readings and
insufficient physical/commit headroom are refused before calling the provider.

Duplicate requests share one task; different models cannot load concurrently.
Failures do not auto-retry. Clean failure needs explicit reset. Cancellation of an
actual native constructor must not be modeled as cooperative task cancellation.
Remote timeout/cancel and noninterruptible local cancellation retain an uncertainty
latch; later loads are denied until fresh operation/model/token-scoped evidence
proves completion or idle, after the local owner has finished. HTTP 200 alone is
not completion evidence. Expired remote readiness becomes unconfirmed again.

MCP readiness is independent metadata, never a model load trigger. Command Center
stopped means life-information unavailable even if MCP transports are ready;
18081 is not a substitute. Client constructed, model available, speech input and
verified conversation are separate. LLM response/TTS/speaker remain not tested.

**No src/ production-path code is modified here.** Actual OllamaLLM still eagerly
preloads. The prototype's client_factory(eager_preload=False) is a fixture contract,
not an argument currently accepted by that constructor. Text-only OpenLLM startup
is not yet implemented or validated. Proposed future integration points:

1. config_manager/stateless_llm.py OllamaConfig: explicit eager_preload policy;
   agent/stateless_llm/ollama_llm.py constructor: separate client construction,
   explicit guarded load, ownership-aware cleanup; never unload an unowned model.
2. service_context.py load_from_config/init_asr/init_agent and ASRFactory: defer
   ASR/native imports and bind to an owned worker; no hidden model download.
3. startup_status.py and websocket_handler.py: append honest capability states;
   preserve ACK protocol; suppress start-mic when ASR disabled. Room remains unchanged.
4. MCP discovery/session lifecycle: initialize separately using existing ownership
   code, retain required/optional failures. Do not duplicate life-information logic.
5. A single approved model coordinator plus owned-memory watchdog: reject external
   competing loads, use fresh identity/config-scoped resident checks, account for
   settled allocations and stop candidate-owned work promptly. A Python lock alone
   cannot serialize another process or stop the Ollama server's pending work.

For C, use a server-side allowlisted endpoint, secrets kept out of the browser,
verified TLS/auth where appropriate, bounded connect/read deadlines, no blind
retry and no automatic local-model fallback. Network failure must expose LLM
unavailability, not invent a reply or initialize another local model. No network
settings or external host connection were made in this phase.

## Verification and next permitted steps

Offline regression: 79 unchanged tests plus 28 new design tests, total 107 PASS.
All model providers/readings/client constructors are mocks. Covered: preload off,
state transitions, unknown budgets, physical and commit refusal, duplicate/parallel
loads, failure, cooperative/noninterruptible/remote cancellation, MCP incomplete,
ASR absent, Ollama connection failure, stale receipts and client-start refusal.
No model or OS stress test was run. During low-memory observation, extra heavy
work was deferred; final regression followed recovery above 1.5 GiB.

Next: review the unwired integration plan; approve a model-free text-only adapter
implementation first. Any real calibration requires separate approval, explicit
provisional process/remote memory bounds, Available reserve plus full peak budget,
commit headroom, sustained CPU <80%, protected PID/hash checks, isolated writes,
private Job and a finite deadline. Measure startup-only, ASR-only and LLM-only
separately before considering combined residency. Do not unload other clients'
models, change pagefile/UMA/VM settings or stop applications to obtain a pass.

Remaining: exact transient peaks, real adapter/owned-native-worker integration,
shared-Ollama request ownership, external-host sizing/security, and actual
conversation/audio readiness. No real next-start approval is implied.

Final 09:13 OS check: Available RAM about 2.53 GiB, no llama-server process in the
OS list, protected listener PIDs unchanged, HA VM running. Production HEAD/index/
config/character hashes and frontend HEAD/gitlink/226 staged deletions are unchanged.
See phase1f2c-completion-checks.json. No model, service or VM was stopped by this
task; the existing production eager-start path and scheduled tasks remain unchanged.
