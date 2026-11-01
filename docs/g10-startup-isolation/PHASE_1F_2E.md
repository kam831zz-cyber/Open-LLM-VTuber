# Phase 1F-2E: model-free preflight and preload regression

Base: `07f891020d84df5ac8b871bda82f94268dbdd0ae`.
Branch: `fix/g10-model-free-preflight`.
Isolated source only. No actual OpenLLM, model, MCP subprocess, inference, TTS,
microphone, or application API request was started/executed in this phase.

## Confirmed regression and fix

`OllamaLLM.preload_model()` referenced `base_url`, `model`, and `keep_alive` as
undefined locals after extraction from the constructor. The resulting NameError
was caught and logged without stopping construction. This could appear to be a
successful legacy initialization even though no preload was sent.

The method now uses instance fields, checks HTTP errors, parses the response and
requires `done is True` without an error field. Connection/HTTP/JSON/incomplete
responses raise a sanitized `RuntimeError('ollama_preload_failed')`. Logs contain
only exception type, not URL, payload, exception text or credentials. Cleanup is
not armed by a failed constructor; it does not attempt an extra unload in reaction
to the failure. A failed HTTP request still does not prove remote work stopped.

Legacy preload request construction is Mock-tested, including trailing `/v1/`,
model name, keep_alive and timeout. Staged construction bypasses preload and
cleanup sends no POST. Existing production settings and source are unchanged.

## Dedicated model-free preparation path

Do **not** run `tools/readiness_trial/candidate.py` for a model-free trial: it
explicitly imports ASR, selects small, installs model timing hooks, and allows an
Ollama preload. It is retained unchanged for historical model trials.

New `tools/model_free_trial/` contains preparation helpers, **not an executable
server launcher**. No CLI/start/listen/uvicorn entry point is enabled here.

`isolated_settings()` deep-copies its input and changes only the candidate's
in-memory settings:

- loopback and non-protected trial port; no default alternate-character reload;
- staged enabled, text_only true, ASR disabled, eager_preload false;
- MCP use false and enabled-server list empty for this first trial;
- Fast Path disabled (no access to 18000 or substitution of 18081);
- Ollama preload/unload disabled, VAD disabled;
- only BasicMemoryAgent + Ollama client + existing VOICEVOX wrapper accepted.

Public default-template validation is tested without environment substitution.
No production configuration is read/copied/edited by these tests. A future
supervisor must explicitly select the intended character in memory before
applying this transform; it must not log the original configuration.

`prepare()` validates admission **before** the engine/server factory and returns
an initialized but **unstarted** object. Tests inject a Mock factory and do not
construct an actual server. It wraps the returned ASGI application in
`ReadinessOnly`: only `GET /openapi.json` and `/client-ws` handshake/initial server
notifications pass. Incoming WebSocket data is closed before conversation,
microphone, config switching or tool dispatch. Other HTTP paths/methods are 403.

The model-free guard adds no-child/no-outbound rules to existing isolated-write
and loopback-bind containment, and blocks known native model imports. It must be
installed **before any application imports** by a separately reviewed future
launcher. It is a Python defense-in-depth guard, not a Windows security sandbox.
Its policy is Mock-tested here; no actual guarded application is launched.

Existing initial-message code is exercised directly with Mock dependencies:
LLM and ASR states are unloaded, model readiness and conversation readiness false,
MCP explicitly disabled (not secretly reported available), and zero start-mic
messages. A TCP connection or HTTP 200 is not evidence of conversation readiness.
There was no real TCP/HTTP/WebSocket service trial in this phase.

## Separate server-startup budget

ASR/LLM model budgets are **not** reused for server startup. `ServerBudget` has
separate physical-peak and commit-peak increments and evidence/profile fields.
The current registered/default budget is `unknown`, with both values null.

Profile: `model-free-v1:mcp-off:asr-off:ollama-preload-off:voicevox-wrapper`.
MCP-off excludes subprocess startup from this profile; a later MCP-enabled
profile needs its own evidence, ownership review and resource measurements.

Admission requires:

1. Measured evidence for the exact profile/code/Python environment, with an
   auditable evidence identifier; fixture budgets are accepted only offline.
2. A fresh OS observation (both memory and runner capture no more than 2 s old).
3. No existing Ollama runner and no unknown/incomplete process inventory.
4. Available RAM >= **1.5 GiB + 0.25 GiB + server physical peak increment**.
5. Commit headroom >= **0.25 GiB + server commit peak increment**.

Missing, non-integer or invalid measurements fail closed. A runner-free instant
does not lock out other clients; a future real trial needs continuous monitoring
and must stop on resource/protection changes. Recheck immediately before process
creation, not just before generating an execution plan. No admission helper here
automatically starts a process.

### What historical measurements can prove

Phase 1F-2B recorded Python/native import interval 9.734 s, ASR native import
13.172 s, and backend peak Working Set ~585.1 MiB / Private Bytes ~816.8 MiB.
Those peaks include ASR and are neither incremental model-free startup costs nor
system commit increments. They cannot be summed or assigned as this budget.
MCP discovery took ~0.046 s but all servers failed in that trial; this is not
proof of the cost of successful required MCP initialization. ASR and LLM model
budgets remain unknown as well.

No saved evidence isolates the complete model-free sequence of SDK/library
imports, Live2D setup, BasicMemoryAgent, VOICEVOX wrapper, framework initialization
and one session. These startup costs remain **unmeasured**. The VOICEVOX wrapper
may create a cache directory; future writes must remain inside a dedicated
runtime directory. Real runtime/native imports have not been validated here.

### Current OS observation

The OS-only `observe_windows()` reader was executed once, without APIs/models:
Available 2,042,449,920 bytes (~1.902 GiB), commit headroom 4,421,619,712 bytes
(~4.118 GiB), zero enumerated Ollama runners. It uses the existing memory reader
and Toolhelp process inventory, never command lines or environment values.
See `phase1f2e-memory-preflight.json`. Admission was **denied** because the server
budget is unmeasured. Earlier in this task availability was ~1.716 GiB, below
even the 1.75 GiB reserve/margin before any server cost. Values fluctuate and
are not a standing permission to start. Later OS/protection values are recorded
separately in `phase1f2e-preservation.json`.

## Offline results

**160 PASS = existing 136 + new 24; zero failures/errors/skips.**
`tools/verify_startup_offline.py` blocked real connections/processes/native model
imports. New tests cover legacy request fields and failures, staged zero POST and
unload, settings isolation/schema, protected ports, admission-before-factory,
physical/commit/stale/runner/unknown-budget rejection, native/process/network
guards, readiness-only ASGI requests, WebSocket unloaded state and no Mic start.
Existing model-free ASR Factory-zero tests are retained. See `test-results.json`.

## Protection and next decision

Production HEAD/index/settings/character/MCP hashes, frontend HEAD/gitlink and
226 staged deletions, protected port owners and HA running state were compared.
No existing service/task/Room source was changed. No network test/API call or
model operation was performed. GitHub communication is limited to the authorized
dedicated-branch save; no PR/merge/force push.

**A single real model-free startup is not yet admissible under the current
budget rule.** Next requires a separately approved, bounded model-free budget
measurement plan (incremental imports/wrappers, no MCP/models/API calls, owned
process/Job, continuous resource guard, sufficient baseline headroom), followed
by review of measured physical and commit peaks. Do not label a guessed ceiling
as measured or bypass admission to obtain it. Launcher/guard integration must
also be reviewed before any real listener/WebSocket trial. The future trial must
retain timeout/first-stop records and stop only proven owned processes.

Production rollback is unnecessary: this branch/worktree alone contains the
candidate. Do not copy it into production or run the old model-loading candidate.
