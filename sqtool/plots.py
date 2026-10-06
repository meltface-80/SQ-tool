"""Compact curves for the web interface: spectra, level over time, bit usage."""

from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from .analysis import _chunks, int_resolution, silence_bounds, to_float
from .wavio import Audio

POINTS = 160  # log-spaced frequency points per spectrum


def log_freqs(rate: int, points: int = POINTS, fmin: float = 10.0) -> np.ndarray:
    return np.geomspace(fmin, rate / 2.0, points)


def reduce_spectrum(freqs: np.ndarray, power: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Mean power around each target frequency (log bands), nearest bin where bands are narrow."""
    ratio = math.sqrt(targets[1] / targets[0]) if len(targets) > 1 else 1.0
    out = np.empty(len(targets))
    for i, f in enumerate(targets):
        sel = (freqs >= f / ratio) & (freqs < f * ratio)
        out[i] = power[sel].mean() if sel.any() else power[int(np.argmin(np.abs(freqs - f)))]
    return out


def _db(power: np.ndarray, floor: float = -200.0) -> List[Optional[float]]:
    with np.errstate(divide="ignore"):
        db = 10 * np.log10(power)
    return [None if not np.isfinite(v) else round(max(float(v), floor), 2) for v in db]


def _nfft(rate: int) -> int:
    return 8192 if rate <= 96000 else 16384


def _window(nfft: int) -> np.ndarray:
    """Periodic Hann window. At 75% overlap the squared windows add up to a constant, so
    every moment of the signal adds equally to the averaged power (no comb-shaped notches
    from a moving sweep, whatever the segments' alignment)."""
    return 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)


def _segment_starts(start: int, end: int, nfft: int, max_segments: int = 3000) -> List[int]:
    if end - start < nfft:
        return []
    hop = nfft // 4
    count = (end - start - nfft) // hop + 1
    if count > max_segments:  # very long files: spread a fixed number of segments evenly
        hop = (end - start - nfft) // (max_segments - 1)
        count = max_segments
    return [start + i * hop for i in range(count)]


def spectrum(audio: Audio, start: Optional[int] = None, end: Optional[int] = None) -> dict:
    """Average spectrum of the music (all channels) in dB; a full-scale sine peaks near 0 dB.

    Stretches quieter than -100 dBFS (silence, dither alone) are left out, so captures
    with and without dithered silence around the music compare like for like."""
    if start is None or end is None:
        start, end = silence_bounds(audio.data)
    nfft = _nfft(audio.rate)
    # Half a window of context on each side, so the music's first and last moments get full weight.
    start, end = max(0, start - nfft // 2), min(audio.frames, end + nfft // 2)
    win = _window(nfft)
    norm = (win.sum() / 2) ** 2
    acc = np.zeros(nfft // 2 + 1)
    used = 0
    for s in _segment_starts(start, end, nfft):
        x = to_float(audio, s, s + nfft)
        if float((x * x).mean()) < 1e-10:
            continue
        acc += (np.abs(np.fft.rfft(x * win[:, None], axis=0)) ** 2).mean(axis=1)
        used += 1
    if not used:
        return {}
    power = acc / used / norm
    freqs = np.fft.rfftfreq(nfft, 1.0 / audio.rate)
    targets = log_freqs(audio.rate)
    return {"freqs": [round(float(f), 2) for f in targets],
            "db": _db(reduce_spectrum(freqs, power, targets))}


def envelope(audio: Audio, max_points: int = 600) -> dict:
    """Level over time (RMS of all channels per block) in dBFS; None marks digital silence."""
    n = audio.frames
    block = max(int(0.05 * audio.rate), -(-n // max_points))
    nb = n // block
    if nb == 0:
        return {}
    power = np.empty(nb)
    step = max(1, (1 << 20) // block) * block
    for a, b in _chunks(0, nb * block, step):
        x = to_float(audio, a, b)
        power[a // block:b // block] = (x * x).mean(axis=1).reshape(-1, block).mean(axis=1)
    return {"t": [round((i + 0.5) * block / audio.rate, 3) for i in range(nb)], "db": _db(power)}


def bit_usage(audio: Audio, max_frames: int = 1 << 21) -> Optional[List[float]]:
    """For each bit of the 32-bit sample word (MSB first), the share of samples that have it set."""
    if audio.is_float:
        return None
    start, end = silence_bounds(audio.data)
    if end <= start:
        return None
    step = max(1, (end - start) // max_frames)
    words = np.ascontiguousarray(audio.data[start:end:step]).reshape(-1).view(np.uint32)
    return [round(float(((words >> np.uint32(31 - b)) & np.uint32(1)).mean()), 4) for b in range(32)]


def item_plots(audio: Audio) -> dict:
    return {"spectrum": spectrum(audio), "envelope": envelope(audio), "bits": bit_usage(audio),
            "resolution": None if audio.is_float else int_resolution(audio.data)}


def null_plots(ref: Audio, cap: Audio, lag: int, model: np.ndarray, max_points: int = 600) -> dict:
    """Residual of a null test (capture minus the fitted reference) over time and frequency."""
    a, b = max(0, -lag), min(ref.frames, cap.frames - lag)
    if b - a < 1:
        return {}
    block = max(int(0.05 * ref.rate), -(-(b - a) // max_points))
    nb = (b - a) // block
    res_power, sig_power = np.zeros(nb), np.zeros(nb)
    end = a + nb * block
    step = max(1, (1 << 20) // block) * block
    for s, e in _chunks(a, end, step):
        r = to_float(ref, s, e)
        c = to_float(cap, s + lag, e + lag)
        err = c - r @ model
        i0 = (s - a) // block
        res_power[i0:i0 + (e - s) // block] = (err * err).mean(axis=1).reshape(-1, block).mean(axis=1)
        sig_power[i0:i0 + (e - s) // block] = (c * c).mean(axis=1).reshape(-1, block).mean(axis=1)
    out = {"envelope": {"t": [round((a + (i + 0.5) * block) / ref.rate, 3) for i in range(nb)],
                        "residual_db": _db(res_power), "signal_db": _db(sig_power)}}
    nfft = _nfft(ref.rate)
    starts = _segment_starts(a, b, nfft)
    if starts:
        win = _window(nfft)
        norm = (win.sum() / 2) ** 2
        acc_r, acc_c = np.zeros(nfft // 2 + 1), np.zeros(nfft // 2 + 1)
        for s in starts:
            r = to_float(ref, s, s + nfft)
            c = to_float(cap, s + lag, s + lag + nfft)
            err = c - r @ model
            acc_r += (np.abs(np.fft.rfft(err * win[:, None], axis=0)) ** 2).mean(axis=1)
            acc_c += (np.abs(np.fft.rfft(c * win[:, None], axis=0)) ** 2).mean(axis=1)
        freqs = np.fft.rfftfreq(nfft, 1.0 / ref.rate)
        targets = log_freqs(ref.rate)
        out["spectrum"] = {"freqs": [round(float(f), 2) for f in targets],
                           "residual_db": _db(reduce_spectrum(freqs, acc_r / len(starts) / norm, targets)),
                           "signal_db": _db(reduce_spectrum(freqs, acc_c / len(starts) / norm, targets))}
    return out


def exact_timeline(exact: dict, rate: int) -> List[dict]:
    """Bands for a timeline bar: identical stretches and the places where the data differs."""
    bands = [{"kind": "identical", "start": round(s["ref_start"] / rate, 3),
              "end": round(s["ref_end"] / rate, 3)} for s in exact["segments"]]
    for ev in exact["events"]:
        length = max(ev["ref_frames"], ev["cap_frames"], 1)
        bands.append({"kind": ev["kind"], "start": round(ev["ref"] / rate, 3),
                      "end": round((ev["ref"] + length) / rate, 3)})
    if exact.get("differs_from") is not None:
        bands.append({"kind": "differs", "start": round(exact["differs_from"] / rate, 3),
                      "end": round((exact["differs_from"] + exact.get("differing_frames", 0)) / rate, 3)})
    return sorted(bands, key=lambda x: x["start"])
