"""Deterministic test tracks to play through a player and capture.

Each track is stereo and made of sections that make differences easy to see:

  2.0 s  digital silence      (any non-zero output here means noise or dither was added)
  1.0 s  white noise -20 dBFS (unique content for alignment; independent left/right)
  0.5 s  digital silence
  3.0 s  tones -6 dBFS        (997 Hz left, 1499 Hz right: a swap or crossfeed is obvious)
  3.0 s  tones -70 dBFS       (low-level detail that truncation or requantisation disturbs)
  4.0 s  log sweeps -12 dBFS  (20 Hz-20 kHz, rising on the left, falling on the right)
  2.0 s  digital silence

Non-silent sections are TPDF-dithered to the file's bit depth and faded in and
out over 5 ms. The files are for capturing, not listening: keep the volume low
if you ever play them through speakers.
"""

from __future__ import annotations

import math
import os
from typing import List, Tuple

import numpy as np

from .wavio import Audio, info_chunk, write_wav

DEFAULT_SET = [(16, 44100), (24, 44100), (24, 96000), (24, 192000)]
SEED = 20261006


def _fade(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 0 or len(x) < 2 * n:
        return x
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, math.pi, n))
    x[:n] *= ramp[:, None]
    x[-n:] *= ramp[::-1, None]
    return x


def _sweep(rate: int, seconds: float, f0: float, f1: float) -> np.ndarray:
    t = np.arange(int(round(seconds * rate))) / rate
    k = math.log(f1 / f0)
    return np.sin(2 * math.pi * f0 * seconds / k * (np.exp(t / seconds * k) - 1))


def sections(rate: int, rng: np.random.Generator) -> List[Tuple[str, np.ndarray]]:
    """(name, float signal in [-1, 1)) for each section, stereo."""
    def frames(sec):
        return int(round(sec * rate))

    def tones(sec, level_db):
        t = np.arange(frames(sec)) / rate
        a = 10 ** (level_db / 20)
        return np.stack([a * np.sin(2 * math.pi * 997 * t), a * np.sin(2 * math.pi * 1499 * t)], axis=1)

    noise = np.clip(rng.standard_normal((frames(1.0), 2)) * 10 ** (-20 / 20), -0.9, 0.9)
    up = _sweep(rate, 4.0, 20.0, min(20000.0, 0.45 * rate)) * 10 ** (-12 / 20)
    return [
        ("silence", np.zeros((frames(2.0), 2))),
        ("noise", noise),
        ("silence", np.zeros((frames(0.5), 2))),
        ("tones -6 dBFS", tones(3.0, -6)),
        ("tones -70 dBFS", tones(3.0, -70)),
        ("sweeps", np.stack([up, up[::-1]], axis=1)),
        ("silence", np.zeros((frames(2.0), 2))),
    ]


def make_test_signal(rate: int, bits: int, seed: int = SEED) -> Audio:
    """The test track as left-justified int32 samples at the given bit depth."""
    if bits not in (16, 24, 32):
        raise ValueError("bits must be 16, 24 or 32")
    rng = np.random.default_rng(seed)
    scale = float(2 ** (bits - 1))
    fade = int(0.005 * rate)
    parts = []
    for name, x in sections(rate, rng):
        if name != "silence":
            x = _fade(x, fade) * scale
            x = x + rng.random(x.shape) - rng.random(x.shape)  # TPDF dither, +-1 LSB
            x = np.clip(np.round(x), -scale, scale - 1)
        parts.append(x.astype(np.int64))
    data = np.concatenate(parts) << (32 - bits)
    return Audio(data=data.astype(np.int32), rate=rate, bits=bits, label="WAV PCM %d-bit" % bits)


def test_track_name(bits: int, rate: int) -> str:
    return "sq-test_%dbit_%dHz.wav" % (bits, rate)


def write_test_track(directory: str, bits: int, rate: int) -> str:
    audio = make_test_signal(rate, bits)
    path = os.path.join(directory, test_track_name(bits, rate))
    title = "SQ-tool test %d-bit %g kHz" % (bits, rate / 1000)
    tags = info_chunk({"INAM": title, "IART": "SQ-tool", "IPRD": "SQ-tool test signals",
                       "ICMT": "Deterministic test signal for bit-perfect capture tests."})
    write_wav(path, audio, extra_chunks=[(b"LIST", tags)])
    return path
