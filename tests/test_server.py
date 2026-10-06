"""Web API tests: the real server, with simulated players on a simulated loopback card."""

import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import numpy as np

import fake_server

from sqtool.wavio import read_wav


def call(base, path, method="GET", json_body=None, data=None):
    headers = {}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    if data is not None:
        body = data
        headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type") == "application/json" else raw), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sq-server-test-")
        cls.old_env = dict(os.environ)
        cls.httpd, cls.base = fake_server.start(os.path.join(cls.tmp, "data"), proc_dir=cls.tmp, seconds=4.0)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.players.close()
        os.environ.clear()
        os.environ.update(cls.old_env)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wait_test(self, tid, ready, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = call(self.base, "/api/tests/" + tid)[1]
            if ready(t):
                return t
            time.sleep(0.1)
        self.fail("timed out: %s" % json.dumps({k: t[k] for k in ("source", "captures", "results")}))

    def record(self, tid, slot):
        status, st, _ = call(self.base, "/api/tests/%s/record" % tid, "POST", {"slot": slot})
        self.assertEqual(status, 200, st)
        self.assertIn(st["state"], ("starting", "waiting"))
        self.assertEqual((st["slot"], st["device"]), (slot, "loopback:1"))
        status, err, _ = call(self.base, "/api/tests/%s/record" % tid, "POST", {"slot": slot})
        self.assertEqual((status, err["error"]), (400, "a recording is already running: stop it first"))
        keys = ["a"] if slot == "a" else ["b", "ab"]
        return self.wait_test(tid, lambda t: all((t["results"].get(k) or {}).get("state") == "ready" for k in keys))

    def test_full_flow(self):
        status, state, _ = call(self.base, "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["capture"]["use"], "loopback:1")
        self.assertEqual(state["capture"]["loopback"]["play_to"], "hw:1,0")
        self.assertEqual(state["capture"]["problems"], [])
        self.assertTrue(state["music"]["available"])
        self.assertEqual(state["settings"]["players"], {"a": "Roon", "b": "Mandarin"})

        root = call(self.base, "/api/browse")[1]
        self.assertEqual(root["dirs"], ["SQ-tool Demo"])
        album = call(self.base, "/api/browse?path=SQ-tool%20Demo/Example%20Album")[1]
        self.assertEqual([f["name"] for f in album["files"]], ["01 Demo Song (24-96).wav", "02 Demo Song (16-44).wav"])
        found = call(self.base, "/api/search?q=demo%2016")[1]
        self.assertEqual([f["name"] for f in found], ["02 Demo Song (16-44).wav"])

        status, r, _ = call(self.base, "/api/tests", "POST", {"path": found[0]["path"]})
        self.assertEqual(status, 200, r)
        tid = r["id"]
        t = self.wait_test(tid, lambda t: t["source"]["state"] != "preparing")
        self.assertEqual(t["source"]["state"], "ready", t["source"])
        self.assertEqual((t["title"], t["artist"], t["source"]["rate"]), ("Demo Song (16-44)", "SQ-tool Demo", 44100))
        self.assertEqual(call(self.base, "/api/state?test=" + tid)[1]["test_rev"], t["rev"])

        t = self.record(tid, "a")  # bit-perfect
        self.assertEqual((t["results"]["a"]["verdict"], t["results"]["a"]["short"]), ("IDENTICAL", "BIT-PERFECT"))
        self.assertEqual(t["captures"]["a"]["player"], "RAATServer")
        t = self.record(tid, "b")  # -0.5 dB with dither
        self.assertEqual(t["captures"]["b"]["player"], "mandarin")
        self.assertEqual(t["captures"]["b"]["format"], "S24_3LE")
        self.assertEqual(t["results"]["b"]["verdict"], "DIFFERENT")
        self.assertEqual(t["results"]["ab"]["verdict"], "DIFFERENT")
        self.assertIn("level changed: -0.500 dB, -0.500 dB (by channel)", t["results"]["b"]["lines"])
        st = call(self.base, "/api/state")[1]["recorder"]
        self.assertEqual((st["state"], st["saved"], st["stop_reason"]), ("done", True, "the player closed the device"))

        listed = call(self.base, "/api/tests")[1]
        self.assertEqual(listed[0]["id"], tid)
        self.assertEqual(listed[0]["results"]["a"]["short"], "BIT-PERFECT")

        # Spectrograms (PNG + their axis information in a header).
        for which in ("source", "a", "b", "diff-a", "diff-b", "diff-ab"):
            status, png, headers = call(self.base, "/api/tests/%s/spectrogram/%s.png?w=400&h=120&floor=-160" % (tid, which))
            self.assertEqual(status, 200, png)
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertTrue(png.startswith(b"\x89PNG"))
            info = json.loads(headers["X-Spectrogram"])
            self.assertEqual((info["width"], info["height"], info["db_low"], info["rate"]), (400, 120, -160, 44100))
        status, err, _ = call(self.base, "/api/tests/%s/spectrogram/nope.png" % tid)
        self.assertEqual(status, 404)
        status, err, _ = call(self.base, "/api/tests/%s/spectrogram/a.png?t0=x" % tid)
        self.assertEqual((status, err["error"]), (400, "t0 must be a number"))

        # Downloads: the recordings and the differences.
        status, wav, headers = call(self.base, "/api/tests/%s/audio/a.wav" % tid)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("Roon.wav", headers["Content-Disposition"])
        self.assertEqual(read_wav(bytes(wav)).label, "WAV PCM 32-bit")
        status, wav, headers = call(self.base, "/api/tests/%s/difference/a.wav" % tid)
        self.assertEqual(status, 200)
        diff = read_wav(bytes(wav))
        self.assertTrue(diff.is_float)
        self.assertFalse(diff.data.any())
        wav = call(self.base, "/api/tests/%s/difference/ab.wav?matched=1" % tid)[1]
        rms = float(np.sqrt(np.mean(read_wav(bytes(wav)).data.astype(np.float64) ** 2)))
        self.assertLess(20 * np.log10(rms), -130)  # only the dither remains

        status, t2, _ = call(self.base, "/api/tests/" + tid, "PATCH", {"players": {"b": "Mandarin 1.2"}})
        self.assertEqual(t2["players"], {"a": "Roon", "b": "Mandarin 1.2"})
        status, _, _ = call(self.base, "/api/tests/" + tid, "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(call(self.base, "/api/tests/" + tid)[0], 404)
        self.assertIsNone(call(self.base, "/api/state?test=" + tid)[1]["test_rev"])

    def test_upload_and_stop(self):
        with open(os.path.join(self.tmp, "music", "SQ-tool Demo", "Example Album", "02 Demo Song (16-44).wav"), "rb") as f:
            song = f.read()
        status, r, _ = call(self.base, "/api/tests/upload?filename=song.wav", "POST", data=song)
        self.assertEqual(status, 200, r)
        t = self.wait_test(r["id"], lambda t: t["source"]["state"] == "ready")
        self.assertEqual(t["source"]["filename"], "song.wav")
        self.assertEqual(os.listdir(os.path.join(self.tmp, "data", "tmp")), [])  # the upload was cleaned up

        # A recording stopped before anything plays saves nothing.
        self.httpd.players.delay = 30
        try:
            call(self.base, "/api/tests/%s/record" % r["id"], "POST", {"slot": "b"})
            st = call(self.base, "/api/record/stop", "POST")[1]
            deadline = time.time() + 10
            while st["state"] not in ("done", "error") and time.time() < deadline:
                time.sleep(0.05)
                st = call(self.base, "/api/state")[1]["recorder"]
            self.assertEqual((st["state"], st["saved"]), ("done", False))
        finally:
            self.httpd.players.delay = 0.8

        status, bad, _ = call(self.base, "/api/tests/upload?filename=notes.txt", "POST", data=b"not audio at all")
        self.assertEqual(status, 200)
        t = self.wait_test(bad["id"], lambda t: t["source"]["state"] != "preparing")
        self.assertEqual(t["source"]["state"], "error")
        self.assertIn("could not read the song", t["source"]["message"])

    def test_static_files_and_errors(self):
        status, html, headers = call(self.base, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"SQ-tool", html)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        for name in ("app.js", "app.css", "icon.svg"):
            self.assertEqual(call(self.base, "/" + name)[0], 200)
        self.assertEqual(call(self.base, "/../server.py")[0], 404)
        self.assertEqual(call(self.base, "/api/tests/nope")[0], 404)
        self.assertEqual(call(self.base, "/api/tests/../../etc")[0], 404)
        status, err, _ = call(self.base, "/api/tests", "POST", {"path": "../../etc/passwd"})
        self.assertEqual((status, err["error"]), (400, "that is outside the music folder"))
        status, err, _ = call(self.base, "/api/tests", "POST", {"path": "SQ-tool Demo/missing.flac"})
        self.assertEqual(status, 404)
        status, err, _ = call(self.base, "/api/settings", "POST", {"device": "spdif:9"})
        self.assertEqual(status, 400)
        status, s, _ = call(self.base, "/api/settings", "POST", {"players": {"a": "Roon"}, "idle_stop": 4})
        self.assertEqual((status, s["idle_stop"]), (200, 4.0))
        status, r, _ = call(self.base, "/api/loopback/load", "POST")
        self.assertEqual((status, r["message"]), (200, "the Loopback card is already there"))


if __name__ == "__main__":
    unittest.main()
