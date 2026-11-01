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
    status = {"conversation_core_ready": all((context.asr_engine is not None,
                context.tts_engine is not None, context.agent_engine is not None)),
            "mcp": mcp,
            "mcp_enabled": enabled,
            "required_mcp_state": "disabled" if enabled is False else "ready" if ready else "unconfirmed",
            "required_mcp_ready": ready,
            "command_center": "not_checked", "llm_response": "not_tested",
            "tts_audio": "not_tested", "speaker_output": "not_tested"}

    controller = getattr(context, "staged_controller", None)
    if controller is not None:
        model_status = controller.status()
        status.update(model_status)
        # MCP belongs to the session, model ownership to the server context.
        status.update(required_mcp_ready=ready,
                      required_mcp_state="disabled" if enabled is False else "ready" if ready else "unconfirmed")
        status["conversation_core_constructed"] = (context.agent_engine is not None
            and (not controller.audio_enabled or context.tts_engine is not None))
        status["audio_mode"] = "enabled" if controller.audio_enabled else "disabled"
        status["text_conversation_ready"] = (status["conversation_core_constructed"]
            and model_status["text_model_ready"] and controller.text_requests_enabled)
        if not controller.audio_enabled:
            status.update(tts_audio="disabled", speaker_output="disabled")
        status["conversation_core_ready"] = (status["conversation_core_constructed"]
            and model_status["text_model_ready"]
            and (controller.policy.text_only or controller.policy.asr_mode == "disabled" or model_status["asr_ready"]))
        status["life_information_available"] = False  # 18000 availability has not been verified
    return status
