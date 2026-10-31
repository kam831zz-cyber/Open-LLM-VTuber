import os
import time

import numpy as np
from loguru import logger
from .asr_interface import ASRInterface
from .local_model import resolve_local_model


class VoiceRecognition(ASRInterface):
    BEAM_SEARCH = True
    # SAMPLE_RATE # Defined in asr_interface.py

    def __init__(
        self,
        model_path: str = "distil-medium.en",
        download_root: str = None,
        language: str = "en",
        device: str = "auto",
        compute_type: str = "int8",
        prompt: str = None,
    ) -> None:
        self.MODEL_PATH = model_path
        self.LANG = language
        self.prompt = prompt
        started = time.perf_counter()
        cpu_started = time.process_time()
        local_path = resolve_local_model(model_path, download_root)
        resolved = time.perf_counter()
        from faster_whisper import WhisperModel
        imported = time.perf_counter()
        self.model = WhisperModel(
            model_size_or_path=str(local_path),
            local_files_only=True,
            device=device,
            compute_type=compute_type,
        )
        finished = time.perf_counter()
        self.initialization_metrics = {
            "resolve_ms": round((resolved-started)*1000),
            "native_import_ms": round((imported-resolved)*1000),
            "model_construct_ms": round((finished-imported)*1000),
            "total_ms": round((finished-started)*1000),
            "cpu_ms": round((time.process_time()-cpu_started)*1000),
        }
        logger.info("ASR initialization phases (local-only): {}", self.initialization_metrics)

    def transcribe_np(self, audio: np.ndarray) -> str:
        diagnostic = os.getenv("KOMUGI_ASR_DIAGNOSTICS", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        started_at = time.perf_counter()
        if diagnostic:
            logger.info(
                "ASR diagnostic start: samples={} duration_s={:.3f} sample_rate={}",
                len(audio),
                len(audio) / self.SAMPLE_RATE,
                self.SAMPLE_RATE,
            )
        if self.prompt:
            segments, info = self.model.transcribe(
                audio,
                beam_size=5 if self.BEAM_SEARCH else 1,
                language=self.LANG if self.LANG else None,
                condition_on_previous_text=False,
                prompt=self.prompt,
            )
        else:
            segments, info = self.model.transcribe(
                audio,
                beam_size=5 if self.BEAM_SEARCH else 1,
                language=self.LANG if self.LANG else None,
                condition_on_previous_text=False,
            )
        text = []
        for segment in segments:
            if diagnostic and not text:
                logger.info(
                    "ASR diagnostic first segment: elapsed_ms={}",
                    round((time.perf_counter() - started_at) * 1000),
                )
            text.append(segment.text)

        if diagnostic:
            logger.info(
                "ASR diagnostic complete: wall_ms={} segments={}",
                round((time.perf_counter() - started_at) * 1000),
                len(text),
            )

        if not text:
            return ""
        else:
            return "".join(text)
