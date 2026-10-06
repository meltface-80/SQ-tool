import os
import struct
import tempfile
import unittest

import numpy as np

from helpers import alsa_bytes, music

from sqtool.wavio import (ALSA_FORMATS, Audio, AudioFileError, WavWriter, load_audio, read_wav,
                          write_wav)


class WavRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)

    def test_integer_depths_and_channel_counts(self):
        for bits in (8, 16, 24, 32):
            for channels in (1, 2, 6):
                data = music(seconds=0.2, lead=0.01, tail=0.01, bits=bits, channels=channels)
                write_wav(self.path("x.wav"), Audio(data, 48000, bits))
                back = read_wav(self.path("x.wav"))
                self.assertEqual((back.rate, back.bits, back.channels), (48000, bits, channels))
                self.assertFalse(back.is_float)
                np.testing.assert_array_equal(back.data, data)

    def test_float(self):
        for bits, dtype in ((32, np.float32), (64, np.float64)):
            data = (np.random.default_rng(3).standard_normal((1000, 2)) * 0.1).astype(dtype)
            write_wav(self.path("f.wav"), Audio(data, 96000, bits, is_float=True))
            back = read_wav(self.path("f.wav"))
            self.assertTrue(back.is_float)
            self.assertEqual(back.bits, bits)
            np.testing.assert_array_equal(back.data, data)

    def test_rf64(self):
        data = music(seconds=0.1, lead=0, tail=0, bits=24)
        with WavWriter(self.path("big.wav"), 44100, 2, 24) as w:
            w.write_array(data, 24)
            w.close(force_rf64=True)
        with open(self.path("big.wav"), "rb") as f:
            self.assertEqual(f.read(4), b"RF64")
        np.testing.assert_array_equal(read_wav(self.path("big.wav")).data, data)

    def test_truncated_file_is_read_up_to_the_last_whole_frame(self):
        data = music(seconds=0.1, lead=0, tail=0)
        write_wav(self.path("t.wav"), Audio(data, 44100, 16))
        with open(self.path("t.wav"), "rb") as f:
            raw = f.read()
        back = read_wav(raw[:-3])
        np.testing.assert_array_equal(back.data, data[:-1])

    def test_extra_chunks_and_bytes_input(self):
        data = music(seconds=0.05, lead=0, tail=0)
        write_wav(self.path("c.wav"), Audio(data, 44100, 16),
                  extra_chunks=[(b"LIST", b"INFOINAM\x04\x00\x00\x00abc\x00")])
        with open(self.path("c.wav"), "rb") as f:
            np.testing.assert_array_equal(read_wav(f.read()).data, data)

    def test_rejects_non_wav(self):
        with open(self.path("n.wav"), "wb") as f:
            f.write(b"not a wav file at all")
        with self.assertRaises(AudioFileError):
            read_wav(self.path("n.wav"))

    def test_sidecar_metadata_sets_label(self):
        data = music(seconds=0.05, lead=0, tail=0)
        write_wav(self.path("cap.wav"), Audio(data, 44100, 32))
        with open(self.path("cap.wav.json"), "w") as f:
            f.write('{"capture": {"alsa_format": "S32_LE"}}')
        back = read_wav(self.path("cap.wav"))
        self.assertTrue(back.is_capture)
        self.assertEqual(back.label, "captured ALSA S32_LE")

    @unittest.skipUnless(os.path.exists("/usr/bin/ffmpeg") or os.path.exists("/usr/local/bin/ffmpeg"),
                         "ffmpeg not installed")
    def test_flac_through_ffmpeg_is_exact(self):
        import subprocess
        for bits in (16, 24):
            data = music(seconds=0.3, lead=0.01, tail=0.01, bits=bits)
            write_wav(self.path("s.wav"), Audio(data, 44100, bits))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", self.path("s.wav"),
                            self.path("s.flac")], check=True)
            back = load_audio(self.path("s.flac"))
            self.assertEqual((back.rate, back.bits), (44100, bits))
            np.testing.assert_array_equal(back.data, data)


class AlsaFormats(unittest.TestCase):
    def test_every_format_converts_to_the_same_values(self):
        data = music(seconds=0.05, lead=0.001, tail=0.001, bits=24)
        with tempfile.TemporaryDirectory() as d:
            for name in ("S24_3LE", "S24_LE", "S32_LE", "S32_BE", "FLOAT_LE"):
                fmt = ALSA_FORMATS[name]
                raw = alsa_bytes(data, name)
                payload = fmt.convert(raw) if fmt.convert else raw
                path = os.path.join(d, name + ".wav")
                with WavWriter(path, 44100, 2, fmt.wav_bits, fmt.is_float) as w:
                    w.write(payload)
                back = read_wav(path)
                values = back.data.astype(np.float64) * (2.0 ** 31 if back.is_float else 1)
                np.testing.assert_array_equal(values, data.astype(np.float64), err_msg=name)

    def test_big_endian_swaps(self):
        x = np.array([[1 << 16, -(1 << 16)], [0x7FFF0000, -0x80000000]], dtype=np.int32)
        for name, raw in (("S16_BE", (x >> 16).astype(">i2").tobytes()),
                          ("S24_3BE", (x >> 8).astype(">i4").view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()),
                          ("S24_BE", (x >> 8).astype(">i4").tobytes())):
            fmt = ALSA_FORMATS[name]
            payload = fmt.convert(raw)
            width = fmt.wav_bits // 8
            if width == 2:
                got = np.frombuffer(payload, "<i2").astype(np.int32) << 16
            else:
                b = np.frombuffer(payload, np.uint8).reshape(-1, 3)
                got = (b[:, 0].astype(np.int32) << 8) | (b[:, 1].astype(np.int32) << 16) | \
                    (b[:, 2].astype(np.int32) << 24)
            np.testing.assert_array_equal(got.reshape(x.shape), x, err_msg=name)

    def test_wav_header_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.wav")
            with WavWriter(path, 192000, 2, 24) as w:
                w.write(b"\0" * 12)
            with open(path, "rb") as f:
                raw = f.read()
            self.assertEqual(raw[:4], b"RIFF")
            self.assertEqual(struct.unpack("<I", raw[4:8])[0], len(raw) - 8)
            i = raw.index(b"data")
            self.assertEqual(struct.unpack("<I", raw[i + 4:i + 8])[0], 12)


if __name__ == "__main__":
    unittest.main()
