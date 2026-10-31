# G10 startup isolation candidate (Phase 1F-1)

Not deployed. No application, ASR model, LLM model, MCP subprocess, microphone or
TTS was started for this change. No production launcher or Python environment was
modified. This branch starts at `34a815c2e783c874814b2938fe5beeaf8c957aaa` and
includes the existing G10 `.python-version` and `mcp_servers.json` recovery settings.
The latter retains three machine-specific Python paths; no secret env was copied.

## Changes and ownership

ToolAdapter caches discovery definitions and availability under an asyncio lock.
Discovery failures are cached too: reconnect does not repeatedly launch a failed
search server. Refresh requires a new adapter; no retry loop is introduced.
Each WebSocket gets separate memory, callback, client ID, MCPClient and executor.
BasicMemoryAgent clones session state while reusing the LLM client without another
Ollama preload. Other agent implementations currently fail closed if they do not
provide session cloning. ASR/TTS engines retain the existing shared ownership.

An OwnedSession task owns both entry and exit of the MCP SDK contexts, avoiding
cross-task AnyIO cancel-scope teardown. Cancellation, initialization failure and
disconnect close owned connections. Windows subprocess termination is delegated to
the installed MCP SDK; actual child-process cleanup still requires a future owned
process test. Mock cleanup is not proof of Windows orphan-process absence.

ddg-search uses the installed console entry point directly with the current Python,
never uvx. Missing local packages are unavailable; package-download launchers are
rejected. Discovery initialization is capped at 5 seconds for search and 10 seconds
for other MCPs (or a shorter configured timeout). Search failure preserves other
MCPs. Required failures are reported separately and failed tools are not advertised.
Python MCP commands follow this process's interpreter, not a persistent env change.

The additive service-status event separates constructed core objects from MCP
availability and untested LLM/TTS/audio stages. Existing initialization events are
preserved. A full-text notice exposes missing MCPs to existing clients. This is not
a promise that HTTP, WebSocket or real conversation is ready. Room Mic/Chat remain
outside this change. Existing VOICEVOX and playback ACK messages are unchanged.

Command Center remains port 18000. Port 18081 is not substituted. Connection failure
for the recognized time/garbage phrases returns an explicit unavailable answer;
ordinary conversation can still use its existing LLM route. This protection does
not generalize every possible paraphrase. MCP process readiness does not establish
its downstream API readiness. Life-information APIs remain explicitly unverified.

## ASR evidence and limits

Existing logs show ASR-stage intervals around 130.147 and 194.836 seconds. The
October 15 launcher started at 00:39:45.339, ASR at 00:40:46.075, next TTS stage at
00:44:00.911, and context completion at 00:44:36.552. A later WebSocket cache load
repeated MCP discovery before the 300-second supervisor deadline. ddg-search also
timed out on a previously successful boot, so it is not the sole proven cause.

The existing small cache has model.bin (483546902 bytes), config.json, tokenizer.json
and vocabulary.txt. File presence is not evidence of successful model initialization.
The candidate resolves local snapshots first and requests local_files_only=True;
no implicit download is allowed. Supported alias is small; other models require an
explicit local directory. Missing required files fail before native import.
Numeric resolve/native-import/model-construction/wall/CPU timings are recorded.
CPU versus file I/O inside model construction is not yet independently measured.
No speed improvement is claimed without a future real model test.

## Python comparison

Both existing environments report 110 distributions with matching versions:
FastAPI 0.115.8, uvicorn 0.34.0, websockets 14.1, faster-whisper 1.2.1,
CTranslate2 4.7.2, ONNX Runtime 1.20.1, NumPy 1.26.4, av 17.0.0,
Torch 2.6.0 and MCP 1.6.0. The production venv points to uv CPython 3.11.9;
the separate official venv points to official Python 3.11.9. This comparison used
metadata, not native import, ABI, DLL signature or model-load validation.
The production supervisor/backend/probe references remain unchanged, including
existing environment-specific native-library setup. A future interpreter switch
must audit all three plus MCP commands together; version equality is insufficient.
Komugi character, speaker 8, VOICEVOX endpoint, model and conversation settings
remain unchanged. No production conf.yaml or authentication file is in this branch.

## Offline verification

Run from this worktree using the existing official OpenLLM venv:

```powershell
& <existing-venv-python> -I -B tools/verify_startup_offline.py docs/g10-startup-isolation/test-results.json
```

The runner blocks external socket connections, subprocess starts and native model
imports, and replaces model construction with explicit mocks. Tests cover shared
discovery, optional/required failures, parallel sessions, reconnect, cancellation,
cleanup, ASR files, staged readiness, Command Center failure and existing routes.
These tests cannot prove real SDK process termination, DLL loading or audio output.

## Production protection / rollback

No rollout has occurred. Stop only any future candidate process that you explicitly
created; this worktree can simply remain unused. Never restore production frontend
or change its index: its 226 staged deletions are intentional protected state for
this task. Production branch/head/index and character/settings hashes are checked
before and after. Existing Room ports 18873-18876 and 18081 are untouched.
