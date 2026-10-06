#!/usr/bin/env python3
"""Run the web interface against a simulated SMSL SU-1 (for tests and UI checks).

Every capture "plays" a track to the simulated DAC: the fake /proc/asound shows
the stream, and a fake usbmon delivers its USB packets. Odd-numbered captures
are bit-perfect; even-numbered ones apply -0.5 dB of digital volume with dither,
like a player with its volume control in the signal path.

    python3 tests/fake_server.py --port 3400 --data /tmp/sq-data
"""

import argparse
import os
import sys
import tempfile
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import FakeLoopback  # noqa: E402
from test_usbmon import kill_urbs, set_interface, set_rate, usb_stream  # noqa: E402

import sqtool.usbmon  # noqa: E402
from sqtool.generate import make_test_signal  # noqa: E402

STATE = {"plays": 0, "fake": None}


class FakePlayer:
    """Stands in for UsbmonSource: plays one track to the DAC shortly after the capture starts."""

    def __init__(self, bus):
        STATE["plays"] += 1
        n = STATE["plays"]
        track = make_test_signal(44100, 16)
        data = track.data
        if n % 2 == 0:
            rng = np.random.default_rng(n)
            x = data.astype(np.float64) * 10 ** (-0.5 / 20) / 256
            x += rng.random(x.shape) - rng.random(x.shape)
            data = (np.round(x) * 256).astype(np.int32)
        events, t_end = usb_stream(data, "S32_LE", 44100, t0=1000.0, other_traffic=False)
        self.items = [lambda: time.sleep(0.8),
                      lambda: STATE["fake"].play("S32_LE", 44100, 2, card=2),
                      set_interface(999.0, 1), set_rate(999.0, 44100)]
        # Deliver the packets in bursts, roughly 20x faster than real time.
        for i in range(0, len(events), 200):
            self.items.extend(events[i:i + 200])
            self.items.append(lambda: time.sleep(0.005))
        self.items += kill_urbs(t_end) + [lambda: STATE["fake"].close(card=2)]

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


def start(data_dir, port=0, proc_dir=None):
    """Start the server in a thread; returns (httpd, base URL)."""
    proc_dir = proc_dir or tempfile.mkdtemp(prefix="sq-proc-")
    STATE["fake"] = FakeLoopback(os.path.join(proc_dir, "asound"))
    os.environ["SQTOOL_PROC_ASOUND"] = STATE["fake"].root
    sqtool.usbmon.UsbmonSource = FakePlayer
    sqtool.usbmon.ensure_usbmon_node = lambda bus: "/dev/null"
    from sqtool.server import make_server
    httpd = make_server(data_dir, "127.0.0.1", port, log=lambda msg: None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3400)
    ap.add_argument("--data", default=tempfile.mkdtemp(prefix="sq-data-"))
    args = ap.parse_args()
    httpd, url = start(args.data, args.port)
    print("fake SQ-tool server on %s (data in %s)" % (url, args.data), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
