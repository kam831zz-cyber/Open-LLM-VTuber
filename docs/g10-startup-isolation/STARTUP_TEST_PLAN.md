# Future single-start test — separate approval required

1. Recheck CPU with multiple samples, Available RAM >=1.5 GiB, commit head,
   production hashes, protected services and an unused loopback test port. Do not
   free memory by stopping existing services. Sustained CPU >=80% is a stop gate.
2. Audit a candidate launcher in isolation: current interpreter, backend, native
   library paths, local ASR snapshot, direct MCP commands, readiness target and
   working directory. Read secrets in process only, never print or copy secret
   env into the worktree. Preserve production settings and the existing timeout.
3. Explicit approval must include the existing eager ASR load and Ollama preload;
   starting this candidate is NOT a model-free action. Do not use the production
   task or its probe, which targets the production port and creates a session.
4. Create one owned candidate process on 127.0.0.1 and a separate unused port,
   with isolated logs/history/write paths and no upgrade/config migration. Record
   PID, parent, creation time and executable. Prefer an independently verified
   Windows Job Object for this candidate only; do not alter shared task settings.
5. Record monotonic durations and process memory for: process -> imports -> ASR
   file resolution/native import/model construction -> MCP discovery -> TCP ->
   HTTP -> one WebSocket -> core objects/MCP availability -> conversation ready.
   HTTP 200 alone is not successful recovery. No automatic reconnect/restart.
6. Later separately authorized stages: one text -> LLM completion -> VOICEVOX
   generation -> playback diagnostics -> human confirms actual sound. Keep mic
   disabled. Readiness failure in one feature must name that feature; 18081 must
   never be used as an alternative to Command Center 18000.
7. Abort new operations for RAM <1.5 GiB, sustained overload, unexpected children,
   security denial, timeout or degraded protected service. Observe pending work;
   do not send an additional inference or retry.
8. Stop only candidate-owned processes after rechecking PID + creation time +
   executable + parent. Verify direct MCP children have exited and the test port
   is released. Never kill by process name or use taskkill /T. If ownership or
   cleanup is uncertain, stop further actions and report remaining PIDs.
9. Confirm protected ports/services and production Git index/settings unchanged.
   No production rollback is needed unless a later separately approved rollout
   changes a launcher; that rollout must preserve original bytes/SHA256 and restore
   only the approved file. Old-environment restart needs its own authorization.
