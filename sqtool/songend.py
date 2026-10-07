"""Where does the song end, in a stream that is still being recorded?

A recording should stop at the song's last sample, even when the player goes
straight on to the next track. SQ-tool has the song, so it recognises it in
the stream:

* Bit-perfect players: short, distinctive fragments of the song ("marks") are
  looked for in the stream, sample for sample. A mark from the last seconds of
  the song gives its exact end, whatever happened earlier in the stream
  (silence before the song, a dropout). A mark from the start gives an early
  estimate.
* Other players (volume, DSP, another sample rate): the stream's loudness is
  lined up with the song's by correlation, and the song's length gives its end.
  The recording then runs a fraction of a second past that estimate, so none
  of the song is lost.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from .analysis import FS_INT, _chunks, _fine_lag, silence_bounds
from .wavio import WAVE_FORMAT_IEEE_FLOAT, WAVE_FORMAT_PCM, Audio, _decode_samples

MARK = 32  # frames per mark
BLOCK_SECONDS = 0.01  # loudness envelope resolution: a whole number of frames at every common rate
HEAD = 20.0  # seconds of music to line up by loudness (less for a short song)
GRACE = 2.0  # seconds to wait for a closing mark after the estimate from an opening mark
MARGIN = 0.25  # seconds recorded past an end that is estimated rather than exact
MIN_SCORE = 0.6  # loudness correlation needed to consider an alignment
MIN_WAVE = 0.7  # waveform correlation needed to accept it, at two separate places (songs with the
                # same beat have similar loudness; one shared drum sample can match in one place)


def _keys(x: np.ndarray) -> np.ndarray:
    """One int64 per frame from its first 8 bytes (matches are verified in full afterwards)."""
    x = np.ascontiguousarray(x)
    row = x.view(np.uint8).reshape(len(x), -1)
    if row.shape[1] >= 8:
        return np.ascontiguousarray(row[:, :8]).view(np.int64)[:, 0]
    padded = np.zeros((len(x), 8), np.uint8)
    padded[:, :row.shape[1]] = row
    return padded.view(np.int64)[:, 0]


def _find(keys: np.ndarray, want: np.ndarray) -> np.ndarray:
    """Start positions where the key sequence `want` occurs in `keys`."""
    n = len(keys) - len(want) + 1
    if n <= 0:
        return np.empty(0, np.int64)
    c = np.flatnonzero(keys[:n] == want[0])
    for j in range(1, len(want)):
        if c.size == 0:
            break
        c = c[keys[c + j] == want[j]]
    return c


def pick_marks(song: Audio) -> List[dict]:
    """Distinctive fragments that occur exactly once in the song: two near its start and five
    spread over its last 25 seconds, each as {"start", "closing", "frames"}."""
    data = song.data
    first, end = silence_bounds(data)
    if end - first < 4 * MARK:
        return []
    rate = song.rate
    spots = [(first + int(s * rate), False) for s in (0.0, 1.5)]
    spots += [(end - MARK - int(s * rate), True) for s in (25.0, 15.0, 8.0, 3.0, 0.0)]
    reach = int(0.25 * rate)
    cands: List[Tuple[int, bool]] = []
    for t, closing in spots:
        t = min(max(t, first), end - MARK)
        lo, hi = max(first, t - reach), min(end - MARK, t + reach) + 1
        mag = np.abs(data[lo:hi].astype(np.float64)).sum(axis=1)
        s = lo + int(np.argmax(mag))  # starts on the loudest frame nearby: rare, never silent
        win = data[s:s + MARK]
        if len(win) == MARK and len(np.unique(_keys(win))) >= MARK // 2:
            if all(abs(s - c) >= MARK for c, _ in cands):
                cands.append((s, closing))
    if not cands:
        return []
    wants = [_keys(data[s:s + MARK]) for s, _ in cands]
    counts = [0] * len(cands)
    for a, b in _chunks(0, len(data)):
        keys = _keys(data[a:min(len(data), b + MARK - 1)])
        for i, want in enumerate(wants):
            counts[i] += int(_find(keys, want).size)
    return [{"start": int(s), "closing": closing, "frames": data[s:s + MARK].tolist()}
            for (s, closing), count in zip(cands, counts) if count == 1]


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


def envelope_lag(song_env: np.ndarray, stream_env: np.ndarray) -> Optional[Tuple[int, float]]:
    """(blocks, score): song block b lines up with stream block b + blocks. None if nothing fits."""
    ls, lc = _log(song_env), _log(stream_env)
    if len(ls) < 10 or len(lc) < 10:
        return None
    a, b = ls - ls.mean(), lc - lc.mean()
    nfft = 1 << int(len(a) + len(b)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    lags = np.concatenate((np.arange(0, len(b)), np.arange(-(len(a) - 1), 0)))
    vals = np.concatenate((corr[:len(b)], corr[nfft - (len(a) - 1):]))
    best = None
    for idx in np.argsort(vals)[::-1][:20]:
        lag = int(lags[idx])
        s0, s1 = max(0, -lag), min(len(ls), len(lc) - lag)
        if s1 - s0 < 200:  # at least 2 s of overlap
            continue
        x, y = ls[s0:s1], lc[s0 + lag:s1 + lag]
        if x.std() == 0 or y.std() == 0:
            continue
        score = float(np.corrcoef(x, y)[0, 1])
        if best is None or score > best[1]:
            best = (lag, score)
    return best


class SongEnd:
    """Follows a stream (fed as WAV payload bytes) and says where to stop recording it."""

    def __init__(self, song: Audio, marks: List[dict], song_env: np.ndarray, rate: int, channels: int,
                 wav_bits: int, is_float: bool):
        self.song = song
        self.song_env = song_env
        self.rate = rate
        self.channels = channels
        self.fmt = {"tag": WAVE_FORMAT_IEEE_FLOAT if is_float else WAVE_FORMAT_PCM, "container": wav_bits // 8}
        dtype = (np.float32 if wav_bits == 32 else np.float64) if is_float else np.int32
        exact = rate == song.rate and channels == song.channels and np.dtype(dtype) == song.data.dtype
        self.marks = []
        if exact:
            for m in marks:
                frames = np.asarray(m["frames"], dtype=dtype).reshape(MARK, channels)
                self.marks.append({"start": m["start"], "closing": m["closing"], "keys": _keys(frames),
                                   "frames": frames})
        self.same_rate = rate == song.rate and channels == song.channels
        self.block = max(1, int(round(rate * BLOCK_SECONDS)))
        song_seconds = song.frames / song.rate
        self.head_frames = int(min(HEAD, max(2.0, 0.6 * song_seconds)) * rate)
        self.frames = 0
        self.first_sound: Optional[int] = None
        self._carry = np.zeros((0, channels), dtype)
        self._partial = np.zeros((0, channels))
        self._env: List[float] = []
        self._head: List[np.ndarray] = []
        self._head_start: Optional[int] = None
        self._aligned = False
        self.lag: Optional[int] = None  # stream frame = song frame + lag (once known)
        self.exact_end: Optional[int] = None  # from a closing mark
        self.open_end: Optional[int] = None  # from an opening mark
        self.approx_end: Optional[int] = None  # from the loudness alignment
        self.found: List[int] = []  # song frames of the marks seen in the stream

    # -- the stream -----------------------------------------------------------------

    def feed(self, payload: bytes) -> Optional[int]:
        """The next stretch of the stream. Returns the frame to stop at (exclusive), once known."""
        x = _decode_samples(payload, self.fmt).reshape(-1, self.channels)
        start = self.frames
        self.frames += len(x)
        if self.first_sound is None:
            nz = np.flatnonzero(x.any(axis=1))
            if nz.size:
                self.first_sound = start + int(nz[0])
        if len(self.found) < len(self.marks):  # later marks correct for a dropout before them
            self._scan(x, start)
        self._loudness(x, start)
        return self.stop_frame()

    def stop_frame(self) -> Optional[int]:
        if self.exact_end is not None:
            return self.exact_end
        if self.open_end is not None:
            return self.open_end + int(GRACE * self.rate)
        if self.approx_end is not None:
            return self.approx_end + int(MARGIN * self.rate)
        return None

    def how(self) -> Optional[str]:
        return ("exact" if self.exact_end is not None else "aligned" if self.open_end is not None
                or self.approx_end is not None else None)

    def song_seconds(self, frame: int) -> Optional[float]:
        """Where in the song the stream is at `frame`, once lined up (lag: the song's first frame)."""
        if self.lag is None:
            return None
        return (frame - self.lag) / self.rate

    def summary(self) -> dict:
        return {"how": self.how(), "stop_frame": self.stop_frame(), "lag": self.lag,
                "marks_found": len(self.found), "marks": len(self.marks)}

    # -- exact: marks -----------------------------------------------------------------

    def _scan(self, x: np.ndarray, start: int) -> None:
        buf = np.concatenate([self._carry, x]) if len(self._carry) else x
        base = start - len(self._carry)
        keys = _keys(buf)
        for m in self.marks:
            if m["start"] in self.found:
                continue
            for c in _find(keys, m["keys"]):
                if np.array_equal(buf[c:c + MARK], m["frames"]):
                    self.found.append(m["start"])
                    lag = base + int(c) - m["start"]
                    self.lag = lag
                    end = self.song.frames + lag
                    if m["closing"]:
                        self.exact_end = end
                    elif self.open_end is None:
                        self.open_end = end
                    break
        self._carry = buf[-(MARK - 1):].copy()

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
        # Keep the start of the music, to check and refine the alignment on the waveform.
        if self._head_start is None:  # (anything before this block was silence)
            self._head_start = max(start, self.first_sound - self.rate // 10)
        held = sum(len(h) for h in self._head)
        want = self.head_frames + self.rate - held
        lo = max(start, self._head_start + held)
        if want > 0 and lo < self.frames:
            self._head.append(x[lo - start:lo - start + want])
        if self.frames - self.first_sound >= self.head_frames:
            self._aligned = True
            self._align()

    def _align(self) -> None:
        head, self._head = self._head, []
        first_block = max(0, self.first_sound // self.block - 100)  # from a second before the music
        got = envelope_lag(self.song_env, np.asarray(self._env[first_block:]))
        if got is None or got[1] < MIN_SCORE or not head:
            return
        # Blocks are BLOCK_SECONDS long in both, so the song's first frame plays at about this
        # stream frame (to within a block):
        lag_frames = self._refine(np.concatenate(head), (got[0] + first_block) * self.block)
        if lag_frames is None:
            return  # the loudness fits, the waveform doesn't: not this song
        if self.lag is None:
            self.lag = lag_frames
        self.approx_end = lag_frames + int(round(self.song.frames / self.song.rate * self.rate))

    def _refine(self, head: np.ndarray, lag_frames: int) -> Optional[int]:
        """Check the alignment on the waveform, and make it exact to the sample (to the song's
        sample, when the rates differ). None unless the waveforms match, in the same place, at two
        separate points."""
        sr, r = self.song.rate, self.rate
        length = min(1 << 14, sr // 2)
        search = int(2 * BLOCK_SECONDS * sr) + 64
        # Song frames that the held stream covers, with room to search either side.
        first = (self._head_start - lag_frames) / r * sr
        lo = max(0, int(first) + search + 1)
        hi = min(self.song.frames, int(first + len(head) / r * sr) - search - 1) - length
        if hi - lo < 2 * length:
            spots = [self._loud_spot(lo, hi, length)]
        else:
            mid = (lo + hi) // 2
            spots = [self._loud_spot(lo, mid - length, length), self._loud_spot(mid, hi, length)]
        found = [self._match(head, lag_frames, rs, length, search) for rs in spots if rs is not None]
        if not found or None in found or max(found) - min(found) > max(2, r // sr * 2):
            return None
        return found[-1]

    def _match(self, head: np.ndarray, lag_frames: int, rs: int, length: int, search: int) -> Optional[int]:
        """The stream frame where the song's first frame plays, from the waveform at song frame rs."""
        sr, r, h0 = self.song.rate, self.rate, self._head_start
        if self.same_rate:
            cap = Audio(data=head, rate=r, bits=32, is_float=not np.issubdtype(head.dtype, np.integer))
            fine = _fine_lag(self.song, cap, rs, length, lag_frames - h0, search)
            return fine[0] + h0 if fine is not None and fine[1] >= MIN_WAVE else None
        # Another rate: resample the stream around the window onto the song's sample grid.
        grid = rs - search + np.arange(length + 2 * search)
        pos = grid / sr * r + lag_frames - h0  # where each song sample falls in the held stream
        if pos[0] < 0 or pos[-1] > len(head) - 1:
            return None
        xs = np.arange(len(head))
        seg = np.stack([np.interp(pos, xs, head[:, c].astype(np.float64)) for c in range(head.shape[1])], axis=1)
        fine = _fine_lag(self.song, Audio(data=seg, rate=sr, bits=32, is_float=True), rs, length, search - rs, search)
        if fine is None or fine[1] < MIN_WAVE:
            return None
        return int(round(lag_frames + (fine[0] - (search - rs)) / sr * r))

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
