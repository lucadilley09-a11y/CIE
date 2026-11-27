"""Audio studio for CIE (Phase 3D). numpy + scipy only; FFmpeg is optional (OGG/MP3 export).

  synthesis   oscillators (sine, square/pulse, saw, triangle, white/pink/brown noise), ADSR,
              pitch sweeps, vibrato, RBJ biquad filters (static and swept), distortion,
              bit-crush, delay, Schroeder reverb
  SFX         coin, jump, laser, shoot, explosion, hit, hurt, powerup, pickup, blip,
              ui_click, ui_confirm, ui_back, error, magic, whoosh, door, footstep_{grass,stone,wood}
              - every preset takes a seed, so 'give me 5 variations' is one call
  ambience    wind, rain, cave, forest, ocean, fire, hum - rendered as seamless loops
  music       loopable tracks from a key, mood and style (chiptune, ambient, lofi, battle),
              with bass, chords/arpeggio, seeded motif melody and drums; also writes a MIDI file
  editing     trim, fades (linear / equal-power / exponential), gain, gain automation,
              normalise (peak or LUFS), 3-band EQ, filters, pitch (with or without speed),
              time-stretch, reverse, echo, reverb, denoise (spectral gate), make-loop,
              trim-silence, split on silence, concat with crossfade, mix/overlay, resample
  analysis    peak, true peak, RMS, integrated LUFS (BS.1770), crest factor, DC, clipping,
              clicks, edge clicks, silence at the ends, loop seam, spectrum; waveform +
              spectrogram preview sheet
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw
from scipy import signal

SR = 44100

# ---------------------------------------------------------------------------
# WAV I/O
# ---------------------------------------------------------------------------


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Returns float32 (n, channels) in -1..1 and the sample rate. 8/16/24/32-bit PCM and 32-bit float."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("Not a WAV file.")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError("WAV is missing fmt or data.")
    tag, ch, sr, _, _, bits = fmt
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the sub-format GUID starts with the real tag
        for c_pos in range(12, len(raw) - 8):
            if raw[c_pos:c_pos + 4] == b"fmt ":
                tag = struct.unpack("<H", raw[c_pos + 8 + 24:c_pos + 8 + 26])[0]
                break
    if tag == 3:
        arr = np.frombuffer(data[: len(data) // 4 * 4], dtype="<f4").astype(np.float32)
    elif bits == 8:
        arr = (np.frombuffer(data, np.uint8).astype(np.float32) - 128) / 128
    elif bits == 16:
        arr = np.frombuffer(data[: len(data) // 2 * 2], "<i2").astype(np.float32) / 32768
    elif bits == 24:
        b = np.frombuffer(data[: len(data) // 3 * 3], np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        arr = v.astype(np.float32) / (1 << 23)
    elif bits == 32:
        arr = np.frombuffer(data[: len(data) // 4 * 4], "<i4").astype(np.float32) / 2147483648
    else:
        raise ValueError(f"Unsupported WAV bit depth {bits}.")
    return arr.reshape(-1, ch), int(sr)


def write_wav(path: Path, x: np.ndarray, sr: int = SR, bits: int = 16) -> Path:
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    ch = x.shape[1]
    if bits == 24:
        v = np.round(x * 8388607).astype(np.int32).reshape(-1)
        b = np.stack([v & 255, (v >> 8) & 255, (v >> 16) & 255], 1).astype(np.uint8).tobytes()
    else:
        bits = 16
        # TPDF dither before truncating to 16 bits
        rng = np.random.default_rng(0)
        d = (rng.random(x.shape) - rng.random(x.shape)) / 32768
        b = np.round(np.clip(x + d, -1, 1) * 32767).astype("<i2").tobytes()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(bits // 8)
        w.setframerate(int(sr))
        w.writeframes(b)
    return path


def export_compressed(wav: Path, fmt: str) -> Path | None:
    """OGG/MP3 through FFmpeg when it is installed (optional)."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    out = wav.with_suffix("." + fmt)
    codec = ["-c:a", "libvorbis", "-q:a", "5"] if fmt == "ogg" else ["-c:a", "libmp3lame", "-q:a", "3"]
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(wav), *codec, str(out)], check=True, timeout=120)  # noqa: S603
    return out


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def ns(dur: float, sr: int = SR) -> int:
    """Sample count for a duration (one rule everywhere, so layers always line up)."""
    return int(dur * sr + 1e-6)


def t_axis(dur: float, sr: int = SR) -> np.ndarray:
    return np.arange(ns(dur, sr)) / sr


def _blep(t: np.ndarray, dt: np.ndarray) -> np.ndarray:
    out = np.zeros_like(t)
    a = t < dt
    x = t[a] / dt[a]
    out[a] = 2 * x - x * x - 1
    b = t > 1 - dt
    x = (t[b] - 1) / dt[b]
    out[b] = x * x + 2 * x + 1
    return out


def _freq(f: Any, n: int) -> np.ndarray:
    return np.full(n, float(f)) if np.isscalar(f) else np.asarray(f, dtype=np.float64)[:n]


def osc(kind: str, freq: Any, dur: float, sr: int = SR, duty: float = 0.5, rng: np.random.Generator | None = None,
        phase: float = 0.0) -> np.ndarray:
    n = ns(dur, sr)
    rng = rng or np.random.default_rng(0)
    if kind in ("noise", "white"):
        return rng.uniform(-1, 1, n)
    if kind == "pink":
        w = rng.standard_normal(n)
        b, a = [0.049922035, -0.095993537, 0.050612699, -0.004408786], [1, -2.494956002, 2.017265875, -0.522189400]
        y = signal.lfilter(b, a, w)
        return y / (np.abs(y).max() or 1)
    if kind == "brown":
        y = np.cumsum(rng.standard_normal(n))
        y = signal.lfilter([1, -1], [1, -0.995], y)
        return y / (np.abs(y).max() or 1)
    f = _freq(freq, n)
    ph = (np.cumsum(f) / sr + phase) % 1.0
    if kind == "sine":
        return np.sin(2 * np.pi * ph)
    dt = np.clip(f / sr, 1e-6, 0.5)
    if kind == "square":  # PolyBLEP band-limited: no aliasing fizz on high notes
        sq = np.where(ph < duty, 1.0, -1.0) + _blep(ph, dt) - _blep((ph + 1 - duty) % 1.0, dt)
        return sq * 0.8
    if kind == "saw":
        return (2 * ph - 1 - _blep(ph, dt)) * 0.8
    if kind == "triangle":
        return 2 * np.abs(2 * ph - 1) - 1
    raise ValueError(f"Unknown oscillator '{kind}'.")


def adsr(n: int, a: float, d: float, s: float, r: float, sr: int = SR) -> np.ndarray:
    A, D, R = int(a * sr), int(d * sr), int(r * sr)
    S = max(0, n - A - D - R)
    env = np.concatenate([np.linspace(0, 1, A, endpoint=False), np.linspace(1, s, D, endpoint=False), np.full(S, s),
                          np.linspace(s, 0, R)])
    if len(env) < n:
        env = np.pad(env, (0, n - len(env)))
    return env[:n]


def decay(n: int, time_const: float, sr: int = SR, attack: float = 0.002) -> np.ndarray:
    t = np.arange(n) / sr
    env = np.exp(-t / max(time_const, 1e-4))
    A = max(1, int(attack * sr))
    env[:A] *= np.linspace(0, 1, A)
    return env


def sweep(f0: float, f1: float, dur: float, curve: str = "exp", sr: int = SR) -> np.ndarray:
    n = ns(dur, sr)
    u = np.linspace(0, 1, n)
    if curve == "exp" and f0 > 0 and f1 > 0:
        return f0 * (f1 / f0) ** u
    return f0 + (f1 - f0) * u


def biquad(kind: str, f0: float, sr: int = SR, q: float = 0.707, gain_db: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """RBJ audio-EQ-cookbook coefficients."""
    f0 = min(max(float(f0), 10.0), sr * 0.49)
    w = 2 * math.pi * f0 / sr
    cw, sw = math.cos(w), math.sin(w)
    alpha = sw / (2 * q)
    A = 10 ** (gain_db / 40)
    if kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "bandpass":
        b = [alpha, 0, -alpha]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]; a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind in ("lowshelf", "highshelf"):
        sq = 2 * math.sqrt(A) * alpha
        if kind == "lowshelf":
            b = [A * ((A + 1) - (A - 1) * cw + sq), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sq)]
            a = [(A + 1) + (A - 1) * cw + sq, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sq]
        else:
            b = [A * ((A + 1) + (A - 1) * cw + sq), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sq)]
            a = [(A + 1) - (A - 1) * cw + sq, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sq]
    else:
        raise ValueError(f"Unknown filter '{kind}'.")
    b, a = np.array(b) / a[0], np.array(a) / a[0]
    return b, a


def filt(x: np.ndarray, kind: str, f0: Any, q: float = 0.707, gain_db: float = 0.0, sr: int = SR, block: int = 256) -> np.ndarray:
    """Static or swept (f0 as an array) biquad filter; state carries across blocks so sweeps are click-free."""
    if np.isscalar(f0):
        b, a = biquad(kind, float(f0), sr, q, gain_db)
        return signal.lfilter(b, a, x, axis=0)
    f0 = np.asarray(f0)
    out = np.zeros_like(x)
    zi = np.zeros(2) if x.ndim == 1 else np.zeros((2, x.shape[1]))
    for s in range(0, len(x), block):
        b, a = biquad(kind, float(f0[min(s, len(f0) - 1)]), sr, q, gain_db)
        out[s:s + block], zi = signal.lfilter(b, a, x[s:s + block], axis=0, zi=zi)
    return out


def distort(x: np.ndarray, drive: float = 2.0) -> np.ndarray:
    return np.tanh(x * drive) / math.tanh(drive)


def bitcrush(x: np.ndarray, bits: int = 6, down: int = 2) -> np.ndarray:
    q = 2 ** (int(bits) - 1)
    y = np.round(x * q) / q
    if down > 1:
        y = np.repeat(y[::down], down)[: len(x)]
    return y


def delay(x: np.ndarray, time: float = 0.25, feedback: float = 0.35, mix: float = 0.3, sr: int = SR) -> np.ndarray:
    d = max(1, int(time * sr))
    out = np.concatenate([x, np.zeros(d * 6)])
    y = out.copy()
    for i in range(1, 7):
        y[d * i:] += out[:len(out) - d * i] * (feedback ** i) * mix
    return y


def _comb(x: np.ndarray, d: int, g: float) -> np.ndarray:
    """y[n] = x[n] + g*y[n-d], computed a delay-length block at a time (fast, exact)."""
    y = x.copy()
    for s in range(d, len(x), d):
        e = min(s + d, len(x))
        y[s:e] += g * y[s - d:e - d]
    return y


def _allpass(x: np.ndarray, d: int, g: float) -> np.ndarray:
    """y[n] = -g*x[n] + x[n-d] + g*y[n-d]."""
    y = -g * x
    for s in range(0, len(x), d):
        e = min(s + d, len(x))
        if s >= d:
            y[s:e] += x[s - d:e - d] + g * y[s - d:e - d]
    return y


def reverb(x: np.ndarray, room: float = 0.6, mix: float = 0.25, sr: int = SR, tail: float = 1.5) -> np.ndarray:
    """Schroeder reverb: 4 parallel combs + 2 allpasses. Appends a tail."""
    x = np.concatenate([x, np.zeros(int(tail * sr))])
    g = 0.7 + 0.28 * float(room)
    wet = np.zeros_like(x)
    for c in (0.0297, 0.0371, 0.0411, 0.0437):
        wet += _comb(x, int(c * sr * (0.8 + 0.4 * room)), g)
    wet /= 4
    for d_s in (0.005, 0.0017):
        wet = _allpass(wet, int(d_s * sr), 0.7)
    wet = filt(wet, "lowpass", 5000)
    return x * (1 - mix) + wet * mix * 1.6


def fade(x: np.ndarray, fin: float = 0.003, fout: float = 0.01, sr: int = SR, curve: str = "equal_power") -> np.ndarray:
    y = x.copy()
    for secs, at_end in ((fin, False), (fout, True)):
        n = min(len(y), int(secs * sr))
        if n <= 1:
            continue
        u = np.linspace(0, 1, n)
        ramp_ = {"linear": u, "equal_power": np.sin(u * np.pi / 2), "exp": u ** 3, "log": 1 - (1 - u) ** 3}.get(curve, u)
        if at_end:
            y[-n:] = (y[-n:].T * ramp_[::-1]).T
        else:
            y[:n] = (y[:n].T * ramp_).T
    return y


def normalize_peak(x: np.ndarray, target_db: float = -1.0) -> np.ndarray:
    p = float(np.abs(x).max())
    return x if p < 1e-9 else x * (10 ** (target_db / 20) / p)


def soft_limit(x: np.ndarray, ceiling_db: float = -1.0) -> np.ndarray:
    c = 10 ** (ceiling_db / 20)
    return np.tanh(x / c) * c


# ---------------------------------------------------------------------------
# Loudness / analysis
# ---------------------------------------------------------------------------


def _k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    """BS.1770 K-weighting (pre-filter shelf + RLB high-pass), derived for any sample rate."""
    K = math.tan(math.pi * 1681.9744509555319 / sr)
    Q = 0.7071752369554193
    Vh = 10 ** (3.99984385397 / 20)
    Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    b1_ = np.array([(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0])
    a1_ = np.array([1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
    K = math.tan(math.pi * 38.13547087613982 / sr)
    Q = 0.5003270373253953
    a0 = 1 + K / Q + K * K
    b2_ = np.array([1.0, -2.0, 1.0])
    a2_ = np.array([1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
    return signal.lfilter(b2_, a2_, signal.lfilter(b1_, a1_, x, axis=0), axis=0)


def lufs(x: np.ndarray, sr: int = SR) -> float:
    """Integrated loudness per ITU-R BS.1770-4 (400 ms blocks, absolute and relative gates)."""
    x = x if x.ndim == 2 else x[:, None]
    if len(x) < int(0.4 * sr):
        x = np.pad(x, ((0, int(0.4 * sr) - len(x)), (0, 0)))
    y = _k_weight(x, sr)
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    ms = np.array([np.mean(y[s:s + blk] ** 2, axis=0).sum() for s in range(0, len(y) - blk + 1, hop)])
    if not len(ms):
        return -120.0
    L = -0.691 + 10 * np.log10(np.maximum(ms, 1e-12))
    g = ms[L > -70]
    if not len(g):
        return -120.0
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g2 = ms[(L > -70) & (L > rel)]
    return float(-0.691 + 10 * np.log10(max(g2.mean() if len(g2) else g.mean(), 1e-12)))


def db(v: float) -> float:
    return round(20 * math.log10(v), 2) if v > 1e-9 else -120.0


def analyse(x: np.ndarray, sr: int = SR, kind: str = "sfx", loop: bool = False) -> dict[str, Any]:
    x = x if x.ndim == 2 else x[:, None]
    mono = x.mean(axis=1)
    n = len(mono)
    issues: list[dict[str, Any]] = []
    peak = float(np.abs(x).max()) if n else 0.0
    up = signal.resample_poly(mono, 4, 1) if n else mono
    tpeak = float(np.abs(up).max()) if n else 0.0
    rms = float(np.sqrt(np.mean(mono ** 2))) if n else 0.0
    L = lufs(x, sr) if n else -120.0
    m: dict[str, Any] = {"duration_s": round(n / sr, 3), "sample_rate": sr, "channels": x.shape[1], "peak_dbfs": db(peak),
                         "true_peak_dbtp": db(tpeak), "rms_dbfs": db(rms), "lufs": round(L, 1),
                         "crest_db": round(db(peak) - db(rms), 1), "dc_offset": round(float(mono.mean()), 5)}
    clipped = int(np.sum(np.abs(x) >= 0.9999))
    m["clipped_samples"] = clipped
    if clipped:
        issues.append({"severity": "error", "message": f"{clipped} clipped samples; lower the gain or limit."})
    if tpeak > 10 ** (-0.5 / 20) and not clipped:
        issues.append({"severity": "warn", "message": f"True peak {db(tpeak)} dBTP; keep under -1 dBTP so encoding to OGG/MP3 doesn't clip."})
    if abs(m["dc_offset"]) > 0.01:
        issues.append({"severity": "warn", "message": f"DC offset {m['dc_offset']}; run remove_dc."})
    # silence at the ends
    thr = 10 ** (-60 / 20)
    loud = np.nonzero(np.abs(mono) > thr)[0]
    if len(loud):
        lead, tail = loud[0] / sr, (n - 1 - loud[-1]) / sr
        m["leading_silence_s"], m["trailing_silence_s"] = round(lead, 3), round(tail, 3)
        if kind == "sfx" and lead > 0.03:
            issues.append({"severity": "warn", "message": f"{lead * 1000:.0f} ms of silence before the sound starts; game SFX should start instantly (trim)."})
        if not loop and tail > 0.5:
            issues.append({"severity": "warn", "message": f"{tail:.2f} s of silence at the end; trim it."})
    else:
        issues.append({"severity": "error", "message": "The file is silent."})
    # silent gaps in the middle (unintended silence)
    win = max(1, int(0.05 * sr))
    if n > win * 4 and len(loud):
        env = np.sqrt(np.convolve(mono ** 2, np.ones(win) / win, mode="same"))
        mid = env[loud[0]:loud[-1]]
        quiet = mid < 10 ** (-55 / 20)
        runs, cur = 0, 0
        longest = 0
        for q in quiet:
            cur = cur + 1 if q else 0
            longest = max(longest, cur)
        if longest / sr > (0.3 if kind == "sfx" else 1.0):
            issues.append({"severity": "warn", "message": f"{longest / sr:.2f} s of silence inside the sound (fine if intended, e.g. an echo gap)."})
        del runs
    # edge clicks
    edge = max(abs(float(mono[0])), abs(float(mono[-1]))) if n else 0
    m["edge_level"] = round(edge, 4)
    if not loop and edge > 0.02:
        issues.append({"severity": "warn", "message": f"Starts or ends at level {edge:.3f} (not near zero): it will click. Add a short fade."})
    # interior clicks: an isolated jump much bigger than anything within 5 ms on either side
    # (periodic edges in square waves have equally big neighbours, so they don't count)
    if n > 64:
        from scipy.ndimage import maximum_filter1d
        d2 = np.abs(np.diff(mono, 2))
        w = max(8, int(0.005 * sr))
        c = maximum_filter1d(d2, w)  # centred window
        a, b = 3 + (w - 1) // 2, 3 + w // 2  # skip the 2 neighbours one spike always disturbs
        left = np.r_[np.zeros(a), c[:-a]] if len(c) > a else np.zeros_like(c)   # window just before each sample
        right = np.r_[c[b:], np.zeros(b)] if len(c) > b else np.zeros_like(c)   # window just after
        iso = (d2 > 0.15) & (d2 > 3.0 * np.maximum(left, right) + 0.02)
        hits = np.flatnonzero(iso)
        clicks = int(1 + np.sum(np.diff(hits) > 3)) if len(hits) else 0
        m["clicks"] = clicks
        if clicks:
            issues.append({"severity": "warn", "message": f"{clicks} clicks/pops (sudden isolated sample jumps)."})
    if loop and n > 10:
        jump = abs(float(mono[0]) - float(mono[-1]))
        slope = float(np.median(np.abs(np.diff(mono)))) + 1e-5
        ewin = min(n // 4, int({"music": 1.0, "ambience": 0.5}.get(kind, 0.05) * sr))
        e_end = float(np.sqrt(np.mean(mono[-ewin:] ** 2)))
        e_start = float(np.sqrt(np.mean(mono[:ewin] ** 2)))
        m["loop_seam_jump"] = round(jump / slope, 2)
        m["loop_level_change_db"] = round(db(e_start + 1e-9) - db(e_end + 1e-9), 2)
        if jump > 6 * slope + 0.01:
            issues.append({"severity": "error", "message": "Loop point clicks: the last sample doesn't flow into the first."})
        # compare with how much the level moves between neighbouring windows inside the clip
        lv = [db(float(np.sqrt(np.mean(mono[i:i + ewin] ** 2))) + 1e-9) for i in range(0, n - ewin + 1, ewin)]
        inner = max((abs(a - b) for a, b in zip(lv[:-1], lv[1:])), default=0.0)
        m["max_inner_level_change_db"] = round(inner, 2)
        if abs(m["loop_level_change_db"]) > max(3.0, inner + 1.5):
            issues.append({"severity": "warn", "message": f"Loudness jumps {m['loop_level_change_db']} dB at the loop point."})
    targets = {"sfx": (-24, -10), "ui": (-26, -12), "ambience": (-34, -18), "music": (-20, -12), "voice": (-22, -14)}
    lo, hi = targets.get(kind, (-30, -8))
    if L > -120 and not lo <= L <= hi:
        issues.append({"severity": "warn", "message": f"Loudness {L:.1f} LUFS is outside the usual {lo} to {hi} LUFS for {kind}; normalise so it sits well in the mix."})
    if n:
        spec = np.abs(np.fft.rfft(mono * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, 1 / sr)
        m["spectral_centroid_hz"] = round(float((spec * freqs).sum() / (spec.sum() or 1)), 1)
    from .studio import verdict
    return {"metrics": m, "issues": issues, "verdict": verdict(issues)}


def waveform_image(x: np.ndarray, w: int = 512, h: int = 160, sr: int = SR) -> Image.Image:
    mono = x.mean(axis=1) if x.ndim == 2 else x
    img = Image.new("RGBA", (w, h), (28, 30, 36, 255))
    d = ImageDraw.Draw(img)
    d.line([(0, h / 2), (w, h / 2)], fill=(60, 64, 74, 255))
    for yv in (-1.0, 1.0):
        y = h / 2 - yv * (h / 2 - 4) * 10 ** (-1 / 20)
        d.line([(0, y), (w, y)], fill=(110, 60, 60, 255))
    if len(mono):
        bins = np.array_split(mono, w)
        for i, b in enumerate(bins):
            if len(b):
                lo, hi = float(b.min()), float(b.max())
                rms = float(np.sqrt(np.mean(b ** 2)))
                col = (255, 90, 90, 255) if max(abs(lo), abs(hi)) >= 0.999 else (110, 190, 255, 255)
                d.line([(i, h / 2 - hi * (h / 2 - 4)), (i, h / 2 - lo * (h / 2 - 4))], fill=col)
                d.line([(i, h / 2 - rms * (h / 2 - 4)), (i, h / 2 + rms * (h / 2 - 4))], fill=(190, 230, 255, 255))
    return img


def spectrogram_image(x: np.ndarray, sr: int = SR, w: int = 512, h: int = 160) -> Image.Image:
    mono = x.mean(axis=1) if x.ndim == 2 else x
    if len(mono) < 512:
        mono = np.pad(mono, (0, 512 - len(mono)))
    f, t, S = signal.spectrogram(mono, sr, nperseg=512, noverlap=384)
    S = 10 * np.log10(S + 1e-12)
    hi = np.percentile(S, 99.7)
    S = np.clip((S - (hi - 70)) / 70, 0, 1)
    # log-frequency rows
    rows = np.geomspace(30, sr / 2, h)
    idx = np.clip(np.searchsorted(f, rows), 0, len(f) - 1)
    img = S[idx][::-1]
    img = np.array(Image.fromarray((img * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)) / 255.0
    # perceptual dark-blue -> purple -> orange -> pale yellow ramp (magma-like)
    stops = np.array([[0.0, 0.02, 0.02, 0.08], [0.3, 0.28, 0.07, 0.45], [0.6, 0.82, 0.24, 0.35], [0.85, 0.99, 0.6, 0.25], [1.0, 0.99, 0.96, 0.75]])
    rgb = np.stack([np.interp(img, stops[:, 0], stops[:, c]) for c in (1, 2, 3)], -1)
    return Image.fromarray((rgb * 255).astype(np.uint8), "RGB").convert("RGBA")


def finish_audio(x: np.ndarray, name: str, title: str, kind: str = "sfx", loop: bool = False, sr: int = SR,
                 extra_files: list[str] | None = None, info: dict[str, Any] | None = None, formats: list[str] | None = None,
                 extra_panels: list[tuple[str, Image.Image]] | None = None) -> dict[str, Any]:
    from .studio import make_sheet, out_path, save_sheet, write_manifest
    p = write_wav(out_path("audio", name, ".wav"), x, sr)
    files = [str(p)] + list(extra_files or [])
    notes = []
    for fmt in formats or []:
        if fmt in ("ogg", "mp3"):
            try:
                c = export_compressed(p, fmt)
            except (subprocess.SubprocessError, OSError) as exc:
                c = None
                notes.append(f"{fmt} export failed: {exc}")
            if c:
                files.append(str(c))
            else:
                notes.append(f"{fmt.upper()} export needs FFmpeg (optional). The WAV is ready.")
    an = analyse(x, sr, kind, loop)
    panels = [("waveform (red line = -1 dBFS)", waveform_image(x, sr=sr)), ("spectrogram (log freq)", spectrogram_image(x, sr))]
    if loop:
        panels.append(("loop seam: end | start", waveform_image(np.concatenate([x[-int(0.05 * sr):], x[:int(0.05 * sr)]]), sr=sr)))
    panels += list(extra_panels or [])
    mt = an["metrics"]
    lines = [f"{mt['duration_s']} s, peak {mt['peak_dbfs']} dBFS, true peak {mt['true_peak_dbtp']} dBTP, {mt['lufs']} LUFS, crest {mt['crest_db']} dB",
             f"kind {kind}{', loop' if loop else ''}, centroid {mt.get('spectral_centroid_hz')} Hz, verdict {an['verdict']}"]
    sheet = make_sheet(title, panels, lines + notes, an["issues"], panel_size=512, cols=2)
    sp = save_sheet(sheet, "audio", name)
    out = {"files": files, "preview": str(sp), **an, **(info or {})}
    if notes:
        out["notes"] = notes
    write_manifest(p, out)
    return out


# ---------------------------------------------------------------------------
# SFX presets
# ---------------------------------------------------------------------------


def _v(rng: np.random.Generator, base: float, spread: float = 0.1) -> float:
    return base * (1 + rng.uniform(-spread, spread))


def sfx_coin(r: np.random.Generator, p: dict[str, Any]) -> np.ndarray:
    f = _v(r, 988) * 2 ** (p.get("pitch", 0) / 12)
    a = osc("square", f, 0.07, duty=0.5) * decay(ns(0.07), 0.2)
    b = osc("square", f * 1.335, 0.32, duty=0.5) * decay(ns(0.32), 0.09)
    return np.concatenate([a, b]) * 0.5


def sfx_jump(r, p):
    d = _v(r, 0.28)
    f = sweep(_v(r, 180), _v(r, 620), d) * 2 ** (p.get("pitch", 0) / 12)
    return osc("square", f, d, duty=0.25) * adsr(len(f), 0.005, 0.05, 0.6, 0.15) * 0.45


def sfx_laser(r, p):
    d = _v(r, 0.25)
    f = sweep(_v(r, 1800), _v(r, 180), d) * 2 ** (p.get("pitch", 0) / 12)
    x = osc("saw", f, d) * decay(len(f), 0.09)
    return filt(x, "lowpass", 6000) * 0.6


def sfx_shoot(r, p):
    d = _v(r, 0.22)
    n = ns(d)
    noise = osc("white", 0, d, rng=r) * decay(n, 0.04)
    thump = osc("sine", sweep(_v(r, 160), 50, d), d) * decay(n, 0.06)
    x = filt(noise, "lowpass", sweep(9000, 800, d)) * 0.8 + thump * 0.9
    return distort(x, 1.8) * 0.6


def sfx_explosion(r, p):
    d = _v(r, 1.4)
    n = ns(d)
    rumble = filt(osc("brown", 0, d, rng=r), "lowpass", sweep(1200, 120, d)) * decay(n, 0.45, attack=0.004)
    crack = filt(osc("white", 0, d, rng=r), "lowpass", sweep(7000, 400, d)) * decay(n, 0.08)
    boom = osc("sine", sweep(90, 30, d), d) * decay(n, 0.25)
    return distort(rumble * 1.2 + crack * 0.6 + boom * 0.9, 1.6) * 0.7


def sfx_hit(r, p):
    d = _v(r, 0.18)
    n = ns(d)
    body = osc("sine", sweep(_v(r, 220), 70, d), d) * decay(n, 0.05)
    snap = filt(osc("white", 0, d, rng=r), "bandpass", _v(r, 2200), q=1.2) * decay(n, 0.02)
    return distort(body * 1.0 + snap * 0.9, 2.0) * 0.6


def sfx_hurt(r, p):
    d = _v(r, 0.3)
    f = sweep(_v(r, 520), _v(r, 160), d) * 2 ** (p.get("pitch", 0) / 12)
    x = osc("square", f * (1 + 0.03 * np.sin(2 * np.pi * 40 * t_axis(d))), d, duty=0.4) * adsr(len(f), 0.004, 0.05, 0.7, 0.15)
    return bitcrush(x, 7, 2) * 0.45


def sfx_powerup(r, p):
    notes = [0, 4, 7, 12, 16, 19, 24]
    base = _v(r, 330, 0.05) * 2 ** (p.get("pitch", 0) / 12)
    parts = [osc("square", base * 2 ** (k / 12) * (1 + 0.004 * np.sin(2 * np.pi * 6 * t_axis(0.065))), 0.065, duty=0.5) * adsr(ns(0.065), 0.002, 0.02, 0.8, 0.02) for k in notes]
    tail = osc("square", base * 4, 0.25, duty=0.5) * decay(ns(0.25), 0.08)
    return np.concatenate(parts + [tail]) * 0.35


def sfx_pickup(r, p):
    base = _v(r, 660, 0.05) * 2 ** (p.get("pitch", 0) / 12)
    a = osc("triangle", base, 0.06) * adsr(ns(0.06), 0.002, 0.02, 0.8, 0.02)
    b = osc("triangle", base * 1.5, 0.18) * decay(ns(0.18), 0.06)
    return np.concatenate([a, b]) * 0.6


def sfx_blip(r, p):
    f = _v(r, 1200, 0.05) * 2 ** (p.get("pitch", 0) / 12)
    return osc("square", f, 0.05, duty=0.5) * adsr(ns(0.05), 0.001, 0.01, 0.7, 0.025) * 0.35


def sfx_ui_click(r, p):
    d = 0.035
    n = ns(d)
    x = filt(osc("white", 0, d, rng=r), "bandpass", _v(r, 3500), q=2.0) * decay(n, 0.006) * 1.6
    x += osc("sine", _v(r, 1800), d) * decay(n, 0.008) * 0.3
    return x * 0.6


def sfx_ui_confirm(r, p):
    base = _v(r, 784, 0.03) * 2 ** (p.get("pitch", 0) / 12)
    a = osc("sine", base, 0.08) * adsr(ns(0.08), 0.003, 0.03, 0.6, 0.03)
    b = osc("sine", base * 1.5, 0.22) * decay(ns(0.22), 0.07)
    return np.concatenate([a, b]) * 0.5


def sfx_ui_back(r, p):
    base = _v(r, 784, 0.03) * 2 ** (p.get("pitch", 0) / 12)
    a = osc("sine", base * 1.5, 0.08) * adsr(ns(0.08), 0.003, 0.03, 0.6, 0.03)
    b = osc("sine", base, 0.2) * decay(ns(0.2), 0.06)
    return np.concatenate([a, b]) * 0.5


def sfx_error(r, p):
    d = 0.32
    x = (osc("square", 140, d, duty=0.5) + osc("square", 148, d, duty=0.5)) * 0.5
    gate = (np.floor(t_axis(d) / 0.08) % 2 == 0).astype(float)
    gate = filt(gate, "lowpass", 300)
    return filt(x * gate, "lowpass", 2500) * adsr(len(x), 0.003, 0.02, 0.9, 0.04) * 0.45


def sfx_magic(r, p):
    d = _v(r, 0.9)
    n = ns(d)
    tt = t_axis(d)
    base = _v(r, 880) * 2 ** (p.get("pitch", 0) / 12)
    x = sum(osc("sine", base * k * (1 + 0.01 * np.sin(2 * np.pi * 5 * tt)), d, phase=r.random()) / k for k in (1, 2, 3, 5))
    shimmer = filt(osc("white", 0, d, rng=r), "bandpass", sweep(3000, 9000, d), q=4) * 0.4
    return reverb((x * 0.3 + shimmer) * adsr(n, 0.05, 0.2, 0.6, 0.5), 0.7, 0.35, tail=0.8) * 0.5


def sfx_whoosh(r, p):
    d = _v(r, 0.55)
    n = ns(d)
    env = np.sin(np.linspace(0, np.pi, n)) ** 2
    x = filt(osc("pink", 0, d, rng=r), "bandpass", sweep(_v(r, 400), _v(r, 2600), d), q=1.5)
    return x * env * 1.8


def sfx_door(r, p):
    d = 0.9
    n = ns(d)
    creak_f = 180 + 60 * np.sin(2 * np.pi * 1.3 * t_axis(d)) + r.uniform(-10, 10)
    creak = filt(osc("saw", creak_f * (1 + 0.02 * osc("white", 0, d, rng=r)), d), "bandpass", 900, q=3) * adsr(n, 0.05, 0.1, 0.7, 0.2)
    thud_d = 0.3
    thud = osc("sine", sweep(110, 50, thud_d), thud_d) * decay(ns(thud_d), 0.06) + filt(osc("white", 0, thud_d, rng=r), "lowpass", 900) * decay(ns(thud_d), 0.03) * 0.5
    out = np.concatenate([creak * 0.5, thud * 0.9])
    return out


def _footstep(surface: str) -> Callable[[np.random.Generator, dict[str, Any]], np.ndarray]:
    def make(r: np.random.Generator, p: dict[str, Any]) -> np.ndarray:
        d = 0.22
        n = ns(d)
        if surface == "grass":
            x = filt(osc("white", 0, d, rng=r), "highpass", 1800) * decay(n, 0.05) * 0.8
            x = filt(x, "lowpass", 7000)
        elif surface == "stone":
            x = filt(osc("white", 0, d, rng=r), "bandpass", _v(r, 1600), q=1.0) * decay(n, 0.018) * 1.6
            x += osc("sine", _v(r, 120), d) * decay(n, 0.02) * 0.4
        else:  # wood
            x = filt(osc("white", 0, d, rng=r), "bandpass", _v(r, 600), q=2.5) * decay(n, 0.03) * 2.0
            x += osc("sine", _v(r, 180), d) * decay(n, 0.04) * 0.5
        return x * 0.7
    return make


SFX: dict[str, Callable[[np.random.Generator, dict[str, Any]], np.ndarray]] = {
    "coin": sfx_coin, "jump": sfx_jump, "laser": sfx_laser, "shoot": sfx_shoot, "explosion": sfx_explosion, "hit": sfx_hit,
    "hurt": sfx_hurt, "powerup": sfx_powerup, "pickup": sfx_pickup, "blip": sfx_blip, "ui_click": sfx_ui_click,
    "ui_confirm": sfx_ui_confirm, "ui_back": sfx_ui_back, "error": sfx_error, "magic": sfx_magic, "whoosh": sfx_whoosh,
    "door": sfx_door, "footstep_grass": _footstep("grass"), "footstep_stone": _footstep("stone"), "footstep_wood": _footstep("wood"),
}


def make_sfx(kind: str, seed: int = 0, params: dict[str, Any] | None = None) -> np.ndarray:
    if kind not in SFX:
        raise ValueError(f"Unknown SFX '{kind}'. Kinds: {', '.join(SFX)}.")
    p = params or {}
    x = SFX[kind](np.random.default_rng(int(seed)), p)
    x = x - np.mean(x)
    if p.get("reverb"):
        x = reverb(x, float(p.get("room", 0.5)), float(p["reverb"]), tail=0.6)
    x = trim_silence(x, -60, keep_tail=0.02)
    x = fade(x, 0.002, 0.02)
    target = float(p.get("lufs", -16.0 if not kind.startswith("ui") else -19.0))
    return master(x, target)


def master(x: np.ndarray, target_lufs: float, ceiling_db: float = -1.0, sr: int = SR) -> np.ndarray:
    """Loudness-normalise, then keep the true peak under the ceiling without clipping."""
    L = lufs(x, sr)
    if L > -100:
        x = x * 10 ** ((target_lufs - L) / 20)
    up = np.abs(signal.resample_poly(x if x.ndim == 1 else x.mean(axis=1), 4, 1)).max()
    c = 10 ** (ceiling_db / 20)
    if up > c:
        x = soft_limit(x * 0.98, ceiling_db - 0.2)
        up2 = np.abs(signal.resample_poly(x if x.ndim == 1 else x.mean(axis=1), 4, 1)).max()
        if up2 > c:
            x = x * (c / up2) * 0.99
    return x


# ---------------------------------------------------------------------------
# Ambience loops
# ---------------------------------------------------------------------------


def make_loop(x: np.ndarray, xfade: float, sr: int = SR) -> np.ndarray:
    """Seamless loop: overlap the tail onto the head with an equal-power crossfade."""
    n = int(xfade * sr)
    if n <= 0 or len(x) <= 2 * n:
        return x
    body = x[:-n].copy()
    u = np.linspace(0, 1, n)
    fin, fout = np.sin(u * np.pi / 2), np.cos(u * np.pi / 2)
    if body.ndim == 1:
        body[:n] = body[:n] * fin + x[-n:] * fout
    else:
        body[:n] = body[:n] * fin[:, None] + x[-n:] * fout[:, None]
    return body


def ambience(kind: str, length: float = 20.0, seed: int = 0, intensity: float = 1.0) -> np.ndarray:
    r = np.random.default_rng(int(seed))
    xf = min(2.0, length / 4)
    d = float(length) + xf
    n = ns(d)
    tt = t_axis(d)
    k = float(intensity)
    lfo = lambda rate, ph=0.0: 0.5 + 0.5 * np.sin(2 * np.pi * rate * tt + ph)  # noqa: E731
    if kind == "wind":
        base = osc("brown", 0, d, rng=r)
        gust = filt(osc("white", 0, d, rng=r), "lowpass", 0.4) * 50
        gust = 0.4 + 0.6 * np.clip(gust / (np.abs(gust).max() or 1) * 0.5 + 0.5, 0, 1)
        whistle = filt(osc("white", 0, d, rng=r), "bandpass", 600 + 500 * lfo(0.07), q=6) * 0.35 * k
        x = filt(base, "lowpass", 900) * gust + whistle * gust
    elif kind == "rain":
        hiss = filt(osc("pink", 0, d, rng=r), "highpass", 1200) * 0.35
        drops = np.zeros(n)
        cnt = int(d * 120 * k)
        for pos in r.integers(0, n - 2000, cnt):
            ln = int(r.uniform(0.004, 0.015) * SR)
            f = r.uniform(1500, 5000)
            drops[pos:pos + ln] += np.sin(2 * np.pi * f * np.arange(ln) / SR) * np.exp(-np.arange(ln) / (ln / 4)) * r.uniform(0.05, 0.3)
        x = hiss + filt(drops, "highpass", 800)
    elif kind == "cave":
        drone = filt(osc("brown", 0, d, rng=r), "lowpass", 160) * 1.2
        drone += osc("sine", 55, d) * 0.05 * lfo(0.05)
        drips = np.zeros(n)
        for pos in r.integers(SR, n - SR, max(1, int(d / 2.5))):
            ln = ns(0.25)
            f = r.uniform(900, 1800)
            drips[pos:pos + ln] += osc("sine", sweep(f, f * 1.8, 0.25), 0.25) * decay(ln, 0.03) * 0.4
        x = drone + reverb(drips, 0.9, 0.6, tail=0)[:n]
    elif kind == "forest":
        x = filt(osc("pink", 0, d, rng=r), "lowpass", 2500) * 0.25 * (0.6 + 0.4 * lfo(0.08))
        birds = np.zeros(n)
        for pos in r.integers(0, n - SR, max(1, int(d * 0.6 * k))):
            notes = r.integers(2, 6)
            f0 = r.uniform(2500, 4500)
            cur = pos
            for _ in range(notes):
                ln = int(r.uniform(0.04, 0.12) * SR)
                ff = sweep(f0 * r.uniform(0.9, 1.1), f0 * r.uniform(1.1, 1.5), ln / SR)
                if cur + ln < n:
                    birds[cur:cur + ln] += osc("sine", ff, ln / SR) * np.sin(np.linspace(0, np.pi, ln)) * r.uniform(0.05, 0.15)
                cur += ln + int(r.uniform(0.02, 0.08) * SR)
        x = x + reverb(birds, 0.5, 0.25, tail=0)[:n]
    elif kind == "ocean":
        wave_env = 0.35 + 0.65 * lfo(1 / 7.0) ** 2
        x = filt(osc("pink", 0, d, rng=r), "lowpass", 600 + 1800 * wave_env) * wave_env * 1.2
    elif kind == "fire":
        x = filt(osc("brown", 0, d, rng=r), "lowpass", 400) * 0.8
        cr = np.zeros(n)
        for pos in r.integers(0, n - 500, int(d * 25 * k)):
            ln = int(r.uniform(0.001, 0.006) * SR)
            cr[pos:pos + ln] += r.uniform(-1, 1, ln) * r.uniform(0.1, 0.6)
        x = x + filt(cr, "highpass", 1500)
    elif kind == "hum":
        x = sum(osc("sine", 60 * h, d) / h for h in (1, 2, 3, 5)) * 0.15 * (0.9 + 0.1 * lfo(0.3))
        x += filt(osc("pink", 0, d, rng=r), "bandpass", 300, q=0.8) * 0.15
    else:
        raise ValueError("Unknown ambience. Kinds: wind, rain, cave, forest, ocean, fire, hum.")
    x = x - np.mean(x)
    x = make_loop(x, xf)
    # stereo width: decorrelate a little with a short delay on one side
    dly = ns(0.011)
    st = np.stack([x, np.r_[x[-dly:], x[:-dly]]], 1)
    return master(st, -24.0)


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10], "dorian": [0, 2, 3, 5, 7, 9, 10],
          "pentatonic": [0, 2, 4, 7, 9], "minor_pentatonic": [0, 3, 5, 7, 10], "harmonic_minor": [0, 2, 3, 5, 7, 8, 11]}
PROGRESSIONS = {"happy": ("major", [0, 4, 5, 3]), "heroic": ("major", [0, 3, 4, 0]), "sad": ("minor", [0, 5, 2, 6]),
                "epic": ("minor", [0, 5, 2, 6]), "mysterious": ("harmonic_minor", [0, 3, 0, 4]), "calm": ("major", [0, 5, 3, 4]),
                "tense": ("minor", [0, 1, 0, 4])}
NOTE = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}


def mtof(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _scale_note(root: int, scale: list[int], degree: int, octave: int) -> int:
    o, d = divmod(degree, len(scale))
    return root + 12 * (octave + o) + scale[d]


def compose(key: str = "C", mood: str = "happy", style: str = "chiptune", tempo: int = 0, bars: int = 8, seed: int = 0) -> dict[str, Any]:
    """Note events per part: {part: [(start_beat, length_beats, midi, velocity)]}."""
    if mood not in PROGRESSIONS:
        raise ValueError(f"Unknown mood '{mood}'. Moods: {', '.join(PROGRESSIONS)}.")
    if key not in NOTE:
        raise ValueError(f"Unknown key '{key}'.")
    r = np.random.default_rng(int(seed))
    scale_name, prog = PROGRESSIONS[mood]
    scale = SCALES[scale_name]
    root = NOTE[key]
    tempo = int(tempo) or {"chiptune": 140, "ambient": 72, "lofi": 82, "battle": 160}.get(style, 120)
    bars = max(2, int(bars))
    ev: dict[str, list[tuple[float, float, int, float]]] = {"bass": [], "chords": [], "melody": [], "drums": []}
    # motif: 2 bars of rhythm + scale-step contour, varied on repeats
    rhythms = [[1, 0.5, 0.5, 1, 1], [0.5, 0.5, 1, 0.5, 0.5, 1], [1.5, 0.5, 1, 1], [0.75, 0.25, 1, 0.5, 0.5, 1]]
    motif_r = rhythms[int(r.integers(0, len(rhythms)))] + rhythms[int(r.integers(0, len(rhythms)))]
    contour = np.cumsum(r.choice([-2, -1, -1, 0, 1, 1, 2], len(motif_r)))
    for bar in range(bars):
        deg = prog[bar % len(prog)]
        b0 = bar * 4.0
        chord = [_scale_note(root, scale, deg + i, 3) for i in (0, 2, 4)]
        # bass
        if style in ("chiptune", "battle"):
            for i in range(8 if style == "battle" else 4):
                step = 4 / (8 if style == "battle" else 4)
                ev["bass"].append((b0 + i * step, step * 0.9, chord[0] - 12 + (12 if (i % 4 == 2 and style == "chiptune") else 0), 0.8))
        else:
            ev["bass"].append((b0, 4.0, chord[0] - 12, 0.7))
        # chords / arpeggio
        if style in ("chiptune", "battle"):
            for i in range(16):
                ev["chords"].append((b0 + i * 0.25, 0.22, chord[i % 3] + 12, 0.45))
        else:
            for m in chord:
                ev["chords"].append((b0, 4.0, m + 12, 0.35))
        # melody
        if style != "ambient" or bar % 2 == 0:
            half = (bar % 2) * 4.0
            t = 0.0
            for i, ln in enumerate(motif_r):
                if t >= half and t < half + 4:
                    degree = deg + int(contour[i]) + (2 if (bar // 2) % 2 else 0)
                    pitch = _scale_note(root, scale, degree, 5)
                    if (t - half) == 0:  # land on a chord tone at the bar start
                        pitch = min([c + 24 for c in chord], key=lambda c: abs(c - pitch))
                    if not (style == "ambient" and r.random() < 0.3):
                        ev["melody"].append((b0 + t - half, ln * 0.95, int(pitch), 0.7))
                t += ln
        # drums
        if style in ("chiptune", "battle", "lofi"):
            for i in range(8):
                pos = b0 + i * 0.5
                if i in (0, 4) or (style == "battle" and i % 2 == 0):
                    ev["drums"].append((pos, 0.25, 36, 0.9))
                if i in (2, 6):
                    ev["drums"].append((pos, 0.25, 38, 0.8))
                ev["drums"].append((pos, 0.1, 42, 0.35 if i % 2 else 0.5))
    return {"tempo": tempo, "key": key, "scale": scale_name, "mood": mood, "style": style, "bars": bars, "events": ev}


def _voice(style: str, part: str, f: float, dur: float, vel: float) -> np.ndarray:
    n = ns(dur)
    if n <= 0:
        return np.zeros(0)
    if part == "drums":
        return np.zeros(n)
    if style in ("chiptune", "battle"):
        wave_ = {"bass": "triangle", "chords": "square", "melody": "square"}[part]
        x = osc(wave_, f * (1 + 0.003 * np.sin(2 * np.pi * 5.5 * t_axis(dur))) if part == "melody" else f, dur, duty=0.25 if part == "chords" else 0.5)
        env = adsr(n, 0.003, 0.05, 0.7, min(0.05, dur * 0.3))
    elif style == "ambient":
        x = sum(osc("saw", f * (1 + det), dur, phase=i * 0.3) for i, det in enumerate((-0.004, 0, 0.004))) / 3
        x = filt(x, "lowpass", 1400 if part != "bass" else 400)
        env = adsr(n, min(0.8, dur * 0.3), 0.3, 0.8, min(1.2, dur * 0.4))
    else:  # lofi
        x = osc("triangle", f, dur) * 0.6 + osc("sine", f * 2, dur) * 0.25
        x = filt(x, "lowpass", 2200)
        env = adsr(n, 0.01, 0.2, 0.5, min(0.2, dur * 0.4))
    return x * env * vel


def _drum(note: int, vel: float, r: np.random.Generator) -> np.ndarray:
    if note == 36:
        d = 0.3
        return osc("sine", sweep(150, 45, d), d) * decay(ns(d), 0.08) * vel
    if note == 38:
        d = 0.2
        return (filt(osc("white", 0, d, rng=r), "bandpass", 1800, q=0.8) * decay(ns(d), 0.05) * 0.9
                + osc("sine", 190, d) * decay(ns(d), 0.03) * 0.5) * vel
    d = 0.06
    return filt(osc("white", 0, d, rng=r), "highpass", 7000) * decay(ns(d), 0.012) * vel


def render_music(song: dict[str, Any], seed: int = 0) -> np.ndarray:
    r = np.random.default_rng(int(seed))
    spb = 60.0 / song["tempo"]
    total = song["bars"] * 4 * spb
    tail = 2.5
    n = int((total + tail) * SR)
    mix = {"bass": 0.55, "chords": 0.28, "melody": 0.42, "drums": 0.7}
    pans = {"bass": 0.0, "chords": -0.3, "melody": 0.25, "drums": 0.0}
    out = np.zeros((n, 2))
    for part, events in song["events"].items():
        buf = np.zeros(n)
        for start, ln, midi, vel in events:
            s = ns(start * spb)
            x = _drum(midi, vel, r) if part == "drums" else _voice(song["style"], part, mtof(midi), ln * spb, vel)
            e = min(n, s + len(x))
            buf[s:e] += x[: e - s]
        p = pans[part]
        out[:, 0] += buf * mix[part] * math.cos((p + 1) * math.pi / 4)
        out[:, 1] += buf * mix[part] * math.sin((p + 1) * math.pi / 4)
    if song["style"] in ("ambient", "lofi"):
        wet = np.stack([reverb(out[:, c], 0.8, 0.35, tail=0) for c in range(2)], 1)
        out = wet[:n]
    if song["style"] in ("chiptune", "battle"):
        out = filt(out, "lowpass", 9000)  # take the edge off pulse-wave fizz
    if song["style"] == "lofi":
        out = filt(out, "lowpass", 5500) + osc("pink", 0, n / SR, rng=r)[:, None] * 0.004
    # fold the tail (reverb and release past the last bar) back onto the start so the loop is seamless
    out = filt(out, "highpass", 25.0)  # DC blocker: pulse waves carry an offset
    L = ns(total)
    looped = out[:L].copy()
    looped[: n - L] += out[L:]
    return master(looped, -16.0)


def write_midi(song: dict[str, Any], path: Path) -> Path:
    """Standard MIDI file (type 1), one track per part; drums on channel 10."""
    tpq = 480

    def vlq(v: int) -> bytes:
        out = [v & 0x7F]
        v >>= 7
        while v:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        return bytes(reversed(out))

    tracks = []
    tempo_us = int(60_000_000 / song["tempo"])
    meta = b"\x00\xff\x51\x03" + tempo_us.to_bytes(3, "big") + b"\x00\xff\x2f\x00"
    tracks.append(meta)
    programs = {"bass": 38, "chords": 80, "melody": 81, "drums": 0}
    for ch, (part, events) in enumerate(song["events"].items()):
        chan = 9 if part == "drums" else ch
        msgs = []
        for start, ln, midi, vel in events:
            on, off = int(start * tpq), int((start + ln) * tpq)
            v = max(1, min(127, int(vel * 110)))
            msgs.append((on, 1, bytes([0x90 | chan, midi & 127, v])))
            msgs.append((off, 0, bytes([0x80 | chan, midi & 127, 0])))
        msgs.sort(key=lambda m: (m[0], m[1]))
        data = bytearray(b"\x00\xff\x03" + vlq(len(part)) + part.encode())
        if part != "drums":
            data += b"\x00" + bytes([0xC0 | chan, programs[part]])
        last = 0
        for t, _, m in msgs:
            data += vlq(t - last) + m
            last = t
        data += b"\x00\xff\x2f\x00"
        tracks.append(bytes(data))
    out = b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), tpq)
    for tr in tracks:
        out += b"MTrk" + struct.pack(">I", len(tr)) + tr
    Path(path).write_bytes(out)
    return Path(path)


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def trim_silence(x: np.ndarray, thresh_db: float = -55.0, keep_tail: float = 0.05, sr: int = SR) -> np.ndarray:
    mono = np.abs(x if x.ndim == 1 else x.mean(axis=1))
    loud = np.nonzero(mono > 10 ** (thresh_db / 20))[0]
    if not len(loud):
        return x
    return x[max(0, loud[0] - int(0.001 * sr)): min(len(x), loud[-1] + int(keep_tail * sr))]


def time_stretch(x: np.ndarray, rate: float, n_fft: int = 2048) -> np.ndarray:
    """Phase vocoder: rate > 1 is faster, pitch unchanged."""
    n_in = len(x)
    hop = n_fft // 4
    win = np.hanning(n_fft)
    x = np.pad(x, (n_fft // 2, n_fft * 2))  # room so the last samples get full frames
    frames = np.array([np.fft.rfft(win * x[i:i + n_fft]) for i in range(0, len(x) - n_fft, hop)])
    if len(frames) < 2:
        return x
    steps = np.arange(0, len(frames) - 1, rate)
    phase = np.angle(frames[0])
    expected = 2 * np.pi * hop * np.arange(n_fft // 2 + 1) / n_fft
    out = np.zeros(int(len(steps) * hop + n_fft))
    for k, s in enumerate(steps):
        i = int(s)
        frac = s - i
        mag = (1 - frac) * np.abs(frames[i]) + frac * np.abs(frames[i + 1])
        dphi = np.angle(frames[i + 1]) - np.angle(frames[i]) - expected
        dphi -= 2 * np.pi * np.round(dphi / (2 * np.pi))
        phase += expected + dphi
        seg = np.fft.irfft(mag * np.exp(1j * phase)) * win
        out[k * hop:k * hop + n_fft] += seg
    out = out / 1.5  # Hann analysis+synthesis windows at 75% overlap sum to 1.5
    start = int(n_fft // 2 / rate)
    want = max(1, int(n_in / rate))
    out = out[start:start + want]
    return np.pad(out, (0, want - len(out))) if len(out) < want else out


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return x
    g = math.gcd(int(sr_in), int(sr_out))
    return signal.resample_poly(x, sr_out // g, sr_in // g, axis=0)


def denoise(x: np.ndarray, sr: int = SR, strength: float = 1.5, profile: tuple[float, float] | None = None) -> np.ndarray:
    """Spectral gate: learn the noise floor from the quietest 0.3 s (or a given range) and subtract it."""
    mono_in = x if x.ndim == 1 else x.mean(axis=1)
    n_fft, hop = 1024, 256
    f, t, Z = signal.stft(mono_in, sr, nperseg=n_fft, noverlap=n_fft - hop)
    mag = np.abs(Z)
    if profile:
        a, b = int(profile[0] * sr / hop), int(profile[1] * sr / hop)
        noise = mag[:, a:b].mean(axis=1)
    else:
        energy = mag.sum(axis=0)
        k = max(1, int(0.3 * sr / hop))
        idx = np.argsort(energy)[:k]
        noise = mag[:, idx].mean(axis=1)
    gain = np.clip(1 - strength * noise[:, None] / np.maximum(mag, 1e-9), 0, 1)
    gain = signal.convolve2d(gain, np.ones((3, 5)) / 15, mode="same", boundary="symm")
    channels = [x] if x.ndim == 1 else [x[:, c] for c in range(x.shape[1])]
    out = []
    for c in channels:
        _, _, Zc = signal.stft(c, sr, nperseg=n_fft, noverlap=n_fft - hop)
        _, y = signal.istft(Zc * gain[:, : Zc.shape[1]], sr, nperseg=n_fft, noverlap=n_fft - hop)
        out.append(y[: len(c)] if len(y) >= len(c) else np.pad(y, (0, len(c) - len(y))))
    return out[0] if x.ndim == 1 else np.stack(out, 1)


def _per_channel(x: np.ndarray, fn: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    if x.ndim == 1:
        return fn(x)
    cols = [fn(x[:, c]) for c in range(x.shape[1])]
    n = min(len(c) for c in cols)
    return np.stack([c[:n] for c in cols], 1)


def edit(x: np.ndarray, sr: int, ops: list[dict[str, Any]]) -> tuple[np.ndarray, int, list[np.ndarray], bool]:
    """Apply editing ops. Returns (audio, sr, extra_segments_from_split, is_loop)."""
    from .studio import resolve_input
    extra: list[np.ndarray] = []
    is_loop = False
    for op in ops or []:
        k = op.get("op")
        if k == "trim":
            s = int(float(op.get("start", 0)) * sr)
            e = int(float(op["end"]) * sr) if op.get("end") is not None else len(x)
            x = x[s:e]
        elif k == "fade_in":
            x = fade(x, float(op.get("seconds", 0.05)), 0, sr, op.get("curve", "equal_power"))
        elif k == "fade_out":
            x = fade(x, 0, float(op.get("seconds", 0.3)), sr, op.get("curve", "equal_power"))
        elif k == "gain":
            x = x * 10 ** (float(op.get("db", 0)) / 20)
        elif k == "automation":
            pts = sorted(op["points"], key=lambda p: p[0])  # [[seconds, dB], ...]
            tt = np.arange(len(x)) / sr
            g = 10 ** (np.interp(tt, [p[0] for p in pts], [p[1] for p in pts]) / 20)
            x = (x.T * g).T
        elif k == "normalize":
            x = master(x, float(op["lufs"]), float(op.get("ceiling_db", -1)), sr) if "lufs" in op else normalize_peak(x, float(op.get("peak_db", -1)))
        elif k == "eq":
            for band, kind_, f0 in (("low", "lowshelf", 200), ("mid", "peak", 1500), ("high", "highshelf", 6000)):
                if op.get(band):
                    x = filt(x, kind_, float(op.get(f"{band}_freq", f0)), 0.8, float(op[band]), sr)
        elif k in ("lowpass", "highpass", "bandpass"):
            x = filt(x, k, float(op.get("freq", 1000)), float(op.get("q", 0.707)), 0, sr)
        elif k == "pitch":
            semis = float(op.get("semitones", 0))
            factor = 2 ** (semis / 12)
            if op.get("keep_speed", True):
                x = _per_channel(x, lambda c: time_stretch(c, 1 / factor))
                x = _per_channel(x, lambda c: signal.resample(c, max(1, int(len(c) / factor))))
            else:
                x = _per_channel(x, lambda c: signal.resample(c, max(1, int(len(c) / factor))))
        elif k == "speed":
            rate = float(op.get("rate", 1.0))
            x = _per_channel(x, lambda c: time_stretch(c, rate)) if op.get("keep_pitch", True) else _per_channel(x, lambda c: signal.resample(c, max(1, int(len(c) / rate))))
        elif k == "reverse":
            x = x[::-1].copy()
        elif k == "echo":
            x = _per_channel(x, lambda c: delay(c, float(op.get("time", 0.25)), float(op.get("feedback", 0.35)), float(op.get("mix", 0.3)), sr))
            x = trim_silence(x, -70, 0.05, sr)
        elif k == "reverb":
            x = _per_channel(x, lambda c: reverb(c, float(op.get("room", 0.6)), float(op.get("mix", 0.25)), sr, float(op.get("tail", 1.5))))
            x = trim_silence(x, -70, 0.05, sr)
        elif k == "denoise":
            x = denoise(x, sr, float(op.get("strength", 1.5)), tuple(op["profile"]) if op.get("profile") else None)
        elif k == "remove_dc":
            x = _per_channel(x, lambda c: signal.lfilter([1, -1], [1, -0.9995], c))
        elif k == "trim_silence":
            x = trim_silence(x, float(op.get("threshold_db", -55)), float(op.get("keep_tail", 0.05)), sr)
        elif k == "make_loop":
            x = make_loop(x, float(op.get("crossfade", 0.25)), sr)
            is_loop = True
        elif k == "split":
            mono = np.abs(x if x.ndim == 1 else x.mean(axis=1))
            win = int(0.02 * sr)
            env = np.convolve(mono, np.ones(win) / win, mode="same")
            loud = env > 10 ** (float(op.get("threshold_db", -45)) / 20)
            edges = np.flatnonzero(np.diff(np.r_[0, loud.astype(int), 0]))
            segs = [x[max(0, a - int(0.005 * sr)): min(len(x), b + int(0.05 * sr))] for a, b in zip(edges[::2], edges[1::2])
                    if (b - a) / sr >= float(op.get("min_length", 0.05))]
            segs = [fade(sg, 0.003, 0.01, sr) for sg in segs]
            if segs:
                x, extra = segs[0], segs[1:]
        elif k in ("concat", "mix"):
            y, sr2 = read_wav(resolve_input(op["source"]))
            y = resample(y, sr2, sr)
            if x.ndim == 1:
                x = x[:, None]
            if y.shape[1] != x.shape[1]:
                y = np.repeat(y.mean(axis=1, keepdims=True), x.shape[1], axis=1)
            y = y * 10 ** (float(op.get("gain_db", 0)) / 20)
            if k == "concat":
                xf = int(float(op.get("crossfade", 0.02)) * sr)
                xf = min(xf, len(x), len(y))
                if xf:
                    u = np.linspace(0, 1, xf)[:, None]
                    mid = x[-xf:] * np.cos(u * np.pi / 2) + y[:xf] * np.sin(u * np.pi / 2)
                    x = np.concatenate([x[:-xf], mid, y[xf:]])
                else:
                    x = np.concatenate([x, y])
            else:
                at = int(float(op.get("at", 0)) * sr)
                need = at + len(y)
                if need > len(x):
                    x = np.pad(x, ((0, need - len(x)), (0, 0)))
                x[at:at + len(y)] += y
        elif k == "resample":
            new = int(op["rate"])
            x = resample(x, sr, new)
            sr = new
        elif k == "mono":
            x = x.mean(axis=1) if x.ndim == 2 else x
        elif k == "stereo":
            x = np.stack([x, x], 1) if x.ndim == 1 else x
        elif k == "distort":
            x = distort(x, float(op.get("drive", 2.0)))
        elif k == "bitcrush":
            x = _per_channel(x, lambda c: bitcrush(c, int(op.get("bits", 6)), int(op.get("downsample", 2))))
        else:
            raise ValueError(f"Unknown audio op '{k}'. Ops: trim, fade_in, fade_out, gain, automation, normalize, eq, lowpass, highpass, "
                             "bandpass, pitch, speed, reverse, echo, reverb, denoise, remove_dc, trim_silence, make_loop, split, concat, mix, "
                             "resample, mono, stereo, distort, bitcrush.")
    return x, sr, extra, is_loop


def review_file(path: str) -> dict[str, Any]:
    from .studio import make_sheet, save_sheet
    p = Path(path)
    x, sr = read_wav(p)
    loop = "loop" in p.stem.lower() or "amb" in p.stem.lower() or "music" in p.stem.lower()
    kind = "music" if "music" in p.stem.lower() else "ambience" if "amb" in p.stem.lower() else "sfx"
    an = analyse(x, sr, kind, loop)
    mt = an["metrics"]
    panels = [("waveform", waveform_image(x, sr=sr)), ("spectrogram", spectrogram_image(x, sr))]
    lines = [f"{mt['duration_s']} s @ {sr} Hz, {mt['channels']} ch, peak {mt['peak_dbfs']} dBFS, true peak {mt['true_peak_dbtp']}, {mt['lufs']} LUFS",
             f"judged as {kind}{' loop' if loop else ''}, verdict {an['verdict']}"]
    sheet = save_sheet(make_sheet(f"Review: {p.name}", panels, lines, an["issues"], panel_size=512, cols=2), "audio", "review_" + p.stem)
    return {"path": str(p), "preview": str(sheet), **an}


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def _sfx_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import safe_name
    kind = a["kind"]
    seed = int(a.get("seed", 0))
    variations = max(1, min(int(a.get("variations", 1)), 12))
    params = a.get("params") or {}
    base = safe_name(a.get("name") or kind)
    results = []
    for v in range(variations):
        x = make_sfx(kind, seed + v, params)
        nm = base if variations == 1 else f"{base}_{v + 1}"
        results.append(finish_audio(x, nm, f"SFX: {nm}", "ui" if kind.startswith("ui") else "sfx", formats=a.get("formats")))
    if variations == 1:
        return results[0]
    from .studio import verdict
    issues = [dict(i, message=f"{Path(r['files'][0]).stem}: {i['message']}") for r in results for i in r["issues"]]
    return {"files": [f for r in results for f in r["files"]], "previews": [r["preview"] for r in results], "preview": results[0]["preview"],
            "variations": [{"file": r["files"][0], "lufs": r["metrics"]["lufs"], "duration_s": r["metrics"]["duration_s"]} for r in results],
            "issues": issues, "verdict": verdict(issues)}


def _ambience_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import safe_name
    kind = a["kind"]
    x = ambience(kind, float(a.get("length", 20)), int(a.get("seed", 0)), float(a.get("intensity", 1.0)))
    name = safe_name(a.get("name") or f"{kind}_loop")
    return finish_audio(x, name, f"Ambience loop: {kind}", "ambience", loop=True, formats=a.get("formats"))


def _music_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import out_path, safe_name
    song = compose(a.get("key", "C"), a.get("mood", "happy"), a.get("style", "chiptune"), int(a.get("tempo", 0)), int(a.get("bars", 8)), int(a.get("seed", 0)))
    x = render_music(song, int(a.get("seed", 0)))
    name = safe_name(a.get("name") or f"music_{song['mood']}_{song['style']}")
    mid = write_midi(song, out_path("audio", name, ".mid"))
    info = {"tempo": song["tempo"], "key": f"{song['key']} {song['scale']}", "bars": song["bars"],
            "loop_length_s": round(song["bars"] * 4 * 60 / song["tempo"], 3), "midi": str(mid),
            "notes": {k: len(v) for k, v in song["events"].items()}}
    return finish_audio(x, name, f"Music: {song['mood']} {song['style']} in {song['key']} {song['scale']}, {song['tempo']} BPM",
                        "music", loop=True, extra_files=[str(mid)], info=info, formats=a.get("formats"))


def _edit_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(a["source"])
    x, sr = read_wav(src)
    x = x[:, 0] if x.shape[1] == 1 else x
    y, sr, extra, is_loop = edit(x, sr, a.get("ops", []))
    name = safe_name(a.get("name") or src.stem + "_edit")
    kind = a.get("kind", "sfx")
    loop = is_loop or bool(a.get("loop", False))
    out = finish_audio(y, name, f"Edited: {src.name}", kind, loop, sr, formats=a.get("formats"))
    for i, seg in enumerate(extra):
        r = finish_audio(seg, f"{name}_{i + 2}", f"Split part {i + 2}", kind, False, sr)
        out["files"] += r["files"]
        out.setdefault("parts", []).append({"file": r["files"][0], "verdict": r["verdict"]})
    return out


def _analyse_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input
    p = resolve_input(a["path"])
    x, sr = read_wav(p)
    an = analyse(x, sr, a.get("kind", "sfx"), bool(a.get("loop", False)))
    return {"path": str(p), **an}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                return {"status": "ok", "summary": f"{summary} Verdict: {data.get('verdict')}.", "data": data}
            except (ValueError, KeyError, FileNotFoundError, PermissionError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    caps = [
        ("sound.sfx", f"Synthesise game SFX ({', '.join(SFX)}), loudness-matched and click-free, with seeded variations.",
         {"kind": "str", "seed": "optional int", "variations": "optional int", "params": "optional dict (pitch semitones, reverb, lufs)",
          "name": "optional str", "formats": "optional list (ogg/mp3 need FFmpeg)"}, _sfx_cap, "SFX made.", ["audio", "sfx", "synth", "procedural"]),
        ("sound.ambience", "Make a seamless stereo ambience loop: wind, rain, cave, forest, ocean, fire, hum.",
         {"kind": "str", "length": "optional float (seconds)", "seed": "optional int", "intensity": "optional float", "name": "optional str",
          "formats": "optional list"},
         _ambience_cap, "Ambience made.", ["audio", "ambience", "loop"]),
        ("sound.music", "Compose and render a loopable music track (moods: happy, heroic, sad, epic, mysterious, calm, tense; styles: chiptune, "
         "ambient, lofi, battle) in any key, plus an editable MIDI file.",
         {"key": "optional str", "mood": "optional str", "style": "optional str", "tempo": "optional int", "bars": "optional int",
          "seed": "optional int", "name": "optional str", "formats": "optional list"}, _music_cap, "Music made.", ["audio", "music", "midi", "loop"]),
        ("sound.edit", "Edit a WAV into a new file: trim, fades, gain, automation, normalize (peak/LUFS), eq, filters, pitch, speed, reverse, "
         "echo, reverb, denoise, remove_dc, trim_silence, make_loop, split, concat, mix, resample, mono/stereo, distort, bitcrush.",
         {"source": "str", "ops": "list[dict]", "name": "optional str", "kind": "optional str (sfx|ui|ambience|music|voice)", "loop": "optional bool",
          "formats": "optional list"},
         _edit_cap, "Audio edited.", ["audio", "edit", "mix", "convert"]),
        ("sound.analyze", "Measure a WAV: peak, true peak, LUFS, crest, DC, clipping, clicks, silence, loop seam, with game loudness targets.",
         {"path": "str", "kind": "optional str", "loop": "optional bool"}, _analyse_cap, "Audio analysed.", ["audio", "qa", "loudness"]),
    ]
    for cid, summary, schema, fn, done, tags in caps:
        reg.register(CapabilitySpec(id=cid, area="audio", summary=summary, input_schema=schema, permission="read",
                                    handler=wrap(fn, done), tags=tags))
