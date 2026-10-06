"""The web interface: capture, library and side-by-side comparison (port 3400 by default).

Plain standard-library HTTP server with a small JSON API; the page itself lives
in sqtool/web/. It is meant for a home network: there is no login.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional

from . import __version__
from .alsa import (ACTIVE_STATES, Capture, CaptureError, CaptureStopped, find_loopback, find_usb_dac,
                   list_cards, playback_streams, proc_asound, process_info, usb_dacs)
from .library import Library, clean
from .usbmon import UsbCapture, usbmon_path
from .wavio import AudioFileError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_UPLOAD = 4 << 30
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}


# --------------------------------------------------------------------------
# Devices


def _playing(streams) -> Optional[dict]:
    for sub, st, hw in streams:
        info = {"state": st["state"], "hw": sub.hw_name}
        if hw:
            info.update({k: hw.get(k) for k in ("format", "rate", "channels", "period_size", "buffer_size")})
        owner = process_info(st.get("owner_pid"))
        if owner:
            info["player"] = owner["name"]
        if st["state"] in ACTIVE_STATES:
            return info
    return {"state": streams[0][1]["state"], "hw": streams[0][0].hw_name} if streams else None


def _usbmon_state(bus: int) -> str:
    path = usbmon_path(bus)
    if os.path.exists(path):
        return "ready" if os.access(path, os.R_OK) else "no-permission"
    return "ready" if os.path.exists("/sys/class/usbmon/usbmon%d" % bus) else "missing"


def devices() -> dict:
    problems: List[str] = []
    cards = list_cards()
    root = proc_asound()
    if not cards:
        if os.path.isdir(root) and not os.listdir(root):
            problems.append("The sound cards are hidden from this container: start it with --privileged.")
        else:
            problems.append("No sound cards found. Is the DAC connected and switched on?")
    out = []
    for dac in usb_dacs():
        state = _usbmon_state(dac.bus)
        if state == "missing":
            problems.append("usbmon is not loaded, so USB audio cannot be recorded. On the host run: "
                            "sudo modprobe usbmon")
        elif state == "no-permission":
            problems.append("No permission to read %s: run the container with --privileged."
                            % usbmon_path(dac.bus))
        out.append({"id": "usb:%d" % dac.card.index, "kind": "usb", "name": dac.card.name,
                    "detail": "USB %s on bus %d, device %d" % (dac.usb_id or "audio", dac.bus, dac.dev),
                    "usb": dac.describe(), "usbmon": state,
                    "playing": _playing(playback_streams(dac.card.index))})
    try:
        loop = find_loopback(cards)
    except CaptureError:
        loop = None
    if loop:
        out.append({"id": "loopback:%d" % loop.index, "kind": "loopback",
                    "name": "ALSA Loopback (play to hw:%d,0)" % loop.index,
                    "detail": "virtual sound card: records what a player sends to it",
                    "playing": _playing(playback_streams(loop.index))})
    return {"devices": out, "problems": problems}


# --------------------------------------------------------------------------
# Recording


class Recorder:
    """Runs one capture at a time and files each finished take in the library."""

    def __init__(self, lib: Library, log: Callable[[str], None]):
        self.lib = lib
        self.log = log
        self.capture = None
        self.request: dict = {}
        self.saved: List[dict] = []
        self.errors: List[str] = []
        self._takes = 0
        self._pending = 0
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._queue: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._import_worker, name="importer", daemon=True).start()

    def active(self) -> bool:
        return self.capture is not None and self.capture.state not in ("done", "error")

    def start(self, device: str, name: str, idle_stop: float, max_seconds: Optional[float]) -> dict:
        with self._lock:
            if self.active():
                raise CaptureError("a capture is already running")
            name = (name or "").strip()[:200] or "Capture %s" % time.strftime("%Y-%m-%d %H:%M")
            self.request = {"device": device, "name": name, "idle_stop": idle_stop,
                            "max_seconds": max_seconds}
            self.saved, self.errors, self._takes = [], [], 0
            out_dir = os.path.join(self.lib.tmp_dir, "capture-%s" % uuid.uuid4().hex[:8])
            kind, _, ref = (device or "").partition(":")
            if kind == "usb":
                cap = UsbCapture(find_usb_dac(ref or None), out_dir, name=name, idle_stop=idle_stop,
                                 max_seconds=max_seconds, on_take=self._on_take, log=self.log)
                cap.start()
            elif kind == "loopback":
                os.makedirs(out_dir, exist_ok=True)
                cap = Capture(os.path.join(out_dir, "loopback.wav"), card=ref or None,
                              duration=max_seconds, silence_stop=idle_stop, log=self.log,
                              handle_sigint=False)
                threading.Thread(target=self._run_loopback, args=(cap,), name="loopback-capture",
                                 daemon=True).start()
            else:
                raise CaptureError("unknown device %r" % device)
            self.capture = cap
        return self.status()

    def _run_loopback(self, cap: Capture) -> None:
        try:
            cap.run()
            self._on_take(cap.out_path, {})
        except CaptureStopped:
            pass
        except Exception as exc:
            cap.message = str(exc)
            self.errors.append(str(exc))
            cap.state = "error"
            return
        cap.state = "done"

    def _on_take(self, path: str, meta: dict) -> None:
        self._takes += 1
        name = self.request["name"] if self._takes == 1 else "%s (%d)" % (self.request["name"], self._takes)
        with self._count_lock:
            self._pending += 1
        self._queue.put((path, name))

    def _import_worker(self) -> None:
        while True:
            path, name = self._queue.get()
            try:
                self.saved.append(self.lib.summary(self.lib.add_wav(path, name, "capture")))
            except Exception as exc:
                self.errors.append("could not save %s: %s" % (name, exc))
            finally:
                with self._count_lock:
                    self._pending -= 1

    def stop(self) -> Optional[dict]:
        if self.capture is not None:
            self.capture.stop()
        return self.status()

    def status(self) -> Optional[dict]:
        cap = self.capture
        if cap is None:
            return None
        st = cap.status()
        st["request"] = self.request
        st["saved"] = list(self.saved)
        st["saving"] = self._pending
        st["errors"] = self.errors + ([st["error"]] if st.get("error") else [])
        return st


class App:
    def __init__(self, data_dir: str, log: Callable[[str], None] = print):
        self.lib = Library(data_dir)
        self.recorder = Recorder(self.lib, log)
        self.log = log
        for name in os.listdir(self.lib.tmp_dir):  # leftovers from an interrupted run
            path = os.path.join(self.lib.tmp_dir, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)

    def state(self) -> dict:
        usage = shutil.disk_usage(self.lib.root)
        out = {"version": __version__, "capture": self.recorder.status(),
               "free_bytes": usage.free, "items": len(os.listdir(self.lib.items_dir))}
        out.update(devices())
        return out


# --------------------------------------------------------------------------
# HTTP


def _content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", filename)
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(filename))


class Handler(BaseHTTPRequestHandler):
    server_version = "sq-tool/" + __version__
    app: App  # set by serve()

    def log_message(self, fmt, *args):  # keep the container log for what matters
        pass

    # -- helpers -----------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
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
            return json.loads(data.decode() or "{}")
        except ValueError:
            raise ValueError("malformed JSON")

    def _stream(self, chunks, size: int, ctype: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", _content_disposition(filename))
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
            self._fail(404, "no such item")
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
        self._json(self.app.state())

    def api_capture_start(self, query):
        body = self._body_json()
        max_seconds = body.get("max_seconds")
        status = self.app.recorder.start(str(body.get("device") or ""), str(body.get("name") or ""),
                                         float(body.get("idle_stop", 5) or 0),
                                         float(max_seconds) if max_seconds else None)
        self._json(status)

    def api_capture_stop(self, query):
        self._json(self.app.recorder.stop())

    def api_items(self, query):
        self._json(self.app.lib.list())

    def api_item(self, item_id, query):
        self._json(self.app.lib.get(item_id))

    def api_item_update(self, item_id, query):
        body = self._body_json()
        self._json(self.app.lib.update(item_id, body.get("name"), body.get("notes")))

    def api_item_delete(self, item_id, query):
        self.app.lib.delete(item_id)
        self._json({"deleted": item_id})

    def api_item_audio(self, item_id, query):
        name = self.app.lib.get(item_id)["meta"].get("name", item_id)
        self._file(self.app.lib.audio_path(item_id), "audio/wav", name + ".wav")

    def api_import(self, query):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError("send the file as the request body (up to 4 GB)")
        filename = os.path.basename(query.get("filename", "upload"))
        tmp = os.path.join(self.app.lib.tmp_dir, "upload-%s-%s" % (uuid.uuid4().hex, re.sub(r"[^A-Za-z0-9.]", "_", filename)))
        try:
            with open(tmp, "wb") as f:
                left = length
                while left:
                    block = self.rfile.read(min(left, 1 << 20))
                    if not block:
                        raise ValueError("upload interrupted")
                    f.write(block)
                    left -= len(block)
            item_id = self.app.lib.import_file(tmp, query.get("name", ""), filename)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._json(self.app.lib.summary(item_id))

    def api_tracks(self, query):
        self._json({"tracks": self.app.lib.test_tracks(), "folder": self.app.lib.tracks_dir})

    def api_tracks_make(self, query):
        added = self.app.lib.make_test_tracks()
        self._json({"tracks": self.app.lib.test_tracks(), "added": added})

    def api_track_file(self, name, query):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.wav", name):
            raise KeyError(name)
        path = os.path.join(self.app.lib.tracks_dir, name)
        if not os.path.isfile(path):
            raise KeyError(name)
        self._file(path, "audio/wav", name)

    def api_null(self, query):
        body = self._body_json()
        self._json(self.app.lib.null_test(str(body.get("a")), str(body.get("b"))))

    def api_difference(self, a, b, query):
        chunks, size, filename = self.app.lib.difference_wav(a, b)
        self._stream(chunks, size, "audio/wav", filename)


ID = r"([A-Za-z0-9][A-Za-z0-9._-]*)"
ROUTES = [
    ("GET", r"/api/state", Handler.api_state),
    ("POST", r"/api/capture", Handler.api_capture_start),
    ("POST", r"/api/capture/stop", Handler.api_capture_stop),
    ("GET", r"/api/items", Handler.api_items),
    ("GET", r"/api/items/" + ID, Handler.api_item),
    ("PATCH", r"/api/items/" + ID, Handler.api_item_update),
    ("DELETE", r"/api/items/" + ID, Handler.api_item_delete),
    ("GET", r"/api/items/" + ID + r"/audio", Handler.api_item_audio),
    ("POST", r"/api/import", Handler.api_import),
    ("GET", r"/api/test-tracks", Handler.api_tracks),
    ("POST", r"/api/test-tracks", Handler.api_tracks_make),
    ("GET", r"/api/test-tracks/([A-Za-z0-9_.-]+)", Handler.api_track_file),
    ("POST", r"/api/null", Handler.api_null),
    ("GET", r"/api/null/" + ID + "/" + ID + r"/difference", Handler.api_difference),
]


def make_server(data_dir: str, host: str = "0.0.0.0", port: int = 3400,
                log: Callable[[str], None] = print) -> ThreadingHTTPServer:
    app = App(data_dir, log)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


def serve(data_dir: str, host: str = "0.0.0.0", port: int = 3400, log: Callable[[str], None] = print) -> None:
    httpd = make_server(data_dir, host, port, log)
    log("SQ-tool %s is running: open http://<this computer's address>:%d on your phone or tablet."
        % (__version__, httpd.server_address[1]))
    log("Data folder: %s" % os.path.abspath(data_dir))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.app.recorder.stop()  # type: ignore[attr-defined]
        httpd.server_close()
