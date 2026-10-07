"""Spectrogram images (PNG) of a song, the captures and their differences.

All images of one test share the source song's timeline: a capture is shifted
by its alignment offset before analysis, so the source, both players and the
differences line up column for column. Levels are in dB relative to full scale
(a full-scale sine peaks at 0 dB) on one shared colour scale, so a difference
image that is black means the difference is below the bottom of the scale.

Each pixel column covers its whole stretch of time: when a column spans more
than one analysis window, several windows are analysed and the loudest value
of each frequency is kept. A click or a single wrong sample therefore always
shows up, however far the view is zoomed out.
"""

from __future__ import annotations

import bisect
import struct
import zlib
from typing import Callable, Optional, Sequence, Tuple, Union

import numpy as np

from .analysis import to_float
from .wavio import Audio

Fetch = Callable[[int, int], np.ndarray]  # frames [start, end) of the source timeline -> (n, channels)

# An "inferno"-style perceptual colour map: black, purple, red, orange, yellow, pale yellow. The
# name goes into cached pictures' keys (and the page's requests): a new map must not be mixed with
# pictures made with an old one.
COLOUR_MAP = "inferno"
ANCHORS = [(0.0, (0, 0, 4)), (0.1, (22, 11, 57)), (0.2, (66, 10, 104)), (0.3, (106, 23, 110)),
           (0.4, (147, 38, 103)), (0.5, (188, 55, 84)), (0.6, (221, 81, 58)), (0.7, (243, 120, 25)),
           (0.8, (252, 165, 10)), (0.9, (246, 215, 70)), (1.0, (252, 255, 164))]


def colour_table() -> np.ndarray:
    pos = np.array([a[0] for a in ANCHORS])
    rgb = np.array([a[1] for a in ANCHORS], dtype=np.float64)
    x = np.linspace(0, 1, 256)
    return np.stack([np.interp(x, pos, rgb[:, k]) for k in range(3)], axis=1).round().astype(np.uint8)


LUT = colour_table()


def png_bytes(rgb: np.ndarray) -> bytes:
    """Encode an (height, width, 3) uint8 array as PNG."""
    h, w, _ = rgb.shape
    rows = np.concatenate([np.zeros((h, 1), np.uint8), rgb.reshape(h, w * 3)], axis=1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)) + chunk(b"IEND", b""))


Lag = Union[int, Sequence[Tuple[int, int]]]


def fetch_from(audio: Audio, lag: Lag = 0) -> Fetch:
    """Frames of `audio` on the source timeline (source frame i is audio frame i + lag), zero-padded.

    `lag` is a number, or a list of (source frame, lag) steps for a recording whose
    offset changes (after a dropout, say): each lag applies from its frame on, the
    first one also before it.
    """
    if isinstance(lag, (int, np.integer)):
        steps = [(0, int(lag))]
    else:
        steps = sorted((int(s), int(v)) for s, v in lag) or [(0, 0)]
    starts = [s for s, _ in steps]

    def fetch(s: int, e: int) -> np.ndarray:
        out = np.zeros((e - s, audio.channels))
        first = max(0, bisect.bisect_right(starts, s) - 1)
        for k in range(first, len(steps)):
            ps = s if k == first else steps[k][0]
            pe = e if k + 1 == len(steps) else min(e, steps[k + 1][0])
            if ps >= e:
                break
            if pe <= ps:
                continue
            shift = steps[k][1]
            a, b = max(ps + shift, 0), min(pe + shift, audio.frames)
            if b > a:
                out[a - shift - s:b - shift - s] = to_float(audio, a, b)
        return out
    return fetch


def difference(a: Fetch, b: Fetch, model: Optional[np.ndarray] = None) -> Fetch:
    """a minus b (b optionally passed through a channel/gain matrix first)."""
    def fetch(s: int, e: int) -> np.ndarray:
        rb = b(s, e)
        return a(s, e) - (rb @ model if model is not None else rb)
    return fetch


def max_nfft(rate: int) -> int:
    return 8192 if rate <= 48000 else 16384 if rate <= 96000 else 32768


def choose_nfft(rate: int, hop: float) -> int:
    """Long windows for fine frequency detail over a whole song; shorter ones when zoomed in."""
    n = 1 << max(11, int(np.ceil(np.log2(max(8 * hop, 1)))))
    return int(min(n, max_nfft(rate)))


def compute(fetch: Fetch, rate: int, start: int, end: int, width: int, height: int,
            scale: str = "log", fmin: float = 20.0, fmax: Optional[float] = None):
    """dB levels as a (height, width) array, highest frequency in the top row.

    Rows above this recording's Nyquist frequency (when `fmax` is higher) are -inf.
    """
    hop = (end - start) / width
    nfft = choose_nfft(rate, hop)
    # Windows per column, spaced at most half a window apart: every sample then lies in
    # the middle half of some window, where the Hann window weighs it at least 0.5.
    sub = max(1, int(np.ceil(2 * hop / nfft)))
    win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)
    norm = (win.sum() / 2) ** 2
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    nyq = rate / 2.0
    top = float(fmax or nyq)
    edges = np.geomspace(fmin, top, height + 1) if scale == "log" else np.linspace(0, top, height + 1)
    starts = np.clip(np.searchsorted(freqs, edges[:-1]), 0, len(freqs) - 1)
    starts = np.maximum.accumulate(starts)
    out = np.empty((width, height))
    batch = max(1, (1 << 22) // (nfft * sub))
    offsets = np.arange(nfft)
    centres = (np.arange(sub) + 0.5) * hop / sub
    for c0 in range(0, width, batch):
        c1 = min(width, c0 + batch)
        cols = np.arange(c0, c1)
        firsts = (start + cols[:, None] * hop + centres[None, :]).astype(np.int64).reshape(-1) - nfft // 2
        block = fetch(int(firsts[0]), int(firsts[-1]) + nfft)
        idx = (firsts - firsts[0])[:, None] + offsets[None, :]
        power = np.zeros((len(firsts), nfft // 2 + 1))
        for ch in range(block.shape[1]):
            power += np.abs(np.fft.rfft(block[:, ch][idx] * win, axis=1)) ** 2
        power /= block.shape[1] * norm
        power = power.reshape(c1 - c0, sub, -1).max(axis=1)
        out[c0:c1] = np.maximum.reduceat(power, starts, axis=1)
    with np.errstate(divide="ignore"):
        db = 10 * np.log10(out)
    db[:, edges[:-1] >= nyq] = -np.inf
    info = {"nfft": nfft, "hz_per_bin": round(rate / nfft, 2), "windows_per_column": sub,
            "ms_per_column": round(1000 * hop / rate, 3), "window_ms": round(1000 * nfft / rate, 1),
            "fmin": fmin if scale == "log" else 0.0, "fmax": top}
    return db.T[::-1], info


def render(fetch: Fetch, rate: int, start: int, end: int, width: int = 1200, height: int = 400,
           scale: str = "log", fmin: float = 20.0, db_low: float = -150.0, db_high: float = 0.0,
           fmax: Optional[float] = None):
    """(PNG bytes, info) for the frames [start, end) of the source timeline.

    Zoomed in to fewer samples than pixel columns, neighbouring columns repeat the same
    analysis, so the picture still spans exactly [start, end).
    """
    width = int(min(max(width, 64), 4096))
    height = int(min(max(height, 64), 2048))
    end = max(end, start + 1)
    db, info = compute(fetch, rate, start, end, width, height, scale, fmin, fmax)
    level = np.nan_to_num((db - db_low) / (db_high - db_low), nan=0.0, neginf=0.0, posinf=1.0)
    rgb = LUT[np.clip((level * 255).round(), 0, 255).astype(np.uint8)]
    return png_bytes(rgb), info
