"""Readiness stages are capabilities, never proof of a successful conversation."""
from copy import deepcopy


def conversation_status(context):
    raw = getattr(getattr(context, "tool_adapter", None), "availability", {})
    mcp = deepcopy(raw) if isinstance(raw, dict) else {}
    enabled = getattr(context, "_mcp_enabled", None)
    expected = getattr(context, "_mcp_expected_servers", None)
    required = [name for name in expected if name != "ddg-search"] if isinstance(expected, (list, tuple)) else []
    initialized = all(getattr(context, name, None) is not None
                       for name in ("mcp_client", "tool_manager", "tool_executor"))
    ready = (enabled is True and bool(required) and initialized
             and all(isinstance(mcp.get(name), dict)
                     and mcp[name].get("required") is True
                     and mcp[name].get("state") == "ready" for name in required))
    return {"conversation_core_ready": all((context.asr_engine is not None,
                context.tts_engine is not None, context.agent_engine is not None)),
            "mcp": mcp,
            "mcp_enabled": enabled,
            "required_mcp_state": "disabled" if enabled is False else "ready" if ready else "unconfirmed",
            "required_mcp_ready": ready,
            "command_center": "not_checked", "llm_response": "not_tested",
            "tts_audio": "not_tested", "speaker_output": "not_tested"}
