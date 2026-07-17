from typing import Union, List, Dict, Any, Optional
import asyncio
import json
import time
import uuid
from loguru import logger
import numpy as np

from .conversation_utils import (
    create_batch_input,
    process_agent_output,
    send_conversation_start_signals,
    process_user_input,
    finalize_conversation_turn,
    cleanup_conversation,
    EMOJI_LIST,
)
from .types import WebSocketSend
from .tts_manager import TTSTaskManager
from ..chat_history_manager import store_message
from ..service_context import ServiceContext

# Import necessary types from agent outputs
from ..agent.output_types import SentenceOutput, AudioOutput, DisplayText, Actions


async def process_single_conversation(
    context: ServiceContext,
    websocket_send: WebSocketSend,
    client_uid: str,
    user_input: Union[str, np.ndarray],
    images: Optional[List[Dict[str, Any]]] = None,
    session_emoji: str = np.random.choice(EMOJI_LIST),
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Process a single-user conversation turn."""
    tts_manager = TTSTaskManager()
    turn_start = time.perf_counter()
    tts_manager.question_start = turn_start
    full_response = ""
    route = "ollama"
    request_id = str(uuid.uuid4())
    ollama_first_token_ms = None
    ollama_total_ms = None
    record_error = None
    fast_path_http_ms = None

    try:
        await send_conversation_start_signals(websocket_send)
        logger.info(f"New Conversation Chain {session_emoji} started!")

        input_text = await process_user_input(
            user_input, context.asr_engine, websocket_send
        )

        batch_input = create_batch_input(
            input_text=input_text,
            images=images,
            from_name=context.character_config.human_name,
            metadata=metadata,
        )

        skip_history = metadata and metadata.get("skip_history", False)
        if context.history_uid and not skip_history:
            store_message(
                conf_uid=context.character_config.conf_uid,
                history_uid=context.history_uid,
                role="human",
                content=input_text,
                name=context.character_config.human_name,
            )

        if skip_history:
            logger.debug("Skipping storing user input to history (proactive speak)")

        logger.info(f"User input: {input_text}")
        if images:
            logger.info(f"With {len(images)} images")

        fast_path_client = getattr(context, "komugi_fast_path_client", None)
        if fast_path_client and not images and not skip_history:
            fast_path_result = await fast_path_client.quick_response(input_text)
            request_id = fast_path_result.request_id
            fast_path_http_ms = fast_path_result.http_roundtrip_ms
            if fast_path_result.handled and fast_path_result.response:
                route = "fast_path"
                logger.info(
                    f"Komugi Fast Path handled intent={fast_path_result.intent} "
                    f"request_id={request_id} http_roundtrip_ms={fast_path_http_ms}"
                )
                _add_agent_memory(context, "user", input_text)
                fast_output = SentenceOutput(
                    display_text=DisplayText(text=fast_path_result.response),
                    tts_text=fast_path_result.response,
                    actions=Actions(),
                )
                response_part = await process_agent_output(
                    output=fast_output,
                    character_config=context.character_config,
                    live2d_model=context.live2d_model,
                    tts_engine=context.tts_engine,
                    websocket_send=websocket_send,
                    tts_manager=tts_manager,
                    translate_engine=context.translate_engine,
                )
                full_response += str(response_part or "")
                _add_agent_memory(context, "assistant", full_response)
            elif fast_path_result.error:
                logger.warning(
                    f"Komugi Fast Path fallback request_id={request_id} error={fast_path_result.error}"
                )

        try:
            agent_output_stream = (
                None if route == "fast_path" else context.agent_engine.chat(batch_input)
            )

            if agent_output_stream is not None:
                async for output_item in agent_output_stream:
                    if _is_first_displayable_output(output_item) and ollama_first_token_ms is None:
                        ollama_first_token_ms = _elapsed_ms(turn_start)

                    if (
                        isinstance(output_item, dict)
                        and output_item.get("type") == "tool_call_status"
                    ):
                        output_item["name"] = context.character_config.character_name
                        logger.debug(f"Sending tool status update: {output_item}")
                        await websocket_send(json.dumps(output_item))

                    elif isinstance(output_item, (SentenceOutput, AudioOutput)):
                        response_part = await process_agent_output(
                            output=output_item,
                            character_config=context.character_config,
                            live2d_model=context.live2d_model,
                            tts_engine=context.tts_engine,
                            websocket_send=websocket_send,
                            tts_manager=tts_manager,
                            translate_engine=context.translate_engine,
                        )
                        full_response += str(response_part or "")
                    else:
                        logger.warning(
                            f"Received unexpected item type from agent chat stream: {type(output_item)}"
                        )
                        logger.debug(f"Unexpected item content: {output_item}")
                ollama_total_ms = _elapsed_ms(turn_start)

        except Exception as e:
            record_error = "agent_response_error"
            logger.exception(f"Error processing agent response stream: {e}")
            await websocket_send(
                json.dumps(
                    {
                        "type": "error",
                        "message": f"Error processing agent response: {str(e)}",
                    }
                )
            )

        if tts_manager.task_list:
            await asyncio.gather(*tts_manager.task_list)
            await websocket_send(json.dumps({"type": "backend-synth-complete"}))

        await finalize_conversation_turn(
            tts_manager=tts_manager,
            websocket_send=websocket_send,
            client_uid=client_uid,
        )

        if context.history_uid and full_response:
            store_message(
                conf_uid=context.character_config.conf_uid,
                history_uid=context.history_uid,
                role="ai",
                content=full_response,
                name=context.character_config.character_name,
                avatar=context.character_config.avatar,
            )
            logger.info(f"AI response: {full_response}")

        _schedule_performance_record(
            context=context,
            request_id=request_id,
            route=route,
            ollama_first_token_ms=ollama_first_token_ms,
            ollama_total_ms=ollama_total_ms,
            voicevox_generation_ms=tts_manager.voicevox_generation_ms,
            audio_start_ms=tts_manager.audio_start_ms,
            total_ms=_elapsed_ms(turn_start),
            error=record_error,
        )
        _log_local_performance(
            route=route,
            request_id=request_id,
            fast_path_http_ms=fast_path_http_ms,
            ollama_first_token_ms=ollama_first_token_ms,
            ollama_total_ms=ollama_total_ms,
            tts_queue_wait_ms=tts_manager.tts_queue_wait_ms,
            voicevox_generation_ms=tts_manager.voicevox_generation_ms,
            audio_payload_prepare_ms=tts_manager.audio_payload_prepare_ms,
            audio_payload_send_ms=tts_manager.audio_payload_send_ms,
            audio_start_ms=tts_manager.audio_start_ms,
            total_ms=_elapsed_ms(turn_start),
            error=record_error,
        )

        return full_response

    except asyncio.CancelledError:
        logger.info(f"Conversation {session_emoji} cancelled because interrupted.")
        _log_local_performance(
            route=route,
            request_id=request_id,
            fast_path_http_ms=fast_path_http_ms,
            ollama_first_token_ms=ollama_first_token_ms,
            ollama_total_ms=ollama_total_ms,
            tts_queue_wait_ms=tts_manager.tts_queue_wait_ms,
            voicevox_generation_ms=tts_manager.voicevox_generation_ms,
            audio_payload_prepare_ms=tts_manager.audio_payload_prepare_ms,
            audio_payload_send_ms=tts_manager.audio_payload_send_ms,
            audio_start_ms=tts_manager.audio_start_ms,
            total_ms=_elapsed_ms(turn_start),
            error="cancelled",
        )
        _schedule_performance_record(
            context=context,
            request_id=request_id,
            route=route,
            ollama_first_token_ms=ollama_first_token_ms,
            ollama_total_ms=ollama_total_ms,
            voicevox_generation_ms=tts_manager.voicevox_generation_ms,
            audio_start_ms=tts_manager.audio_start_ms,
            total_ms=_elapsed_ms(turn_start),
            error="cancelled",
        )
        raise
    except Exception as e:
        logger.error(f"Error in conversation chain: {e}")
        _log_local_performance(
            route=route,
            request_id=request_id,
            fast_path_http_ms=fast_path_http_ms,
            ollama_first_token_ms=ollama_first_token_ms,
            ollama_total_ms=ollama_total_ms,
            tts_queue_wait_ms=tts_manager.tts_queue_wait_ms,
            voicevox_generation_ms=tts_manager.voicevox_generation_ms,
            audio_payload_prepare_ms=tts_manager.audio_payload_prepare_ms,
            audio_payload_send_ms=tts_manager.audio_payload_send_ms,
            audio_start_ms=tts_manager.audio_start_ms,
            total_ms=_elapsed_ms(turn_start),
            error="conversation_error",
        )
        _schedule_performance_record(
            context=context,
            request_id=request_id,
            route=route,
            ollama_first_token_ms=ollama_first_token_ms,
            ollama_total_ms=ollama_total_ms,
            voicevox_generation_ms=tts_manager.voicevox_generation_ms,
            audio_start_ms=tts_manager.audio_start_ms,
            total_ms=_elapsed_ms(turn_start),
            error="conversation_error",
        )
        await websocket_send(
            json.dumps({"type": "error", "message": f"Conversation error: {str(e)}"})
        )
        raise
    finally:
        cleanup_conversation(tts_manager, session_emoji)


def _is_first_displayable_output(output_item: Any) -> bool:
    if isinstance(output_item, SentenceOutput):
        return bool(output_item.display_text and str(output_item.display_text.text or "").strip())
    if isinstance(output_item, AudioOutput):
        return bool(str(output_item.transcript or "").strip())
    return False


def _add_agent_memory(context: ServiceContext, role: str, content: str) -> None:
    add_message = getattr(getattr(context, "agent_engine", None), "_add_message", None)
    if callable(add_message) and content:
        add_message(content, role)


def _schedule_performance_record(
    *,
    context: ServiceContext,
    request_id: str,
    route: str,
    ollama_first_token_ms: int | None,
    ollama_total_ms: int | None,
    voicevox_generation_ms: int | None,
    audio_start_ms: int | None,
    total_ms: int | None,
    error: str | None,
) -> None:
    client = getattr(context, "komugi_fast_path_client", None)
    if not client:
        return
    client.schedule_performance_record(
        {
            "request_id": request_id,
            "route": route,
            "ollama_first_token_ms": ollama_first_token_ms,
            "ollama_total_ms": ollama_total_ms,
            "voicevox_generation_ms": voicevox_generation_ms,
            "audio_start_ms": audio_start_ms,
            "total_ms": total_ms,
            "error": error,
        }
    )


def _log_local_performance(
    *,
    route: str,
    request_id: str,
    fast_path_http_ms: int | None,
    ollama_first_token_ms: int | None,
    ollama_total_ms: int | None,
    tts_queue_wait_ms: int | None,
    voicevox_generation_ms: int | None,
    audio_payload_prepare_ms: int | None,
    audio_payload_send_ms: int | None,
    audio_start_ms: int | None,
    total_ms: int | None,
    error: str | None,
) -> None:
    logger.info(
        "Komugi local performance: "
        f"route={route} request_id={request_id} "
        f"fast_path_http_ms={fast_path_http_ms} "
        f"ollama_first_token_ms={ollama_first_token_ms} "
        f"ollama_total_ms={ollama_total_ms} "
        f"tts_queue_wait_ms={tts_queue_wait_ms} "
        f"voicevox_generation_ms={voicevox_generation_ms} "
        f"audio_payload_prepare_ms={audio_payload_prepare_ms} "
        f"audio_payload_send_ms={audio_payload_send_ms} "
        f"audio_start_ms={audio_start_ms} total_ms={total_ms} error={error}"
    )


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))
