"""Analysis of captured PCM streams and sample-exact comparison with a reference.

The comparison first looks for an exact (bit-identical) alignment between the
reference and the capture and walks along it, recording every place where the
two stop matching: frames that were dropped, inserted (for example silence
from a player underrun) or altered. When no exact alignment exists at all, it
falls back to a correlation-based alignment and characterises the difference:
gain, channel mixing, frequency response, and the residual noise (rounding,
dither, noise shaping or heavier processing).
"""

from __future__ import annotations

import hashlib
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from .wavio import Audio

CHUNK = 1 << 20  # frames per processing block; keeps temporary arrays small
FS_INT = float(2 ** 31)
WINDOW = 32  # frames per exact-match window
MAX_CANDIDATES = 1 << 18


def db_amp(ratio: float) -> float:
    return 20.0 * math.log10(ratio) if ratio > 0 else float("-inf")


def db_pow(ratio: float) -> float:
    return 10.0 * math.log10(ratio) if ratio > 0 else float("-inf")


def _chunks(start: int, end: int, step: int = CHUNK):
    a = start
    while a < end:
        b = min(end, a + step)
        yield a, b
        a = b


def _next_pow2(n: int) -> int:
    return 1 << max(0, int(n - 1).bit_length())


def to_float(audio: Audio, start: int = 0, end: Optional[int] = None) -> np.ndarray:
    """A slice of the audio as float64 with full scale at 1.0 (exact for <= 32-bit ints)."""
    d = audio.data[start:end]
    if audio.is_float:
        return d.astype(np.float64)
    return d.astype(np.float64) * (1.0 / FS_INT)


def _rowsum(x: np.ndarray) -> np.ndarray:
    """Sum across channels; much faster than x.sum(axis=1) for a handful of channels."""
    out = x[:, 0].copy()
    for j in range(1, x.shape[1]):
        out += x[:, j]
    return out


def _frame_keys(x: np.ndarray) -> Optional[np.ndarray]:
    """One integer per frame for mono/stereo int32 audio (much faster to compare), else None."""
    if x.dtype == np.int32 and x.ndim == 2 and x.flags.c_contiguous:
        if x.shape[1] == 1:
            return x[:, 0]
        if x.shape[1] == 2:
            return x.view(np.int64)[:, 0]
    return None


def nonzero_frames(data: np.ndarray, start: int = 0, end: Optional[int] = None) -> np.ndarray:
    block = data[start:end]
    keys = _frame_keys(block)
    if keys is not None:
        return keys != 0
    return np.any(block != 0, axis=1)


def silence_bounds(data: np.ndarray) -> Tuple[int, int]:
    """(first, end): span from the first to just past the last non-zero frame. (0, 0) if silent."""
    n = len(data)
    first = None
    for a, b in _chunks(0, n):
        nz = nonzero_frames(data, a, b)
        if nz.any():
            first = a + int(np.argmax(nz))
            break
    if first is None:
        return 0, 0
    for a, b in reversed(list(_chunks(first, n))):
        nz = nonzero_frames(data, a, b)
        if nz.any():
            return first, a + len(nz) - int(np.argmax(nz[::-1]))
    return first, first + 1  # not reached


def _is_silent(data: np.ndarray, start: int, end: int) -> bool:
    for a, b in _chunks(start, end):
        if np.any(data[a:b] != 0):
            return False
    return True


def int_resolution(data: np.ndarray) -> int:
    """Significant bits in use: 32 minus the trailing zero bits common to every sample."""
    acc = 0
    for a, b in _chunks(0, len(data)):
        block = np.ascontiguousarray(data[a:b], dtype=np.int32).reshape(-1).view(np.uint32)
        if block.size:
            acc |= int(np.bitwise_or.reduce(block))
    if acc == 0:
        return 0
    return 32 - ((acc & -acc).bit_length() - 1)


def float_resolution(data: np.ndarray) -> Optional[int]:
    """Smallest N (8/16/24/32) such that every float sample is an exact N-bit integer value."""
    if len(data) == 0:
        return None
    for nbits in (8, 16, 24, 32):
        scale = float(2 ** (nbits - 1))
        ok = True
        for a, b in _chunks(0, len(data)):
            x = data[a:b].astype(np.float64) * scale
            if not (np.all(x == np.round(x)) and x.min() >= -scale and x.max() <= scale - 1):
                ok = False
                break
        if ok:
            return nbits
    return None


def resolution(audio: Audio) -> Optional[int]:
    return float_resolution(audio.data) if audio.is_float else int_resolution(audio.data)


def detect_dop(audio: Audio) -> Optional[str]:
    """Recognise DSD-over-PCM: 0x05/0xFA markers alternating in the top byte of each frame."""
    if audio.is_float or audio.frames < 64:
        return None
    n = min(audio.frames, 1 << 16)
    start = (audio.frames - n) // 2
    markers = (audio.data[start:start + n] >> 24) & 0xFF
    m0 = markers[:, 0]
    if not np.all(markers == m0[:, None]):
        return None
    if not np.all((m0 == 0x05) | (m0 == 0xFA)) or not np.all(m0[1:] != m0[:-1]):
        return None
    dsd_rate = audio.rate * 16
    base = 44100 if audio.rate % 44100 == 0 else 48000
    return "DoP (DSD over PCM): DSD%d, %.4f MHz" % (dsd_rate // base, dsd_rate / 1e6)


def block_power(audio: Audio, block: int) -> np.ndarray:
    """Mean square (all channels) per block of `block` frames."""
    nb = audio.frames // block
    out = np.empty(nb)
    step = max(1, CHUNK // block) * block
    for a, b in _chunks(0, nb * block, step):
        x = to_float(audio, a, b)
        out[a // block:b // block] = (_rowsum(x * x) / x.shape[1]).reshape(-1, block).mean(axis=1)
    return out


def fingerprint(audio: Audio, start: int, end: int) -> str:
    """SHA-256 of the sample values (as left-justified 32-bit ints) in [start, end).

    Identical values give identical fingerprints regardless of the container:
    a 16-bit file and a capture carrying the same samples in S32_LE match.
    """
    h = hashlib.sha256()
    as_int = not audio.is_float or float_resolution(audio.data[start:end]) is not None
    if not as_int:
        h.update(b"float64:")
    for a, b in _chunks(start, end):
        d = audio.data[a:b]
        if audio.is_float:
            d = d.astype(np.float64)
            d = np.round(d * FS_INT).astype("<i4") if as_int else d.astype("<f8")
        h.update(np.ascontiguousarray(d, dtype=d.dtype.newbyteorder("<")).tobytes())
    return h.hexdigest()


# --------------------------------------------------------------------------
# Single-file analysis


def _channel_stats(audio: Audio, start: int, end: int, res: Optional[int]) -> List[dict]:
    ch = audio.channels
    peak = np.zeros(ch)
    sq = np.zeros(ch)
    total = np.zeros(ch)
    clipped = np.zeros(ch, dtype=np.int64)
    if audio.is_float:
        hi, lo = 1.0, -1.0
    else:
        bits = res or audio.bits
        hi = ((1 << (bits - 1)) - 1) << (32 - bits)
        lo = -(1 << 31)
    scale = 1.0 if audio.is_float else 1.0 / FS_INT
    for a, b in _chunks(start, end):
        for c in range(ch):
            raw = audio.data[a:b, c]
            x = raw.astype(np.float64) * scale
            peak[c] = max(peak[c], x.max(), -x.min())
            sq[c] += float(np.dot(x, x))
            total[c] += float(x.sum())
            clipped[c] += int(np.count_nonzero(raw >= hi) + np.count_nonzero(raw <= lo))
    n = max(1, end - start)
    return [{"peak_db": db_amp(float(peak[c])), "rms_db": db_pow(float(sq[c] / n)),
             "dc": float(total[c] / n), "clipped": int(clipped[c])} for c in range(ch)]


def analyze_file(audio: Audio) -> dict:
    n = audio.frames
    first, end = silence_bounds(audio.data)
    res = resolution(audio)
    out = {
        "path": audio.path,
        "label": audio.label,
        "rate": audio.rate,
        "channels": audio.channels,
        "frames": n,
        "duration": audio.duration,
        "bits": audio.bits,
        "is_float": audio.is_float,
        "resolution": res,
        "silent": end == 0,
        "lead_silence": first if end else n,
        "trail_silence": n - end if end else 0,
        "dop": detect_dop(audio),
        "lossy_source": audio.meta.get("lossy_source"),
    }
    out["channel_stats"] = _channel_stats(audio, first, end, res) if end else []
    blk = max(1, int(0.05 * audio.rate))
    levels = block_power(audio, blk)
    nonzero = levels[levels > 0]
    out["zero_blocks"] = int((levels == 0).sum())
    out["blocks"] = int(len(levels))
    out["quietest_block_db"] = db_pow(float(nonzero.min())) if nonzero.size else None
    out["fingerprint"] = fingerprint(audio, first, end) if end else None
    out["capture"] = audio.meta.get("capture")
    out["cpu"] = audio.meta.get("cpu")
    return out


# --------------------------------------------------------------------------
# Exact alignment


def comparable(ref: Audio, cap: Audio) -> Tuple[np.ndarray, np.ndarray]:
    """Arrays in a common domain where equality means identical sample values."""
    if not ref.is_float and not cap.is_float:
        return ref.data, cap.data
    return to_float(ref), to_float(cap)


def _magnitude(frames: np.ndarray) -> np.ndarray:
    if frames.dtype.kind == "f":
        return _rowsum(np.abs(frames))
    return _rowsum(np.abs(frames.astype(np.int64)))


def _frames_equal(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ka, kb = _frame_keys(a), _frame_keys(b)
    if ka is not None and kb is not None:
        return ka == kb
    return np.all(a == b, axis=1)


def _find_window(hay: np.ndarray, win: np.ndarray, key: int, lo: int, hi: int):
    """Start positions j in [lo, hi) where hay[j:j+len(win)] equals win exactly.

    `key` is the index (within win) of a distinctive frame used for the first
    pass. Returns None when the window occurs too often to be useful.
    """
    width = len(win)
    hi = min(hi, len(hay) - width + 1)
    if hi <= lo:
        return np.empty(0, dtype=np.int64)
    kf = win[key][None, :]
    found = []
    total = 0
    for a, b in _chunks(lo, hi):
        c = np.flatnonzero(_frames_equal(hay[a + key:b + key], kf)) + a
        for k in range(width):
            if c.size == 0:
                break
            if k != key:
                c = c[_frames_equal(hay[c + k], win[k][None, :])]
        if c.size:
            total += c.size
            if total > MAX_CANDIDATES:
                return None
            found.append(c)
    return np.concatenate(found) if found else np.empty(0, dtype=np.int64)


def _anchors(ref: np.ndarray, n_onsets: int = 8, n_peaks: int = 16) -> List[Tuple[int, int, int]]:
    """Distinctive windows of the reference as (start, end, key frame index), in order."""
    n = len(ref)
    first, end = silence_bounds(ref)
    if end - first < 1 or n < 4:
        return []
    width = min(WINDOW, n)
    out = []
    onsets: List[int] = []
    for a, b in _chunks(first, end):
        nz = nonzero_frames(ref, a, b)
        prev = bool(nonzero_frames(ref, a - 1, a)[0]) if a > 0 else False
        starts = np.flatnonzero(nz & ~np.concatenate(([prev], nz[:-1])))
        onsets.extend((starts + a).tolist())
        if len(onsets) >= n_onsets:
            break
    for i in onsets[:n_onsets]:
        s = max(0, min(i - 8, n - width))
        k = i + int(np.argmax(_magnitude(ref[i:s + width])))
        out.append((s, s + width, k))
    edges = np.linspace(first, end, n_peaks + 1).astype(np.int64)
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        k = int(a) + int(np.argmax(_magnitude(ref[a:b])))
        s = max(0, min(k - width // 2, n - width))
        out.append((s, s + width, k))
    out.sort()
    return out


def _count_mismatch(ref: np.ndarray, cap: np.ndarray, lag: int, limit: Optional[int] = None) -> int:
    start, end = max(0, -lag), min(len(ref), len(cap) - lag)
    bad = 0
    for a, b in _chunks(start, end):
        bad += int((~_frames_equal(ref[a:b], cap[a + lag:b + lag])).sum())
        if limit is not None and bad > limit:
            break
    return bad


def _initial_lag(ref: np.ndarray, cap: np.ndarray) -> Optional[int]:
    """Lag (capture index - reference index) of the first reference window found exactly once."""
    votes: Dict[int, int] = {}
    for s, e, k in _anchors(ref):
        pos = _find_window(cap, ref[s:e], k - s, 0, len(cap))
        if pos is None or pos.size == 0:
            continue
        if pos.size == 1:
            return int(pos[0]) - s
        for p in pos[:64]:
            lag = int(p) - s
            votes[lag] = votes.get(lag, 0) + 1
    if not votes:
        return None
    # Only repeating windows (e.g. a pure tone): take the best-matching candidate.
    candidates = sorted(votes, key=lambda lag: -votes[lag])[:8]
    best, best_bad = None, None
    for lag in candidates:
        bad = _count_mismatch(ref, cap, lag, best_bad)
        if best_bad is None or bad < best_bad:
            best, best_bad = lag, bad
    return best


def _first_mismatch(ref, cap, lag, start, end) -> Optional[int]:
    for a, b in _chunks(start, end):
        eq = _frames_equal(ref[a:b], cap[a + lag:b + lag])
        if not eq.all():
            return a + int(np.argmin(eq))
    return None


def _match_start(ref, cap, lag, lo, hi) -> int:
    """Smallest q in [lo, hi] such that ref[q:hi] matches cap[q+lag:hi+lag] exactly."""
    b = hi
    while b > lo:
        a = max(lo, b - CHUNK)
        eq = _frames_equal(ref[a:b], cap[a + lag:b + lag])
        if not eq.all():
            return a + len(eq) - int(np.argmin(eq[::-1]))
        b = a
    return lo


def _relocate(ref, cap, m, lag, max_shift) -> Optional[Tuple[int, int]]:
    """After a mismatch at reference frame m, find where exact matching resumes.

    Takes distinctive windows of the capture from the mismatch onwards and looks
    them up in the reference near the expected position. Returns (q, new_lag):
    reference frame q onwards matches the capture at q + new_lag.
    """
    nr, nc = len(ref), len(cap)
    c0 = m + lag
    step = 4 * WINDOW
    offsets = [t * step for t in range(32)]
    resume = _next_sound(cap, c0 + 32 * step)
    if resume is not None:  # after a long stretch of silence (an underrun, say), try where sound resumes
        offsets += [resume - c0 + t * step for t in range(8)]
    offsets += [32 * step + 2048 * t for t in range(1, 64)]  # then every 2048 frames for a while
    offsets += [step * 32 * (2 ** i) for i in range(6, 24)]  # then further and further out
    tried = set()
    for off in offsets:
        if off in tried:
            continue
        tried.add(off)
        cs = c0 + off
        if cs >= nc:
            return None
        ce = min(nc, cs + step)
        mag = _magnitude(cap[cs:ce])
        if not mag.any():
            continue
        k = cs + int(np.argmax(mag))
        ws = max(c0, k - WINDOW // 2)
        we = min(nc, ws + WINDOW)
        if we - ws < 8:
            continue
        expected = ws - lag
        lo, hi = max(0, expected - max_shift - off), min(nr, expected + max_shift + 1)
        pos = _find_window(ref, cap[ws:we], k - ws, lo, hi)
        if pos is None or pos.size == 0:
            continue
        qw = int(pos[np.argmin(np.abs(pos - expected))])
        new_lag = ws - qw
        qlo = max(m, c0 - new_lag)
        if qw < qlo:  # the capture went back and plays earlier frames again
            qlo = max(0, c0 - new_lag)
        return _match_start(ref, cap, new_lag, qlo, qw), new_lag
    return None


def _last_sound_end(data: np.ndarray, start: int) -> int:
    """Index just after the last non-silent frame at or after start (start if all silent)."""
    for a, b in reversed(list(_chunks(start, len(data)))):
        nz = nonzero_frames(data, a, b)
        if nz.any():
            return a + len(nz) - int(np.argmax(nz[::-1]))
    return start


def _next_sound(data: np.ndarray, start: int) -> Optional[int]:
    """Index of the first non-silent frame at or after start, or None."""
    for a, b in _chunks(start, len(data)):
        nz = nonzero_frames(data, a, b)
        if nz.any():
            return a + int(np.argmax(nz))
    return None


def _union_length(ranges, lo: int, hi: int) -> int:
    """Total length of the union of [a, b) ranges, clipped to [lo, hi)."""
    total, reach = 0, lo
    for a, b in sorted(ranges):
        a, b = max(a, reach), min(b, hi)
        if b > a:
            total += b - a
            reach = b
    return total


def _diff_stats(ref, cap, rs, re_, cs, ce, is_int) -> dict:
    n = min(re_ - rs, ce - cs)
    out = {}
    if n <= 0:
        return out
    r = ref[rs:rs + n].astype(np.float64)
    c = cap[cs:cs + n].astype(np.float64)
    if is_int:
        r /= FS_INT
        c /= FS_INT
    d = c - r
    rr = float((r * r).sum())
    out["diff_rms_db"] = db_pow(float((d * d).mean()))
    out["diff_peak_db"] = db_amp(float(np.abs(d).max()))
    out["gain_db"] = db_amp(abs(float((r * c).sum()) / rr)) if rr > 0 else None
    return out


def exact_compare(ref: np.ndarray, cap: np.ndarray, rate: int, max_shift_s: float = 10.0) -> Optional[dict]:
    """Walk the bit-exact alignment of ref within cap. None if no exact alignment exists."""
    lag0 = _initial_lag(ref, cap)
    if lag0 is None:
        return None
    is_int = ref.dtype.kind != "f"
    nr, nc = len(ref), len(cap)
    max_shift = max(WINDOW * 4, int(max_shift_s * rate))
    lag = lag0
    pos = start = max(0, -lag0)
    segments: List[Tuple[int, int, int]] = []
    events: List[dict] = []
    differs_from = None
    ended_at = None
    while True:
        end = min(nr, nc - lag)
        if pos >= end:
            break
        m = _first_mismatch(ref, cap, lag, pos, end)
        if m is None:
            segments.append((pos, end, lag))
            pos = end
            break
        if m > pos:
            segments.append((pos, m, lag))
        found = _relocate(ref, cap, m, lag, max_shift) if len(events) < 1000 else None
        if found is None:
            c0 = m + lag
            sound_end = _last_sound_end(cap, c0)
            if sound_end - c0 <= int(2 * rate):
                # The capture's audio stops here (playback was stopped), perhaps after a short
                # fade: the capture ends, it does not differ.
                ended_at = m
                if sound_end > c0:
                    ev = {"kind": "ending", "ref": m, "cap": c0, "ref_frames": 0,
                          "cap_frames": sound_end - c0, "lag_before": lag, "lag_after": lag}
                    ev.update(_diff_stats(ref, cap, m, min(nr, sound_end - lag), c0, sound_end, is_int))
                    events.append(ev)
            else:
                differs_from = m
            pos = m
            break
        q, new_lag = found
        c0, c1 = m + lag, q + new_lag
        ev = {"ref": m, "cap": c0, "ref_frames": q - m, "cap_frames": c1 - c0,
              "lag_before": lag, "lag_after": new_lag}
        if q < m:
            ev["kind"] = "repeated"
            ev["repeated_frames"] = new_lag - lag
        elif q - m == c1 - c0:
            ev["kind"] = "altered"
        elif q == m:
            ev["kind"] = "inserted"
            k = c1 - c0
            if k <= m and np.array_equal(cap[c0:c1], ref[m - k:m]) and not _is_silent(cap, c0, c1):
                # The inserted frames are a copy of what was just played: a repeat.
                ev["kind"] = "repeated"
                ev["repeated_frames"] = k
        elif c1 == c0:
            ev["kind"] = "dropped"
        else:
            ev["kind"] = "replaced"
        if c1 > c0:
            ev["cap_silent"] = _is_silent(cap, c0, c1)
        if ev["kind"] in ("altered", "replaced"):
            ev.update(_diff_stats(ref, cap, m, q, c0, c1, is_int))
        events.append(ev)
        pos, lag = q, new_lag
    first_r, end_r = silence_bounds(ref)
    # End of the part of the reference the capture could hold.
    stop = ended_at if ended_at is not None else min(nr, nc - lag)
    spans = [(a, b) for a, b, _ in segments]
    compared_audio = max(0, min(stop, end_r) - max(start, first_r))
    exact_compared = _union_length(spans, max(start, first_r), min(stop, end_r))
    result = {
        "lag": lag0,
        "segments": [{"ref_start": a, "ref_end": b, "lag": l} for a, b, l in segments],
        "events": events,
        "exact_frames": _union_length(spans, 0, nr),
        "ref_frames": nr,
        # Share of the reference's audio (first to last non-zero sample) that arrived
        # bit-identical, and the same share within the part the capture could hold.
        "identical_audio_fraction": _union_length(spans, first_r, end_r) / (end_r - first_r),
        "exact_audio_fraction": exact_compared / compared_audio if compared_audio else 0.0,
        "missing_start": start,
        "missing_start_silent": _is_silent(ref, 0, start),
        "differs_from": differs_from,
        "missing_end": nr - stop if stop < nr else 0,
        "missing_end_silent": _is_silent(ref, stop, nr) if stop < nr else True,
    }
    if differs_from is not None:
        result["differing_frames"] = max(0, stop - differs_from)
    # What the capture holds outside the reference.
    cap_first = segments[0][0] + segments[0][2] if segments else lag0 + start
    cap_end = (segments[-1][1] + segments[-1][2]) if segments else cap_first
    result["cap_before"] = cap_first
    result["cap_before_silent"] = _is_silent(cap, 0, cap_first)
    result["cap_after"] = nc - cap_end
    result["cap_after_silent"] = _is_silent(cap, cap_end, nc)
    return result


# --------------------------------------------------------------------------
# Approximate alignment and difference characterisation


def _coarse_lags(er: np.ndarray, ec: np.ndarray, count: int = 3) -> List[int]:
    """Candidate lags (in blocks) from cross-correlating log-level envelopes."""
    if len(er) == 0 or len(ec) == 0:
        return []
    lx = np.maximum(10 * np.log10(er + 1e-30), -120.0)
    ly = np.maximum(10 * np.log10(ec + 1e-30), -120.0)
    lx -= lx.mean()
    ly -= ly.mean()
    nfft = _next_pow2(len(lx) + len(ly))
    c = np.fft.irfft(np.fft.rfft(ly, nfft) * np.conj(np.fft.rfft(lx, nfft)), nfft)
    lags = np.concatenate((np.arange(0, len(ly)), np.arange(-(len(lx) - 1), 0)))
    vals = np.concatenate((c[:len(ly)], c[nfft - (len(lx) - 1):] if len(lx) > 1 else []))
    picked: List[int] = []
    for idx in np.argsort(vals)[::-1][:5000]:
        lag = int(lags[idx])
        if all(abs(lag - p) >= 20 for p in picked):
            picked.append(lag)
        if len(picked) == count:
            break
    return picked


def _fine_lag(ref: Audio, cap: Audio, rs: int, length: int, lag0: int, search: int):
    """Correlation-maximising lag near lag0 for ref[rs:rs+length]. Returns (lag, score).

    Each capture channel is matched against every reference channel and the best
    absolute correlation counts, so inverted polarity, swapped channels or a
    silent channel do not hide the alignment.
    """
    cs = max(0, rs + lag0 - search)
    ce = min(cap.frames, rs + lag0 + length + search)
    if ce - cs < length or length < 16:
        return None
    r = to_float(ref, rs, rs + length)
    c = to_float(cap, cs, ce)
    r_e = (r * r).sum(axis=0)
    if not r_e.any():
        return None
    nfft = _next_pow2(len(c) + length)
    n_out = len(c) - length + 1
    rf = np.fft.rfft(r, nfft, axis=0)
    cf = np.fft.rfft(c, nfft, axis=0)
    c2 = np.concatenate((np.zeros((1, c.shape[1])), np.cumsum(c * c, axis=0)))
    win_e = c2[length:] - c2[:-length]
    score = np.zeros(n_out)
    for j in range(c.shape[1]):
        best = np.zeros(n_out)
        for i in range(r.shape[1]):
            if r_e[i] == 0:
                continue
            corr = np.fft.irfft(cf[:, j] * np.conj(rf[:, i]), nfft)[:n_out]
            # (A window with next to no energy scores next to nothing, not FFT rounding noise over zero.)
            best = np.maximum(best, np.abs(corr) / np.sqrt(np.maximum(win_e[:, j], 1e-9 * r_e[i]) * r_e[i]))
        score += best
    score /= c.shape[1]
    k = int(np.argmax(score))
    return cs + k - rs, float(score[k])


def _loud_windows(power: np.ndarray, block: int, length: int, parts: int = 3) -> List[int]:
    """Start frames of the loudest `length`-frame window in each of `parts` sections."""
    nb = max(1, length // block)
    if len(power) < nb:
        return [0]
    sums = np.concatenate(([0.0], np.cumsum(power)))
    moving = sums[nb:] - sums[:-nb]
    starts = []
    edges = np.linspace(0, len(moving), parts + 1).astype(int)
    for a, b in zip(edges[:-1], edges[1:]):
        if b > a and moving[a:b].max() > 0:
            starts.append((a + int(np.argmax(moving[a:b]))) * block)
    return starts


def approximate_align(ref: Audio, cap: Audio) -> Optional[Tuple[int, float]]:
    block = max(1, ref.rate // 1000)
    er, ec = block_power(ref, block), block_power(cap, block)
    length = min(1 << 15, ref.frames)
    search = 8 * block + 256
    best = None
    for coarse in _coarse_lags(er, ec):
        for rs in _loud_windows(er, block, length):
            got = _fine_lag(ref, cap, rs, min(length, ref.frames - rs), coarse * block, search)
            if got and (best is None or abs(got[1]) > abs(best[1])):
                best = got
    return best


def _bands(rate: int) -> List[Tuple[float, float, float]]:
    """Third-octave bands (low, centre, high) from 20 Hz to 0.45 x sample rate."""
    out = []
    for k in range(-17, 40):
        fc = 1000.0 * 2 ** (k / 3)
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        if lo < 18:
            continue
        if hi > 0.45 * rate:
            break
        out.append((lo, fc, hi))
    return out


def characterize(ref: Audio, cap: Audio, lag: int) -> dict:
    """Describe how cap differs from ref, given their alignment (cap index = ref index + lag)."""
    ch = ref.channels
    rate = ref.rate
    a, b = max(0, -lag), min(ref.frames, cap.frames - lag)
    out: dict = {"lag": lag, "overlap_frames": max(0, b - a)}
    if b - a < 64:
        return out
    # Least-squares channel matrix: cap ~= ref @ M.
    rtr = np.zeros((ch, ch))
    rtc = np.zeros((ch, ch))
    for s, e in _chunks(a, b):
        r = to_float(ref, s, e)
        c = to_float(cap, s + lag, e + lag)
        rtr += r.T @ r
        rtc += r.T @ c
    diag = np.array([rtc[i, i] / rtr[i, i] if rtr[i, i] > 0 else 0.0 for i in range(ch)])
    full = None
    if ch > 1 and np.all(np.diag(rtr) > 0) and np.linalg.cond(rtr) < 1e6:
        full = np.linalg.solve(rtr, rtc)
    # With identical channels (mono content) the full matrix is ill-defined: assume no mixing.
    model = full if full is not None else np.diag(diag)
    # Capture channel j mainly carries reference channel mapping[j]. A channel with
    # nothing in it (silent in the reference or the capture) keeps its own number.
    mapping, gains = [], []
    for j in range(ch):
        col = np.abs(model[:, j])
        i = int(np.argmax(col)) if col.max() > 0 else j
        mapping.append(i)
        gains.append(float(model[i, j]) if col.max() > 0 else None)
    out["channel_map"] = mapping
    out["model"] = model.tolist()
    out["gain_db"] = [db_amp(abs(g)) if g else None for g in gains]
    out["polarity_inverted"] = [bool(g is not None and g < 0) for g in gains]
    out["silent_channels"] = [j for j in range(ch) if gains[j] is None and rtr[j, j] > 0]
    if full is not None:
        out["matrix"] = full.tolist()
        leaks = [abs(full[i, j]) / abs(gains[j]) for j in range(ch) for i in range(ch)
                 if i != mapping[j] and gains[j]]
        out["crosstalk_db"] = db_amp(max(leaks)) if leaks else None
    # Residual pass.
    blk = max(1, rate // 4)
    nblk = (b - a + blk - 1) // blk
    num, den = np.zeros(nblk), np.zeros(nblk)
    e2 = 0.0
    e_sum = 0.0
    e_sign = 0.0
    c2 = 0.0
    nons = 0
    sil, sil_c2, sil_nonzero = 0, 0.0, 0
    margin = int(0.05 * rate)  # judge silence away from the edges of the music (filter tails)
    for s, e in _chunks(a, b):
        r = to_float(ref, s, e)
        c = to_float(cap, s + lag, e + lag)
        est = r @ model
        err = c - est
        loud = nonzero_frames(ref.data, s, e)
        silent = _far_from_signal(ref.data, s, e, margin)
        if loud.any():
            el, cl = err[loud], c[loud]
            nons += int(loud.sum())
            e2 += float((el * el).sum())
            e_sum += float(el.sum())
            e_sign += float((el * np.sign(est[loud])).sum())
            c2 += float((cl * cl).sum())
        if silent.any():
            cs_ = c[silent]
            sil += int(silent.sum())
            sil_c2 += float((cs_ * cs_).sum())
            sil_nonzero += int(np.any(cs_ != 0, axis=1).sum())
        # Gain of the capture relative to the fitted model, block by block.
        ids = (np.arange(s, e) - a) // blk
        i0 = int(ids[0])
        num_part = np.bincount(ids - i0, weights=_rowsum(est * c))
        den_part = np.bincount(ids - i0, weights=_rowsum(est * est))
        num[i0:i0 + len(num_part)] += num_part
        den[i0:i0 + len(den_part)] += den_part
    nsamp = max(1, nons * ch)
    out["residual_rms_db"] = db_pow(e2 / nsamp)
    out["residual_frames"] = nons
    out["null_depth_db"] = db_pow(e2 / c2) if c2 > 0 else None
    out["residual_mean"] = e_sum / nsamp
    out["residual_rms"] = math.sqrt(e2 / nsamp)
    out["residual_sign_corr"] = (e_sign / nsamp) / out["residual_rms"] if e2 > 0 else 0.0
    out["silent_frames"] = sil
    out["silent_nonzero_frames"] = sil_nonzero
    out["silent_rms_db"] = db_pow(sil_c2 / max(1, sil * ch)) if sil else None
    valid = den > blk * ch * 1e-6  # blocks louder than about -60 dBFS
    if valid.any():
        g = np.abs(num[valid] / den[valid])
        g = g[g > 0]
        if g.size:
            out["block_gain_db_min"] = db_amp(float(g.min()))
            out["block_gain_db_max"] = db_amp(float(g.max()))
    out.update(_spectra(ref, cap, lag, a, b, model, mapping))
    return out


def _far_from_signal(data: np.ndarray, s: int, e: int, d: int) -> np.ndarray:
    """For frames s..e-1: True where every reference frame within d frames is digital silence."""
    lo, hi = max(0, s - d), min(len(data), e + d)
    counts = np.concatenate(([0], np.cumsum(nonzero_frames(data, lo, hi), dtype=np.int64)))
    idx = np.arange(s, e) - lo
    return counts[np.minimum(idx + d + 1, hi - lo)] == counts[np.maximum(idx - d, 0)]


def _spectra(ref: Audio, cap: Audio, lag: int, a: int, b: int, model: np.ndarray,
             mapping: List[int]) -> dict:
    """Frequency response (per channel) and residual spectral tilt from averaged FFTs."""
    rate = ref.rate
    nfft = 16384 if rate <= 96000 else 32768
    if b - a < nfft:
        return {}
    starts = np.linspace(a, b - nfft, min(256, max(1, (b - a) // (nfft // 2)))).astype(np.int64)
    win = np.hanning(nfft)[:, None]
    ch = ref.channels
    nbins = nfft // 2 + 1
    srr, scc = np.zeros((nbins, ch)), np.zeros((nbins, ch))
    scr = np.zeros((nbins, ch), dtype=complex)
    see = np.zeros(nbins)
    for s in starts:
        r = to_float(ref, s, s + nfft)
        c = to_float(cap, s + lag, s + lag + nfft)
        err = c - r @ model
        rf = np.fft.rfft(r[:, mapping] * win, axis=0)  # reference channel feeding each capture channel
        cf = np.fft.rfft(c * win, axis=0)
        ef = np.fft.rfft(err * win, axis=0)
        srr += np.abs(rf) ** 2
        scc += np.abs(cf) ** 2
        scr += cf * np.conj(rf)
        see += (np.abs(ef) ** 2).sum(axis=1)
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    out = {}
    lowband = (freqs > 20) & (freqs < rate / 4)
    highband = freqs >= rate / 4
    if see[lowband].sum() > 0 and see[highband].sum() > 0:
        out["residual_tilt_db"] = db_pow(float(see[highband].mean() / see[lowband].mean()))
    response = []
    for lo, fc, hi in _bands(rate):
        sel = (freqs >= lo) & (freqs < hi)
        if sel.sum() < 2:
            continue
        row = {"freq": fc, "gain_db": [], "coherent": []}
        for c in range(ch):
            pr, pc = srr[sel, c].sum(), scc[sel, c].sum()
            cr = scr[sel, c].sum()
            if pr <= 0 or pc <= 0:
                row["gain_db"].append(None)
                row["coherent"].append(False)
                continue
            row["gain_db"].append(db_amp(abs(cr) / pr))
            row["coherent"].append(bool(abs(cr) ** 2 / (pr * pc) > 0.98 and pr / nfft > 1e-12))
        response.append(row)
    out["response"] = response
    return out


def describe_residual(ch: dict, out_bits: Optional[int], ref_bits: Optional[int] = None) -> List[str]:
    """Plain-language findings from characterize() output."""
    notes = []
    null = ch.get("null_depth_db")
    heavy = null is not None and null > -40
    mapping = ch.get("channel_map", [])
    if mapping and mapping != list(range(len(mapping))):
        if mapping == [1, 0]:
            notes.append("left and right channels are swapped")
        else:
            notes.append("channels are reordered (capture channel %s carries reference channel %s)"
                         % (", ".join(str(j + 1) for j in range(len(mapping))),
                            ", ".join(str(i + 1) for i in mapping)))
    for j in ch.get("silent_channels", []):
        notes.append("capture channel %d is silent, though the reference has audio there" % (j + 1))
    gains = [g for g in ch.get("gain_db", []) if g is not None]
    if gains and max(abs(g) for g in gains) > 0.0005:
        notes.append("level changed: " + ", ".join(
            "%+.3f dB" % g for g in gains) + " (by channel)")
    if any(ch.get("polarity_inverted", [])):
        notes.append("polarity inverted on channel(s) %s" % ", ".join(
            str(i + 1) for i, p in enumerate(ch["polarity_inverted"]) if p))
    xt = ch.get("crosstalk_db")
    if xt is not None and xt > -100 and (null is None or xt > null + 20):
        notes.append("channels are mixed into each other (crossfeed, balance or mono): "
                     "%.1f dB" % xt)
    lo, hi = ch.get("block_gain_db_min"), ch.get("block_gain_db_max")
    if lo is not None and hi - lo > 0.05 and not heavy:
        notes.append("level varies over time by %.2f dB (volume changed during capture, or "
                     "dynamic processing)" % (hi - lo))
    if out_bits and ref_bits and out_bits < ref_bits:
        notes.append("resolution reduced: the reference uses %d bits, the capture only %d"
                     % (ref_bits, out_bits))
    resp = [r for r in ch.get("response", []) if all(r["coherent"])]
    if len(resp) >= 3:
        devs = []
        for c in range(len(resp[0]["gain_db"])):
            vals = np.array([r["gain_db"][c] for r in resp])
            devs.append(vals - np.median(vals))
        worst = float(max(np.abs(d).max() for d in devs))
        span = "%s-%s" % (_fmt_freq(resp[0]["freq"]), _fmt_freq(resp[-1]["freq"]))
        if worst > 0.05:
            notes.append("frequency response is not flat: deviates up to %.2f dB across %s "
                         "(EQ, filtering or resampling)" % (worst, span))
        else:
            notes.append("frequency response flat within %.3f dB across %s" % (worst, span))
    rms = ch.get("residual_rms", 0.0)
    if heavy:
        notes.append("heavy processing: the difference is only %.1f dB below the music" % -null)
    elif rms < 1e-10:  # below -200 dBFS: nothing left but float rounding in this analysis
        notes.append("no other difference: apart from the above, every sample is exact")
    elif not out_bits:
        notes.append("remaining difference %.1f dBFS rms" % ch["residual_rms_db"])
    else:
        lsb = 2.0 ** -(out_bits - 1)
        mean = ch.get("residual_mean", 0.0) / lsb
        std = math.sqrt(max(0.0, (rms / lsb) ** 2 - mean ** 2))
        tilt = ch.get("residual_tilt_db")
        where = "%d-bit output" % out_bits
        if abs(ch.get("residual_sign_corr", 0.0)) > 0.5:
            kind = "truncation toward zero at %s (no dither)" % where
        elif mean < -0.3:
            kind = "truncation (rounding down) at %s, no dither" % where
        elif tilt is not None and tilt > 6:
            kind = "noise-shaped dither at %s (residual rises %.0f dB at high frequencies)" % (where, tilt)
        elif std < 0.33:
            kind = "plain rounding to %s, no dither" % where
        elif std < 0.75:
            kind = "dither at %s (about %.2f LSB rms)" % (where, std)
        else:
            kind = "added noise or processing of about %.1f LSB rms at %s" % (std, where)
        notes.append("remaining difference %.1f dBFS rms: consistent with %s"
                     % (ch["residual_rms_db"], kind))
    if ch.get("silent_frames"):
        if ch.get("silent_nonzero_frames"):
            notes.append("where the reference is digital silence, the capture is not: %.1f dBFS rms "
                         "(dither or noise added)" % ch["silent_rms_db"])
        else:
            notes.append("digital silence stays digital silence")
    return notes


def _fmt_freq(f: float) -> str:
    return "%.0f Hz" % f if f < 1000 else "%.1f kHz" % (f / 1000)


# --------------------------------------------------------------------------
# Top-level comparison


def compare(ref: Audio, cap: Audio) -> dict:
    """Compare a capture with a reference. See the module docstring."""
    res: dict = {
        "ref": {"path": ref.path, "label": ref.label, "rate": ref.rate, "channels": ref.channels,
                "frames": ref.frames, "bits": ref.bits, "is_float": ref.is_float},
        "cap": {"path": cap.path, "label": cap.label, "rate": cap.rate, "channels": cap.channels,
                "frames": cap.frames, "bits": cap.bits, "is_float": cap.is_float},
        "ref_is_capture": ref.is_capture,
        "ref_resolution": resolution(ref),
        "cap_resolution": resolution(cap),
        "notes": [],
    }
    if ref.meta.get("lossy_source"):
        res["notes"].append("the reference is a lossy file (%s): decoders differ, so it cannot be "
                            "used to test for bit-perfect output" % ref.meta["lossy_source"])
    if ref.rate != cap.rate:
        res["verdict"] = "RESAMPLED"
        res["summary"] = "the sample rate changed from %d Hz to %d Hz, so the player resampled" % (
            ref.rate, cap.rate)
        return res
    if ref.channels != cap.channels:
        res["verdict"] = "CHANNELS"
        res["summary"] = "the channel count changed from %d to %d" % (ref.channels, cap.channels)
        return res
    if silence_bounds(ref.data)[1] == 0:
        res["verdict"] = "NO SIGNAL"
        res["summary"] = "the reference is digital silence, nothing to compare"
        return res
    r_arr, c_arr = comparable(ref, cap)
    exact = exact_compare(r_arr, c_arr, ref.rate)
    del r_arr, c_arr
    res["exact"] = exact
    if exact is not None and exact["exact_audio_fraction"] >= 0.5:
        res["verdict"] = _exact_verdict(exact)
        # Mostly identical. Only characterise further when matching stopped for good.
        if exact["differs_from"] is None:
            return res
    aligned = approximate_align(ref, cap)
    if aligned is None or abs(aligned[1]) < 0.3:
        if "verdict" not in res:
            res["verdict"] = "NO MATCH"
            res["summary"] = "could not find the reference in the capture"
        return res
    lag, rho = aligned
    res["approx"] = characterize(ref, cap, lag)
    res["approx"]["correlation"] = rho
    res["approx"]["findings"] = describe_residual(res["approx"], res["cap_resolution"],
                                                  res["ref_resolution"])
    res.setdefault("verdict", "DIFFERENT")
    return res


def _exact_verdict(exact: dict) -> str:
    kinds = {e["kind"] for e in exact["events"]}
    if exact["differs_from"] is not None or kinds & {"altered", "replaced"}:
        return "ALTERED"
    if kinds & {"inserted", "dropped", "repeated"}:
        return "GAPS"
    if ((exact["missing_start"] and not exact["missing_start_silent"])
            or (exact["missing_end"] and not exact["missing_end_silent"])):
        return "PARTIAL"
    return "IDENTICAL"
