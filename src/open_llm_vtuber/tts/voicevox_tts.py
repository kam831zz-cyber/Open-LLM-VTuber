import os

import requests
from loguru import logger

from .tts_interface import TTSInterface


class TTSEngine(TTSInterface):
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

        if not os.path.exists(self.new_audio_dir):
            os.makedirs(self.new_audio_dir)

    def generate_audio(self, text, file_name_no_ext=None):
        file_name = self.generate_cache_file_name(file_name_no_ext, self.file_extension)

        try:
            query_response = requests.post(
                f"{self.base_url}/audio_query",
                params={"text": text, "speaker": self.speaker_id},
                timeout=30,
            )
            query_response.raise_for_status()
            audio_query = query_response.json()

            audio_query["speedScale"] = self.speed_scale
            audio_query["pitchScale"] = self.pitch_scale
            audio_query["intonationScale"] = self.intonation_scale
            audio_query["volumeScale"] = self.volume_scale

            synthesis_response = requests.post(
                f"{self.base_url}/synthesis",
                params={"speaker": self.speaker_id},
                json=audio_query,
                timeout=120,
            )
            synthesis_response.raise_for_status()

            with open(file_name, "wb") as audio_file:
                audio_file.write(synthesis_response.content)

            return file_name
        except Exception as e:
            logger.critical(
                f"Error: VOICEVOX unable to generate audio "
                f"(base_url={self.base_url}, speaker_id={self.speaker_id}): {e}"
            )
            return None
