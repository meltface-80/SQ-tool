"""The web interface (port 3400 by default): pick a song, record two players, compare.

Plain standard-library HTTP server with a small JSON API; the page itself lives
in sqtool/web/. It is meant for a home network: there is no login.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional

from . import __version__
from .alsa import (ACTIVE_STATES, CaptureError, find_loopback, kernel_modules_dir, list_cards, load_loopback,
                   playback_streams, proc_asound, process_info, usb_dacs)
from .sessions import Recorder, Tests, clean, resolve_device
from .usbmon import usbmon_path
from .wavio import AudioFileError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_UPLOAD = 4 << 30
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}


# --------------------------------------------------------------------------
# What can be recorded


def _playing(card_index: int) -> Optional[dict]:
    """What a player is sending to a card right now (None when nothing is open)."""
    for sub, st, hw in playback_streams(card_index):
        if st["state"] not in ACTIVE_STATES:
            continue
        info = {"state": st["state"], "device": sub.hw_name}
        if hw:
            info.update({k: hw.get(k) for k in ("format", "rate", "channels")})
        owner = process_info(st.get("owner_pid"))
        if owner:
            info["player"] = owner["name"]
        return info
    return None


def _usbmon_state(bus: int) -> str:
    path = usbmon_path(bus)
    if os.path.exists(path):
        return "ready" if os.access(path, os.R_OK) else "no-permission"
    return "ready" if os.path.exists("/sys/class/usbmon/usbmon%d" % bus) else "missing"


def devices(setting: str, arecord: str) -> dict:
    problems: List[str] = []
    cards = list_cards()
    root = proc_asound()
    if not cards:
        if os.path.isdir(root) and not os.listdir(root):
            problems.append("The sound cards are hidden from this container: start it with --privileged.")
        else:
            problems.append("No sound cards found.")
    loop = find_loopback(cards) if cards else None
    loopback = None
    if loop:
        loopback = {"card": loop.index, "id": loop.id, "play_to": "hw:%d,0" % loop.index,
                    "playing": _playing(loop.index)}
    dacs = [{"id": "usb:%d" % d.card.index, "name": d.card.name, "usbmon": _usbmon_state(d.bus),
             "playing": _playing(d.card.index)} for d in usb_dacs()]
    kind = setting.partition(":")[0]
    try:
        use = resolve_device(setting)
    except CaptureError:
        use = None
    if kind in ("auto", "loopback") and not loopback and cards:
        problems.append("The Loopback sound card is not loaded, so nothing can be recorded yet.")
    if use and use.startswith("usb"):
        dac = [d for d in dacs if d["id"] == use][0]
        if dac["usbmon"] != "ready":
            problems.append("Recording the USB DAC needs the usbmon driver: on the server run "
                            "sudo modprobe usbmon")
    if use and use.startswith("loopback") and not shutil.which(arecord):
        problems.append("arecord is missing: install alsa-utils.")
    return {"use": use, "loopback": loopback, "dacs": dacs, "problems": problems,
            "can_load_loopback": bool(not loopback and shutil.which("modprobe")
                                      and os.path.isdir(kernel_modules_dir()))}


class App:
    def __init__(self, data_dir: str, music_dir: str, log: Callable[[str], None] = print):
        self.tests = Tests(data_dir, music_dir, log)
        self.recorder = Recorder(self.tests, log)
        self.log = log
        self._loading = threading.Lock()

    def state(self, tid: Optional[str] = None) -> dict:
        usage = shutil.disk_usage(self.tests.data_dir)
        settings = self.tests.settings()
        out = {"version": __version__, "free_bytes": usage.free, "settings": settings,
               "music": {"available": self.tests.music_available(), "root": self.tests.music_dir},
               "recorder": self.recorder.status(), "busy": not self.tests.idle(),
               "capture": devices(settings["device"], self.recorder.arecord)}
        if tid:
            try:
                out["test_rev"] = self.tests.rev(tid)
            except KeyError:
                out["test_rev"] = None
        return out

    def load_loopback(self) -> dict:
        with self._loading:
            ok, message = load_loopback()
        self.log(message)
        if not ok:
            raise CaptureError(message)
        return {"message": message}


# --------------------------------------------------------------------------
# HTTP


def _content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", filename)
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(filename))


def _num(query: dict, key: str, default: float) -> float:
    try:
        return float(query.get(key, default))
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % key)


class Handler(BaseHTTPRequestHandler):
    server_version = "sq-tool/" + __version__
    app: App  # set by make_server()

    def log_message(self, fmt, *args):  # keep the container log for what matters
        pass

    # -- helpers -----------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(headers or {})
        headers.setdefault("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(clean(obj)).encode(), "application/json")

    def _fail(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1 << 20:
            raise ValueError("request too large")
        data = self.rfile.read(length) if length else b""
        try:
            body = json.loads(data.decode() or "{}")
        except ValueError:
            raise ValueError("malformed JSON")
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        return body

    def _stream(self, chunks, size: int, ctype: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", _content_disposition(filename))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        for chunk in chunks:
            self.wfile.write(chunk)

    def _file(self, path: str, ctype: str, filename: str) -> None:
        size = os.path.getsize(path)

        def chunks():
            with open(path, "rb") as f:
                while True:
                    block = f.read(1 << 20)
                    if not block:
                        return
                    yield block

        self._stream(chunks(), size, ctype, filename)

    # -- routing -----------------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        try:
            for m, pattern, func in ROUTES:
                match = re.fullmatch(pattern, url.path)
                if m == method and match:
                    func(self, *[urllib.parse.unquote(g) for g in match.groups()], query=query)
                    return
            if method == "GET":
                self._static(url.path)
            else:
                self._fail(404, "not found")
        except KeyError:
            self._fail(404, "not found")
        except (CaptureError, AudioFileError, ValueError) as exc:
            self._fail(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            self._fail(500, "internal error: %s" % exc)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        full = os.path.join(WEB_DIR, name)
        if not re.fullmatch(r"[a-z0-9_.-]+", name) or not os.path.isfile(full):
            self._fail(404, "not found")
            return
        with open(full, "rb") as f:
            self._send(200, f.read(), STATIC_TYPES.get(os.path.splitext(name)[1], "application/octet-stream"))

    # -- API -----------------------------------------------------------------------

    def api_state(self, query):
        self._json(self.app.state(query.get("test")))

    def api_settings(self, query):
        self._json(self.app.tests.settings())

    def api_settings_save(self, query):
        self._json(self.app.tests.save_settings(self._body_json()))

    def api_loopback_load(self, query):
        self._json(self.app.load_loopback())

    def api_browse(self, query):
        self._json(self.app.tests.browse(query.get("path", "")))

    def api_search(self, query):
        self._json(self.app.tests.search(query.get("q", "")))

    def api_tests(self, query):
        self._json(self.app.tests.list())

    def api_test_create(self, query):
        path = str(self._body_json().get("path") or "")
        if not path:
            raise ValueError("choose a song")
        self._json({"id": self.app.tests.create(music_rel=path)})

    def api_test_upload(self, query):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError("send the file as the request body (up to 4 GB)")
        filename = os.path.basename(query.get("filename", "upload"))
        tmp = os.path.join(self.app.tests.tmp, "upload-%s-%s" % (
            uuid.uuid4().hex[:8], re.sub(r"[^A-Za-z0-9.]", "_", filename)[-80:]))
        try:
            with open(tmp, "wb") as f:
                left = length
                while left:
                    block = self.rfile.read(min(left, 1 << 20))
                    if not block:
                        raise ValueError("upload interrupted")
                    f.write(block)
                    left -= len(block)
            tid = self.app.tests.create(upload=tmp, filename=filename)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._json({"id": tid})

    def api_test(self, tid, query):
        self._json(self.app.tests.get(tid))

    def api_test_update(self, tid, query):
        self._json(self.app.tests.set_players(tid, self._body_json().get("players") or {}))

    def api_test_delete(self, tid, query):
        self.app.recorder.stop(tid)
        self.app.tests.delete(tid)
        self._json({"deleted": tid})

    def api_record(self, tid, query):
        self._json(self.app.recorder.start(tid, str(self._body_json().get("slot") or "")))

    def api_record_stop(self, query):
        self._json(self.app.recorder.stop())

    def api_spectrogram(self, tid, which, query):
        png, info = self.app.tests.spectrogram(
            tid, which, _num(query, "t0", 0), _num(query, "t1", 0), int(_num(query, "w", 1200)),
            int(_num(query, "h", 360)), query.get("scale", "log"), _num(query, "floor", -150),
            query.get("matched") in ("1", "true"))
        self._send(200, png, "image/png", {"X-Spectrogram": json.dumps(clean(info)),
                                           "Cache-Control": "private, max-age=86400"})

    def api_audio(self, tid, which, query):
        self._file(self.app.tests.audio_path(tid, which), "audio/wav", self.app.tests.audio_filename(tid, which))

    def api_difference(self, tid, key, query):
        chunks, size, filename = self.app.tests.difference_wav(tid, key, query.get("matched") in ("1", "true"))
        self._stream(chunks, size, "audio/wav", filename)


ID = r"([A-Za-z0-9][A-Za-z0-9._-]*)"
ROUTES = [
    ("GET", r"/api/state", Handler.api_state),
    ("GET", r"/api/settings", Handler.api_settings),
    ("POST", r"/api/settings", Handler.api_settings_save),
    ("POST", r"/api/loopback/load", Handler.api_loopback_load),
    ("GET", r"/api/browse", Handler.api_browse),
    ("GET", r"/api/search", Handler.api_search),
    ("GET", r"/api/tests", Handler.api_tests),
    ("POST", r"/api/tests", Handler.api_test_create),
    ("POST", r"/api/tests/upload", Handler.api_test_upload),
    ("GET", r"/api/tests/" + ID, Handler.api_test),
    ("PATCH", r"/api/tests/" + ID, Handler.api_test_update),
    ("DELETE", r"/api/tests/" + ID, Handler.api_test_delete),
    ("POST", r"/api/tests/" + ID + r"/record", Handler.api_record),
    ("POST", r"/api/record/stop", Handler.api_record_stop),
    ("GET", r"/api/tests/" + ID + r"/spectrogram/([a-z-]+)\.png", Handler.api_spectrogram),
    ("GET", r"/api/tests/" + ID + r"/audio/([a-z]+)\.wav", Handler.api_audio),
    ("GET", r"/api/tests/" + ID + r"/difference/([a-z]+)\.wav", Handler.api_difference),
]


def make_server(data_dir: str, music_dir: str, host: str = "0.0.0.0", port: int = 3400,
                log: Callable[[str], None] = print) -> ThreadingHTTPServer:
    app = App(data_dir, music_dir, log)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


def serve(data_dir: str, music_dir: str, host: str = "0.0.0.0", port: int = 3400,
          load_driver: bool = False, log: Callable[[str], None] = print) -> None:
    if load_driver and not find_loopback(list_cards()):
        ok, message = load_loopback()
        log(("Loopback: %s" if ok else "Could not load the Loopback driver: %s") % message)
    httpd = make_server(data_dir, music_dir, host, port, log)
    log("SQ-tool %s is running: open http://<this computer's address>:%d on your phone or tablet."
        % (__version__, httpd.server_address[1]))
    log("Data folder: %s" % os.path.abspath(data_dir))
    log("Music folder: %s%s" % (os.path.abspath(music_dir), "" if os.path.isdir(music_dir) else " (not found)"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.app.recorder.stop()  # type: ignore[attr-defined]
        httpd.server_close()
