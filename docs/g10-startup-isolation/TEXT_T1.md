# Text T1 — bounded calibration launcher, not an execution approval

Base: `d72e6dc0df58891150d7c8f694fcea9776baa6ab`.
Branch: `feat/g10-text-calibration-supervisor`.
This phase implemented and Mock-tested the worker/monitor. **No actual worker,
OpenLLM server, Windows Job, model, MCP process, inference or TTS was started.**
Only separate read-only OS/protection inventory was performed.

## Files and scope

`tools/text_calibration/launch.py` defaults to review-only. Real execution requires
an explicit `--execute-calibration` flag plus an external approval file, its
SHA256, an unused diagnostics directory and the production path (hash/read only).
It requires the existing official 3.11.9 `openllm-venv3119`, clean/pinned source,
and a valid unexpired approval. Do not use the old model-loading candidate.

`approval.template.json` is intentionally **authorized=false**, with null limits,
run ID, source SHA and expiry. It cannot authorize a run. No real envelope was
selected, approved or registered during T1. Actual approvals/runtime data must be
outside the repository and under diagnostics; never commit them automatically.

The new calibration scope is:

`text-only-v1:mcp-off:asr-off:tts-off:ollama-preload-off:calibration-asgi-one-session-no-listener`

It measures public-template configuration and an in-process session, not a real
TCP/HTTP/WebSocket connection. Production config, persona secrets, `.env`, models
and credentials are not copied into the worker. A production-character profile
or listening/HTTP/WebSocket profile needs its own evidence/review.

The worker is launched suspended, attached to a private random named Job, identity
checkpointed, then resumed. It verifies its own membership using a temporary
query-only Job handle, closes that handle, installs guards before application
imports and disables application log sinks. It reads the committed public config
template without environment interpolation. Candidate settings explicitly turn
off ASR, VAD, TTS/VOICEVOX, Live2D, translation, MCP, Fast Path, eager preload and
text requests. Forbidden engines are checked to remain None after context setup.

The Python audit/import guard denies outbound requests, subprocess creation and
known native model imports. There is no uvicorn/listen/API/chat stage. An asyncio
internal socketpair is the existing restricted exception. These are Python
defense-in-depth controls, not an OS firewall/security sandbox; unexpected native
dependency behavior still needs review. SDK/environment/native compatibility has
not been established by these Mock tests.

## Distinct authorization and provisional caps

`policy.py` is a **separate calibration-only gate**. Normal `admit_server()` and
measured-budget policy are unchanged. A calibration receipt always says
normal_startup_admitted=false and models_admitted=false. Results never update or
promote ServerBudget/model budgets automatically.

The later human-reviewed approval must pin:

- full source SHA, exact scope, one run ID / max_runs=1;
- positive physical envelope, aggregate Job commit envelope and rapid-drop trigger;
- CPU rate cap (1–50%), deadline (at most 120 seconds), expiry within one hour;
- a written rationale and `budget_evidence=provisional_not_measured`.

Cap selection must account for existing measured bootstrap/import context,
unknown full imports/framework/session costs, other services' fluctuations and
the requested measurement scope. Existing ASR-inclusive 585 MiB WS / 817 MiB
Private Bytes are **not** measured text-only costs. Model files are not budgets.
Do not derive a "safe" cap solely by subtracting the reserve from current free
RAM. If no defensible envelope fits, defer; do not stop other services or widen it
automatically. A too-small envelope may safely fail without completing useful
measurement. That failure is not a measured normal startup budget.

Admission occurs in preflight and again immediately before Job/process creation:

1. Fresh OS physical/commit readings (at most 2 s), no Ollama runner, complete inventory.
2. Available RAM >= **1.75 GiB + approved physical envelope**.
3. Commit headroom >= **0.25 GiB + approved Job commit envelope**.
4. Protected listeners/files and VM process identities confirmed; sustained high
   preflight CPU refuses. Unused test port is checked without starting a listener.
5. Approval hash/scope/expiry/source/environment verified. A local exclusive-use
   receipt in the shared diagnostics ledger reserves the attempt before Job creation.
   Copying the approval to a different folder does not create a new run allowance.
   It cannot be reused, even if
   subsequent setup fails; no automatic retry occurs.

This file-based mechanism is not cryptographic proof of human consent. Actual
execution still requires the separate user authorization and reviewed values.

## Job and monitoring limits

The private Job has KILL_ON_JOB_CLOSE, hard per-process and aggregate **commit**
limits, a hard CPU rate cap, and active-process limit 4 for the venv redirector,
interpreter and Windows console support. No MCP argv is allowed. Python descendants
must match the approved interpreter paths and expected redirector-parent topology;
console hosts retain existing exact-path/parent/creation-time checks. Unsupported
Job limits, assignment failure or ownership uncertainty fail without fallback.

Job memory limits are not a hard physical-RAM reservation. Physical consumption
is monitored separately using system availability and a conservative sum of owned
Working Sets. The latter can double-count shared pages and is an upper bound,
**not** attributable physical consumption. Private Bytes, Job peak commit and
system commit headroom are recorded separately and never naively added.

Samples target 250 ms; a gap over 1 s stops on the next observed sample. Stops
include reserve depletion (physical 1.75 GiB / commit 0.25 GiB), approved envelope
or rapid-drop trigger, runner appearance/unknown inventory, missing/error metrics,
sustained >=80% system CPU, protected changes, unexpected processes, deadline,
checkpoint failure and phase mismatch. First CPU/process CPU deltas can be None
while no interval exists; later missing CPU is a failure, not 0%.

A scheduler stall or fast allocation can outrun a polling guard. Other services
can also consume memory independently. No real-time guarantee of preserving the
physical floor is claimed. Hard commit/CPU limits are a second control; current
headroom and a conservative reviewed envelope remain mandatory. The exact new
Job-limit combination is Mock-tested, not exercised on G10 in T1.

## Phase records

Ordered begin/end barriers cover:

1. Python-baseline checkpoint (pre-application bootstrap is visible in process samples).
2. Python/application imports.
3. Public configuration parsing/validation.
4. Context initialization, containing a separately instrumented agent constructor.
5. ASGI object construction without a listener or frontend/submodule assets.
6. One session context cloned/prepared in process; no real WebSocket client.

Every boundary is atomically written and acknowledged only after the parent
sample/checkpoint is saved. Gaps, wrong order, incomplete nesting, invalid clocks
or early completion are rejected. `work_elapsed_ns` excludes the initial boundary
wait; other wall intervals include monitoring/acknowledgment overhead. Parent and
child monotonic clocks and boundary sample indices remain available for audit.

Per sample: OS CPU, available RAM, commit headroom, owned PID/creation/image/parent,
WS, Private Bytes, CPU ticks/deltas, Job lifetime peak commit, monitor's own memory,
runner/protection state and elapsed clocks. Summary contains interval sampled
peaks and unfinished phases. Context/agent intervals overlap and must not be summed.
Lifetime Job peak is not a phase-specific incremental peak. 250 ms sampling can
miss transient physical peaks; therefore physical attribution is explicitly false.

The owned Job contains a possible venv redirector; supervisor overhead is outside
the Job and recorded separately. Public-template content and package/version/
source identity must match when interpreting any later measurements.

## Failure and cleanup records

`checkpoint.json`, `pre-cleanup.json`, then `result.json` preserve the first stop,
exception type (no raw exception text/locals/env/prompt), candidate PID, exit code,
partial metrics and phase data. A worker-provided sanitized exception class is
retained when available; early failures without that record remain unidentified,
not guessed. Storage errors remain explicit; all-disk/power failures cannot be
guaranteed recoverable by a journal.

Cleanup requests stop and immediately closes **only** the private Job; it does
not wait three seconds on memory/protection failures. A still-suspended unassigned
child is terminated via its retained creation handle. Remaining ownership is
verified by PID + creation time, not process name. PID reuse and unknown ownership
are not reasons to kill another process. Forced Job exit code 0 is never reported
as application success. Protected ports/files and owned remnants are checked;
VM identity checks are OS process checks, not proof of HA application health or
unchanged VM pause/config state. No HA API probe is made.

## Tests and decision

See `test-results.json`: **215 PASS (188 existing + 27 new), zero skips/failures/errors**.
No real Job/worker/server/model/MCP was created by tests. New cases cover approval,
RAM+cap/commit gates, unknown/stale/runner rejection, one-use receipt, review-only
CLI, resource/monitor/protection stops, first-stop/early-exit/persistence/cleanup,
strict phase order, nested summaries and mocked Win32 Job limits. Existing
ownership/PID-reuse/guard/text-only regressions are retained. T1's own actual OS
state/protection check is separate in `text-t1-preservation.json`.

## Exact next one-shot procedure (not executed in T1)

1. Review this source/risks and approve exact provisional values and rationale;
   copy the false/null template outside Git, fill a new run ID, current full SHA,
   expiry and explicit authorization. Do not reuse fixture test values.
2. Confirm source clean, official venv, protected service PID baselines, HA state
   using existing information, sufficient fresh RAM/commit and no runner. If
   conditions do not fit, stop without retries/service changes.
3. With **separate user approval**, invoke official venv Python `-I -B` with
   `tools/text_calibration/launch.py --execute-calibration --approval <external>
   --approval-sha256 <reviewed-hash> --runtime <unused-diagnostics-directory>
   --production <production-path>` once. No default caps or executable approval
   are shipped. The launcher itself does not create approval.
4. Review all boundaries, stop/cleanup and observer overhead. Complete ASGI/session
   measurement is not TCP/HTTP/WebSocket/conversation success. Review attribution
   and missing listening/native/profile costs before manually considering any
   measured normal budget. Normal admission must remain unchanged.
5. Verify no owned remnants, protected state unchanged, no model requests and no
   runner/unload action. Do not proceed to a listener, conversation or production
   deployment in that same approval. No PR/merge/force push in T1.


## T1 read-only G10 inventory

At the saved OS check: Available RAM 2,236,444,672 bytes (~2.083 GiB), commit
headroom 4,657,180,672 bytes (~4.337 GiB), CPU samples 24%, 15%, 8%, and zero known
Ollama runner processes. This is an OS inventory, not a candidate measurement.
All eight previously protected listener owners, production hashes/HEAD/index,
frontend HEAD/gitlink/226 staged deletions and HA running matched the saved
baseline. The four life-app ports were observed listening; their earlier PID
comparison is not asserted by this inventory. No model/API request or
calibration process was used to obtain it. OS runner absence is not an Ollama
model-list API result. The startup/measurement permission remains unissued.

Runtime monitoring additionally compares protected service PID creation times,
Git HEAD/ref metadata and frontend index, and preserves presence/absence on the
additional life-app/production ports. It cannot guarantee all application-level
health from OS metadata. Do not issue other model requests during a separately
approved calibration; an unexpected external runner causes only the calibration
Job to stop, without stopping/unloading that other service.
