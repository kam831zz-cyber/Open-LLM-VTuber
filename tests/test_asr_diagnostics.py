import os
import unittest
from unittest.mock import patch

import numpy as np

from src.open_llm_vtuber.asr.faster_whisper_asr import VoiceRecognition


class _Segment:
    text = "室温は"


class _Model:
    def transcribe(self, audio, **kwargs):
        return iter([_Segment()]), object()


class FasterWhisperDiagnosticsTest(unittest.TestCase):
    def test_diagnostic_logs_contain_timing_not_audio_or_text(self):
        recognizer = VoiceRecognition.__new__(VoiceRecognition)
        recognizer.prompt = None
        recognizer.LANG = "ja"
        recognizer.model = _Model()

        with patch.dict(os.environ, {"KOMUGI_ASR_DIAGNOSTICS": "1"}), patch(
            "src.open_llm_vtuber.asr.faster_whisper_asr.logger.info"
        ) as log_info:
            result = recognizer.transcribe_np(np.zeros(32000, dtype=np.float32))

        self.assertEqual(result, "室温は")
        messages = [call.args[0] for call in log_info.call_args_list]
        self.assertEqual(
            messages,
            [
                "ASR diagnostic start: samples={} duration_s={:.3f} sample_rate={}",
                "ASR diagnostic first segment: elapsed_ms={}",
                "ASR diagnostic complete: wall_ms={} segments={}",
            ],
        )
        self.assertNotIn(result, " ".join(messages))


if __name__ == "__main__":
    unittest.main()
