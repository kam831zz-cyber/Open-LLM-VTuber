# Phase 1F-2: single-start attempt, readiness NOT established

Follow-up: [Phase 1F-2A](PHASE_1F_2A.md) reproduced a console-host false-positive
with a model-free dummy and validated durable result recording. The original
attempt's missing stop reason remains UNKNOWN; this historical result is unchanged.

Base: c3578025fc136901bbf939b35d20110afa823a0c.
Branch: test/g10-openllm-readiness-single. No production rollout.

## Readiness correction

An empty availability mapping previously made all(...) return True. The new
predicate requires an explicitly enabled configuration, a nonempty expected
required-server list, evidence for every required server and constructed session
client/manager/executor. Missing fields fail closed. Disabled MCP is reported as
disabled and is not conflated with verified required-MCP readiness. ddg-search
remains optional. Six new readiness mock tests plus the previous 36 passed before
the actual attempt. Five cleanup/guard mocks were added afterward: total 47 PASS.

## Authorized attempt: 2026-11-01 JST

One candidate was launched using the existing official Python 3.11.9 OpenLLM venv,
from an independent worktree, bound configuration 127.0.0.1:12394, deadline 300s.
No scheduled task, production supervisor or production probe was used.
The backend entry bypasses run_server's submodule/config upgrade behavior.
Production configuration is read into memory; no secret config is copied.
The existing local small/ja/int8 snapshot is selected. Local files were verified
before launch: model.bin 483546902 bytes, config.json 2370, tokenizer.json 2203239,
vocabulary.txt 459861. No model download is permitted.

Candidate-only isolation changes: no proxy, separate write/cache/history/temp
directories, finite Ollama lease 60s, unload_at_exit=False. Only the existing
qwen2.5:3b preload payload without messages is permitted; inference/TTS HTTP is
rejected. MCP discovery uses the current interpreter and an additional trial guard
for stdio initialization/list-tools, with all child network requests prohibited.
This guarded trial is not proof of unguarded production behavior.

The candidate is assigned to a private kill-on-close Windows Job before the start
gate is released. Owned process measurements include PID, creation time, executable
and parent, but this attempt's aggregate measurements were lost as described below.

## Observed result and measurement defect

Start gate timestamp: 07:09:32.161 JST.
The sole recorded application stage is python_native_import_begin at +0.172s from
the candidate's own timer. No import-complete, ASR, MCP, TCP, HTTP or WebSocket
readiness was established. The stop file was written at 07:09:32.203 JST.

The supervisor encountered identity_path_failed while checking an already-ended
owned process after closing its private Job. This prevented the final result.json
from being written, including the in-memory stop reason and CPU/working-set series.
The initial stop reason cannot be recovered and is UNKNOWN. It must not be labeled
ASR delay, RAM exhaustion, Code Integrity rejection or application startup failure
without evidence. No second start was attempted.

Cleanup measurement now checks process exit time and handles the race between
image-path and exit-code queries. Five offline mocks cover this and trial guards.
Samples/stop reason are persisted before teardown in future runs. These changes
have NOT been revalidated with another real OpenLLM launch.

Before the attempt Available RAM was about 2.36 GiB, CPU samples 62/5/4 percent
(not sustained overload). Afterward Available RAM was about 2.34 GiB at the initial
check. Peak RAM, process working set and staged CPU are NOT AVAILABLE. Ollama model
lists were empty before and after; no preload stage was recorded. Thus model-load
RAM impact and ASR timing breakdown remain unmeasured.

## Protection and next gate

Post-attempt OS checks found no candidate command line, no listener on 12394 or
12393, unchanged protected listener PIDs, and no new 3033/3077 events in the checked
window. Private Job close limits termination to the assigned candidate lineage.
No additional process termination by name or other service operation was performed.

Readiness remains UNCONFIRMED; do not proceed to text, TTS, microphone or Room
integration. First validate checkpointing and the monitor's initial stop conditions
with a non-model mock/dummy process; then obtain approval for another single real
start. Never extend the deadline or bypass resource/security gates to obtain a pass.
Command Center 18000 remains stopped; 18081 is never substituted. MCP transport
initialization alone would not prove downstream life-information API availability.
