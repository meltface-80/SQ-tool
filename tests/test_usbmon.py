"""USB capture tests: usbmon events encoded exactly as the kernel delivers them."""

import errno
import json
import os
import struct
import tempfile
import time
import unittest

import numpy as np

from helpers import FakeLoopback, alsa_bytes, music

from sqtool.alsa import list_cards, parse_stream_playback, usb_audio_info, usb_dacs
from sqtool.analysis import compare
from sqtool.usbmon import (HEADER, XFER_BULK, XFER_CONTROL, XFER_ISO, UsbCapture, UsbEvent,
                           build_event)
from sqtool.wavio import Audio, read_wav

BUS, DEV, EP_OUT, EP_FB = 1, 5, 0x01, 0x81


class FakeUsbmon:
    """Stands in for /dev/usbmonN: hands out one prepared event per read."""

    def __init__(self, items):
        self.items = list(items)  # event bytes, or callables run when reached

    def wait(self, timeout):
        while self.items and callable(self.items[0]):
            self.items.pop(0)()
        if self.items:
            return True
        time.sleep(timeout)
        return False

    def readinto(self, buf):
        ev = self.items.pop(0)
        buf[:len(ev)] = ev
        return len(ev)

    def dropped(self):
        return 0

    def close(self):
        pass


def usb_stream(data: np.ndarray, fmt: str, rate: int, t0: float = 1000.0, packets_per_urb: int = 8,
               other_traffic: bool = True):
    """Split frames into URBs of isochronous packets, as snd-usb-audio sends them at high speed."""
    raw = alsa_bytes(data, fmt)
    stride = len(raw) // len(data)
    per_packet = rate / 8000.0  # frames per 125 us microframe
    events, pos, acc, t = [], 0, 0.0, t0
    while pos < len(data):
        packets = []
        for _ in range(packets_per_urb):
            acc += per_packet
            n = min(int(acc), len(data) - pos)
            acc -= int(acc)
            if n <= 0:
                break
            packets.append(raw[pos * stride:(pos + n) * stride])
            pos += n
        if not packets:
            break
        events.append(build_event("S", XFER_ISO, EP_OUT, DEV, BUS, ts=t, status=-115, packets=packets))
        events.append(build_event("C", XFER_ISO, EP_OUT, DEV, BUS, ts=t + 0.001,
                                  packets=[b""] * len(packets)))
        if other_traffic:  # feedback endpoint and an unrelated device on the same bus
            events.append(build_event("C", XFER_ISO, EP_FB, DEV, BUS, ts=t, packets=[b"\x33\x33\x05\x00"]))
            events.append(build_event("S", XFER_BULK, 0x02, 7, BUS, ts=t, data=b"x" * 512))
        t += packets_per_urb / 8000.0
    return events, t


def kill_urbs(t):
    return [build_event("C", XFER_ISO, EP_OUT, DEV, BUS, ts=t, status=-errno.ENOENT, packets=[b""] * 8)]


def set_interface(t, alt):
    setup = struct.pack("<BBHHH", 0x01, 0x0B, alt, 1, 0)
    return build_event("S", XFER_CONTROL, 0x00, DEV, BUS, ts=t, setup=setup, flag_setup=b"\0")


def set_rate(t, rate):
    setup = struct.pack("<BBHHH", 0x21, 0x01, 0x0100, 0x2900, 4)
    return build_event("S", XFER_CONTROL, 0x00, DEV, BUS, ts=t, setup=setup, flag_setup=b"\0",
                       data=struct.pack("<I", rate))


class UsbCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeLoopback(os.path.join(self.tmp.name, "asound"))
        self.old = os.environ.get("SQTOOL_PROC_ASOUND")
        os.environ["SQTOOL_PROC_ASOUND"] = self.fake.root
        self.dac = usb_audio_info(list_cards()[2])
        self.out = os.path.join(self.tmp.name, "out")

    def tearDown(self):
        if self.old is None:
            del os.environ["SQTOOL_PROC_ASOUND"]
        else:
            os.environ["SQTOOL_PROC_ASOUND"] = self.old
        self.tmp.cleanup()

    def capture(self, items, **kw):
        cap = UsbCapture(self.dac, self.out, source=FakeUsbmon(items), idle_stop=0.3,
                         log=lambda msg: None, **kw)
        return cap, cap.run()

    def test_dac_discovery(self):
        self.assertEqual((self.dac.bus, self.dac.dev, self.dac.endpoints), (BUS, DEV, [EP_OUT]))
        self.assertEqual(self.dac.formats, ["S32_LE", "DSD_U32_BE"])
        self.assertEqual([d.card.index for d in usb_dacs()], [2])
        eps, fmts, _ = parse_stream_playback("Capture:\n  Endpoint: 0x82 (2 IN) (ASYNC)\n")
        self.assertEqual((eps, fmts), ([], []))

    def test_event_round_trip(self):
        raw = build_event("S", XFER_ISO, EP_OUT, DEV, BUS, ts=12.5, packets=[b"ab" * 4, b"cd" * 4])
        self.assertEqual(HEADER.size, 48)
        ev = UsbEvent.parse(raw)
        self.assertEqual((ev.kind, ev.xfer, ev.ep, ev.dev, ev.bus, ev.ts), ("S", 0, 1, 5, 1, 12.5))
        self.assertEqual([bytes(p) for _, _, p in ev.iso_packets()], [b"ab" * 4, b"cd" * 4])

    def test_records_exactly_what_the_dac_receives(self):
        src = music(seconds=2, lead=0.2, tail=0.2)
        self.fake.play("S32_LE", 44100, 2, card=2)
        events, _ = usb_stream(src, "S32_LE", 44100)
        cap, takes = self.capture([set_interface(999.0, 1), set_rate(999.0, 44100)] + events)
        self.assertEqual(len(takes), 1)
        audio = read_wav(takes[0]["path"])
        np.testing.assert_array_equal(audio.data, src)
        self.assertEqual(compare(Audio(src, 44100, 16), audio)["verdict"], "IDENTICAL")
        with open(takes[0]["path"] + ".json") as f:
            meta = json.load(f)["capture"]
        self.assertEqual(meta["method"], "usbmon")
        self.assertEqual(meta["alsa_format"], "S32_LE")
        self.assertEqual(meta["usb"]["frames_per_packet"], {"5": meta["usb"]["frames_per_packet"]["5"],
                                                           "6": meta["usb"]["frames_per_packet"]["6"]})
        self.assertEqual(meta["usb"]["dac_rate_set"], 44100)
        self.assertEqual(meta["usb"]["bad_packets"], 0)
        self.assertEqual(meta["interruptions"], [])
        self.assertEqual(meta["player"]["pid"], os.getpid())
        self.assertIn("without music", meta["stop_reason"])

    def test_stream_stop_and_restart_is_an_interruption(self):
        src = music(seconds=2, lead=0.1, tail=0.1)
        self.fake.play("S32_LE", 44100, 2, card=2)
        a, t = usb_stream(src[:44100], "S32_LE", 44100)
        b, _ = usb_stream(src[44100:], "S32_LE", 44100, t0=t + 0.05)
        cap, takes = self.capture(a + kill_urbs(t) + b)
        self.assertEqual(len(takes), 1)
        with open(takes[0]["path"] + ".json") as f:
            meta = json.load(f)["capture"]
        self.assertEqual([i["frame"] for i in meta["interruptions"]], [44100])
        np.testing.assert_array_equal(read_wav(takes[0]["path"]).data, src)

    def test_format_change_makes_a_new_take(self):
        first = music(seconds=1, lead=0.1, tail=0.1, seed=1)
        second = music(rate=96000, seconds=1, lead=0.1, tail=0.1, bits=24, seed=2)
        self.fake.play("S32_LE", 44100, 2, card=2)
        a, t = usb_stream(first, "S32_LE", 44100)
        b, _ = usb_stream(second, "S24_3LE", 96000, t0=t + 0.2)
        switch = lambda: self.fake.play("S24_3LE", 96000, 2, card=2)  # noqa: E731
        cap, takes = self.capture(a + kill_urbs(t) + [switch, set_interface(t + 0.1, 2)] + b)
        self.assertEqual([(x["format"], x["rate"]) for x in takes], [("S32_LE", 44100), ("S24_3LE", 96000)])
        np.testing.assert_array_equal(read_wav(takes[0]["path"]).data, first)
        np.testing.assert_array_equal(read_wav(takes[1]["path"]).data, second)

    def test_silent_probe_is_discarded_and_uncaptured_data_is_flagged(self):
        self.fake.play("S32_LE", 44100, 2, card=2)
        silence, t = usb_stream(np.zeros((4410, 2), np.int32), "S32_LE", 44100, other_traffic=False)
        src = music(seconds=1, lead=0.05, tail=0.05)
        audio_events, _ = usb_stream(src, "S32_LE", 44100, t0=t + 1.0, other_traffic=False)
        broken = build_event("S", XFER_ISO, EP_OUT, DEV, BUS, ts=t + 0.9, packets=[b"\0" * 40],
                             flag_data=b"D")
        cap, takes = self.capture(silence + kill_urbs(t) + [broken] + audio_events)
        self.assertEqual(len(takes), 1)
        self.assertFalse(takes[0]["complete"])
        with open(takes[0]["path"] + ".json") as f:
            self.assertEqual(json.load(f)["capture"]["usb"]["uncaptured_transfers"], 1)

    def test_status_while_waiting(self):
        cap = UsbCapture(self.dac, self.out, source=FakeUsbmon([]), idle_stop=0.3, log=lambda m: None)
        cap.start()
        time.sleep(0.2)
        st = cap.status()
        self.assertEqual(st["state"], "waiting")
        self.assertEqual(st["device"]["name"], "SMSL SU-1")
        cap.stop()
        cap.join(5)
        self.assertEqual(cap.status()["state"], "done")
        self.assertEqual(cap.status()["stop_reason"], "stopped by user")


if __name__ == "__main__":
    unittest.main()
