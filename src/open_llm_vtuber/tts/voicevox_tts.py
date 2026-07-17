import os
import threading
import time

import requests
from loguru import logger

from .tts_interface import TTSInterface


class TTSEngine(TTSInterface):
    _engine_locks = {}
    _engine_locks_guard = threading.Lock()

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:50021",
        speaker_id: int = 8,
        speed_scale: float = 1.0,
        pitch_scale: float = 0.0,
        intonation_scale: float = 1.0,
        volume_scale: float = 1.0,
    ):
        self.base_url = (base_url or "http://127.0.0.1:50021").rstrip("/")
        self.speaker_id = speaker_id if speaker_id is not None else 8
        self.speed_scale = speed_scale if speed_scale is not None else 1.0
        self.pitch_scale = pitch_scale if pitch_scale is not None else 0.0
        self.intonation_scale = (
            intonation_scale if intonation_scale is not None else 1.0
        )
        self.volume_scale = volume_scale if volume_scale is not None else 1.0
        self.file_extension = "wav"
        self.new_audio_dir = "cache"
        self.last_timing = {}
        self.timing_by_file = {}
        self._timing_lock = threading.Lock()

        if not os.path.exists(self.new_audio_dir):
            os.makedirs(self.new_audio_dir)

    def generate_audio(self, text, file_name_no_ext=None):
        file_name = self.generate_cache_file_name(file_name_no_ext, self.file_extension)
        total_start = time.perf_counter()
        timing = {
            "text_length": len(text or ""),
            "tts_lock_wait_ms": None,
            "audio_query_ms": None,
            "synthesis_ms": None,
            "file_write_ms": None,
            "voicevox_generation_ms": None,
            "voicevox_total_ms": None,
            "wav_bytes": None,
            "error": None,
        }
        self._set_last_timing(timing)

        engine_lock = self._get_engine_lock(self.base_url)
        lock_wait_start = time.perf_counter()
        with engine_lock:
            timing["tts_lock_wait_ms"] = _elapsed_ms(lock_wait_start)
            logger.info(
                "VOICEVOX lock acquired: "
                f"base_url={self.base_url} "
                f"tts_lock_wait_ms={timing['tts_lock_wait_ms']} "
                f"text_length={timing['text_length']}"
            )
            audio_query_start = time.perf_counter()
            try:
                query_response = requests.post(
                    f"{self.base_url}/audio_query",
                    params={"text": text, "speaker": self.speaker_id},
                    timeout=30,
                )
                query_response.raise_for_status()
                audio_query = query_response.json()
                timing["audio_query_ms"] = _elapsed_ms(audio_query_start)

                audio_query["speedScale"] = self.speed_scale
                audio_query["pitchScale"] = self.pitch_scale
                audio_query["intonationScale"] = self.intonation_scale
                audio_query["volumeScale"] = self.volume_scale

                synthesis_start = time.perf_counter()
                synthesis_response = requests.post(
                    f"{self.base_url}/synthesis",
                    params={"speaker": self.speaker_id},
                    json=audio_query,
                    timeout=120,
                )
                synthesis_response.raise_for_status()
                timing["synthesis_ms"] = _elapsed_ms(synthesis_start)
                timing["wav_bytes"] = len(synthesis_response.content)

                file_write_start = time.perf_counter()
                with open(file_name, "wb") as audio_file:
                    audio_file.write(synthesis_response.content)
                timing["file_write_ms"] = _elapsed_ms(file_write_start)
                timing["voicevox_generation_ms"] = (
                    (timing["audio_query_ms"] or 0)
                    + (timing["synthesis_ms"] or 0)
                )
                timing["voicevox_total_ms"] = _elapsed_ms(total_start)
                self._store_timing(file_name, timing)
                logger.info(f"VOICEVOX timing: {timing}")

                return file_name
            except Exception as e:
                timing["voicevox_total_ms"] = _elapsed_ms(total_start)
                timing["error"] = str(e)
                self._set_last_timing(timing)
                logger.critical(
                    f"Error: VOICEVOX unable to generate audio "
                    f"(base_url={self.base_url}, speaker_id={self.speaker_id}): {e}"
                )
                return None

    @classmethod
    def _get_engine_lock(cls, base_url: str) -> threading.Lock:
        with cls._engine_locks_guard:
            if base_url not in cls._engine_locks:
                cls._engine_locks[base_url] = threading.Lock()
            return cls._engine_locks[base_url]

    def _set_last_timing(self, timing: dict) -> None:
        with self._timing_lock:
            self.last_timing = timing

    def _store_timing(self, file_name: str, timing: dict) -> None:
        with self._timing_lock:
            self.timing_by_file[file_name] = timing
            self.last_timing = timing


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))
