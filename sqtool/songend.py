"""Where does the song end, in a stream that is still being recorded?

A recording should stop at the song's last sample, even when the player goes
straight on to the next track. SQ-tool has the song, so it follows it in the
stream:

* Bit-perfect players: once a stretch of the stream is found in the song (an
  index of the song's samples makes that quick, wherever playback is), every
  following sample is checked against the song. The song's end is then known
  exactly. A jump (a dropout, a repeat, a seek, a restart) breaks the match;
  the stream is found again in the song and the end moves with it.
* Other players (volume, DSP, another sample rate): the stream's loudness is
  lined up with the song's and checked on the waveform at a few places, which
  says where the song should end. Its last notes are then looked for on the
  waveform, where they are due or later (after a dropout), and the end is
  placed from them.

The end can be known only once the stream has gone a little past it: the
recorder then cuts the recording back to it.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, List, Optional, Tuple

import numpy as np

from .analysis import FS_INT, _chunks, _fine_lag, to_float
from .wavio import WAVE_FORMAT_IEEE_FLOAT, WAVE_FORMAT_PCM, Audio, _decode_samples

STEP = 64  # every STEP-th frame of the song is indexed
VERIFY = 64  # frames that must match to (re)place the stream in the song
BLOCK_SECONDS = 0.01  # loudness envelope resolution: a whole number of frames at every common rate
HEAD = 20.0  # seconds of music to line up by loudness (less for a short song)
RETRY = 2.0  # seconds between tries to line it up, on the latest HEAD seconds, while it won't fit
LATE = 5.0  # seconds past the song's expected end to wait for the rest of it (after a dropout)
MARGIN = 0.25  # seconds kept past an end that couldn't be checked on the waveform
LOOK = 0.25  # seconds between looks for the song's last notes, once they are due
LAST_NOTES = 2.0  # seconds of the song's last notes to recognise its end by (long enough to hold
                  # more than one drum hit: those can repeat exactly)
LAST_NOTES_DB = 50.0  # ... where at most a quarter of the time is this many dB below the loudest
                      # stretch of the song's last 30 s (or below -80 dBFS)
FINE = 1 << 14  # frames compared to place a stretch to the sample (a longer one at a high rate is
                # compared at about 48 kHz first)
MIN_SCORE = 0.6  # loudness correlation needed to consider an alignment
MIN_WAVE = 0.7  # waveform correlation needed to accept it, at each of a few separate places (songs
                # with the same beat have similar loudness; one shared drum sample can match in one)


def _keys(x: np.ndarray) -> np.ndarray:
    """One int64 per frame from its first 8 bytes (all-zero for digital silence)."""
    x = np.ascontiguousarray(x)
    row = x.view(np.uint8).reshape(len(x), -1)
    if row.shape[1] >= 8:
        return np.ascontiguousarray(row[:, :8]).view(np.int64)[:, 0]
    padded = np.zeros((len(x), 8), np.uint8)
    padded[:, :row.shape[1]] = row
    return padded.view(np.int64)[:, 0]


def song_index(song: Audio) -> Tuple[np.ndarray, np.ndarray]:
    """(keys, frames): every STEP-th non-silent frame of the song, sorted by key."""
    pos = np.arange(0, song.frames, STEP, dtype=np.int64)
    keys = _keys(song.data[pos])
    keep = keys != 0
    pos, keys = pos[keep], keys[keep]
    order = np.argsort(keys, kind="stable")
    return keys[order], pos[order]


def envelope(audio: Audio) -> np.ndarray:
    """Mean square (all channels, full scale 1.0) per BLOCK_SECONDS block."""
    block = max(1, int(round(audio.rate * BLOCK_SECONDS)))
    nb = audio.frames // block
    out = np.empty(nb)
    scale = 1.0 if audio.is_float else 1.0 / FS_INT
    step = max(1, (1 << 20) // block) * block
    for a, b in _chunks(0, nb * block, step):
        x = audio.data[a:b].astype(np.float64) * scale
        out[a // block:b // block] = (x * x).mean(axis=1).reshape(-1, block).mean(axis=1)
    return out


def _log(e: np.ndarray) -> np.ndarray:
    return np.maximum(10 * np.log10(np.asarray(e, np.float64) + 1e-30), -100.0)


def envelope_lags(song_env: np.ndarray, stream_env: np.ndarray, lo: Optional[int] = None,
                  hi: Optional[int] = None) -> List[Tuple[int, float]]:
    """Ways the loudness lines up, best first: (blocks, score), song block b with stream block
    b + blocks (looked for in lo..hi when given), scoring at least MIN_SCORE. Of fits about as good
    as the best (a part of the song that repeats), the one leaving most of the song to come is put
    first: if it is wrong, the recording runs on rather than cutting the song short."""
    ls, lc = _log(song_env), _log(stream_env)
    if len(ls) < 10 or len(lc) < 10:
        return []
    a, b = ls - ls.mean(), lc - lc.mean()
    nfft = 1 << int(len(a) + len(b)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    lags = np.concatenate((np.arange(0, len(b)), np.arange(-(len(a) - 1), 0)))
    vals = np.concatenate((corr[:len(b)], corr[nfft - (len(a) - 1):]))
    if lo is not None:
        vals = np.where((lags >= lo) & (lags <= hi), vals, -np.inf)
    picked: List[int] = []
    for idx in np.argsort(vals)[::-1][:5000]:
        if not np.isfinite(vals[idx]) or len(picked) == 40:
            break
        lag = int(lags[idx])
        if all(abs(lag - p) >= 25 for p in picked):  # separate peaks, not one peak's shoulders
            picked.append(lag)
    found = []
    for lag in picked:
        s0, s1 = max(0, -lag), min(len(ls), len(lc) - lag)
        if s1 - s0 < 200:  # at least 2 s of overlap
            continue
        x, y = ls[s0:s1], lc[s0 + lag:s1 + lag]
        if x.std() == 0 or y.std() == 0:
            continue
        score = float(np.corrcoef(x, y)[0, 1])
        if score >= MIN_SCORE:
            found.append((lag, score))
    if not found:
        return []
    top = max(score for _, score in found)
    return sorted(found, key=lambda f: (f[1] < top - 0.02, -f[0] if f[1] >= top - 0.02 else -f[1]))


def last_notes(song: Audio, song_env: np.ndarray) -> Optional[Tuple[int, int]]:
    """(song frame, frames): the song's last notes, to recognise its end by on the waveform: its
    last LAST_NOTES seconds with sound most of the time. None if it has none."""
    n = int(LAST_NOTES * song.rate)
    block = max(1, int(round(song.rate * BLOCK_SECONDS)))
    nb = -(-n // block)  # blocks that cover a stretch
    env = np.asarray(song_env, np.float64)
    if len(env) < nb or song.frames < n:
        return None
    sums = np.concatenate(([0.0], np.cumsum(env)))
    power = (sums[nb:] - sums[:-nb]) / nb  # mean square of the stretch from each block on
    floor = max(power[-int(30 / BLOCK_SECONDS):].max() * 10 ** (-LAST_NOTES_DB / 10), 1e-8)
    quiet = np.concatenate(([0], np.cumsum(env < floor)))
    ok = np.flatnonzero(quiet[nb:] - quiet[:-nb] <= nb // 4)
    if ok.size == 0:
        return None
    return int(ok[-1]) * block, n


def _decimate(x: np.ndarray, k: int) -> np.ndarray:
    """Every k frames averaged into one: a rough low-pass and decimation, enough to compare music by
    (whose sound is almost all far below the new rate's limit)."""
    m = len(x) // k
    return x[:m * k].reshape(m, k, x.shape[1]).mean(axis=1)


def _as_int(x: np.ndarray) -> np.ndarray:
    """Float samples as left-justified int32: exact for integer samples sent as floats."""
    return np.clip(np.round(x.astype(np.float64) * FS_INT), -FS_INT, FS_INT - 1).astype(np.int32)


class SongEnd:
    """Follows a stream (fed as WAV payload bytes) and says where to stop recording it."""

    def __init__(self, song: Audio, index: Tuple[np.ndarray, np.ndarray], song_env: np.ndarray, rate: int,
                 channels: int, wav_bits: int, is_float: bool):
        self.song = song
        self.song_env = song_env
        self.rate = rate
        self.channels = channels
        self.fmt = {"tag": WAVE_FORMAT_IEEE_FLOAT if is_float else WAVE_FORMAT_PCM, "container": wav_bits // 8}
        dtype = (np.float32 if wav_bits == 32 else np.float64) if is_float else np.int32
        # Integer samples sent as floats (scaled by a power of 2) are compared as integers.
        self._as_int = is_float and song.data.dtype == np.int32
        same = np.int32 if self._as_int else dtype
        exact = rate == song.rate and channels == song.channels and np.dtype(same) == song.data.dtype
        self.ikeys, self.ipos = index if exact and len(index[0]) else (None, None)
        self.same_rate = rate == song.rate and channels == song.channels
        self.block = max(1, int(round(rate * BLOCK_SECONDS)))
        song_seconds = song.frames / song.rate
        self.head_frames = int(min(HEAD, max(2.0, 0.6 * song_seconds)) * rate)
        self.frames = 0
        self.first_sound: Optional[int] = None
        self._carry = np.zeros((0, channels), same)
        self._partial = np.zeros((0, channels))
        self._env: List[float] = []
        self._head: Deque[np.ndarray] = deque()  # the latest stretch of the stream, to line up with
        self._held = 0  # frames in _head
        self._head_start: Optional[int] = None  # its first frame
        self._next_align: Optional[int] = None
        self._prior: Optional[int] = None  # where a try that failed further on did find the song (in
                                           # blocks): later tries look from there on
        self._aligned = False
        self.lag: Optional[int] = None  # stream frame = song frame + lag (once known)
        self.synced = False  # every sample since the stream was placed matched the song
        self.exact_end: Optional[int] = None  # from following the song sample by sample
        self.lost_silent = False  # out of step, and only digital silence since (a dropout)
        self.jumps = 0  # times the stream had to be found again (dropouts, repeats, seeks)
        self._placed_at = 0
        self._left_off: Optional[int] = None  # song frame where the stream last stopped matching
        self.approx_end: Optional[int] = None  # from the alignment: where the song should end
        self.checked_end: Optional[int] = None  # the same, from its last notes found on the waveform
        self._last_notes: Optional[Tuple[int, int]] = None  # (song frame, frames), while looked for
        self._tail: List[np.ndarray] = []  # the stream from where they are due
        self._tail_start = 0
        self._tail_held = 0
        self._next_look = 0
        self._looked_to = 0  # lags looked at so far, up to

    # -- the stream -----------------------------------------------------------------

    def feed(self, payload: bytes) -> Optional[int]:
        """The next stretch of the stream. Returns stop_frame()."""
        x = _decode_samples(payload, self.fmt).reshape(-1, self.channels)
        start = self.frames
        self.frames += len(x)
        if self.first_sound is None:
            nz = np.flatnonzero(x.any(axis=1))
            if nz.size:
                self.first_sound = start + int(nz[0])
        if self.ikeys is not None:
            self._follow(_as_int(x) if self._as_int else x, start)
        if self.exact_end is None:  # (a bit-perfect stream needs none of this)
            self._loudness(x, start)
            if self._last_notes is not None:
                self._watch(x, start)
        return self.stop_frame()

    def end_frame(self) -> Optional[int]:
        """Where the song ends in the stream (an exclusive frame), as far as known."""
        if self.exact_end is not None:
            return self.exact_end
        if self.checked_end is not None:
            return self.checked_end
        if self.approx_end is not None:
            return self.approx_end + int(MARGIN * self.rate)
        return None

    def stop_frame(self) -> Optional[int]:
        """Where to stop recording, once sure. It may be behind the stream already: cut back to it."""
        if self.exact_end is not None:
            due, waiting = self.exact_end, not self.synced and self.lost_silent  # a dropout
        else:
            due, waiting = self.approx_end, self._last_notes is not None
        if waiting and self.frames < due + int(LATE * self.rate):
            return None  # the rest of the song may yet come
        return self.end_frame()

    def how(self) -> Optional[str]:
        if self.exact_end is not None:
            return "exact"
        if self.checked_end is not None:
            return "aligned"
        return "estimated" if self.approx_end is not None else None

    def song_seconds(self, frame: int) -> Optional[float]:
        """Where in the song the stream is at `frame`, once lined up (lag: the song's first frame)."""
        if self.lag is None:
            return None
        return (frame - self.lag) / self.rate

    def summary(self) -> dict:
        return {"how": self.how(), "end_frame": self.end_frame(), "lag": self.lag, "jumps": self.jumps}

    # -- exact: following the song sample by sample ----------------------------------------

    def _follow(self, x: np.ndarray, start: int) -> None:
        if self.synced:
            bad = self._check(x, start)
            if bad is None:
                self._carry = x[-(STEP + VERIFY):].copy()
                return
            self.synced, self.lost_silent = False, True
            self.jumps += 1
            self._left_off = bad - self.lag
            # Look for the stream again from where it stopped matching.
            self._carry = np.zeros((0, self.channels), x.dtype)
            x, start = x[bad - start:], bad
        buf = np.concatenate([self._carry, x]) if len(self._carry) else x
        base = start - len(self._carry)
        lag = self._place(buf, base)
        if lag is not None:
            self.lag, self.synced = lag, True
            self.exact_end = self.song.frames + lag
            # Check the rest of this stretch at once: the end may be in it.
            done = max(start, base + self._placed_at + VERIFY)
            if done < start + len(x):
                self._follow(x[done - start:], done)
            return
        if self.exact_end is not None and x.any():
            self.lost_silent = False  # sound, but not the song: crossfaded or processed from here
        self._carry = buf[-(STEP + VERIFY):].copy()

    def _check(self, x: np.ndarray, start: int) -> Optional[int]:
        """First stream frame in x that differs from the song at the current lag (None: all match)."""
        lo = max(start, self.lag)
        hi = min(start + len(x), self.lag + self.song.frames)
        if hi <= lo:
            return None
        seg, ref = x[lo - start:hi - start], self.song.data[lo - self.lag:hi - self.lag]
        eq = np.all(seg == ref, axis=1) if seg.itemsize * seg.shape[1] > 8 else _keys(seg) == _keys(ref)
        return None if eq.all() else lo + int(np.argmin(eq))

    def _place(self, buf: np.ndarray, base: int) -> Optional[int]:
        """The lag at which VERIFY frames of buf match the song, found through the index (first
        trying where the song left off: after a dropout, it goes on from there)."""
        if self._left_off is not None:
            lag = self._took_up(buf, base)
            if lag is not None:
                return lag
        keys = _keys(buf)
        cand = np.flatnonzero(keys[:max(0, len(buf) - VERIFY + 1)] != 0)
        if cand.size == 0:
            return None
        k = keys[cand]
        lo = np.searchsorted(self.ikeys, k, side="left")
        hi = np.searchsorted(self.ikeys, k, side="right")
        tried = 0
        for j in np.flatnonzero(hi > lo):
            s = int(cand[j])
            for p in self.ipos[lo[j]:min(hi[j], lo[j] + 8)]:
                p = int(p)
                if p + VERIFY <= self.song.frames and np.array_equal(buf[s:s + VERIFY], self.song.data[p:p + VERIFY]):
                    self._placed_at = s
                    return base + s - p
            tried += 1
            if tried >= 512:  # quiet passages repeat values: many false leads
                break
        return None

    def _took_up(self, buf: np.ndarray, base: int) -> Optional[int]:
        """The lag if the stream's first sound in buf goes on with the song where it left off.
        Quiet passages can't be found through the index (their few values are everywhere)."""
        sound = np.flatnonzero(buf.any(axis=1))
        if sound.size == 0:
            return None  # silence still: wait
        s, p = int(sound[0]), self._left_off
        ahead = np.flatnonzero(self.song.data[p:p + self.rate].any(axis=1))
        if ahead.size:
            p += int(ahead[0])  # (the song's own silence went by in the dropout)
            n = min(VERIFY, self.song.frames - p)
            if len(buf) - s < n:
                return None  # not enough of it yet: look again with more
            if np.array_equal(buf[s:s + n], self.song.data[p:p + n]):
                self._left_off, self._placed_at = None, s
                return base + s - p
        self._left_off = None  # it went on from somewhere else
        return None

    # -- approximate: loudness ---------------------------------------------------------

    def _loudness(self, x: np.ndarray, start: int) -> None:
        if self._aligned:
            return
        xf = x.astype(np.float64)
        if np.issubdtype(x.dtype, np.integer):
            xf /= FS_INT
        joined = np.concatenate([self._partial, xf]) if len(self._partial) else xf
        nb = len(joined) // self.block
        if nb:
            p = (joined[:nb * self.block] ** 2).mean(axis=1).reshape(nb, self.block).mean(axis=1)
            self._env.extend(p.tolist())
        self._partial = joined[nb * self.block:]
        if self.first_sound is None:
            return
        # Hold the latest stretch of the music, to check and refine the alignment on the waveform.
        if self._head_start is None:  # (anything before this block was silence)
            self._head_start = max(start, self.first_sound - self.rate // 10)
            self._next_align = self.first_sound + self.head_frames
        lo = max(start, self._head_start + self._held)
        if lo < self.frames:
            self._head.append(x[lo - start:])
            self._held += self.frames - lo
        while len(self._head) > 1 and self._held - len(self._head[0]) >= self.head_frames + self.rate:
            dropped = len(self._head.popleft())
            self._held -= dropped
            self._head_start += dropped
        if self.frames >= self._next_align:
            # A dropout in the stretch spoils the fit: then try again later, on a later stretch.
            self._next_align = self.frames + int(RETRY * self.rate)
            self._align()

    def _align(self) -> None:
        first_block = self._head_start // self.block
        lo = hi = None
        if self._prior is not None:  # a try again: a dropout can only have delayed the song since
            lo, hi = self._prior - first_block - 2, self._prior - first_block + int(LATE / BLOCK_SECONDS)
        head = None
        for blocks, _ in envelope_lags(self.song_env, np.asarray(self._env[first_block:]), lo, hi)[:5]:
            if head is None:
                head = np.concatenate(self._head)
            # Blocks are BLOCK_SECONDS long in both, so the song's first frame plays at about this
            # stream frame (to within a block):
            found = self._refine(head, (blocks + first_block) * self.block)
            if found and None not in found and max(found) - min(found) <= max(2, self.rate // self.song.rate * 2):
                lag_frames = found[-1]
                break
            if self._prior is None and found and found[0] is not None:
                self._prior = found[0] // self.block  # the song was there, then: a dropout since?
        else:
            return  # the loudness fits, the waveform doesn't: not this song (or not in one piece)
        self._aligned = True
        self._head = deque()
        if self.lag is None:
            self.lag = lag_frames
        self.approx_end = lag_frames + self._song_frames()
        self._await_last_notes(head)

    def _song_frames(self) -> int:
        """The song's length in stream frames."""
        return int(round(self.song.frames / self.song.rate * self.rate))

    # -- approximate: the song's last notes ------------------------------------------------

    def _await_last_notes(self, head: np.ndarray) -> None:
        """Pick the song's last notes, and hold the stream from where they are due (head: the stream
        held from _head_start up to now)."""
        notes = last_notes(self.song, self.song_env)
        if notes is None:
            return  # nothing to recognise: the end stays an estimate
        sr, r = self.song.rate, self.rate
        slack = int(np.ceil(self._slack() * r / sr))
        due = self.approx_end - self._song_frames() + int(notes[0] / sr * r)
        if due - slack < self.frames:  # a short song: due already
            if due - slack < self._head_start:
                return  # and gone: the end stays an estimate
            self._tail = [head[due - slack - self._head_start:].copy()]
            self._tail_held = self.frames - (due - slack)
        self._last_notes = notes
        self._tail_start = due - slack
        self._looked_to = self.lag - slack
        self._next_look = due + int(np.ceil(notes[1] / sr * r)) + slack

    def _slack(self) -> int:
        """Song frames an alignment can be out by: the loudness is lined up to within a block."""
        return int(2 * BLOCK_SECONDS * self.song.rate) + 64

    def _watch(self, x: np.ndarray, start: int) -> None:
        lo = max(start, self._tail_start + self._tail_held)
        if lo < self.frames:
            self._tail.append(x[lo - start:])
            self._tail_held += self.frames - lo
        if self.frames >= self.approx_end + int(LATE * self.rate):
            self._last_notes, self._tail = None, []  # not found: the end stays an estimate
        elif self.frames >= self._next_look:
            self._next_look = self.frames + int(LOOK * self.rate)
            self._look()

    def _look(self) -> None:
        """Look for the song's last notes in the stream held so far, at lags not yet looked at."""
        rs, n = self._last_notes
        tail = np.concatenate(self._tail) if len(self._tail) > 1 else self._tail[0]
        self._tail = [tail]
        span = int(np.ceil((rs + n) / self.song.rate * self.rate))  # song start to the notes' end
        hi = self._tail_start + len(tail) - span - 1  # the latest lag at which they are all held
        lo = self._looked_to
        if hi < lo:
            return
        self._looked_to = hi
        found = self._match(tail, self._tail_start, lo, hi, rs, n)
        if found is not None:
            self.lag = found
            self.checked_end = found + self._song_frames()
            self._last_notes, self._tail = None, []

    def _refine(self, head: np.ndarray, lag_frames: int) -> List[Optional[int]]:
        """Check the alignment on the waveform at up to four points spread over the held stream
        (parts of a song can repeat, all but exactly), in order: the lag found at each, exact to the
        sample (to the song's sample when the rates differ), or None where the waveforms don't
        match."""
        sr, r = self.song.rate, self.rate
        length = min(1 << 14, sr // 2)
        search = self._slack()
        # Song frames that the held stream covers, with room to search either side.
        first = (self._head_start - lag_frames) / r * sr
        lo = max(0, int(first) + search + 1)
        hi = min(self.song.frames, int(first + len(head) / r * sr) - search - 1) - length
        parts = max(1, min(4, (hi - lo) // (2 * length)))
        edges = np.linspace(lo, hi + length, parts + 1).astype(int)
        spots = [self._loud_spot(a, b - length, length) for a, b in zip(edges[:-1], edges[1:])]
        lo, hi = lag_frames - int(np.ceil(search * r / sr)), lag_frames + int(np.ceil(search * r / sr))
        return [self._match(head, self._head_start, lo, hi, rs, length) for rs in spots if rs is not None]

    def _match(self, held: np.ndarray, h0: int, lo: int, hi: int, rs: int, length: int) -> Optional[int]:
        """The lag (the stream frame where the song's first frame plays) in lo..hi at which the stream
        held (from stream frame h0) matches the song's frames rs..rs+length on the waveform; None if
        it matches nowhere."""
        sr, r = self.song.rate, self.rate
        if self.same_rate:
            got = self._correlate(held, rs, length, (lo + hi) // 2 - h0, (hi - lo) // 2 + 1)
            return None if got is None else got + h0
        # Another rate: resample the stream onto the song's sample grid, as if it were at lag `mid`.
        mid = (lo + hi) / 2
        half = int(np.ceil((hi - lo) / 2 / r * sr)) + 1
        grid = rs - half + np.arange(length + 2 * half)
        pos = grid / sr * r + mid - h0  # where each song sample falls in the held stream
        keep = (pos >= 0) & (pos <= len(held) - 1)
        if np.count_nonzero(keep) < length:
            return None
        grid, pos = grid[keep], pos[keep]
        a = int(pos[0])
        part = held[a:int(np.ceil(pos[-1])) + 1].astype(np.float64)
        xs = np.arange(a, a + len(part))
        seg = np.stack([np.interp(pos, xs, part[:, c]) for c in range(part.shape[1])], axis=1)
        got = self._correlate(seg, rs, length, -int(grid[0]), half)
        return None if got is None else int(round(mid + (got + int(grid[0])) / sr * r))

    def _correlate(self, cap: np.ndarray, rs: int, length: int, lag0: int, search: int) -> Optional[int]:
        """The lag (cap frame - song frame) in lag0 +- search at which cap, at the song's rate, matches
        the song's frames rs..rs+length on the waveform; None if it matches nowhere. A long stretch at
        a high rate is compared at about 48 kHz first (much quicker), then to the sample on its
        loudest part."""
        song = self.song
        audio = Audio(data=cap, rate=song.rate, bits=32, is_float=not np.issubdtype(cap.dtype, np.integer))
        k = song.rate // 48000
        if k > 1 and length > FINE:
            cs = max(0, rs + lag0 - search)
            ce = min(len(cap), rs + lag0 + length + search)
            if ce - cs < length:
                return None
            ref = Audio(data=_decimate(to_float(song, rs, rs + length), k), rate=song.rate // k, bits=32, is_float=True)
            seg = Audio(data=_decimate(to_float(audio, cs, ce), k), rate=song.rate // k, bits=32, is_float=True)
            coarse = _fine_lag(ref, seg, 0, ref.frames, (rs + lag0 - cs) // k, -(-search // k) + 1)
            if coarse is None or coarse[1] < MIN_WAVE:
                return None
            lag0, search = coarse[0] * k + cs - rs, k + 1
            spot = self._loud_spot(rs, rs + length - FINE, FINE)
            rs, length = (rs if spot is None else spot), FINE
        fine = _fine_lag(song, audio, rs, length, lag0, search)
        return fine[0] if fine is not None and fine[1] >= MIN_WAVE else None

    def _loud_spot(self, lo: int, hi: int, length: int) -> Optional[int]:
        """Start of the loudest `length` song frames starting within [lo, hi]."""
        if hi <= lo:
            return None
        sb = self.song.rate * BLOCK_SECONDS
        b0, b1 = int(lo / sb), int(hi / sb) + 1
        env = np.asarray(self.song_env[b0:b1])
        if env.size == 0:
            return None
        return int(min(hi, max(lo, (b0 + int(np.argmax(env))) * sb - length // 2)))
