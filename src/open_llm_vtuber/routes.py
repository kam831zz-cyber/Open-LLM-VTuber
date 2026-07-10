import os
import json
import re
from uuid import uuid4
import numpy as np
from datetime import datetime
import requests
from fastapi import APIRouter, WebSocket, UploadFile, File, Response, Body
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect
from loguru import logger
from .service_context import ServiceContext
from .websocket_handler import WebSocketHandler
from .proxy_handler import ProxyHandler


def init_client_ws_route(default_context_cache: ServiceContext) -> APIRouter:
    """
    Create and return API routes for handling the `/client-ws` WebSocket connections.

    Args:
        default_context_cache: Default service context cache for new sessions.

    Returns:
        APIRouter: Configured router with WebSocket endpoint.
    """

    router = APIRouter()
    ws_handler = WebSocketHandler(default_context_cache)

    @router.websocket("/client-ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for client connections"""
        await websocket.accept()
        client_uid = str(uuid4())

        try:
            await ws_handler.handle_new_connection(websocket, client_uid)
            await ws_handler.handle_websocket_communication(websocket, client_uid)
        except WebSocketDisconnect:
            await ws_handler.handle_disconnect(client_uid)
        except Exception as e:
            logger.error(f"Error in WebSocket connection: {e}")
            await ws_handler.handle_disconnect(client_uid)
            raise

    return router


def init_proxy_route(server_url: str) -> APIRouter:
    """
    Create and return API routes for handling proxy connections.

    Args:
        server_url: The WebSocket URL of the actual server

    Returns:
        APIRouter: Configured router with proxy WebSocket endpoint
    """
    router = APIRouter()
    proxy_handler = ProxyHandler(server_url)

    @router.websocket("/proxy-ws")
    async def proxy_endpoint(websocket: WebSocket):
        """WebSocket endpoint for proxy connections"""
        try:
            await proxy_handler.handle_client_connection(websocket)
        except Exception as e:
            logger.error(f"Error in proxy connection: {e}")
            raise

    return router


def init_webtool_routes(default_context_cache: ServiceContext) -> APIRouter:
    """
    Create and return API routes for handling web tool interactions.

    Args:
        default_context_cache: Default service context cache for new sessions.

    Returns:
        APIRouter: Configured router with WebSocket endpoint.
    """

    router = APIRouter()

    voicevox_standard_speaker_id = 8

    def get_voicevox_config():
        if not default_context_cache.character_config:
            return None
        tts_config = default_context_cache.character_config.tts_config
        if not tts_config or not tts_config.voicevox_tts:
            return None
        return tts_config.voicevox_tts

    def flatten_voicevox_speakers(speakers):
        result = []
        if isinstance(speakers, dict) and "value" in speakers:
            speakers = speakers["value"]
        if not isinstance(speakers, list):
            return result

        for speaker in speakers:
            speaker_name = speaker.get("name", "")
            for style in speaker.get("styles", []):
                style_id = style.get("id")
                if style_id is None:
                    continue
                style_name = style.get("name", "")
                label = f"{speaker_name} / {style_name}"
                if style_id == voicevox_standard_speaker_id:
                    label = f"標準: {label}"
                result.append(
                    {
                        "id": style_id,
                        "speaker_name": speaker_name,
                        "style_name": style_name,
                        "label": label,
                    }
                )
        return sorted(result, key=lambda item: item["id"])

    def update_voicevox_speaker_id_in_file(file_path: str, speaker_id: int) -> bool:
        if not os.path.exists(file_path):
            return False

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        pattern = (
            r"(voicevox_tts:\s*(?:\r?\n[ \t]+[^\r\n]*)*?"
            r"\r?\n[ \t]*speaker_id:\s*)\d+"
        )
        updated, count = re.subn(pattern, rf"\g<1>{speaker_id}", content, count=1)
        if count == 0:
            return False

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(updated)
        return True

    def persist_voicevox_speaker_id(speaker_id: int) -> list[str]:
        updated_files = []
        conf_uid = default_context_cache.character_config.conf_uid
        target_files = []
        if conf_uid == "komugi_001":
            target_files.append(
                os.path.join(
                    default_context_cache.system_config.config_alts_dir, "komugi.yaml"
                )
            )
        else:
            target_files.append("conf.yaml")

        for file_path in target_files:
            try:
                if update_voicevox_speaker_id_in_file(file_path, speaker_id):
                    updated_files.append(file_path)
            except Exception as e:
                logger.warning(f"Failed to persist VOICEVOX speaker in {file_path}: {e}")
        return updated_files

    @router.get("/api/voicevox/speakers")
    async def get_voicevox_speakers():
        """Return available VOICEVOX speakers and the active speaker ID."""
        voicevox_config = get_voicevox_config()
        if not voicevox_config:
            return JSONResponse(
                {
                    "enabled": False,
                    "message": "VOICEVOX TTS is not configured.",
                    "standard_speaker_id": voicevox_standard_speaker_id,
                    "speakers": [],
                }
            )

        base_url = voicevox_config.base_url.rstrip("/")
        current_speaker_id = voicevox_config.speaker_id
        speakers = []
        engine_ok = False
        try:
            response = requests.get(f"{base_url}/speakers", timeout=5)
            response.raise_for_status()
            response.encoding = "utf-8"
            speakers = flatten_voicevox_speakers(response.json())
            engine_ok = True
        except Exception as e:
            logger.warning(f"Failed to fetch VOICEVOX speakers from {base_url}: {e}")

        current = next(
            (speaker for speaker in speakers if speaker["id"] == current_speaker_id),
            None,
        )
        return JSONResponse(
            {
                "enabled": True,
                "engine_ok": engine_ok,
                "base_url": base_url,
                "current_speaker_id": current_speaker_id,
                "current_label": current["label"] if current else f"speaker {current_speaker_id}",
                "standard_speaker_id": voicevox_standard_speaker_id,
                "standard_label": "標準: 春日部つむぎ / ノーマル",
                "speakers": speakers,
            }
        )

    @router.post("/api/voicevox/speaker")
    async def set_voicevox_speaker(payload: dict = Body(...)):
        """Set the active VOICEVOX speaker ID for the running TTS engine."""
        voicevox_config = get_voicevox_config()
        if not voicevox_config:
            return JSONResponse(
                {"ok": False, "message": "VOICEVOX TTS is not configured."},
                status_code=400,
            )

        try:
            speaker_id = int(payload.get("speaker_id"))
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "message": "speaker_id must be an integer."},
                status_code=400,
            )

        voicevox_config.speaker_id = speaker_id
        if (
            default_context_cache.config
            and default_context_cache.config.character_config
            and default_context_cache.config.character_config.tts_config.voicevox_tts
        ):
            default_context_cache.config.character_config.tts_config.voicevox_tts.speaker_id = speaker_id

        if hasattr(default_context_cache.tts_engine, "speaker_id"):
            default_context_cache.tts_engine.speaker_id = speaker_id

        persisted_files = persist_voicevox_speaker_id(speaker_id)
        logger.info(
            f"VOICEVOX speaker changed to {speaker_id}; persisted={persisted_files}"
        )

        return JSONResponse(
            {
                "ok": True,
                "speaker_id": speaker_id,
                "standard": speaker_id == voicevox_standard_speaker_id,
                "persisted_files": persisted_files,
            }
        )

    @router.get("/web-tool")
    async def web_tool_redirect():
        """Redirect /web-tool to /web_tool/index.html"""
        return Response(status_code=302, headers={"Location": "/web-tool/index.html"})

    @router.get("/web_tool")
    async def web_tool_redirect_alt():
        """Redirect /web_tool to /web_tool/index.html"""
        return Response(status_code=302, headers={"Location": "/web-tool/index.html"})

    @router.get("/live2d-models/info")
    async def get_live2d_folder_info():
        """Get information about available Live2D models"""
        live2d_dir = "live2d-models"
        if not os.path.exists(live2d_dir):
            return JSONResponse(
                {"error": "Live2D models directory not found"}, status_code=404
            )

        valid_characters = []
        supported_extensions = [".png", ".jpg", ".jpeg"]

        for entry in os.scandir(live2d_dir):
            if entry.is_dir():
                folder_name = entry.name.replace("\\", "/")
                model3_file = os.path.join(
                    live2d_dir, folder_name, f"{folder_name}.model3.json"
                ).replace("\\", "/")

                if os.path.isfile(model3_file):
                    # Find avatar file if it exists
                    avatar_file = None
                    for ext in supported_extensions:
                        avatar_path = os.path.join(
                            live2d_dir, folder_name, f"{folder_name}{ext}"
                        )
                        if os.path.isfile(avatar_path):
                            avatar_file = avatar_path.replace("\\", "/")
                            break

                    valid_characters.append(
                        {
                            "name": folder_name,
                            "avatar": avatar_file,
                            "model_path": model3_file,
                        }
                    )
        return JSONResponse(
            {
                "type": "live2d-models/info",
                "count": len(valid_characters),
                "characters": valid_characters,
            }
        )

    @router.post("/asr")
    async def transcribe_audio(file: UploadFile = File(...)):
        """
        Endpoint for transcribing audio using the ASR engine
        """
        logger.info(f"Received audio file for transcription: {file.filename}")

        try:
            contents = await file.read()

            # Validate minimum file size
            if len(contents) < 44:  # Minimum WAV header size
                raise ValueError("Invalid WAV file: File too small")

            # Decode the WAV header and get actual audio data
            wav_header_size = 44  # Standard WAV header size
            audio_data = contents[wav_header_size:]

            # Validate audio data size
            if len(audio_data) % 2 != 0:
                raise ValueError("Invalid audio data: Buffer size must be even")

            # Convert to 16-bit PCM samples to float32
            try:
                audio_array = (
                    np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                    / 32768.0
                )
            except ValueError as e:
                raise ValueError(
                    f"Audio format error: {str(e)}. Please ensure the file is 16-bit PCM WAV format."
                )

            # Validate audio data
            if len(audio_array) == 0:
                raise ValueError("Empty audio data")

            text = await default_context_cache.asr_engine.async_transcribe_np(
                audio_array
            )
            logger.info(f"Transcription result: {text}")
            return {"text": text}

        except ValueError as e:
            logger.error(f"Audio format error: {e}")
            return Response(
                content=json.dumps({"error": str(e)}),
                status_code=400,
                media_type="application/json",
            )
        except Exception as e:
            logger.error(f"Error during transcription: {e}")
            return Response(
                content=json.dumps(
                    {"error": "Internal server error during transcription"}
                ),
                status_code=500,
                media_type="application/json",
            )

    @router.websocket("/tts-ws")
    async def tts_endpoint(websocket: WebSocket):
        """WebSocket endpoint for TTS generation"""
        await websocket.accept()
        logger.info("TTS WebSocket connection established")

        try:
            while True:
                data = await websocket.receive_json()
                text = data.get("text")
                if not text:
                    continue

                logger.info(f"Received text for TTS: {text}")

                # Split text into sentences
                sentences = [s.strip() for s in text.split(".") if s.strip()]

                try:
                    # Generate and send audio for each sentence
                    for sentence in sentences:
                        sentence = sentence + "."  # Add back the period
                        file_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid4())[:8]}"
                        audio_path = (
                            await default_context_cache.tts_engine.async_generate_audio(
                                text=sentence, file_name_no_ext=file_name
                            )
                        )
                        logger.info(
                            f"Generated audio for sentence: {sentence} at: {audio_path}"
                        )

                        await websocket.send_json(
                            {
                                "status": "partial",
                                "audioPath": audio_path,
                                "text": sentence,
                            }
                        )

                    # Send completion signal
                    await websocket.send_json({"status": "complete"})

                except Exception as e:
                    logger.error(f"Error generating TTS: {e}")
                    await websocket.send_json({"status": "error", "message": str(e)})

        except WebSocketDisconnect:
            logger.info("TTS WebSocket client disconnected")
        except Exception as e:
            logger.error(f"Error in TTS WebSocket connection: {e}")
            await websocket.close()

    return router
