# Phase 1F-2A — startup meter reliability, no models

Base: f98eb26434ef4e6eb1a049c86ef96298f97d8dd0.
Work branch: fix/g10-startup-meter-reliability.
No OpenLLM, ASR, Ollama model, MCP server, TTS or microphone was started.
No service/task settings were changed. candidate.py and guard.py are unchanged.

## Previous attempt: facts versus hypothesis

Preserved filesystem timestamps from the previous attempt show:

| Evidence (JST, 2026-11-01) | Time |
|---|---|
| owned.start write | 07:09:32.161065 |
| stop write | 07:09:32.203296 |
| gate-to-stop interval | about 42 ms |
| only application stage | native import begin, candidate timer +0.172 s |
| final CPU/PID/stop-reason series | missing |

The candidate's relative clock and filesystem timestamps are separate clocks;
no exact application start time is invented. The retained traceback shows the
supervisor failed querying the image of an exited process during post-Job cleanup,
before writing result.json. The original stop reason, original candidate identity
and resource peaks cannot be recovered. Original cause remains **UNKNOWN**.

The first dummy run reproduced an early `unexpected_owned_executable` stop:
Windows created a private console host after the suspended Python was assigned to
the Job and resumed. The old Python-only allowlist rejected conhost.exe. This is a
demonstrated meter defect and a plausible explanation for the earlier early stop,
not proof that it was the original reason. There is no evidence here of an ASR,
RAM, DLL or Code Integrity cause in that original attempt.

## Changes

* Separate pure `monitor.py` state machine and injected Windows backend.
* Persist candidate PID immediately after Popen and before identity queries.
* Launch suspended, assign to an unnamed private kill-on-close Job, record identity
  and membership, then resume. A failed assignment only terminates the retained
  creation handle of the still-suspended candidate, never a name-selected process.
* Persist first stop reason, all observed stop causes, exception class/phase/errno,
  elapsed time and partial measurements before cleanup. Do not log exception
  messages, configuration, environment, credentials or application data.
* Atomic checkpoint/pre-cleanup/final JSON writes use flush + fsync + replace.
  Persistence errors are explicit. An unwritable final destination produces a
  sanitized terminal fallback. No software can guarantee a file when storage is
  unavailable; such a trial is failed, never silently accepted.
* Each cleanup step is independent: a capture or stop exception cannot skip Job
  closure, wait, residual verification or final-save attempt. Preserve an already
  observed nonzero exit code instead of replacing it with cleanup status.
* Keep Available RAM, commit headroom, system CPU interval, per-process Working Set,
  Private Bytes and process CPU ticks distinct. A CPU interval shorter than 100 ms
  is null, not an invented zero. A failed identity query retains other measurements.
* PID reuse, exited/missing and access-denied are distinct. Access-denied is unknown,
  not proof of exit. Residual unknown processes prevent a clean result.
* Recognize a console host only by OS-reported System32 path, current private Job
  membership, measured Python parent and creation time not preceding that parent.
  No global conhost name exception or external process termination is introduced.
* An expected dummy has one Python worker; its OS console support process is
  counted separately. Other executables/workers and unexpected stop reasons fail
  the CLI even if the process exit code happens to be zero.

## Tests

70 offline tests PASS: the existing 47 plus 23 new reliability/ownership cases.
These include normal/immediate/abnormal exit; PID-query exception; Job cleanup;
CPU/RAM/Working Set separation; mid-sample exception; deadline; simulated low RAM;
cleanup/Job-close failure; checkpoint/final save; unknown residual; PID reuse;
access denial; and strict console-host classification. External connections,
subprocess creation and real native/model imports are blocked in this suite.

Two OS dummy runs were performed sequentially, never concurrently. Each explicitly
launched exactly one Python dummy. Windows additionally created one private console
host; no dummy-created child, application, model or network service was started.

| OS trial | Stop/result |
|---|---|
| 1, before console fix | false-positive reproduced; cause/PIDs/memory/final result preserved; no residual |
| 2, after console fix | dummy_observation_complete; 5 samples; no measurement/persistence error; no residual |

Confirmed dummy: PID 1556; its owned console host: PID 2276. The Job initially
contained only the suspended dummy. At closure it contained exactly these two
identities. The dummy deliberately ignored cooperative stop; it remained pending
until Job close, then was exited and the console host gone. Exit code 0 here is the
Job termination status, **not** evidence of a voluntary normal application exit.
Normal and abnormal voluntary exits were verified with mocks.

Confirmed OS run: 1.922 s total; valid CPU samples 21.53/3.68/6.62/2.94 percent;
minimum Available RAM 2562195456 bytes (about 2.39 GiB); peak dummy Working Set
16674816 bytes (15.90 MiB), Private Bytes 10489856 bytes (10.00 MiB). CPU/WS are
sampled values, not a continuously measured peak. First short CPU interval is null.
Checkpoints and pre-cleanup/final files exist and parse correctly. File hashes and
sanitized numeric results are in [meter-os-tests.json](meter-os-tests.json).
Full local diagnostics remain outside the repository under the two named diagnostic
directories. No environment or secret configuration is copied into saved reports.

## Reproduction and next gate

Use an **unused** runtime directory and the existing Python environment:

```powershell
& <existing-python> -I -B tools/readiness_trial/supervise.py --dummy --runtime <new-diagnostic-directory> --production <existing-production-repo>
```

The dummy flag bypasses application code, model/asset setup, HTTP and WebSocket
probing. Tests of errors, low-memory and timeouts use injected mock backends; no OS
memory pressure, CPU stress or service failures were induced.

Meter reliability is sufficient to propose another separately authorized single
OpenLLM startup. It is NOT proof of native-library/model/MCP/readiness success.
Recheck resources, protected services and all production hashes before that trial.
Maintain the 300-second deadline, RAM/CPU gates, private Job and first-stop records.
No text/TTS/microphone or production/Room integration is authorized by this result.
The old missing stop reason remains UNKNOWN, and the model-free test does not
establish behavior under full ASR/Ollama memory pressure or real MCP children.
