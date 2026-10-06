"""Web API tests: the real server, capturing from a simulated SMSL SU-1 over fake usbmon."""

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

from sqtool.wavio import read_wav, write_wav


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
        cls.old_proc = os.environ.get("SQTOOL_PROC_ASOUND")
        cls.httpd, cls.base = fake_server.start(os.path.join(cls.tmp, "data"), proc_dir=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if cls.old_proc is None:
            os.environ.pop("SQTOOL_PROC_ASOUND", None)
        else:
            os.environ["SQTOOL_PROC_ASOUND"] = cls.old_proc
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def capture(self, name):
        status, st, _ = call(self.base, "/api/capture", "POST",
                             {"device": "usb:2", "name": name, "idle_stop": 0.5, "max_seconds": 600})
        self.assertEqual(status, 200, st)
        deadline = time.time() + 120
        while time.time() < deadline:
            st = call(self.base, "/api/state")[1]["capture"]
            if st["state"] in ("done", "error") and not st["saving"]:
                break
            time.sleep(0.2)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["errors"], [])
        self.assertEqual(len(st["saved"]), 1, st)
        return st["saved"][0]

    def test_full_flow(self):
        status, state, _ = call(self.base, "/api/state")
        self.assertEqual(status, 200)
        ids = {d["id"]: d for d in state["devices"]}
        self.assertEqual(ids["usb:2"]["name"], "SMSL SU-1")
        self.assertEqual(ids["usb:2"]["usbmon"], "missing")  # no real usbmon in the test machine
        self.assertIn("loopback:1", ids)

        # A source file imported over the API (the 16-bit test track the fake player plays).
        from sqtool.generate import make_test_signal
        src = make_test_signal(44100, 16)
        buf = os.path.join(self.tmp, "src.wav")
        write_wav(buf, src)
        with open(buf, "rb") as f:
            status, ref, _ = call(self.base, "/api/import?name=Source&filename=src.wav", "POST", data=f.read())
        self.assertEqual(status, 200, ref)
        self.assertEqual(ref["kind"], "reference")

        roon = self.capture("Roon")  # bit-perfect
        mandarin = self.capture("Mandarin")  # -0.5 dB digital volume with dither
        self.assertEqual(roon["kind"], "capture")
        self.assertEqual(roon["alsa_format"], "S32_LE")
        self.assertEqual(roon["fingerprint"], ref["fingerprint"])
        self.assertNotEqual(mandarin["fingerprint"], ref["fingerprint"])

        items = call(self.base, "/api/items")[1]
        mine = [i["name"] for i in items if i["id"] in (ref["id"], roon["id"], mandarin["id"])]
        self.assertEqual(mine, ["Mandarin", "Roon", "Source"])  # newest first
        detail = call(self.base, "/api/items/" + roon["id"])[1]
        self.assertEqual(detail["analysis"]["capture"]["method"], "usbmon")
        self.assertEqual(detail["analysis"]["resolution"], 16)
        self.assertTrue(detail["analysis"]["plots"]["spectrum"]["freqs"])
        self.assertEqual(len(detail["analysis"]["plots"]["bits"]), 32)

        r1 = call(self.base, "/api/null", "POST", {"a": ref["id"], "b": roon["id"]})[1]
        self.assertEqual((r1["verdict"], r1["short"]), ("IDENTICAL", "BIT-PERFECT"))
        self.assertEqual(r1["plots"]["timeline"][0]["kind"], "identical")
        r2 = call(self.base, "/api/null", "POST", {"a": roon["id"], "b": mandarin["id"]})[1]
        self.assertEqual(r2["verdict"], "DIFFERENT")
        self.assertIn("level changed: -0.500 dB, -0.500 dB (by channel)", r2["lines"])
        self.assertTrue(r2["plots"]["envelope"]["t"] and r2["plots"]["spectrum"]["freqs"])
        self.assertLess(r2["null_db"], -100)

        # The difference file: digital silence for the bit-perfect pair, dither for the other.
        status, wav, headers = call(self.base, "/api/null/%s/%s/difference" % (ref["id"], roon["id"]))
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        diff = read_wav(bytes(wav))
        self.assertTrue(diff.is_float)
        self.assertEqual(diff.frames, src.frames)
        self.assertFalse(diff.data.any())
        wav2 = call(self.base, "/api/null/%s/%s/difference" % (roon["id"], mandarin["id"]))[1]
        rms = float(np.sqrt(np.mean(read_wav(bytes(wav2)).data.astype(np.float64) ** 2)))
        self.assertLess(20 * np.log10(rms), -130)

        status, audio, _ = call(self.base, "/api/items/%s/audio" % roon["id"])
        self.assertEqual(read_wav(bytes(audio)).label, "WAV PCM 32-bit")

        status, meta, _ = call(self.base, "/api/items/" + mandarin["id"], "PATCH", {"name": "Mandarin v2", "notes": "volume at 95"})
        self.assertEqual(meta["name"], "Mandarin v2")
        status, _, _ = call(self.base, "/api/items/" + mandarin["id"], "DELETE")
        self.assertEqual(status, 200)
        self.assertNotIn(mandarin["id"], [i["id"] for i in call(self.base, "/api/items")[1]])
        self.assertFalse(any(mandarin["id"] in f for f in os.listdir(os.path.join(self.tmp, "data", "nulls"))))

    def test_static_files_and_errors(self):
        status, html, headers = call(self.base, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"SQ-tool", html)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(call(self.base, "/app.js")[0], 200)
        self.assertEqual(call(self.base, "/../server.py")[0], 404)
        status, err, _ = call(self.base, "/api/items/nope")
        self.assertEqual((status, err["error"]), (404, "no such item"))
        status, err, _ = call(self.base, "/api/capture", "POST", {"device": "spdif:9"})
        self.assertEqual(status, 400)
        status, err, _ = call(self.base, "/api/import?filename=x.wav", "POST", data=b"not audio")
        self.assertEqual(status, 400)

    def test_test_tracks(self):
        status, r, _ = call(self.base, "/api/test-tracks", "POST")
        self.assertEqual(status, 200, r)
        self.assertEqual(len(r["tracks"]), 4)
        self.assertEqual(len(r["added"]), 4)
        again = call(self.base, "/api/test-tracks", "POST")[1]
        self.assertEqual(again["added"], [])  # not added twice
        status, wav, _ = call(self.base, "/api/test-tracks/sq-test_16bit_44100Hz.wav")
        self.assertEqual(read_wav(bytes(wav)).frames, int(15.5 * 44100))


if __name__ == "__main__":
    unittest.main()
