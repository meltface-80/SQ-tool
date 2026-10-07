"""Test sessions end to end: a song from the music folder, two simulated players on the loopback card."""

import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import zlib

import numpy as np

from helpers import FAKE_ARECORD, FakeLoopback, alsa_bytes, music

from sqtool.alsa import CaptureError
from sqtool.sessions import Recorder, Tests, _steps, longest_silence, readable_formats, resolve_device
from sqtool.spectrogram import LUT
from sqtool.wavio import Audio, info_chunk, read_wav, write_wav

RATE = 44100


def png_pixels(png: bytes) -> np.ndarray:
    """Decode the 8-bit RGB, filter-0 PNGs that sq-tool writes."""
    w, h = struct.unpack(">II", png[16:24])
    pos, idat = 8, b""
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        if png[pos + 4:pos + 8] == b"IDAT":
            idat += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    rows = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, 1 + 3 * w)
    return rows[:, 1:].reshape(h, w, 3)


def is_black(png: bytes) -> bool:
    return bool((png_pixels(png) == LUT[0]).all())


def volume_with_dither(data, db=-0.5, seed=7):
    """What a player with digital volume does: scale, add TPDF dither, round to 24 bits."""
    rng = np.random.default_rng(seed)
    x = data.astype(np.float64) * 10 ** (db / 20) / 256
    x += rng.random(x.shape) - rng.random(x.shape)
    return (np.round(x) * 256).astype(np.int32)


def named_process(tmp, name):
    """A process listed under `name`, standing in for a player program."""
    exe = os.path.join(tmp, name)
    if not os.path.exists(exe):
        shutil.copy("/bin/sleep", exe)
    return subprocess.Popen([exe, "1000"])


def wait_for(fn, timeout=60.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting for " + what)


class SessionBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sq-sessions-")
        cls.fake = FakeLoopback(os.path.join(cls.tmp, "asound"))
        cls.old_env = dict(os.environ)
        os.environ.update({"SQTOOL_PROC_ASOUND": cls.fake.root,
                           "FAKE_ARGS_LOG": os.path.join(cls.tmp, "arecord.log"),
                           "FAKE_CLOSE_DIR": cls.fake.subdir(0, "p", 0)})
        cls.music_dir = os.path.join(cls.tmp, "music")
        album = os.path.join(cls.music_dir, "Artist", "Album")
        os.makedirs(album)
        cls.src = music(rate=RATE, seconds=4.0, lead=0.5, tail=0.5, bits=16)
        cls.song = os.path.join(album, "01 Song.wav")
        tags = info_chunk({"INAM": "Song", "IART": "Artist", "IPRD": "Album"})
        write_wav(cls.song, Audio(data=cls.src, rate=RATE, bits=16), extra_chunks=[(b"LIST", tags)])
        with open(os.path.join(album, "cover.jpg"), "wb") as f:
            f.write(b"\xff\xd8")
        cls.tests = Tests(os.path.join(cls.tmp, "data"), cls.music_dir, log=lambda m: None)
        cls.rec = Recorder(cls.tests, log=lambda m: None, arecord=FAKE_ARECORD)

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls.old_env)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def new_test(self):
        tid = self.tests.create(music_rel="Artist/Album/01 Song.wav")
        wait_for(lambda: self.tests.get(tid)["source"]["state"] in ("ready", "error"), what="the source")
        self.assertEqual(self.tests.get(tid)["source"]["state"], "ready", self.tests.get(tid)["source"])
        return tid

    def add(self, tid, slot, data, rate=RATE, bits=32, meta=None):
        """File a recording as if the recorder had made it."""
        d = tempfile.mkdtemp(dir=self.tests.tmp)
        path = os.path.join(d, slot + ".wav")
        write_wav(path, Audio(data=data, rate=rate, bits=bits))
        with open(path + ".json", "w") as f:
            json.dump({"capture": dict({"alsa_format": "S32_LE", "rate": rate, "channels": data.shape[1]},
                                       **(meta or {}))}, f)
        self.tests.add_capture(tid, slot, path)

    def results(self, tid, keys):
        def ready():
            t = self.tests.get(tid)
            states = [(t["results"].get(k) or {}).get("state") for k in keys]
            caps = [c.get("state") for c in t["captures"].values()]
            if "error" in caps:
                raise AssertionError(t["captures"])
            return t if all(s == "ready" for s in states + caps) else None
        return wait_for(ready, what="results " + ",".join(keys))


class MusicFolder(SessionBase):
    def test_browse_and_search(self):
        root = self.tests.browse("")
        self.assertEqual((root["path"], root["dirs"], root["files"], root["available"]), ("", ["Artist"], [], True))
        album = self.tests.browse("Artist/Album")
        self.assertEqual([f["name"] for f in album["files"]], ["01 Song.wav"])  # not cover.jpg
        self.assertEqual(self.tests.search("song artist"),
                         [{"path": "Artist/Album/01 Song.wav", "name": "01 Song.wav", "folder": "Artist/Album",
                           "ok": True}])
        self.assertEqual(self.tests.search("nothing like this"), [])
        with self.assertRaises(ValueError):
            self.tests.browse("../..")
        with self.assertRaises(KeyError):
            self.tests.browse("Nope")
        with self.assertRaises(ValueError):
            self.tests.create(music_rel="../data/settings.json")

    def test_formats_that_cannot_be_read_here(self):
        from unittest import mock
        path = os.path.join(self.music_dir, "Artist", "Album", "02 Other.m4a")
        with open(path, "wb") as f:
            f.write(b"\0" * 64)
        self.tests._index = None  # searched before this file existed
        try:
            files = {f["name"]: f["ok"] for f in self.tests.browse("Artist/Album")["files"]}
            self.assertEqual(files, {"01 Song.wav": True, "02 Other.m4a": ".m4a" in readable_formats()})
            with mock.patch("shutil.which", return_value=None):  # no ffmpeg, no flac
                self.assertEqual(readable_formats(), {".wav", ".wave", ".aif", ".aiff", ".aifc"})
                self.assertFalse(self.tests.search("other")[0]["ok"])
                with self.assertRaises(ValueError):
                    self.tests.create(music_rel="Artist/Album/02 Other.m4a")
        finally:
            os.unlink(path)

    def test_settings(self):
        s = self.tests.save_settings({"players": {"a": "Roon 2", "b": ""}, "idle_stop": "8"})
        self.assertEqual(s["players"], {"a": "Roon 2", "b": "Mandarin"})
        self.assertEqual(s["idle_stop"], 8.0)
        with self.assertRaises(ValueError):
            self.tests.save_settings({"device": "spdif:9"})
        self.tests.save_settings({"players": {"a": "Roon"}, "idle_stop": 5})


class Recording(SessionBase):
    def record(self, tid, slot, data, fmt, lead_frames=0, sub=0, pid=None, meanwhile=None):
        """Record `slot` while a simulated player plays `data` to the loopback card in format `fmt`
        (`meanwhile` runs while the recording waits for it, which it must go on doing)."""
        pcm = os.path.join(self.tmp, "%s.pcm" % slot)
        payload = np.concatenate([np.zeros((lead_frames, data.shape[1]), np.int32), data])
        with open(pcm, "wb") as f:
            f.write(alsa_bytes(payload, fmt))
        os.environ["FAKE_PCM"] = pcm
        os.environ["FAKE_FRAME_BYTES"] = str({"S32_LE": 8, "S24_3LE": 6, "S16_LE": 4}[fmt])
        st = self.rec.start(tid, slot)
        self.assertEqual(st["device"], "loopback:1")
        self.assertAlmostEqual(st["expected_seconds"], 5.0, places=3)
        time.sleep(0.2)
        self.assertEqual(self.rec.status()["state"], "waiting")
        if meanwhile:
            meanwhile()
            time.sleep(0.3)
            self.assertEqual(self.rec.status()["state"], "waiting")
        self.fake.play(fmt, RATE, 2, sub=sub, pid=pid)
        wait_for(lambda: not self.rec.active(), what="the recording")
        st = self.rec.status()
        self.assertTrue(st["saved"], st)
        self.assertIsNone(st["error"])
        return st

    def test_roon_and_mandarin(self):
        tid = self.new_test()
        t = self.tests.get(tid)
        self.assertEqual((t["title"], t["artist"], t["album"]), ("Song", "Artist", "Album"))
        self.assertEqual((t["source"]["rate"], t["source"]["bits"], t["source"]["label"]), (RATE, 16, "WAV PCM 16-bit"))
        self.assertEqual(t["players"], {"a": "Roon", "b": "Mandarin"})

        # Roon: bit-perfect, 16-bit samples padded into 32-bit words, after 0.2 s of silence.
        self.record(tid, "a", self.src, "S32_LE", lead_frames=int(0.2 * RATE))
        t = self.results(tid, ["a"])
        cap = t["captures"]["a"]
        self.assertEqual((cap["format"], cap["rate"], cap["method"], cap["resolution"]), ("S32_LE", RATE, "loopback", 16))
        self.assertEqual(cap["stop_reason"], "reached the end of the song")
        self.assertEqual(cap["fingerprint"], t["source"]["fingerprint"])
        res = t["results"]["a"]
        self.assertEqual((res["verdict"], res["short"], res["match"]), ("IDENTICAL", "BIT-PERFECT", True))
        self.assertEqual(res["alignment"]["steps"], [[0, int(0.2 * RATE)]])
        self.assertIsNone(res["zoom"])

        # Mandarin: -0.5 dB of digital volume with dither, sent as packed 24-bit.
        self.record(tid, "b", volume_with_dither(self.src), "S24_3LE")
        t = self.results(tid, ["a", "b", "ab"])
        self.assertEqual(t["captures"]["b"]["format"], "S24_3LE")
        rb, rab = t["results"]["b"], t["results"]["ab"]
        self.assertEqual((rb["verdict"], rb["match"]), ("DIFFERENT", False))
        np.testing.assert_allclose(rb["gain_db"], [-0.5, -0.5], atol=0.005)
        self.assertEqual(rab["verdict"], "DIFFERENT")
        self.assertTrue(rb["plots"]["envelope"]["t"] and rb["plots"]["spectrum"]["freqs"])
        self.assertTrue(rb["zoom"]["ref"] and rb["zoom"]["cap"])
        self.assertEqual(sorted(t["analysis"]), ["a", "b", "source"])

        # Spectrograms on the source's timeline: Roon's difference is digital silence.
        png, info = self.tests.spectrogram(tid, "source", 0, 0, 600, 200)
        self.assertEqual(png_pixels(png).shape, (200, 600, 3))
        self.assertAlmostEqual(info["t1"], 5.0, places=2)
        self.assertFalse(is_black(png))
        self.assertTrue(is_black(self.tests.spectrogram(tid, "diff-a", 0, 0, 600, 200)[0]))
        self.assertFalse(is_black(self.tests.spectrogram(tid, "diff-b", 0, 0, 600, 200)[0]))
        self.assertFalse(is_black(self.tests.spectrogram(tid, "diff-ab", 1.0, 2.0, 300, 100, "linear", -180)[0]))
        # Matching Mandarin's level leaves only the dither: far quieter than the raw difference.
        raw = png_pixels(self.tests.spectrogram(tid, "diff-b", 0, 0, 600, 200)[0]).astype(int).sum()
        matched = png_pixels(self.tests.spectrogram(tid, "diff-b", 0, 0, 600, 200, matched=True)[0]).astype(int).sum()
        self.assertLess(matched, raw * 0.8)
        cached = self.tests.spectrogram(tid, "source", 0, 0, 600, 200)
        self.assertEqual(cached[0], png)

        # Difference files: silence for Roon, the volume change for Mandarin.
        chunks, size, name = self.tests.difference_wav(tid, "a")
        wav = b"".join(chunks)
        self.assertEqual(len(wav), size)
        self.assertEqual(name, "Song - Roon minus the source.wav")
        diff = read_wav(wav)
        self.assertEqual((diff.frames, diff.is_float), (len(self.src), True))
        self.assertFalse(diff.data.any())
        diff_b = read_wav(b"".join(self.tests.difference_wav(tid, "ab")[0]))
        self.assertTrue(diff_b.data.any())

        roon = read_wav(self.tests.audio_path(tid, "a")).data
        lead = int(0.2 * RATE)
        self.assertEqual(len(roon), lead + len(self.src))  # ends with the song's last sample
        self.assertTrue(np.array_equal(roon[lead:], self.src))
        self.assertFalse(roon[:lead].any())
        self.assertEqual(self.tests.audio_filename(tid, "b"), "Song - Mandarin.wav")
        listed = [x for x in self.tests.list() if x["id"] == tid][0]
        self.assertEqual(listed["results"]["a"]["short"], "BIT-PERFECT")
        self.tests.set_players(tid, {"a": "Roon 2.0"})
        self.assertEqual(self.tests.get(tid)["players"]["a"], "Roon 2.0")

        # Recording Roon again replaces its results.
        self.record(tid, "a", self.src, "S16_LE")
        t = self.results(tid, ["a", "ab"])
        self.assertEqual((t["captures"]["a"]["format"], t["results"]["a"]["short"]), ("S16_LE", "BIT-PERFECT"))
        self.assertEqual(os.listdir(self.tests.tmp), [])

        self.tests.delete(tid)
        with self.assertRaises(KeyError):
            self.tests.get(tid)

    def test_silent_passage_does_not_end_the_recording(self):
        # 6 s of digital silence inside the song: longer than the 5 s silence stop.
        a = music(rate=RATE, seconds=1.0, lead=0.5, tail=0.0, bits=16, seed=2)
        b = music(rate=RATE, seconds=1.0, lead=6.0, tail=0.5, bits=16, seed=3)
        song = np.concatenate([a, b])
        self.assertAlmostEqual(longest_silence(Audio(data=song, rate=RATE, bits=16)), 6.0, places=4)
        path = os.path.join(self.music_dir, "Artist", "Album", "02 Gap.wav")
        write_wav(path, Audio(data=song, rate=RATE, bits=16))
        tid = self.tests.create(music_rel="Artist/Album/02 Gap.wav")
        wait_for(lambda: self.tests.get(tid)["source"]["state"] == "ready", what="the source")
        self.assertEqual(self.tests.get(tid)["source"]["longest_silence"], 6.0)
        pcm = os.path.join(self.tmp, "gap.pcm")
        with open(pcm, "wb") as f:
            f.write(alsa_bytes(song, "S32_LE"))
        os.environ.update({"FAKE_PCM": pcm, "FAKE_FRAME_BYTES": "8"})
        self.rec.start(tid, "a")
        time.sleep(0.2)
        self.fake.play("S32_LE", RATE, 2)
        wait_for(lambda: not self.rec.active(), what="the recording")
        t = self.results(tid, ["a"])
        self.assertEqual(t["captures"]["a"]["stop_reason"], "reached the end of the song")
        self.assertEqual(t["results"]["a"]["verdict"], "IDENTICAL")
        self.tests.delete(tid)

    def test_stops_at_the_end_of_the_song_before_the_next_track(self):
        # Players go straight on to the next track in their queue: the recording still ends with
        # the song's last sample.
        tid = self.new_test()
        following = music(rate=RATE, seconds=3.0, lead=0.0, tail=0.0, bits=16, seed=11)
        lead = int(0.2 * RATE)
        roon, mandarin = named_process(self.tmp, "RAATServer"), named_process(self.tmp, "mandarin")
        self.addCleanup(lambda: [p.kill() or p.wait() for p in (roon, mandarin)])
        self.record(tid, "a", np.concatenate([self.src, following]), "S32_LE", lead_frames=lead, pid=roon.pid)
        t = self.results(tid, ["a"])
        cap, res = t["captures"]["a"], t["results"]["a"]
        self.assertEqual(cap["stop_reason"], "reached the end of the song")
        self.assertEqual(read_wav(self.tests.audio_path(tid, "a")).frames, lead + len(self.src))
        self.assertEqual(t["analysis"]["a"]["capture"]["song_end"]["how"], "exact")
        self.assertEqual(res["verdict"], "IDENTICAL")

        # Roon goes on playing its next track (the simulated one never closes its output), and
        # is paused. Mandarin plays on the next free substream: that is what gets recorded.
        # It isn't bit-perfect (-0.5 dB with dither): found on the waveform, and stopped at the song's end.
        self.assertEqual(self.fake.subdir(0, "p", 0)[-4:], "sub0")
        pause_roon = lambda: self.fake.play("S32_LE", RATE, 2, state="PAUSED", sub=0, pid=roon.pid)  # noqa: E731
        self.record(tid, "b", np.concatenate([volume_with_dither(self.src), volume_with_dither(following, seed=3)]),
                    "S24_3LE", lead_frames=lead, sub=1, pid=mandarin.pid, meanwhile=pause_roon)
        self.assertEqual(self.rec.status()["ignoring"], "Roon")
        t = self.results(tid, ["b"])
        self.assertEqual(t["captures"]["b"]["player"], "mandarin")
        frames = read_wav(self.tests.audio_path(tid, "b")).frames
        self.assertEqual(t["captures"]["b"]["stop_reason"], "reached the end of the song")
        self.assertEqual(frames, lead + len(self.src))
        self.assertEqual(t["analysis"]["b"]["capture"]["song_end"]["how"], "aligned")
        self.assertEqual(t["results"]["b"]["verdict"], "DIFFERENT")
        self.fake.close(sub=0)
        self.fake.close(sub=1)
        self.tests.delete(tid)

    def test_output_open_and_silent_long_before_the_song(self):
        # Squeezelite keeps its output open, sending silence, until a song starts: here for 45 s,
        # longer than the song plus every margin. The song is still recorded whole.
        tid = self.new_test()
        self.record(tid, "a", self.src, "S32_LE", lead_frames=45 * RATE)
        t = self.results(tid, ["a"])
        self.assertEqual(t["results"]["a"]["verdict"], "IDENTICAL")
        self.tests.delete(tid)

    def silent_player(self, seconds, close):
        pcm = os.path.join(self.tmp, "silence.pcm")
        with open(pcm, "wb") as f:
            f.write(b"\0" * (seconds * RATE * 8))
        os.environ.update({"FAKE_PCM": pcm, "FAKE_FRAME_BYTES": "8"})
        if not close:
            os.environ.pop("FAKE_CLOSE_DIR")

    def test_only_silence_arrives(self):
        tid = self.new_test()
        self.silent_player(3, close=True)
        self.rec.max_wait_audio = 1
        try:
            self.rec.start(tid, "a")
            time.sleep(0.2)
            self.fake.play("S32_LE", RATE, 2)
            wait_for(lambda: not self.rec.active(), what="the recording")
        finally:
            self.rec.max_wait_audio = 600
            self.fake.close()  # stopped early, the simulated player never closed its output
        st = self.rec.status()
        self.assertEqual((st["state"], st["saved"]), ("error", False))
        self.assertIn("only digital silence arrived (no music arrived within 1 s)", st["error"])
        self.assertEqual(self.tests.get(tid)["captures"], {})
        self.tests.delete(tid)

    def test_cancel_while_the_output_is_silent(self):
        tid = self.new_test()
        self.silent_player(60, close=False)
        try:
            self.rec.start(tid, "a")
            time.sleep(0.2)
            self.fake.play("S32_LE", RATE, 2)
            st = wait_for(lambda: self.rec.status().get("take") and self.rec.status(), what="the open output")
            self.assertEqual(st["state"], "recording")
            self.assertIsNone(st["take"]["music_seconds"])  # listening, but no music yet
            self.rec.stop()
            wait_for(lambda: not self.rec.active(), what="the stop")
        finally:
            os.environ["FAKE_CLOSE_DIR"] = self.fake.subdir(0, "p", 0)
            self.fake.close()
        st = self.rec.status()
        self.assertEqual((st["state"], st["saved"], st["error"]), ("done", False, None))
        self.assertEqual(self.tests.get(tid)["captures"], {})
        self.tests.delete(tid)

    def test_record_waits_for_the_song(self):
        tid = self.new_test()
        self.tests._update(tid, lambda t: t["source"].update(state="preparing"))
        with self.assertRaises(ValueError):
            self.rec.start(tid, "a")
        self.tests._update(tid, lambda t: t["source"].update(state="error"))
        with self.assertRaises(ValueError):
            self.rec.start(tid, "a")
        self.assertFalse(self.rec.active())
        self.tests.delete(tid)

    def test_stop_before_playback(self):
        tid = self.new_test()
        self.rec.start(tid, "a")
        time.sleep(0.1)
        self.rec.stop()
        wait_for(lambda: not self.rec.active(), what="the stop")
        st = self.rec.status()
        self.assertEqual((st["state"], st["saved"]), ("done", False))
        self.assertEqual(self.tests.get(tid)["captures"], {})


class Devices(SessionBase):
    def test_resolve_device(self):
        self.assertEqual(resolve_device("auto"), "loopback:1")
        self.assertEqual(resolve_device("loopback:7"), "loopback:1")  # the card number moved
        self.assertEqual(resolve_device("usb"), "usb:2")
        self.assertEqual(resolve_device("usb:2"), "usb:2")

    def test_no_loopback_card(self):
        # Automatic falls back to a USB DAC only when usbmon can record it (it can't here).
        cards = os.path.join(self.fake.root, "cards")
        with open(cards) as f:
            original = f.read()
        try:
            with open(cards, "w") as f:
                f.write("".join(line + "\n" for line in original.splitlines() if "Loopback" not in line))
            with self.assertRaises(CaptureError):
                resolve_device("auto")
            self.assertEqual(resolve_device("usb"), "usb:2")  # chosen explicitly: tried anyway
        finally:
            with open(cards, "w") as f:
                f.write(original)


class Alignment(SessionBase):
    def test_dropout_keeps_the_rest_aligned(self):
        tid = self.new_test()
        # 50 ms of inserted silence 2 s in (an underrun): the samples themselves are untouched.
        at = 2 * RATE
        gap = np.zeros((int(0.05 * RATE), 2), np.int32)
        self.add(tid, "a", np.concatenate([self.src[:at], gap, self.src[at:]]))
        t = self.results(tid, ["a"])
        res = t["results"]["a"]
        self.assertEqual(res["verdict"], "GAPS")
        self.assertEqual(res["alignment"]["steps"], [[0, 0], [at, len(gap)]])
        # On the source's timeline the dropout is skipped, so nothing else shows as different.
        self.assertTrue(is_black(self.tests.spectrogram(tid, "diff-a", 0, 0, 400, 100)[0]))
        self.assertFalse(read_wav(b"".join(self.tests.difference_wav(tid, "a")[0])).data.any())

    def test_spectrogram_time_range(self):
        tid = self.new_test()
        # Zoomed in further than one sample per pixel: the picture still spans exactly what was asked.
        png, info = self.tests.spectrogram(tid, "source", 2.0, 2.01, 2000, 100)
        self.assertEqual(png_pixels(png).shape, (100, 2000, 3))
        self.assertAlmostEqual(info["t0"], 2.0, places=4)
        self.assertAlmostEqual(info["t1"], 2.01, places=4)
        # Beyond the end of the song: kept within it.
        png, info = self.tests.spectrogram(tid, "source", 0, 1e7, 200, 64)
        self.assertAlmostEqual(info["t1"], 5.0, places=4)
        png, info = self.tests.spectrogram(tid, "source", 1e6, 0, 200, 64)
        self.assertLess(info["t0"], 5.0)
        self.tests.delete(tid)

    def test_resampled_capture(self):
        tid = self.new_test()
        up = np.repeat(self.src, 2, axis=0)  # stands in for a player upsampling to 88.2 kHz
        self.add(tid, "a", np.concatenate([np.zeros((8820, 2), np.int32), up]), rate=2 * RATE)
        t = self.results(tid, ["a"])
        res = t["results"]["a"]
        self.assertEqual(res["verdict"], "RESAMPLED")
        self.assertAlmostEqual(res["alignment"]["offset_seconds"], 0.1, places=4)
        png, info = self.tests.spectrogram(tid, "a", 0, 0, 400, 100)
        self.assertEqual(info["rate"], 2 * RATE)
        self.assertEqual(info["fmax"], RATE)  # the highest Nyquist frequency of the test
        with self.assertRaises(ValueError):
            self.tests.spectrogram(tid, "diff-a", 0, 0, 400, 100)

    def test_steps(self):
        exact = {"segments": [{"ref_start": 0, "ref_end": 100, "lag": 5},
                              {"ref_start": 90, "ref_end": 200, "lag": 25},  # 10 frames played twice
                              {"ref_start": 200, "ref_end": 300, "lag": 25}]}
        self.assertEqual(_steps(exact), [[0, 5], [100, 25]])


if __name__ == "__main__":
    unittest.main()
