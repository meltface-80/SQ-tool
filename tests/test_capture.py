"""End-to-end capture tests against a simulated loopback card.

There is no sound hardware in CI, so these tests fake the two things sq-tool
talks to: /proc/asound (via SQTOOL_PROC_ASOUND) and arecord (tests/fake_arecord.py).
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np

from helpers import FAKE_ARECORD, SQ_TOOL, FakeLoopback, alsa_bytes, music

from sqtool.alsa import list_cards, parse_hw_params, parse_status, status_report
from sqtool.analysis import compare
from sqtool.wavio import Audio, read_wav

RATE = 44100


class CaptureRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.fake = FakeLoopback(os.path.join(self.dir, "asound"))
        self.args_log = os.path.join(self.dir, "arecord.log")
        self.src = music(rate=RATE, seconds=2, lead=0.5, tail=0.5)

    def tearDown(self):
        self.tmp.cleanup()

    def pcm(self, data, fmt, name="player.pcm"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(alsa_bytes(data, fmt))
        return path

    def start(self, args, pcm, frame_bytes, close_dir=True, extra_env=None, dev=0, sub=0):
        env = dict(os.environ, SQTOOL_PROC_ASOUND=self.fake.root, FAKE_PCM=pcm,
                   FAKE_FRAME_BYTES=str(frame_bytes), FAKE_ARGS_LOG=self.args_log)
        if close_dir:
            env["FAKE_CLOSE_DIR"] = self.fake.subdir(dev, "p", sub)
        env.update(extra_env or {})
        self.out = os.path.join(self.dir, "cap.wav")
        return subprocess.Popen([sys.executable, SQ_TOOL, "capture", self.out, "--arecord", FAKE_ARECORD]
                                + args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def finish(self, proc, expect_code=0):
        out, _ = proc.communicate(timeout=60)
        text = out.decode()
        self.assertEqual(proc.returncode, expect_code, text)
        return text

    def meta(self):
        with open(self.out + ".json") as f:
            return json.load(f)["capture"]

    def arecord_calls(self):
        with open(self.args_log) as f:
            return [line.split() for line in f.read().splitlines()]

    def test_waits_for_player_and_captures_bit_perfect(self):
        proc = self.start([], self.pcm(self.src, "S32_LE"), 8,
                          extra_env={"FAKE_SKIP_BYTES": str(8 * 1000)})
        time.sleep(0.4)
        self.fake.play("S32_LE", RATE, 2, hw_ptr=5000)
        text = self.finish(proc)
        self.assertIn("Player found on hw:1,0,0", text)
        call = self.arecord_calls()[0]
        self.assertEqual(call[:8], ["-D", "hw:1,1,0", "-f", "S32_LE", "-r", "44100", "-c", "2"])
        meta = self.meta()
        self.assertEqual(meta["stop_reason"], "the player closed the device")
        self.assertEqual((meta["alsa_format"], meta["period_size"], meta["buffer_size"]), ("S32_LE", 1024, 4096))
        self.assertEqual(meta["player"]["pid"], os.getpid())
        self.assertEqual(meta["capture_overruns"], 0)
        cap = read_wav(self.out)
        self.assertEqual(cap.label, "captured ALSA S32_LE")
        res = compare(Audio(self.src, RATE, 16), cap)
        self.assertEqual(res["verdict"], "IDENTICAL")
        self.assertEqual(res["exact"]["missing_start"], 1000)

    def test_arecord_runs_untranslated_with_short_periods(self):
        proc = self.start([], self.pcm(self.src, "S32_LE"), 8)
        time.sleep(0.3)
        self.fake.play()
        self.finish(proc)
        self.assertEqual(self.arecord_calls()[0][-4:], ["-B", "500000", "-F", "25000"])
        with open(self.args_log + ".env") as f:
            self.assertEqual(f.read().split(), ["LC_ALL=C"])

    def test_prearm_with_only_silence_is_an_error(self):
        silent = self.pcm(np.zeros((RATE, 2), np.int32), "S32_LE", "silence.pcm")
        proc = self.start(["--prearm", "S32_LE:44100:2", "--wait", "1"], silent, 8, close_dir=False)
        text = self.finish(proc, expect_code=2)
        self.assertIn("only silence was captured", text)
        self.assertFalse(os.path.exists(self.out))

    def test_formats_and_the_other_loopback_device(self):
        for fmt, width in (("S24_3LE", 3), ("S24_LE", 4), ("S16_LE", 2), ("FLOAT_LE", 4)):
            with self.subTest(fmt=fmt):
                if os.path.exists(self.args_log + ".runs"):
                    os.unlink(self.args_log + ".runs")
                self.fake.close(dev=1, sub=1)
                proc = self.start([], self.pcm(self.src, fmt), 2 * width, dev=1, sub=1)
                time.sleep(0.3)
                self.fake.play(fmt, RATE, 2, dev=1, sub=1)
                self.finish(proc)
                self.assertEqual(self.arecord_calls()[-1][:4], ["-D", "hw:1,0,1", "-f", fmt])
                res = compare(Audio(self.src, RATE, 16), read_wav(self.out))
                self.assertEqual(res["verdict"], "IDENTICAL")

    def test_player_that_leaves_without_audio_is_ignored(self):
        silent = self.pcm(np.zeros((RATE // 4, 2), np.int32), "S32_LE", "probe.pcm")
        real = self.pcm(self.src, "S32_LE")
        proc = self.start([], silent + os.pathsep + real, 8)
        time.sleep(0.3)
        self.fake.play("S32_LE", RATE, 2)
        deadline = time.time() + 20
        while len(self.arecord_calls() if os.path.exists(self.args_log) else []) < 1 or \
                self.fake_is_running():
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        time.sleep(0.3)
        self.fake.play("S32_LE", RATE, 2)
        text = self.finish(proc)
        self.assertIn("waiting again", text)
        self.assertEqual(len(self.arecord_calls()), 2)
        self.assertEqual(compare(Audio(self.src, RATE, 16), read_wav(self.out))["verdict"], "IDENTICAL")

    def fake_is_running(self):
        with open(os.path.join(self.fake.subdir(), "status")) as f:
            return parse_status(f.read()) is not None

    def test_prearm_starts_immediately_with_the_given_format(self):
        self.fake.play("S24_3LE", 96000, 2)
        proc = self.start(["--prearm", "S24_3LE:96000:2"], self.pcm(self.src, "S24_3LE"), 6)
        text = self.finish(proc)
        self.assertIn("Pre-armed", text)
        self.assertEqual(self.arecord_calls()[0][:8],
                         ["-D", "hw:1,1,0", "-f", "S24_3LE", "-r", "96000", "-c", "2"])
        self.assertEqual(self.meta()["mode"], "prearm")

    def test_overrun_is_reported(self):
        proc = self.start([], self.pcm(self.src, "S32_LE"), 8, extra_env={"FAKE_OVERRUN_AT": "100000"})
        time.sleep(0.3)
        self.fake.play()
        text = self.finish(proc)
        self.assertIn("WARNING: the capture overran 1 time", text)
        self.assertEqual(self.meta()["capture_overruns"], 1)

    def test_duration_limit(self):
        proc = self.start(["--duration", "1"], self.pcm(self.src, "S32_LE"), 8, close_dir=False)
        time.sleep(0.3)
        self.fake.play()
        self.finish(proc)
        self.assertEqual(read_wav(self.out).frames, RATE)
        self.assertEqual(self.meta()["stop_reason"], "reached --duration")

    def test_silence_stop(self):
        proc = self.start(["--silence-stop", "0.5"], self.pcm(self.src, "S32_LE"), 8, close_dir=False)
        time.sleep(0.3)
        self.fake.play()
        self.finish(proc)
        self.assertIn("digital silence", self.meta()["stop_reason"])

    def test_ctrl_c_keeps_what_was_captured(self):
        proc = self.start(["--silence-stop", "0"], self.pcm(self.src, "S32_LE"), 8, close_dir=False)
        time.sleep(0.3)
        self.fake.play()
        time.sleep(1.5)
        proc.send_signal(signal.SIGINT)
        self.finish(proc)
        self.assertEqual(self.meta()["stop_reason"], "stopped with Ctrl+C")
        self.assertEqual(compare(Audio(self.src, RATE, 16), read_wav(self.out))["verdict"], "IDENTICAL")

    def test_arecord_failure_is_explained(self):
        proc = self.start([], self.pcm(self.src, "S32_LE"), 8, extra_env={"FAKE_EXPECT": "S16_LE:44100:2"})
        time.sleep(0.3)
        self.fake.play()
        text = self.finish(proc, expect_code=2)
        self.assertIn("Sample format non available", text)
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_missing_loopback_card(self):
        with open(os.path.join(self.fake.root, "cards"), "w") as f:
            f.write(" 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n                      x\n")
        text = self.finish(self.start([], self.pcm(self.src, "S32_LE"), 8), expect_code=2)
        self.assertIn("modprobe snd-aloop", text)


class ProcParsing(unittest.TestCase):
    def test_hw_params_and_status(self):
        self.assertIsNone(parse_hw_params("closed\n"))
        self.assertIsNone(parse_hw_params("no setup\n"))
        hw = parse_hw_params("access: RW_INTERLEAVED\nformat: S24_3LE\nsubformat: STD\nchannels: 2\n"
                             "rate: 192000 (192000/1)\nperiod_size: 4800\nbuffer_size: 19200\n")
        self.assertEqual(hw, {"access": "RW_INTERLEAVED", "format": "S24_3LE", "subformat": "STD",
                              "channels": 2, "rate": 192000, "period_size": 4800, "buffer_size": 19200})
        self.assertIsNone(parse_status("closed\n"))
        st = parse_status("state: RUNNING\nowner_pid   : 4321\ntrigger_time: 1.5\ndelay       : 10\n"
                          "-----\nhw_ptr      : 123456\nappl_ptr    : 124000\n")
        self.assertEqual((st["state"], st["owner_pid"], st["hw_ptr"]), ("RUNNING", 4321, 123456))

    def test_cards_and_status_report(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeLoopback(d)
            old = os.environ.get("SQTOOL_PROC_ASOUND")
            os.environ["SQTOOL_PROC_ASOUND"] = d
            try:
                cards = list_cards()
                self.assertEqual([(c.index, c.id, c.driver) for c in cards],
                                 [(0, "PCH", "HDA-Intel"), (1, "Loopback", "Loopback"),
                                  (2, "SU1", "USB-Audio")])
                self.assertEqual(cards[2].name, "SMSL SU-1")
                fake.play("S32_LE", 44100, 2, card=2)
                report = status_report()
                self.assertIn("<- loopback", report)
                self.assertIn("hw:2,0,0", report)
                self.assertIn("S32_LE, 44100 Hz, 2 ch, period 1024 frames (23.2 ms)", report)
                self.assertIn("player: ", report)
            finally:
                if old is None:
                    del os.environ["SQTOOL_PROC_ASOUND"]
                else:
                    os.environ["SQTOOL_PROC_ASOUND"] = old


if __name__ == "__main__":
    unittest.main()
