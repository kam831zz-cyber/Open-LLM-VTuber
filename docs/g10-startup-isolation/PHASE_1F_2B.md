# Phase 1F-2B — one real isolated startup, stopped for low RAM

Executed baseline: 19ca3e7c727e7d3810d9adff4585028534c20d72, after all 70
baseline offline tests passed. Work branch: test/g10-readiness-phase1f2b.
Exactly one OpenLLM start, 127.0.0.1:12394, unchanged 300-second limit.
No production task/supervisor, inference, speech synthesis, microphone, Room or
production rollout. A private Windows Job contained the venv launcher PID 5340,
backend Python PID 15180 and its console host PID 15160. No restart was attempted.

## Preconditions

All protected listener PIDs matched earlier observations; HA VM was running.
Production HEAD/index/settings/character hashes and frontend's 226 staged deletions
matched. 12393, 12394 and production Command Center 18000 had no listener.
Available RAM was about 2.37 GiB at the monitor's preflight. CPU samples were
10-12 percent. Local small/ja/int8 model files and the official 3.11.9 venv were
present. SSL standard-library import passed. Native library files were present;
this does not independently establish every actual loaded DLL path or ABI.
Ollama had no loaded model before the trial.

## Outcome

First stop reason: **available_RAM_below_1.5GiB**, saved at controller +33.406 s.
15 resource samples, no supervisor/persistence error. Teardown completed at
+36.531 s. The candidate was still pending at Job closure. Exit code 0 is the
Windows Job termination status, NOT a normal voluntary application exit.

Available RAM at the stop sample was 1222762496 bytes (about 1.139 GiB).
The cleanup observation was lower still: 72642560 bytes (about 69.3 MiB).
Resource pressure could continue during the three-second cooperative grace and
while Ollama, an independently owned service, processed its preload request.
The trial must not be reported as stable or successful. A later read-only OS
check showed recovery to about 2.57 GiB, with only the original Ollama service
and no loaded model. No service or model was forcibly stopped to recover memory.

CPU samples: 14.44-47.82 percent (first short interval is null). Peak sampled
backend Working Set was 613494784 bytes (about 585.1 MiB); Private Bytes were
856494080 bytes (about 816.8 MiB). These are different measurements, not summed
as physical RAM. The rapid system RAM loss coincided with model preload; an
Ollama-only increment and successful model load were not measured or proven.

## Stages actually observed

Application-relative times below use the candidate clock; controller times use
a different origin. This executed version did not record the common clock, so
exact per-stage resource values are unavailable, not zero. The complete separate
resource series remains in the diagnostic result. No exact cross-clock alignment
or continuously measured peak is invented.

| Stage | Candidate-relative time / result |
|---|---|
| Python/native imports | begin 0.188 s, complete 9.922 s; interval 9.734 s |
| Configuration validated | 9.969 s |
| ASR local file resolution | 9.985-10.000 s; 0.015 s |
| ASR native import | 10.000-23.172 s; 13.172 s |
| ASR construction | returned before subsequent MCP discovery; exact time not persisted |
| MCP discovery | 25.704-25.750 s; 1 pass, all unavailable |
| Ollama qwen2.5:3b preload | began 26.360 s; no completion recorded |
| TCP / HTTP / WebSocket | not reached |
| Session initialization / MCP rediscovery | not reached; no rediscovery observed |
| Conversation core / required MCP readiness | unconfirmed / unavailable |
| LLM reply / TTS audio / speaker | not tested |

The ASR constructor returned because later ServiceContext stages were reached.
The 2.532 seconds from native-import completion to discovery start also include
TTS/VAD/registry setup; it is an upper bound, not an ASR constructor measurement.
The historical 130-195-second delay was not reproduced or explained by this run.

## MCP finding and offline-only corrections after the attempt

All four MCPs returned unavailable/RuntimeError in about 46 ms. The trial guard
accepted only list/tuple Popen argv. Installed CPython 3.11.9 subprocess.py converts
Windows argv using list2cmdline before the subprocess.Popen audit event. This
causes the guarded Windows MCP spawn to be rejected before starting a child.
This is a trial-guard incompatibility, not proof that all actual MCP servers fail.

The new code accepts only exact list2cmdline serializations of four fixed approved
argv sequences. It does not parse arbitrary Windows strings, add a shell, allow
extra arguments or accept another executable/cwd. Child processes still cannot
spawn further processes or access external APIs. No real MCP was started to test
the correction during this phase.

Two further diagnostic corrections: emit ASR metrics immediately on construction
return (not after a potentially blocking Ollama initialization); add a common
monotonic clock to application stages and monitor samples. Low-memory/other urgent
stops skip the cooperative grace and proceed to private Job closure. This frees
owned memory promptly but cannot cancel or control an already received request
inside the separate Ollama service. The fixes were made AFTER the single attempt;
they were not re-run with real models. Offline tests: **79 PASS** (70 existing +9).

## Readiness and cleanup

No WebSocket state notification was received. HTTP, WebSocket, constructed core,
and usable life-information features must not be declared restored. Required
weather/komugi-context/komugi-weather-ai and optional ddg-search were unavailable
in this guarded attempt. Command Center 18000 remained stopped; 18081 was never
used as a substitute. MCP transport readiness, even if reached later, would not
prove downstream service availability.

Job closure and identity checks found no remaining candidate/console/MCP process.
12394 was released and production 12393 stayed unused. Protected listener PIDs,
production files/index and frontend state remained unchanged. The original
diagnostics directory contains checkpoint.json, pre-cleanup.json, result.json and
stages.jsonl. A sanitized summary with file hashes is in
[phase1f2b-observation.json](phase1f2b-observation.json).

Do not advance to text or VOICEVOX tests. Before another separately approved start,
design a RAM budget for simultaneous ASR and LLM loading and validate the corrected
MCP guard. Do not reduce the 1.5-GiB gate, increase the deadline, stop existing
services or unload models without separate authorization. Deferred ASR/text-only
startup or a separate LLM host may be design options, not changes made here.
