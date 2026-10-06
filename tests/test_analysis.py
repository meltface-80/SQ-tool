import unittest

import numpy as np

from helpers import audio, music

from sqtool.analysis import analyze_file, compare, detect_dop, fingerprint, silence_bounds
from sqtool.report import comparison_lines
from sqtool.generate import make_test_signal
from sqtool.wavio import Audio

RATE = 44100
PAD = np.zeros((3000, 2), dtype=np.int32)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = music(rate=RATE, seconds=4, lead=1, tail=1)  # 16-bit values
        cls.ref = audio(cls.src)

    def check(self, cap_data, verdict, bits=32, ref=None, **kw):
        res = compare(ref or self.ref, Audio(cap_data.astype(np.int32), RATE, bits, **kw))
        self.assertEqual(res["verdict"], verdict, res.get("approx", {}).get("findings"))
        return res


class ExactComparison(Fixture):
    def test_identical_with_offset_and_padding(self):
        res = self.check(np.vstack([PAD, self.src, PAD]), "IDENTICAL")
        ex = res["exact"]
        self.assertEqual(ex["lag"], 3000)
        self.assertEqual(ex["events"], [])
        self.assertEqual(ex["exact_frames"], len(self.src))
        self.assertTrue(ex["cap_before_silent"] and ex["cap_after_silent"])

    def test_late_start_inside_leading_silence_is_still_bit_perfect(self):
        res = self.check(self.src[2000:], "IDENTICAL")
        self.assertEqual(res["exact"]["missing_start"], 2000)
        self.assertTrue(res["exact"]["missing_start_silent"])

    def test_capture_missing_music_at_either_end_is_partial(self):
        res = self.check(self.src[RATE + 500:], "PARTIAL")
        self.assertFalse(res["exact"]["missing_start_silent"])
        res = self.check(self.src[:3 * RATE], "PARTIAL")
        self.assertGreater(res["exact"]["missing_end"], 0)
        self.assertEqual(res["exact"]["exact_audio_fraction"], 1.0)

    def test_inserted_silence_is_reported_as_underrun(self):
        cap = np.vstack([PAD, self.src[:2 * RATE], np.zeros((2048, 2), np.int32), self.src[2 * RATE:]])
        ev = self.check(cap, "GAPS")["exact"]["events"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["kind"], ev[0]["ref"], ev[0]["cap_frames"]), ("inserted", 2 * RATE, 2048))
        self.assertTrue(ev[0]["cap_silent"])

    def test_dropped_frames(self):
        cap = np.vstack([PAD, self.src[:2 * RATE], self.src[2 * RATE + 1000:]])
        ev = self.check(cap, "GAPS")["exact"]["events"]
        self.assertEqual([(e["kind"], e["ref"], e["ref_frames"]) for e in ev], [("dropped", 2 * RATE, 1000)])

    def test_several_discontinuities(self):
        cap = np.vstack([self.src[:RATE + 10000], np.zeros((500, 2), np.int32),
                         self.src[RATE + 10000:3 * RATE], self.src[3 * RATE + 77:]])
        ev = self.check(cap, "GAPS")["exact"]["events"]
        self.assertEqual([e["kind"] for e in ev], ["inserted", "dropped"])

    def test_altered_samples(self):
        cap = np.vstack([PAD, self.src, PAD])
        cap[3000 + 3 * RATE:3000 + 3 * RATE + 10] += 1 << 16
        ev = self.check(cap, "ALTERED")["exact"]["events"]
        self.assertEqual([(e["kind"], e["ref"], e["ref_frames"]) for e in ev], [("altered", 3 * RATE, 10)])

    def test_float_capture_of_integer_values(self):
        cap = Audio((self.src / 2.0 ** 31).astype(np.float32), RATE, 32, is_float=True)
        self.assertEqual(compare(self.ref, cap)["verdict"], "IDENTICAL")

    def test_periodic_signal_aligns_on_its_onset(self):
        t = np.arange(2 * 48000)
        tone = np.round(np.sin(2 * np.pi * 1000 * t / 48000) * 16000).astype(np.int32) << 16
        src = np.zeros((3 * 48000, 2), np.int32)
        src[48000 // 2:48000 // 2 + len(t)] = tone[:, None]
        ref = Audio(src, 48000, 16)
        res = compare(ref, Audio(np.vstack([PAD, src]), 48000, 32))
        self.assertEqual(res["verdict"], "IDENTICAL")
        self.assertEqual(res["exact"]["lag"], 3000)


class ReviewRegressions(Fixture):
    """Cases found by an independent review of the comparison."""

    def test_capture_stopped_mid_track_with_silence_after(self):
        cap = np.vstack([PAD, self.src[:int(2.5 * RATE)], np.zeros((5 * RATE, 2), np.int32)])
        res = self.check(cap, "PARTIAL")
        ex = res["exact"]
        self.assertIsNone(ex["differs_from"])
        self.assertFalse(ex["missing_end_silent"])
        self.assertAlmostEqual(ex["identical_audio_fraction"], 1.5 / 4, places=3)
        self.assertNotIn("approx", res)

    def test_capture_stopped_with_a_fade_out(self):
        stop = int(2.5 * RATE)
        tail = self.src[stop:stop + 4410].astype(np.float64) * np.linspace(1, 0, 4410)[:, None]
        cap = np.vstack([PAD, self.src[:stop], (np.round(tail / 65536) * 65536).astype(np.int32),
                         np.zeros((RATE, 2), np.int32)])
        res = self.check(cap, "PARTIAL")
        self.assertEqual([e["kind"] for e in res["exact"]["events"]], ["ending"])
        self.assertIn("playback stopped", " ".join(comparison_lines(res)))

    def test_one_channel_inverted(self):
        cap = self.src.copy()
        cap[:, 1] = -cap[:, 1]
        res = self.check(np.vstack([PAD, cap]), "DIFFERENT")
        self.assertEqual(res["approx"]["lag"], 3000)
        f = " | ".join(res["approx"]["findings"])
        self.assertIn("polarity inverted on channel(s) 2", f)
        self.assertIn("every sample is exact", f)

    def test_repeats_are_measured(self):
        for n in (10, 500):
            cap = np.vstack([PAD, self.src[:2 * RATE], self.src[2 * RATE - n:2 * RATE], self.src[2 * RATE:]])
            res = self.check(cap, "GAPS")
            ev = res["exact"]["events"]
            self.assertEqual([(e["kind"], e["repeated_frames"]) for e in ev], [("repeated", n)])
            self.assertLessEqual(res["exact"]["exact_frames"], len(self.src))
            self.assertLessEqual(res["exact"]["identical_audio_fraction"], 1.0)

    def test_dropped_frames_are_not_counted_as_present(self):
        cap = np.vstack([PAD, self.src[:2 * RATE], self.src[2 * RATE + 4410:]])
        res = self.check(cap, "GAPS")
        self.assertAlmostEqual(res["exact"]["identical_audio_fraction"], 1 - 0.1 / 4, places=3)
        self.assertNotIn("100.00%", " ".join(comparison_lines(res)))

    def test_silent_reference_channel_is_not_a_reordering(self):
        src = self.src.copy()
        src[:, 1] = 0
        cap = np.round(src * 10 ** (-1 / 20) / 256) * 256
        res = self.check(cap, "DIFFERENT", ref=Audio(src, RATE, 16))
        f = " | ".join(res["approx"]["findings"])
        self.assertNotIn("reordered", f)
        self.assertIn("level changed: -1.000 dB", f)

    def test_float_note_only_when_identical(self):
        cap = (self.src / 2.0 ** 31).astype(np.float32)
        cap[3 * RATE:3 * RATE + 10] += np.float32(0.25)
        res = compare(self.ref, Audio(cap, RATE, 32, is_float=True))
        self.assertEqual(res["verdict"], "ALTERED")
        self.assertNotIn("floating point", " ".join(comparison_lines(res)))

    def test_long_inserted_silence_near_the_end(self):
        n = len(self.src)
        end_music = n - RATE  # one second of trailing silence in the reference
        cap = np.vstack([self.src[:end_music - 900], np.zeros((4603, 2), np.int32), self.src[end_music - 900:]])
        ev = self.check(cap, "GAPS")["exact"]["events"]
        self.assertEqual([(e["kind"], e["cap_frames"]) for e in ev], [("inserted", 4603)])


class ApproximateComparison(Fixture):
    def findings(self, res):
        return " | ".join(res["approx"]["findings"])

    def test_gain_with_rounding(self):
        cap = np.round(self.src * 10 ** (-3 / 20) / 256) * 256
        res = self.check(np.vstack([PAD, cap, PAD]), "DIFFERENT")
        self.assertAlmostEqual(res["approx"]["gain_db"][0], -3.0, places=3)
        f = self.findings(res)
        self.assertIn("level changed", f)
        self.assertIn("plain rounding to 24-bit", f)
        self.assertIn("digital silence stays digital silence", f)

    def test_gain_with_tpdf_dither(self):
        rng = np.random.default_rng(5)
        d = rng.random(self.src.shape) - rng.random(self.src.shape)
        cap = np.round(self.src * 10 ** (-1 / 20) / 256 + d) * 256
        f = self.findings(self.check(cap, "DIFFERENT"))
        self.assertIn("dither at 24-bit", f)
        self.assertIn("is not", f)  # noise where the reference is digital silence

    def test_truncation_to_16_bits(self):
        src24 = music(rate=RATE, seconds=4, bits=24)
        ref = Audio(src24, RATE, 24)
        f = self.findings(self.check(np.floor(src24 / 65536.0) * 65536, "DIFFERENT", ref=ref))
        self.assertIn("resolution reduced", f)
        self.assertIn("truncation", f)

    def test_polarity_and_swap(self):
        self.assertIn("polarity inverted", self.findings(self.check(-self.src, "DIFFERENT")))
        f = self.findings(self.check(self.src[:, ::-1].copy(), "DIFFERENT"))
        self.assertIn("swapped", f)
        self.assertIn("every sample is exact", f)

    def test_eq_is_heavy_processing(self):
        x = self.src.astype(np.float64)
        y = x.copy()
        y[1:] = 0.7 * x[1:] + 0.3 * x[:-1]
        f = self.findings(self.check(np.round(y / 256) * 256, "DIFFERENT"))
        self.assertIn("frequency response is not flat", f)
        self.assertNotIn("crossfeed", f)

    def test_crossfeed(self):
        x = self.src.astype(np.float64)
        y = x * 0.9 + x[:, ::-1] * 0.05
        f = self.findings(self.check(np.round(y / 256) * 256, "DIFFERENT"))
        self.assertIn("mixed into each other", f)

    def test_sample_rate_and_channel_changes(self):
        self.assertEqual(compare(self.ref, Audio(self.src, 48000, 16))["verdict"], "RESAMPLED")
        self.assertEqual(compare(self.ref, Audio(self.src[:, :1].copy(), RATE, 16))["verdict"], "CHANNELS")

    def test_unrelated_audio(self):
        other = music(rate=RATE, seconds=4, seed=99)
        res = compare(self.ref, Audio(other, RATE, 16))
        self.assertIn(res["verdict"], ("NO MATCH", "DIFFERENT"))
        self.assertNotEqual(res["verdict"], "IDENTICAL")


class FileAnalysis(unittest.TestCase):
    def test_padding_detected_and_fingerprint_ignores_container(self):
        src = music(seconds=1)
        a16 = analyze_file(Audio(src, RATE, 16))
        a32 = analyze_file(Audio(np.vstack([PAD, src]), RATE, 32))
        self.assertEqual(a32["resolution"], 16)
        self.assertEqual(a16["fingerprint"], a32["fingerprint"])
        f = Audio((src / 2.0 ** 31).astype(np.float32), RATE, 32, is_float=True)
        self.assertEqual(analyze_file(f)["fingerprint"], a16["fingerprint"])
        self.assertEqual(analyze_file(f)["resolution"], 16)

    def test_silence_and_levels(self):
        src = music(seconds=1, lead=0.5, tail=0.25)
        a = analyze_file(Audio(src, RATE, 16))
        self.assertEqual(a["lead_silence"], int(0.5 * RATE))
        self.assertEqual(a["trail_silence"], int(0.25 * RATE))
        self.assertAlmostEqual(a["channel_stats"][0]["rms_db"], -20, delta=0.3)
        self.assertEqual(silence_bounds(np.zeros((10, 2), np.int32)), (0, 0))

    def test_dop(self):
        n = 4096
        markers = np.where(np.arange(n) % 2 == 0, 0x05, 0xFA).astype(np.int64)
        payload = np.random.default_rng(1).integers(0, 65536, (n, 2))
        data = ((markers[:, None] << 24) | (payload << 8)).astype(np.uint32).view(np.int32)
        self.assertIn("DSD64", detect_dop(Audio(data, 176400, 24)))
        self.assertIsNone(detect_dop(Audio(music(seconds=0.2), 176400, 24)))

    def test_fingerprint_changes_with_one_lsb(self):
        src = music(seconds=0.5)
        a = Audio(src, RATE, 16)
        b = Audio(src.copy(), RATE, 16)
        b.data[RATE // 2 + 100, 0] += 1 << 16
        self.assertNotEqual(fingerprint(a, 0, a.frames), fingerprint(b, 0, b.frames))


class GeneratedSignal(unittest.TestCase):
    def test_structure(self):
        for bits in (16, 24):
            a = make_test_signal(44100, bits)
            self.assertEqual(a.frames, int(15.5 * 44100))
            self.assertFalse(a.data[:2 * 44100].any())  # leading digital silence
            self.assertFalse(a.data[-2 * 44100:].any())
            self.assertEqual(analyze_file(a)["resolution"], bits)
        np.testing.assert_array_equal(make_test_signal(48000, 16).data, make_test_signal(48000, 16).data)

    def test_generated_track_compares_to_itself(self):
        a = make_test_signal(96000, 24)
        cap = Audio(np.vstack([np.zeros((777, 2), np.int32), a.data]), 96000, 32)
        res = compare(a, cap)
        self.assertEqual(res["verdict"], "IDENTICAL")
        self.assertEqual(res["exact"]["lag"], 777)


if __name__ == "__main__":
    unittest.main()
