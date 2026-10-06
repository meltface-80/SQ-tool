"""Shared test helpers: synthetic signals and a simulated /proc/asound loopback card."""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqtool.wavio import Audio  # noqa: E402

FAKE_ARECORD = os.path.join(ROOT, "tests", "fake_arecord.py")
SQ_TOOL = os.path.join(ROOT, "sq-tool")

CARDS = """ 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0xf7f10000 irq 32
 1 [Loopback       ]: Loopback - Loopback
                      Loopback 1
 2 [SU1            ]: USB-Audio - SMSL SU-1
                      SMSL SU-1 at usb-0000:00:14.0-2, high speed
"""


STREAM0 = """SMSL SU-1 at usb-0000:00:14.0-2, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S32_LE
    Channels: 2
    Endpoint: 0x01 (1 OUT) (ASYNC)
    Rates: 44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000, 705600, 768000
    Data packet interval: 125 us
    Bits: 32
    Channel map: FL FR
    Sync Endpoint: 0x81 (1 IN)
    Sync EP Interface: 1
    Sync EP Altset: 1
    Implicit Feedback Mode: No
  Interface 1
    Altset 2
    Format: DSD_U32_BE
    Channels: 2
    Endpoint: 0x01 (1 OUT) (ASYNC)
    Rates: 705600, 768000, 1411200, 1536000
    Data packet interval: 125 us
    Bits: 32
    DSD raw: DOP=0, bitrev=0
"""


def music(rate=44100, seconds=4.0, lead=1.0, tail=1.0, bits=16, seed=1, channels=2):
    """Noise-like test content with digital silence around it, as left-justified int32."""
    rng = np.random.default_rng(seed)
    n_lead, n_body, n_tail = int(lead * rate), int(seconds * rate), int(tail * rate)
    body = rng.standard_normal((n_body, channels)) * 0.1
    scale = 2 ** (bits - 1)
    q = np.clip(np.round(body * scale), -scale, scale - 1).astype(np.int64) << (32 - bits)
    data = np.zeros((n_lead + n_body + n_tail, channels), dtype=np.int32)
    data[n_lead:n_lead + n_body] = q
    return data


def audio(data, rate=44100, bits=16, **kw):
    return Audio(data=data, rate=rate, bits=bits, **kw)


def alsa_bytes(data: np.ndarray, fmt: str) -> bytes:
    """Left-justified int32 frames -> what a player would write in ALSA format `fmt`."""
    if fmt == "S16_LE":
        return (data >> 16).astype("<i2").tobytes()
    if fmt == "S24_3LE":
        return data.astype("<i4").view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()
    if fmt == "S24_LE":
        return (data >> 8).astype("<i4").tobytes()
    if fmt == "S32_LE":
        return data.astype("<i4").tobytes()
    if fmt == "S32_BE":
        return data.astype(">i4").tobytes()
    if fmt == "FLOAT_LE":
        return (data / 2.0 ** 31).astype("<f4").tobytes()
    raise ValueError(fmt)


class FakeLoopback:
    """A /proc/asound tree with a Loopback card (card 1) whose state tests can change."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "cards"), "w") as f:
            f.write(CARDS)
        for card in (0, 1, 2):
            for dev in ((0, 1) if card == 1 else (0,)):
                for stream in "pc":
                    for sub in (0, 1):
                        d = self.subdir(dev, stream, sub, card)
                        os.makedirs(d, exist_ok=True)
                        self._write(d, "closed\n", "closed\n")

        # Card 2 is a USB DAC: where it sits on the bus and its playback endpoint.
        with open(os.path.join(root, "card2", "usbbus"), "w") as f:
            f.write("001/005\n")
        with open(os.path.join(root, "card2", "usbid"), "w") as f:
            f.write("262a:18a1\n")
        with open(os.path.join(root, "card2", "stream0"), "w") as f:
            f.write(STREAM0)

    def subdir(self, dev=0, stream="p", sub=0, card=1):
        return os.path.join(self.root, "card%d" % card, "pcm%d%s" % (dev, stream), "sub%d" % sub)

    @staticmethod
    def _write(d, hw, status):
        with open(os.path.join(d, "hw_params"), "w") as f:
            f.write(hw)
        with open(os.path.join(d, "status"), "w") as f:
            f.write(status)

    def play(self, fmt="S32_LE", rate=44100, channels=2, state="RUNNING", hw_ptr=0,
             dev=0, sub=0, card=1, pid=None):
        hw = ("access: MMAP_INTERLEAVED\nformat: %s\nsubformat: STD\nchannels: %d\n"
              "rate: %d (%d/1)\nperiod_size: 1024\nbuffer_size: 4096\n" % (fmt, channels, rate, rate))
        status = ("state: %s\nowner_pid   : %d\ntrigger_time: 100.000000000\n"
                  "tstamp      : 101.000000000\ndelay       : 3072\navail       : 1024\n"
                  "avail_max   : 1024\n-----\nhw_ptr      : %d\nappl_ptr    : %d\n"
                  % (state, pid or os.getpid(), hw_ptr, hw_ptr + 3072))
        self._write(self.subdir(dev, "p", sub, card), hw, status)

    def close(self, dev=0, sub=0, card=1):
        self._write(self.subdir(dev, "p", sub, card), "closed\n", "closed\n")
