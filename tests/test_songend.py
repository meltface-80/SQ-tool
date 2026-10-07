"""Finding the end of the song in a stream as it is recorded (sqtool.songend)."""

import unittest

import numpy as np

from fake_server import demo_song, volume_with_dither

from sqtool.songend import MARGIN, SongEnd, envelope, pick_marks
from sqtool.wavio import Audio

R = 44100


def zeros(seconds, rate=R):
    return np.zeros((int(seconds * rate), 2), np.int32)


class SongEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.song = Audio(data=demo_song(R, 16, seconds=30.0, seed=3), rate=R, bits=16)
        cls.next = demo_song(R, 16, seconds=10.0, seed=9)  # the next track in the player's queue
        cls.marks = pick_marks(cls.song)
        cls.env = envelope(cls.song)

    def follow(self, stream, rate=R, chunk=8192):
        """Feed the stream as a recorder would, stopping where the tracker says."""
        tracker = SongEnd(self.song, self.marks, self.env, rate, 2, 32, False)
        for a in range(0, len(stream), chunk):
            stop = tracker.feed(stream[a:a + chunk].astype("<i4").tobytes())
            if stop is not None and stop < a + chunk:
                return stop, tracker
        return None, tracker

    def test_marks(self):
        starts = [m["start"] for m in self.marks]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual([m["closing"] for m in self.marks][:2], [False, False])
        self.assertTrue(sum(m["closing"] for m in self.marks) >= 3)
        last = self.marks[-1]
        self.assertTrue(last["closing"] and last["start"] > self.song.frames - R)  # within the last second

    def test_bit_perfect_into_the_next_track(self):
        lead = zeros(0.2)
        stop, tracker = self.follow(np.concatenate([lead, self.song.data, self.next]))
        self.assertEqual(stop, len(lead) + self.song.frames)  # exactly the song's last sample
        self.assertEqual(tracker.how(), "exact")

    def test_dropout_moves_the_end(self):
        at, gap = 10 * R, zeros(0.03)
        stream = np.concatenate([zeros(0.2), self.song.data[:at], gap, self.song.data[at:], self.next])
        stop, tracker = self.follow(stream)
        self.assertEqual(stop, int(0.2 * R) + self.song.frames + len(gap))

    def test_long_silence_first_and_late_start(self):
        stop, _ = self.follow(np.concatenate([zeros(45), self.song.data, self.next]))  # Squeezelite
        self.assertEqual(stop, 45 * R + self.song.frames)
        stop, _ = self.follow(np.concatenate([self.song.data[5 * R:], self.next]))  # recording began 5 s in
        self.assertEqual(stop, self.song.frames - 5 * R)

    def test_volume_change(self):
        lead = zeros(0.2)
        stream = np.concatenate([lead, volume_with_dither(self.song.data), volume_with_dither(self.next, seed=5)])
        stop, tracker = self.follow(stream)
        self.assertEqual(tracker.how(), "aligned")
        # Lined up to the sample by correlation; a short margin past it, as it isn't exact.
        self.assertEqual(stop, len(lead) + self.song.frames + int(MARGIN * R))

    def test_upsampled(self):
        stream = np.repeat(np.concatenate([zeros(0.2), self.song.data, self.next]), 2, axis=0)
        stop, tracker = self.follow(stream, rate=2 * R)
        expected = 2 * (int(0.2 * R) + self.song.frames) + int(MARGIN * 2 * R)
        self.assertLessEqual(abs(stop - expected), int(0.01 * 2 * R))  # within 10 ms
        self.assertAlmostEqual(tracker.song_seconds(2 * int(0.2 * R) + 2 * R), 1.0, places=2)

    def test_a_different_song_is_not_placed(self):
        # Another song: the demo song made at another rate plays slower and lower when sent at R.
        other = demo_song(48000, 16, seconds=40.0, seed=21)
        stop, tracker = self.follow(np.concatenate([zeros(0.2), other]))
        self.assertIsNone(stop)
        self.assertIsNone(tracker.how())


if __name__ == "__main__":
    unittest.main()
