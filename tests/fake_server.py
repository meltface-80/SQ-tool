#!/usr/bin/env python3
"""Run the web interface against a simulated loopback card and players (for tests and UI checks).

A music folder with two generated songs stands in for your library. Whenever a
recording waits for a player, a simulated player "plays" the test's song to the
loopback card: the fake /proc/asound shows the stream and a fake arecord
delivers it. The first player (Roon) is bit-perfect; the second (Mandarin)
applies -0.5 dB of digital volume with dither and sends packed 24-bit samples.

    python3 tests/fake_server.py --port 3400 --data /tmp/sq-data --speed 4
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import FAKE_ARECORD, FakeLoopback, alsa_bytes  # noqa: E402

from sqtool.wavio import Audio, info_chunk, read_wav, write_wav  # noqa: E402

FRAME_BYTES = {"S16_LE": 2, "S24_3LE": 3, "S24_LE": 4, "S32_LE": 4}


def demo_song(rate: int, bits: int, seconds: float = 20.0, seed: int = 3) -> np.ndarray:
    """Something with the look of music: chords with harmonics, a kick drum and hi-hats."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    out = np.zeros((n, 2))
    chords = [[220.0, 277.18, 329.63], [196.0, 246.94, 293.66], [174.61, 220.0, 261.63], [196.0, 246.94, 311.13]]
    for i in range(int(seconds / 2.0)):
        s0 = int(i * 2.0 * rate)
        s1 = min(n, s0 + int(2.6 * rate))
        tt = t[s0:s1] - t[s0]
        sig = np.zeros(s1 - s0)
        for f in chords[i % len(chords)]:
            for h in range(1, 9):
                if f * h < rate / 2:
                    sig += np.sin(2 * np.pi * f * h * tt + rng.random() * 6.283) / h ** 1.6
        env = np.exp(-tt * 1.1) * np.minimum(1.0, tt / 0.01)
        out[s0:s1, 0] += 0.07 * sig * env
        out[s0:s1, 1] += 0.06 * sig * env
    for k in range(int(seconds / 0.5)):
        s0 = int(k * 0.5 * rate)
        m = min(n - s0, int(0.25 * rate))
        tt = np.arange(m) / rate
        kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 12) * 0.25
        out[s0:s0 + m] += kick[:, None]
    for k in range(int(seconds / 0.25)):
        s0 = int(k * 0.25 * rate) + int(0.125 * rate)
        m = min(n - s0, int(0.05 * rate))
        if m <= 0:
            continue
        burst = np.diff(rng.standard_normal(m + 1)) * np.exp(-np.arange(m) / (0.008 * rate)) * 0.02
        out[s0:s0 + m, 0] += burst
        out[s0:s0 + m, 1] += 0.8 * burst
    fade = int(0.05 * rate)
    out[:fade] *= np.linspace(0, 1, fade)[:, None]
    out[-fade:] *= np.linspace(1, 0, fade)[:, None]
    scale = 2.0 ** (bits - 1)
    q = np.round(out * scale + rng.random(out.shape) - rng.random(out.shape))
    q = np.clip(q, -scale, scale - 1).astype(np.int64) << (32 - bits)
    pad = lambda s: np.zeros((int(s * rate), 2), np.int64)  # noqa: E731
    return np.concatenate([pad(0.3), q, pad(0.5)]).astype(np.int32)


def make_music(music_dir: str, seconds: float = 20.0) -> None:
    album = os.path.join(music_dir, "SQ-tool Demo", "Example Album")
    os.makedirs(album, exist_ok=True)
    for name, rate, bits in (("01 Demo Song (24-96).wav", 96000, 24), ("02 Demo Song (16-44).wav", 44100, 16)):
        path = os.path.join(album, name)
        if not os.path.exists(path):
            tags = info_chunk({"INAM": name[3:-4], "IART": "SQ-tool Demo", "IPRD": "Example Album"})
            write_wav(path, Audio(data=demo_song(rate, bits, seconds), rate=rate, bits=bits), extra_chunks=[(b"LIST", tags)])


def volume_with_dither(data: np.ndarray, db: float = -0.5, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = data.astype(np.float64) * 10 ** (db / 20) / 256
    x += rng.random(x.shape) - rng.random(x.shape)
    return (np.round(x) * 256).astype(np.int32)


class FakePlayers:
    """Plays the test's song to the simulated loopback card whenever a recording waits for a player."""

    def __init__(self, app, fake: FakeLoopback, tmp: str, speed: float = 0.0, delay: float = 0.8):
        self.app, self.fake, self.tmp, self.speed, self.delay = app, fake, tmp, speed, delay
        self.procs = {"a": self._named_process("RAATServer"), "b": self._named_process("mandarin")}
        self.plays = 0
        threading.Thread(target=self._run, name="fake-players", daemon=True).start()

    def _named_process(self, name: str):
        exe = os.path.join(self.tmp, name)
        shutil.copy("/bin/sleep", exe)  # a process is listed under its program's file name
        return subprocess.Popen([exe, "100000"])

    def _run(self) -> None:
        seen = None
        while True:
            time.sleep(0.05)
            cap = self.app.recorder.capture
            if cap is None or cap is seen or getattr(cap, "state", None) != "waiting":
                continue
            seen = cap
            info = dict(self.app.recorder.info)
            time.sleep(self.delay)
            if cap.state == "waiting":
                self.play(info["test"], info["slot"])

    def play(self, tid: str, slot: str) -> None:
        src = read_wav(self.app.tests.audio_path(tid, "source"))
        data = src.data if slot == "a" else volume_with_dither(src.data)
        fmt = "S32_LE" if slot == "a" else "S24_3LE"
        payload = np.concatenate([np.zeros((int(0.15 * src.rate), src.channels), np.int32), data])
        path = os.path.join(self.tmp, "play-%s.pcm" % slot)
        with open(path, "wb") as f:
            f.write(alsa_bytes(payload, fmt))
        os.environ.update({"FAKE_PCM": path, "FAKE_FRAME_BYTES": str(FRAME_BYTES[fmt] * src.channels),
                           "FAKE_SPEED": str(self.speed), "FAKE_SKIP_BYTES": "0"})
        self.plays += 1
        self.fake.play(fmt, src.rate, src.channels, pid=self.procs[slot].pid)

    def close(self) -> None:
        for p in self.procs.values():
            p.kill()
            p.wait()


def start(data_dir: str, port: int = 0, proc_dir: str = None, music_dir: str = None, speed: float = 0.0,
          seconds: float = 20.0):
    """Start the server in a thread; returns (httpd, base URL). httpd.players is the FakePlayers."""
    proc_dir = proc_dir or tempfile.mkdtemp(prefix="sq-proc-")
    fake = FakeLoopback(os.path.join(proc_dir, "asound"))
    music_dir = music_dir or os.path.join(proc_dir, "music")
    make_music(music_dir, seconds)
    os.environ.update({"SQTOOL_PROC_ASOUND": fake.root, "SQTOOL_ARECORD": FAKE_ARECORD,
                       "FAKE_ARGS_LOG": os.path.join(proc_dir, "arecord.log"),
                       "FAKE_CLOSE_DIR": fake.subdir(0, "p", 0)})
    from sqtool.server import make_server
    httpd = make_server(data_dir, music_dir, "127.0.0.1", port, log=lambda msg: None)
    httpd.players = FakePlayers(httpd.app, fake, proc_dir, speed)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3400)
    ap.add_argument("--data", default=tempfile.mkdtemp(prefix="sq-data-"))
    ap.add_argument("--speed", type=float, default=4.0, help="play this many times faster than real time")
    ap.add_argument("--seconds", type=float, default=20.0, help="length of the generated songs")
    args = ap.parse_args()
    httpd, url = start(args.data, args.port, speed=args.speed, seconds=args.seconds)
    print("fake SQ-tool server on %s (data in %s)" % (url, args.data), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.players.close()
