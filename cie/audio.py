"""Sound and audio intelligence subsystem: inspection, FFT, silence/clipping, conversion, procedural SFX, game audio manifests."""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .config import resolve_in_projects


def _read_wav(path: Path) -> tuple[np.ndarray, int, int, int]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sr = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    if sampwidth == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sampwidth}")
    if channels > 1:
        arr = arr.reshape(-1, channels)
    else:
        arr = arr.reshape(-1, 1)
    return arr, sr, channels, sampwidth


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    channels = 1 if pcm16.ndim == 1 else pcm16.shape[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(pcm16.tobytes())


def audio_inspector(path: str) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    arr, sr, channels, sampwidth = _read_wav(resolved)
    mono = np.mean(arr, axis=1)
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
    dc = float(np.mean(mono)) if mono.size else 0.0
    peak_db = round(20.0 * math.log10(peak), 2) if peak > 1e-9 else -120.0
    rms_db = round(20.0 * math.log10(rms), 2) if rms > 1e-9 else -120.0
    return {
        "path": str(resolved),
        "format": "wav",
        "sample_rate": sr,
        "channels": channels,
        "bit_depth": sampwidth * 8,
        "duration_sec": round(len(mono) / sr, 4) if sr else 0.0,
        "peak_amplitude": round(peak, 5),
        "peak_dbfs": peak_db,
        "rms_dbfs": rms_db,
        "dc_offset": round(dc, 5),
    }


def silence_clipping_detector(path: str, silence_threshold: float = 0.005, min_silence_sec: float = 0.25) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    arr, sr, _, _ = _read_wav(resolved)
    mono = np.abs(np.mean(arr, axis=1))
    clipped_samples = int(np.sum(mono >= 0.999))
    win = max(1, int(sr * 0.05))
    silent_runs: list[dict[str, float]] = []
    cur_start: int | None = None
    for i in range(0, len(mono), win):
        chunk = mono[i:i + win]
        if chunk.size and float(np.max(chunk)) < silence_threshold:
            if cur_start is None:
                cur_start = i
        else:
            if cur_start is not None:
                dur = (i - cur_start) / sr
                if dur >= min_silence_sec:
                    silent_runs.append({"start_sec": round(cur_start / sr, 3), "end_sec": round(i / sr, 3), "duration_sec": round(dur, 3)})
                cur_start = None
    if cur_start is not None:
        dur = (len(mono) - cur_start) / sr
        if dur >= min_silence_sec:
            silent_runs.append({"start_sec": round(cur_start / sr, 3), "end_sec": round(len(mono) / sr, 3), "duration_sec": round(dur, 3)})
    return {
        "path": str(resolved),
        "clipped_samples": clipped_samples,
        "has_clipping": clipped_samples > 0,
        "silent_segments": silent_runs,
    }


def frequency_analyzer(path: str, top_k: int = 5) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    arr, sr, _, _ = _read_wav(resolved)
    mono = np.mean(arr, axis=1)
    if mono.size == 0:
        return {"path": str(resolved), "dominant_frequencies_hz": [], "spectral_centroid_hz": 0.0}
    n = min(len(mono), 65536)
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(mono[:n] * win))
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    top_idx = np.argsort(spec)[::-1][:top_k]
    dominant = [{"hz": round(float(freqs[i]), 2), "magnitude": round(float(spec[i]), 2)} for i in top_idx]
    denom = float(np.sum(spec)) or 1.0
    centroid = float(np.sum(freqs * spec) / denom)
    return {
        "path": str(resolved),
        "dominant_frequencies_hz": dominant,
        "spectral_centroid_hz": round(centroid, 2),
    }


def audio_optimizer(path: str, output_path: str, target_peak_dbfs: float = -1.0) -> dict[str, Any]:
    res_in, _ = resolve_in_projects(path)
    res_out, _ = resolve_in_projects(output_path)
    arr, sr, _, _ = _read_wav(res_in)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    target_amp = 10.0 ** (float(target_peak_dbfs) / 20.0)
    gain = (target_amp / peak) if peak > 1e-9 else 1.0
    out_arr = arr * gain
    _write_wav(res_out, out_arr, sr)
    return {"input": str(res_in), "output": str(res_out), "gain_applied": round(gain, 4), "target_peak_dbfs": target_peak_dbfs}


def procedural_sfx_builder(
    output_path: str,
    waveform: str = "sine",
    freq_hz: float = 440.0,
    duration_sec: float = 0.35,
    sample_rate: int = 44100,
    decay: bool = True,
) -> dict[str, Any]:
    res_out, _ = resolve_in_projects(output_path)
    n = max(1, int(float(duration_sec) * int(sample_rate)))
    t = np.linspace(0.0, float(duration_sec), n, endpoint=False)
    wf = waveform.lower()
    if wf == "square":
        sig = np.sign(np.sin(2.0 * np.pi * freq_hz * t))
    elif wf == "sawtooth":
        sig = 2.0 * ((t * freq_hz) % 1.0) - 1.0
    elif wf == "noise":
        rng = np.random.default_rng(42)
        sig = rng.uniform(-1.0, 1.0, size=n)
    else:
        sig = np.sin(2.0 * np.pi * freq_hz * t)
    if decay:
        env = np.exp(-4.0 * (t / max(1e-6, float(duration_sec))))
        sig = sig * env
    sig = sig * 0.8
    _write_wav(res_out, sig.astype(np.float32), sample_rate)
    return {"output": str(res_out), "waveform": wf, "freq_hz": freq_hz, "duration_sec": duration_sec, "sample_rate": sample_rate}


def audio_compare(path_a: str, path_b: str) -> dict[str, Any]:
    a = audio_inspector(path_a)
    b = audio_inspector(path_b)
    return {
        "a": a,
        "b": b,
        "duration_diff_sec": round(b["duration_sec"] - a["duration_sec"], 4),
        "rms_diff_db": round(b["rms_dbfs"] - a["rms_dbfs"], 2),
        "peak_diff_db": round(b["peak_dbfs"] - a["peak_dbfs"], 2),
    }


def game_audio_manifest(events: list[dict[str, Any]], output_path: str) -> dict[str, Any]:
    res_out, _ = resolve_in_projects(output_path)
    res_out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "cie.game_audio.v1", "events": events}
    res_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"manifest_path": str(res_out), "event_count": len(events)}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="audio.inspector",
        area="audio",
        summary="Inspect WAV sample rate, channels, bit depth, duration, peak, RMS, and DC offset.",
        input_schema={"path": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Audio inspected.", "data": audio_inspector(args["path"])},
        tags=["audio", "wav"],
    ))
    reg.register(CapabilitySpec(
        id="audio.silence_clipping",
        area="audio",
        summary="Detect clipping samples and silent segments in WAV audio.",
        input_schema={"path": "str", "silence_threshold": "optional float", "min_silence_sec": "optional float"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Silence and clipping check complete.", "data": silence_clipping_detector(args["path"], float(args.get("silence_threshold", 0.005)), float(args.get("min_silence_sec", 0.25)))},
        tags=["audio", "qa"],
    ))
    reg.register(CapabilitySpec(
        id="audio.frequency",
        area="audio",
        summary="Analyze dominant frequencies and spectral centroid via FFT.",
        input_schema={"path": "str", "top_k": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Frequency analysis complete.", "data": frequency_analyzer(args["path"], int(args.get("top_k", 5)))},
        tags=["audio", "fft"],
    ))
    reg.register(CapabilitySpec(
        id="audio.optimize",
        area="audio",
        summary="Peak-normalize an approved WAV file to a target dBFS.",
        input_schema={"path": "str", "output_path": "str", "target_peak_dbfs": "optional float"},
        permission="write",
        handler=lambda args: {"status": "ok", "summary": "Audio optimized.", "data": audio_optimizer(args["path"], args["output_path"], float(args.get("target_peak_dbfs", -1.0)))},
        tags=["audio", "normalize"],
    ))
    reg.register(CapabilitySpec(
        id="audio.procedural_sfx",
        area="audio",
        summary="Generate deterministic procedural WAV SFX (sine, square, sawtooth, noise).",
        input_schema={"output_path": "str", "waveform": "optional str", "freq_hz": "optional float", "duration_sec": "optional float"},
        permission="write",
        handler=lambda args: {"status": "ok", "summary": "Procedural SFX generated.", "data": procedural_sfx_builder(args["output_path"], args.get("waveform", "sine"), float(args.get("freq_hz", 440.0)), float(args.get("duration_sec", 0.35)))},
        tags=["audio", "sfx"],
    ))
    reg.register(CapabilitySpec(
        id="audio.compare",
        area="audio",
        summary="Compare duration, RMS, and peak loudness between two WAV files.",
        input_schema={"path_a": "str", "path_b": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Audio comparison complete.", "data": audio_compare(args["path_a"], args["path_b"])},
        tags=["audio", "compare"],
    ))
    reg.register(CapabilitySpec(
        id="audio.game_manifest",
        area="audio",
        summary="Write a reusable game audio event manifest JSON file.",
        input_schema={"events": "list[dict]", "output_path": "str"},
        permission="write",
        handler=lambda args: {"status": "ok", "summary": "Game audio manifest written.", "data": game_audio_manifest(args.get("events") or [], args["output_path"])},
        tags=["audio", "game"],
    ))