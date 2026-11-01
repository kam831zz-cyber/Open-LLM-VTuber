# Komugi text-first candidate

Base `1f1ce4fae912202806882caebfea19794ca837f3`.
Branch `feat/g10-text-only-conversation`. Offline candidate only; no actual
server, model, inference, TTS, microphone or application API was run.

## Configuration and implementation

Use `tools/text_only_trial/preparation.py` for the new profile. It deep-copies
candidate settings; production configuration is neither edited nor copied by
tests. Settings are:

```yaml
staged_loading:
  enabled: true
  text_only: true
  asr_mode: disabled
  audio_enabled: false
  eager_preload: false
  text_requests_enabled: false
```

Legacy defaults remain compatible. Changing an already constructed context's
audio/text mode is rejected. Audio-disabled mode rejects eager/ASR-enabled
combinations. The candidate initializes neither ASR, VAD, TTS/VOICEVOX wrapper,
speech translation nor Live2D. It uses the existing persona for plain text;
speech/tool/Live2D prompt fragments are not required. Schema-level TTS fields
remain for backward compatibility, but their provider is not instantiated.

The API-only server does not register `/asr`, `/tts-ws`, VOICEVOX setting routes,
proxy routes, audio cache, frontend/submodule or static visual assets. Existing
frontend and Room assets are untouched. WebSocket model metadata may be empty;
Room continues to own its Character/monitor rendering independently.

Audio chunks, audio ACKs, group/config-switch/proactive-speech events are not
routed in text mode. Only text-input, heartbeat and request-init-config pass.
No start-mic, audio payload, synthesis completion or playback ACK wait is used.

## Conversation contract

- `GET /api/text/status`: distinguishes constructed wrappers, loaded models,
  ASR/audio disabled, MCP availability and text request enablement. It makes no
  provider request. An unloaded model is not ready. Functional conversation
  remains `not_tested` in capability status even after a successful Mock test.
- `/client-ws`: existing initial service-status and config events; explicit
  `text-input` enters the new text-only path. It returns text-delta chunks,
  final full-text, model-status and conversation-chain-end. Final full-text
  replaces the current partial bubble; do not append it a second time.
- `POST /api/text/chat`: explicit text only, loopback and same-origin required,
  8 KiB body / 4096-character input caps. No images/tools/audio. Each HTTP call
  has an ephemeral agent memory; default context memory is never shared between
  unrelated requests. Use WebSocket for a persistent per-session conversation.

Both conversation paths default to disabled. The first model-free trial's ASGI
wrapper permits only openapi/text-status GET and initial WebSocket notifications;
it still blocks chat POST and incoming WebSocket data. Enabling conversation is
a separate future stage, not part of the model-free startup approval.

BasicMemoryAgent.text_chat reuses its session memory and existing persona but
bypasses audio decorators, automatic weather/context prefetch, MCP tools and UI
actions. Only a complete, nonempty response is committed to memory. This path
does not write conversation history to disk yet. Ordinary legacy conversation
and VOICEVOX protocols remain unchanged outside this opt-in mode.

Ollama text_completion uses the existing OpenAI-compatible SDK client and model
selection, with one explicit streamed request, SDK retries disabled, 30-second
request timeout and a 60-second turn deadline. A terminal stop is required;
truncated/tool/failed output does not count as successful conversation. Shared
LLM concurrency is refused instead of queued. Errors exposed to the UI are
short codes; the new path does not log conversation bodies or raw exceptions.
Existing SDK construction/legacy logs still require process-local log suppression
in a future diagnostic launcher.

## Memory and readiness

No automatic preload or download is introduced. A verified loaded-model receipt
and fresh exclusive Ollama ownership evidence remain necessary. The real loader
and ownership adapter are still unregistered; real model readiness cannot be
asserted from these Mock fixtures. A model disappearing after an observation is
a race that needs an approved ownership/lease design, not optimistic readiness.

The explicit text request also needs a measured `text_request` budget for
generation/KV-cache growth. Unknown budget, physical/commit shortage or stale
readings rejects before SDK invocation. Failed/cancelled remote completion
latches PENDING; closing the client stream is not proof of server-side completion.
No unload request is sent to resolve uncertainty. No real model budget was added.

The **new server profile** is
`text-only-v1:mcp-off:asr-off:tts-off:ollama-preload-off`.
The previous VOICEVOX-wrapper profile budget cannot admit this different profile.
Both physical and commit incremental peaks remain null/unknown. No guessed
savings are registered as measurements. The 1.5 GiB physical reserve, 0.25 GiB
margin, fresh OS observation, runner exclusion and commit check are maintained.

Potential reductions: ASR/native initialization, VAD, TTS wrapper/cache, speech
translation, Live2D/static setup and MCP children are omitted. Common SDK,
framework, array and legacy module imports remain; full process import/native
ABI behavior and memory savings are **unmeasured**. No claim of a numeric saving
or successful actual startup is made.

## Command Center and life information

Initial profile disables MCP/Fast Path. The new plain-text method never calls
the old automatic prefetch path. Command Center remains `not_checked` and life
information unavailable; 18081 is not substituted for 18000. Weather, garbage,
notifications and disaster information are **not restored by this commit**.

Before connecting them: audit each existing endpoint for effects, establish
source/freshness/unavailable metadata and explicit request authorization; use
the existing formal disaster Resolver unchanged. Do not use generated LLM text
as live household/weather/disaster facts. A future provider layer should return
typed results (capability, source, fetched/expiry time, status, payload), separate
from chat tokens. Notifications are reads first; no device operation/job execution
is enabled. Natural-language routing and factual refusal need separate tests
before these questions are offered as supported functions.

## Future Room integration (not implemented in Room)

Keep 18874 untouched. Use a new independent host/branch and same-origin proxy
for text status plus a session-scoped WebSocket. The proxy must first validate the Room origin, then use the configured backend
origin for its trusted upstream connection; forwarding an arbitrary Origin or
using wildcard authorization is not permitted. Browser receives no credentials
or unrestricted upstream URL. Show separate states: backend offline, server
available/model unloaded, model busy/uncertain, text ready, provider unavailable.
Do not make all providers ONLINE from one WebSocket connection.

One explicit send per turn, no reconnect-triggered resend, bounded input/output,
disabled send while pending; partial text is preserved on error. Map text-delta,
full-text and model-status without audio/ACK assumptions. Character may reflect
only actual waiting/streaming/idle state; no fabricated job progress. Leave Mic,
speaker and Jobs disabled. Future autonomous work belongs behind a separate
authorized task interface, not model-generated tool calls in this text path.

## Verification and next smallest real trial

Offline tests use fake SDK streams/model receipts/resource readings and injected
class methods; no actual server socket or model is started. The common regression
runner blocks native model imports, connections and subprocess creation. See
`test-results.json` for exact counts: **188 PASS (160 existing + 28 new),
zero failures/errors/skips**. Syntax validation passed for 15 changed Python
files; whitespace and credential-signature checks passed. HTTP endpoints are called directly with
Mock requests; server construction uses fake ASGI/static/router factories.

Next: separately approve a bounded, owned-process **text-only startup cost
measurement plan**, including Python/imports, agent wrapper, one session and
framework. Never mark an estimate measured to bypass admission. Once measured
physical/commit peaks and sufficient headroom are established, review the
launcher/Job/guard integration and allow one model-free startup, status GET,
and one initial WebSocket only. Keep text requests disabled and all models/MCP
off. Stop only proven owned processes and check protected services/port release.

Actual AI response needs a later separately approved LLM load, measured load and
generation budgets, fresh ownership proof, and then one text-only request.
Server startup, WebSocket connection, model readiness and conversation success
must be reported separately. No production rollout, PR or merge is performed.


## OS/protection observation

At the saved post-test observation, CPU was ~18%, Available RAM 2,316,169,216
bytes (~2.157 GiB), commit headroom 4,711,272,448 bytes (~4.388 GiB), runner count
zero. These fluctuate; the startup budget remains unknown and admission remains
blocked. Production hashes/index/HEAD, frontend 226 staged deletions, protected
port owner PIDs and HA running matched the previous baseline. See
`text-first-preservation.json`. GitHub save does not deploy this candidate.
