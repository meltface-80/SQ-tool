"""Finding the end of the song in a stream as it is recorded (sqtool.songend)."""

import unittest

import numpy as np

from fake_server import demo_song, volume_with_dither

from sqtool.songend import LATE, MARGIN, SongEnd, envelope, last_notes, song_index
from sqtool.wavio import Audio

R = 44100


def zeros(seconds, rate=R):
    return np.zeros((int(seconds * rate), 2), np.int32)


class SongEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.song = Audio(data=demo_song(R, 16, seconds=30.0, seed=3), rate=R, bits=16)
        cls.next = demo_song(48000, 16, seconds=10.0, seed=9)  # the next track in the player's queue
        cls.index = song_index(cls.song)
        cls.env = envelope(cls.song)

    def follow(self, stream, rate=R, chunk=8192):
        """Feed the stream as a recorder would, stopping where the tracker says (a recorder cuts back
        to a stop behind the stream)."""
        tracker = SongEnd(self.song, self.index, self.env, rate, 2, 32, False)
        for a in range(0, len(stream), chunk):
            stop = tracker.feed(stream[a:a + chunk].astype("<i4").tobytes())
            if stop is not None and stop < a + chunk:
                return stop, tracker
        return None, tracker

    def check(self, stream, expected, how="exact", jumps=None):
        stop, tracker = self.follow(stream)
        self.assertEqual((stop, tracker.how()), (expected, how))
        if jumps is not None:
            self.assertEqual(tracker.jumps, jumps)

    def test_bit_perfect_into_the_next_track(self):
        lead = zeros(0.2)
        self.check(np.concatenate([lead, self.song.data, self.next]), len(lead) + self.song.frames, jumps=0)

    def test_dropouts_and_repeats_move_the_end(self):
        d, n = self.song.data, self.song.frames
        gap = zeros(0.03)
        for at in (10 * R, n - 6 * R // 10):  # in the middle, and just before the end
            self.check(np.concatenate([d[:at], gap, d[at:], self.next]), n + len(gap), jumps=1)
        rep = R // 20  # 50 ms played twice
        self.check(np.concatenate([d[:20 * R], d[20 * R - rep:20 * R], d[20 * R:], self.next]), n + rep, jumps=1)
        self.check(np.concatenate([d[:15 * R], zeros(3), d[15 * R:], self.next]), n + 3 * R, jumps=1)  # a pause

    def test_seeks_and_restarts(self):
        d, n = self.song.data, self.song.frames
        self.check(np.concatenate([d[:10 * R], d, self.next]), 10 * R + n)  # started again from the top
        # Back, after the end was known:
        self.check(np.concatenate([d[:25 * R], d[5 * R:], self.next]), 25 * R + n - 5 * R)
        self.check(np.concatenate([d[:5 * R], d[20 * R:], self.next]), 5 * R + n - 20 * R)  # forward

    def test_dropout_at_the_end(self):
        # The song never comes back: the recording is cut back to where it should have ended.
        d, n = self.song.data, self.song.frames
        stream = np.concatenate([d[:n - 3 * R // 2], zeros(10)])
        stop, tracker = self.follow(stream)
        self.assertEqual((stop, tracker.how()), (n, "exact"))
        self.assertGreaterEqual(tracker.frames, n + int(LATE * R))  # after waiting for it

    def test_dropout_in_dither(self):
        # A song that starts and ends with +-1 LSB of dither: those few values are everywhere, so
        # after a dropout in the end the stream is found where the song left off.
        rng = np.random.default_rng(2)

        def hiss(n):
            lsb = np.round(rng.random((n, 2)) - rng.random((n, 2))).astype(np.int64)
            return (lsb << 16).astype(np.int32)
        d = np.concatenate([hiss(10 * R), self.song.data[R:-R], hiss(5 * R)])
        song = Audio(data=d, rate=R, bits=16)
        at, gap = len(d) - 2 * R, zeros(0.3)
        tracker = SongEnd(song, song_index(song), envelope(song), R, 2, 32, False)
        stream = np.concatenate([d[:at], gap, d[at:], self.next])
        for a in range(0, len(stream), 8192):
            stop = tracker.feed(stream[a:a + 8192].astype("<i4").tobytes())
            if stop is not None and stop < a + 8192:
                break
        self.assertEqual((stop, tracker.how(), tracker.jumps), (len(d) + len(gap), "exact", 1))

    def test_silence_first_late_start_and_crossfade(self):
        d, n = self.song.data, self.song.frames
        self.check(np.concatenate([zeros(45), d, self.next]), 45 * R + n)  # Squeezelite's open output
        self.check(np.concatenate([d[5 * R:], self.next]), n - 5 * R)  # recording began 5 s in
        xf = 2 * R  # the player crossfades into the next track: stop at the song's own end
        fade = np.linspace(1, 0, xf)[:, None]
        mix = (d[n - xf:] * fade + self.next[:xf] * (1 - fade)).astype(np.int32)
        self.check(np.concatenate([d[:n - xf], mix, self.next[xf:]]), n)

    def test_last_notes(self):
        start, n = last_notes(self.song, self.env)
        music_end = np.flatnonzero(self.song.data.any(axis=1))[-1] + 1
        self.assertTrue(start < music_end - 1.4 * R and music_end <= start + n <= self.song.frames)
        self.assertTrue(np.abs(self.song.data[start:start + n]).max() > 0)

    def test_volume_change(self):
        # Not bit-perfect: lined up by loudness and on the waveform, then the song's last notes are
        # found on the waveform, so it stops at the song's end.
        lead, d, n = zeros(0.2), self.song.data, self.song.frames
        vol = lambda x, seed=7: volume_with_dither(x, seed=seed)  # noqa: E731
        self.check(np.concatenate([lead, vol(d), vol(self.next, 5)]), len(lead) + n, how="aligned")
        # Dropouts delay the end: found where the last notes turn up.
        for at, gap in ((20 * R, zeros(1.0)), (n - 3 * R, zeros(3.0))):
            stream = np.concatenate([lead, vol(d[:at]), gap, vol(d[at:]), vol(self.next, 5)])
            self.check(stream, len(lead) + n + len(gap), how="aligned")
        # One in the first stretch the loudness is lined up on: lined up again on a later stretch.
        gap = zeros(0.3)
        stream = np.concatenate([lead, vol(d[:10 * R]), gap, vol(d[10 * R:]), vol(self.next, 5)])
        self.check(stream, len(lead) + n + len(gap), how="aligned")

    def test_volume_change_repeated_parts_and_a_late_start(self):
        # The same part over and over (copied, sample for sample), recorded from 6 s in.
        part = lambda seed, s: demo_song(R, 16, seconds=s, seed=seed)[int(0.3 * R):-int(0.5 * R)]  # noqa: E731
        a, b = part(31, 8.0), part(32, 6.0)
        d = np.concatenate([zeros(0.3), a, b, a, b, a, a, zeros(0.5)])
        song = Audio(data=d, rate=R, bits=16)
        tracker = SongEnd(song, song_index(song), envelope(song), R, 2, 32, False)
        stream = np.concatenate([zeros(0.2), volume_with_dither(d[6 * R:]), volume_with_dither(self.next, seed=5)])
        for at in range(0, len(stream), 8192):
            stop = tracker.feed(stream[at:at + 8192].astype("<i4").tobytes())
            if stop is not None and stop < at + 8192:
                break
        self.assertEqual((stop, tracker.how()), (int(0.2 * R) + len(d) - 6 * R, "aligned"))

    def test_volume_change_and_crossfade(self):
        lead, d, n = zeros(0.2), self.song.data, self.song.frames
        xf = 2 * R
        fade = np.linspace(1, 0, xf)[:, None]
        mix = (d[n - xf:] * fade + self.next[:xf] * (1 - fade)).astype(np.int32)
        stream = volume_with_dither(np.concatenate([lead, d[:n - xf], mix, self.next[xf:]]))
        self.check(stream, len(lead) + n, how="aligned")  # the last notes show through the crossfade
        # Mixed in too deep to recognise: the end is an estimate, with a margin.
        mix = (d[n - xf:] * 0.2 + self.next[:xf]).astype(np.int32)
        stream = volume_with_dither(np.concatenate([lead, d[:n - xf], mix, self.next[xf:]]))
        self.check(stream, len(lead) + n + int(MARGIN * R), how="estimated")

    def test_upsampled(self):
        lead = int(0.2 * R)
        stream = np.repeat(np.concatenate([zeros(0.2), self.song.data, self.next]), 2, axis=0)
        stop, tracker = self.follow(stream, rate=2 * R)
        self.assertLessEqual(abs(stop - 2 * (lead + self.song.frames)), 2)
        self.assertEqual(tracker.how(), "aligned")
        self.assertAlmostEqual(tracker.song_seconds(2 * lead + 2 * R), 1.0, places=4)

    def test_a_different_song_is_not_placed(self):
        # Same beat, chords and kick drum, other notes; then another tempo and pitch altogether.
        for other in (demo_song(R, 16, seconds=40.0, seed=21), demo_song(48000, 16, seconds=40.0, seed=21)):
            stop, tracker = self.follow(np.concatenate([zeros(0.2), other]))
            self.assertEqual((stop, tracker.how()), (None, None))


if __name__ == "__main__":
    unittest.main()
