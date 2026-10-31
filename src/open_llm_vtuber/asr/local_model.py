"""Resolve existing Faster-Whisper files without importing a model or using HF."""
from pathlib import Path
import re


def resolve_local_model(model, download_root):
    requested = Path(model)
    if requested.is_dir():
        directory = requested.resolve()
    else:
        # Current G10 model. Other aliases need an explicit existing local path;
        # never silently resolve a new hub model or select an arbitrary snapshot.
        if model != "small":
            raise FileNotFoundError("ASR requires an explicit local model directory")
        cache = Path(download_root or "models/whisper") / "models--Systran--faster-whisper-small"
        ref = cache / "refs/main"
        if not ref.is_file():
            raise FileNotFoundError("ASR local snapshot reference missing")
        revision = ref.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise ValueError("Invalid ASR local snapshot reference")
        directory = (cache / "snapshots" / revision).resolve()
        if not directory.is_relative_to(cache.resolve()):
            raise ValueError("ASR snapshot escapes cache")
    required = ("model.bin", "config.json", "tokenizer.json")
    if any(not (directory / name).is_file() or (directory / name).stat().st_size == 0 for name in required):
        raise FileNotFoundError("ASR local model files missing or empty")
    if not any((directory / name).is_file() and (directory / name).stat().st_size > 0
               for name in ("vocabulary.txt", "vocabulary.json")):
        raise FileNotFoundError("ASR local vocabulary missing")
    return directory
