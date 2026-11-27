"""Optional local model registry with mandatory license, size, and hardware metadata (no silent downloads)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_config
from .diagnostics import hardware_monitor

MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "bge-small-en-v1.5",
        "name": "BGE Small English v1.5",
        "publisher": "BAAI",
        "task": "local_embeddings",
        "license": "MIT",
        "size_mb": 130,
        "min_ram_mb": 2048,
        "runtime": "onnxruntime / sentence-transformers",
        "source_url": "https://huggingface.co/BAAI/bge-small-en-v1.5",
        "note": "Optional semantic search enhancement; FTS5 works without it.",
    },
    {
        "id": "faster-whisper-base.en",
        "name": "Faster-Whisper Base English",
        "publisher": "SYSTRAN / OpenAI Whisper",
        "task": "local_audio_transcription",
        "license": "MIT",
        "size_mb": 145,
        "min_ram_mb": 2048,
        "runtime": "faster-whisper",
        "source_url": "https://huggingface.co/Systran/faster-whisper-base.en",
        "note": "Optional speech-to-text; audio inspection/SFX work without it.",
    },
    {
        "id": "moondream2-local",
        "name": "Moondream2 Vision",
        "publisher": "vikhyatk",
        "task": "local_vision_captioning",
        "license": "Apache-2.0",
        "size_mb": 1700,
        "min_ram_mb": 6144,
        "runtime": "llama.cpp / transformers",
        "source_url": "https://huggingface.co/vikhyatk/moondream2",
        "note": "Optional semantic image critique; deterministic metrics work without it.",
    },
]


def list_models() -> dict[str, Any]:
    cfg = load_config()
    models_dir = Path(cfg.get("models_dir", "")).expanduser()
    hw = hardware_monitor()
    ram_mb = hw.get("memory_total_mb") or 0
    items = []
    for m in MODEL_CATALOG:
        installed = (models_dir / m["id"]).exists() if str(models_dir) else False
        ram_ok = (ram_mb >= m["min_ram_mb"]) if ram_mb else None
        items.append({
            **m,
            "installed": installed,
            "hardware_compatible": ram_ok,
            "auto_download": False,
        })
    return {"models_dir": str(models_dir), "models": items}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="models.list",
        area="models",
        summary="List optional local models with license, size, hardware check, and install status (no auto-download).",
        input_schema={},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Optional model catalog listed.", "data": list_models()},
        tags=["models", "local-ai"],
    ))