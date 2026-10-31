"""Readiness stages are capabilities, never proof of a successful conversation."""
from copy import deepcopy


def conversation_status(context):
    mcp = deepcopy(getattr(context.tool_adapter, "availability", {}))
    return {"conversation_core_ready": all((context.asr_engine is not None,
                context.tts_engine is not None, context.agent_engine is not None)),
            "mcp": mcp,
            "required_mcp_ready": all(v["state"] == "ready" for v in mcp.values() if v["required"]),
            "command_center": "not_checked", "llm_response": "not_tested",
            "tts_audio": "not_tested", "speaker_output": "not_tested"}
