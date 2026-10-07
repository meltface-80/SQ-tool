"""A test: one source song, what two players sent for it, and how it all compares.

Layout of a test under <data>/tests/<id>/:

    test.json              the source, the players, the captures and the results
    source.wav             the song, decoded at its own bit depth
    a.wav, b.wav           what player A and player B sent (with .json capture details)
    analysis_*.json        analysis and chart data for source, a and b
    cache/*.png            rendered spectrograms

The comparisons ("results") are source-vs-a, source-vs-b and a-vs-b.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from typing import Callable, Iterator, List, Optional, Tuple

import numpy as np

from .alsa import Capture, CaptureError, CaptureStopped, find_loopback, find_usb_dac, list_cards, usb_dacs
from .analysis import _chunks, analyze_file, compare, silence_bounds, to_float
from .plots import exact_timeline, item_plots, null_plots
from .report import comparison_lines, headline, short_verdict
from .spectrogram import difference, fetch_from, render
from .usbmon import UsbCapture, usbmon_state
from .wavio import (SIDECAR_SUFFIX, Audio, AudioFileError, float_wav_header, load_audio, read_tags,
                    read_wav, write_wav)

NATIVE_EXTENSIONS = {".wav", ".wave", ".aif", ".aiff", ".aifc"}  # read by SQ-tool itself
AUDIO_EXTENSIONS = NATIVE_EXTENSIONS | {".flac", ".m4a", ".alac", ".wv", ".ape"}
SLOTS = ("a", "b")
ITEMS = ("source",) + SLOTS
# result key -> (reference, compared)
PAIRS = {"a": ("source", "a"), "b": ("source", "b"), "ab": ("a", "b")}
# spectrogram name -> (minuend, subtrahend, result key whose level matching applies)
DIFFS = {"diff-a": ("a", "source", "a"), "diff-b": ("b", "source", "b"), "diff-ab": ("b", "a", "ab")}
DEFAULT_SETTINGS = {"players": {"a": "Roon", "b": "Mandarin"}, "device": "auto", "idle_stop": 5}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
DEVICE_RE = re.compile(r"^(auto|loopback(:\w+)?|usb(:\w+)?)$")
INDEX_TTL = 300.0  # seconds before the music folder is scanned again for search
AUDIO_CACHE_IDLE = 600.0  # decoded recordings unused this long are dropped from memory


def readable_formats() -> set:
    """Extensions of the music files SQ-tool can decode here (FLAC needs flac or ffmpeg)."""
    ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    if ffmpeg:
        return set(AUDIO_EXTENSIONS)
    return NATIVE_EXTENSIONS | ({".flac"} if shutil.which("flac") else set())


def clean(obj):
    """JSON-safe copy: NaN/inf become None, numpy values become plain Python."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return clean(obj.tolist())
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        obj = obj.item()
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(clean(data), f)
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _steps(exact: dict) -> List[List[int]]:
    """Where the capture's offset from the reference changes: [(reference frame, lag), ...]."""
    out: List[List[int]] = []
    reach = None
    for seg in exact.get("segments") or []:
        start = seg["ref_start"] if reach is None else max(seg["ref_start"], reach)
        if start < seg["ref_end"] and (not out or out[-1][1] != seg["lag"]):
            out.append([start, seg["lag"]])
        reach = seg["ref_end"] if reach is None else max(reach, seg["ref_end"])
    return out


def _alignment(res: dict, ref: Audio, cap: Audio) -> dict:
    """How the compared recording lines up with the reference (for charts and difference files)."""
    exact, approx = res.get("exact"), res.get("approx")
    out = {"lag": None, "steps": None, "model": None, "offset_seconds": None}
    if exact:
        out["lag"] = exact["lag"]
        out["steps"] = _steps(exact) or [[0, exact["lag"]]]
    if approx and approx.get("model") is not None:
        out["model"] = approx["model"]
        if out["lag"] is None:
            out["lag"] = approx["lag"]
            out["steps"] = [[0, approx["lag"]]]
    if out["lag"] is None:
        # No alignment (different sample rates, say): line up the first sounds instead.
        a, b = _first_sound(ref), _first_sound(cap)
        if a is not None and b is not None:
            out["offset_seconds"] = round(b / cap.rate - a / ref.rate, 6)
    return out


def longest_silence(audio: Audio) -> float:
    """The longest stretch of digital silence inside the music (not before or after it), in seconds."""
    first, end = silence_bounds(audio.data)
    last, best = None, 0
    for a, b in _chunks(first, end):
        idx = np.flatnonzero(audio.data[a:b].any(axis=1)) + a
        if idx.size == 0:
            continue
        if last is not None:
            best = max(best, int(idx[0] - last - 1))
        if idx.size > 1:
            best = max(best, int((np.diff(idx) - 1).max()))
        last = int(idx[-1])
    return best / audio.rate


def _first_sound(audio: Audio) -> Optional[int]:
    for a, b in _chunks(0, audio.frames):
        nz = np.flatnonzero(audio.data[a:b].any(axis=1))
        if nz.size:
            return a + int(nz[0])
    return None


class Tests:
    """All tests, the background worker that analyses them, and the settings."""

    def __init__(self, data_dir: str, music_dir: str, log: Callable[[str], None] = print):
        self.data_dir = os.path.abspath(data_dir)
        self.music_dir = os.path.abspath(music_dir)
        self.root = os.path.join(self.data_dir, "tests")
        self.tmp = os.path.join(self.data_dir, "tmp")
        for d in (self.root, self.tmp):
            os.makedirs(d, exist_ok=True)
        for name in os.listdir(self.tmp):  # leftovers from an interrupted run
            path = os.path.join(self.tmp, name)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.unlink(path)
        self.log = log
        self._lock = threading.RLock()
        self._audio_cache: "OrderedDict[str, Tuple[float, Audio, float]]" = OrderedDict()
        self._render_slots = threading.BoundedSemaphore(2)
        self._index: Optional[List[str]] = None
        self._index_time = 0.0
        self._index_lock = threading.Lock()
        self._jobs: "queue.Queue" = queue.Queue()
        self.busy: Optional[str] = None  # what the worker is doing, for the status line
        threading.Thread(target=self._worker, name="analysis", daemon=True).start()

    # -- settings ------------------------------------------------------------------

    def settings(self) -> dict:
        s = json.loads(json.dumps(DEFAULT_SETTINGS))
        stored = _read_json(os.path.join(self.data_dir, "settings.json"))
        s.update({k: v for k, v in stored.items() if k in s and k != "players"})
        s["players"].update({k: v for k, v in (stored.get("players") or {}).items() if k in SLOTS and v})
        return s

    def save_settings(self, changes: dict) -> dict:
        s = self.settings()
        if isinstance(changes.get("players"), dict):
            for k in SLOTS:
                name = str(changes["players"].get(k) or "").strip()[:40]
                if name:
                    s["players"][k] = name
        if "device" in changes:
            device = str(changes["device"] or "auto").strip()
            if not DEVICE_RE.match(device):
                raise ValueError("unknown recording device %r" % device)
            s["device"] = device
        if "idle_stop" in changes:
            try:
                s["idle_stop"] = max(0.0, min(60.0, float(changes["idle_stop"] or 0)))
            except (TypeError, ValueError):
                raise ValueError("idle_stop must be a number of seconds")
        _write_json(os.path.join(self.data_dir, "settings.json"), s)
        return s

    # -- the music folder ----------------------------------------------------------------

    def music_available(self) -> bool:
        return os.path.isdir(self.music_dir)

    def _music_path(self, rel: str) -> str:
        base = os.path.realpath(self.music_dir)
        path = os.path.realpath(os.path.join(base, rel or ""))
        if path != base and not path.startswith(base + os.sep):
            raise ValueError("that is outside the music folder")
        return path

    def browse(self, rel: str = "") -> dict:
        if not self.music_available():
            return {"path": "", "dirs": [], "files": [], "available": False, "root": self.music_dir}
        path = self._music_path(rel)
        if not os.path.isdir(path):
            raise KeyError(rel)
        dirs, files = [], []
        ok = readable_formats()
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                ext = os.path.splitext(entry.name)[1].lower()
                if entry.is_dir():
                    dirs.append(entry.name)
                elif ext in AUDIO_EXTENSIONS:
                    files.append({"name": entry.name, "size": entry.stat().st_size, "ok": ext in ok})
            except OSError:
                continue
        rel = os.path.relpath(path, os.path.realpath(self.music_dir))
        return {"path": "" if rel == "." else rel, "dirs": dirs, "files": files, "available": True,
                "root": self.music_dir}

    def _music_index(self, budget: float) -> List[str]:
        with self._index_lock:
            if self._index is not None and time.monotonic() - self._index_time < INDEX_TTL:
                return self._index
            base = os.path.realpath(self.music_dir)
            found, t0 = [], time.monotonic()
            for root, dirs, files in os.walk(base):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS and not name.startswith("."):
                        found.append(os.path.relpath(os.path.join(root, name), base))
                if time.monotonic() - t0 > budget:
                    break  # a partial index; tried again on the next search
            else:
                self._index_time = time.monotonic()
            self._index = found
            return found

    def search(self, query: str, limit: int = 100, budget: float = 8.0) -> List[dict]:
        words = [w for w in query.lower().split() if w]
        if not words or not self.music_available():
            return []
        found, ok = [], readable_formats()
        for rel in self._music_index(budget):
            low = rel.lower()
            if all(w in low for w in words):
                found.append({"path": rel, "name": os.path.basename(rel), "folder": os.path.dirname(rel),
                              "ok": os.path.splitext(low)[1] in ok})
                if len(found) >= limit:
                    break
        return found

    # -- tests -------------------------------------------------------------------------

    def _dir(self, tid: str) -> str:
        if not ID_RE.match(tid or ""):
            raise KeyError(tid)
        d = os.path.join(self.root, tid)
        if not os.path.isfile(os.path.join(d, "test.json")):
            raise KeyError(tid)
        return d

    def _load(self, tid: str) -> dict:
        return _read_json(os.path.join(self._dir(tid), "test.json"))

    def _update(self, tid: str, fn: Callable[[dict], None]) -> dict:
        with self._lock:
            path = os.path.join(self._dir(tid), "test.json")
            t = _read_json(path)
            fn(t)
            _write_json(path, t)
            return t

    def rev(self, tid: str) -> int:
        """Changes whenever the test does (for cheap polling)."""
        return os.stat(os.path.join(self._dir(tid), "test.json")).st_mtime_ns

    def create(self, music_rel: Optional[str] = None, upload: Optional[str] = None,
               filename: str = "") -> str:
        """New test from a song in the music folder (or an uploaded file)."""
        if music_rel:
            src = self._music_path(music_rel)
            if not os.path.isfile(src):
                raise KeyError(music_rel)
            filename = os.path.basename(src)
            ext = os.path.splitext(filename)[1].lower()
            if ext in AUDIO_EXTENSIONS and ext not in readable_formats():
                raise ValueError("SQ-tool can't read %s files here: choose the FLAC, WAV or AIFF version "
                                 "of the song" % ext)
        elif upload is not None:
            src = upload
        else:
            raise ValueError("choose a song")
        tags = read_tags(src)
        title = tags.get("title") or os.path.splitext(filename)[0]
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "test"
        with self._lock:
            tid, n = "%s-%s" % (stamp, slug), 2
            while os.path.exists(os.path.join(self.root, tid)):
                tid, n = "%s-%s-%d" % (stamp, slug, n), n + 1
            d = os.path.join(self.root, tid)
            os.makedirs(os.path.join(d, "cache"))
            test = {"id": tid, "created": _now(), "title": title, "artist": tags.get("artist"),
                    "album": tags.get("album"),
                    "source": {"state": "preparing", "filename": filename, "music_path": music_rel or None},
                    "players": dict(self.settings()["players"]), "captures": {}, "results": {}}
            _write_json(os.path.join(d, "test.json"), test)
        self._jobs.put(("source", tid, src, upload is not None))
        return tid

    def list(self) -> List[dict]:
        out = []
        for tid in os.listdir(self.root):
            t = _read_json(os.path.join(self.root, tid, "test.json"))
            if not t:
                continue
            src = t.get("source") or {}
            out.append({"id": tid, "title": t.get("title"), "artist": t.get("artist"), "created": t.get("created"),
                        "players": t.get("players"),
                        "source": {k: src.get(k) for k in ("label", "rate", "bits", "duration", "state")},
                        "captures": {k: c.get("state") for k, c in (t.get("captures") or {}).items()},
                        "results": {k: {"verdict": r.get("verdict"), "short": r.get("short"), "match": r.get("match")}
                                    for k, r in (t.get("results") or {}).items()}})
        out.sort(key=lambda x: x.get("created") or "", reverse=True)
        return out

    def get(self, tid: str) -> dict:
        d = self._dir(tid)
        with self._lock:
            t = self._load(tid)
            t["rev"] = self.rev(tid)
        t["analysis"] = {w: _read_json(os.path.join(d, "analysis_%s.json" % w))
                         for w in ITEMS if os.path.exists(os.path.join(d, "analysis_%s.json" % w))}
        return t

    def set_players(self, tid: str, players: dict) -> dict:
        def fn(t):
            for k in SLOTS:
                name = str((players or {}).get(k) or "").strip()[:40]
                if name:
                    t["players"][k] = name
        return self._update(tid, fn)

    def delete(self, tid: str) -> None:
        d = self._dir(tid)
        with self._lock:
            trash = os.path.join(self.tmp, "deleted-%s-%s" % (tid, uuid.uuid4().hex[:6]))
            os.rename(d, trash)  # gone at once; anything still writing into it fails harmlessly
        self._forget(d)
        shutil.rmtree(trash, ignore_errors=True)

    def audio_path(self, tid: str, which: str) -> str:
        if which not in ITEMS:
            raise KeyError(which)
        path = os.path.join(self._dir(tid), "%s.wav" % which)
        if not os.path.exists(path):
            raise KeyError(which)
        return path

    def audio_filename(self, tid: str, which: str) -> str:
        t = self._load(tid)
        who = "source" if which == "source" else t["players"][which]
        return _filename("%s - %s.wav" % (t.get("title") or tid, who))


    # -- audio cache ---------------------------------------------------------------------

    def _audio(self, path: str) -> Audio:
        mtime = os.path.getmtime(path)
        with self._lock:
            hit = self._audio_cache.get(path)
            if hit and hit[0] == mtime:
                self._audio_cache[path] = (mtime, hit[1], time.monotonic())
                self._audio_cache.move_to_end(path)
                return hit[1]
        audio = read_wav(path)
        with self._lock:
            self._audio_cache[path] = (mtime, audio, time.monotonic())
            while len(self._audio_cache) > 4:
                self._audio_cache.popitem(last=False)
        return audio

    def _forget(self, prefix: str) -> None:
        with self._lock:
            for k in [k for k in self._audio_cache if k.startswith(prefix)]:
                del self._audio_cache[k]

    def _expire_audio(self) -> None:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._audio_cache.items() if now - v[2] > AUDIO_CACHE_IDLE]:
                del self._audio_cache[k]

    # -- background work ---------------------------------------------------------------------

    def add_capture(self, tid: str, slot: str, wav_path: str) -> None:
        """A finished recording for player `slot`: file it and analyse it in the background."""
        def fn(t):
            t["captures"][slot] = {"state": "analysing"}
            for key in [k for k in t["results"] if slot in PAIRS[k]]:
                del t["results"][key]  # out of date
        self._update(tid, fn)
        self._jobs.put(("capture", tid, slot, wav_path))

    def idle(self) -> bool:
        return self.busy is None and self._jobs.empty()

    def _worker(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=60)
            except queue.Empty:
                self._expire_audio()
                continue
            self.busy = "%s %s" % (job[0], job[1])
            try:
                if job[0] == "source":
                    self._do_source(*job[1:])
                else:
                    self._do_capture(*job[1:])
            except KeyError:
                if job[0] == "capture":  # the test was deleted meanwhile: drop its recording
                    rec_dir = os.path.dirname(os.path.abspath(job[3]))
                    if os.path.dirname(rec_dir) == self.tmp:
                        shutil.rmtree(rec_dir, ignore_errors=True)
            except Exception as exc:
                if isinstance(exc, AudioFileError):
                    self.log("Test %s: %s" % (job[1], exc))
                else:
                    traceback.print_exc()
                tid = job[1]
                where = "source" if job[0] == "source" else job[2]

                def fail(t, where=where, msg=str(exc) or exc.__class__.__name__):
                    target = t["source"] if where == "source" else t["captures"].setdefault(where, {})
                    target.update({"state": "error", "message": msg})
                    for r in t.get("results", {}).values():
                        if r.get("state") == "comparing":
                            r.update({"state": "error", "message": msg})
                try:
                    self._update(tid, fail)
                except KeyError:
                    pass
            finally:
                self.busy = None
                if job[0] == "source" and job[3] and os.path.exists(job[2]):
                    os.unlink(job[2])  # the uploaded file

    def _do_source(self, tid: str, src: str, uploaded: bool) -> None:
        d = self._dir(tid)
        try:
            audio = load_audio(src)
        except AudioFileError as exc:
            name = (self._load(tid).get("source") or {}).get("filename") or "the file"
            raise AudioFileError("could not read the song: %s" % str(exc).replace(src, name))
        if audio.meta.get("lossy_source"):
            raise AudioFileError("this is a lossy file (%s): use the lossless original"
                                 % audio.meta["lossy_source"])
        label = audio.label
        dest = os.path.join(d, "source.wav")
        write_wav(dest, audio)
        del audio
        audio = self._audio(dest)
        analysis = analyze_file(audio)
        analysis["plots"] = item_plots(audio)
        analysis["label"] = label
        _write_json(os.path.join(d, "analysis_source.json"), analysis)

        def done(t):
            t["source"].update({"state": "ready", "label": label, "rate": audio.rate, "bits": audio.bits,
                                "channels": audio.channels, "duration": audio.duration,
                                "resolution": analysis.get("resolution"),
                                "longest_silence": round(longest_silence(audio), 3),
                                "fingerprint": analysis["fingerprint"]})
        self._update(tid, done)
        for slot in SLOTS:  # captures made before the source was ready
            if os.path.exists(os.path.join(d, "%s.wav" % slot)):
                self._compare_pairs(tid, slot)

    def _do_capture(self, tid: str, slot: str, wav_path: str) -> None:
        d = self._dir(tid)
        dest = os.path.join(d, "%s.wav" % slot)
        self._forget(dest)
        shutil.move(wav_path, dest)
        if os.path.exists(wav_path + SIDECAR_SUFFIX):
            shutil.move(wav_path + SIDECAR_SUFFIX, dest + SIDECAR_SUFFIX)
        rec_dir = os.path.dirname(os.path.abspath(wav_path))
        if os.path.dirname(rec_dir) == self.tmp:
            shutil.rmtree(rec_dir, ignore_errors=True)
        self._clear_cache(d)
        audio = self._audio(dest)
        analysis = analyze_file(audio)
        analysis["plots"] = item_plots(audio)
        _write_json(os.path.join(d, "analysis_%s.json" % slot), analysis)
        cap = audio.meta.get("capture") or {}
        usb = cap.get("usb") or {}

        def done(t):
            t["captures"][slot] = {
                "state": "ready", "format": cap.get("alsa_format"), "rate": audio.rate,
                "channels": audio.channels, "seconds": round(audio.duration, 3),
                "player": (cap.get("player") or {}).get("name"), "method": cap.get("method", "loopback"),
                "recorded": cap.get("started"), "fingerprint": analysis["fingerprint"],
                "resolution": analysis.get("resolution"), "stop_reason": cap.get("stop_reason"),
                "period_size": cap.get("period_size"), "buffer_size": cap.get("buffer_size"),
                "missed_frames": cap.get("player_frames_before_capture"),
                "overruns": cap.get("capture_overruns", 0), "player_xruns": cap.get("player_xruns_seen", 0),
                "interruptions": len(cap.get("interruptions") or []),
                "usb_problems": (usb.get("iso_errors", 0) + usb.get("usbmon_dropped", 0)
                                 + usb.get("uncaptured_transfers", 0)) if usb else 0,
            }
            for key in [k for k in t["results"] if slot in PAIRS[k]]:
                del t["results"][key]
        self._update(tid, done)
        self._compare_pairs(tid, slot)

    def _clear_cache(self, d: str) -> None:
        cache = os.path.join(d, "cache")
        for f in os.listdir(cache):
            try:
                os.unlink(os.path.join(cache, f))
            except OSError:
                pass

    def _compare_pairs(self, tid: str, slot: str) -> None:
        t = self._load(tid)
        d = self._dir(tid)
        if t["source"].get("state") != "ready":
            return
        for key, (ra, rb) in PAIRS.items():
            if slot not in (ra, rb) or not all(os.path.exists(os.path.join(d, "%s.wav" % w)) for w in (ra, rb)):
                continue
            self._update(tid, lambda t, key=key: t["results"].__setitem__(key, {"state": "comparing"}))
            result = self._compare(d, ra, rb)
            self._update(tid, lambda t, key=key, result=result: t["results"].__setitem__(key, result))
        self._clear_cache(d)  # alignments changed

    def _compare(self, d: str, ra: str, rb: str) -> dict:
        ref = self._audio(os.path.join(d, "%s.wav" % ra))
        cap = self._audio(os.path.join(d, "%s.wav" % rb))
        res = compare(ref, cap)
        exact, approx = res.get("exact"), res.get("approx")
        plots: dict = {}
        if exact and res["verdict"] in ("IDENTICAL", "PARTIAL", "GAPS", "ALTERED"):
            plots["timeline"] = exact_timeline(exact, ref.rate)
        if approx and approx.get("model") is not None:
            plots.update(null_plots(ref, cap, approx["lag"], np.array(approx["model"])))
        verdict = res["verdict"]
        return {
            "state": "ready",
            "verdict": verdict,
            "short": short_verdict(res),
            "headline": headline(res),
            "lines": comparison_lines(res),
            "match": verdict in ("IDENTICAL", "PARTIAL"),
            "null_db": (approx or {}).get("null_depth_db"),
            "gain_db": (approx or {}).get("gain_db"),
            "residual_rms_db": (approx or {}).get("residual_rms_db"),
            "identical_fraction": (exact or {}).get("identical_audio_fraction"),
            "missing_start_seconds": exact["missing_start"] / ref.rate if exact else None,
            "missing_start_silent": exact.get("missing_start_silent") if exact else None,
            "events": len(exact["events"]) if exact else None,
            "alignment": _alignment(res, ref, cap),
            "plots": plots,
            "zoom": _zoom(ref, cap, res),
            "created": _now(),
        }

    # -- outputs ------------------------------------------------------------------------------

    def _placement(self, tid: str):
        """The recordings of a test and how each sits on the source's timeline."""
        d = self._dir(tid)
        t = self._load(tid)
        audio = {w: self._audio(os.path.join(d, "%s.wav" % w))
                 for w in ITEMS if os.path.exists(os.path.join(d, "%s.wav" % w))}
        if "source" not in audio:
            raise KeyError("source")
        place = {"source": 0}
        for slot in SLOTS:
            if slot not in audio:
                continue
            al = (t["results"].get(slot) or {}).get("alignment") or {}
            if al.get("steps"):
                place[slot] = [tuple(s) for s in al["steps"]]
            elif al.get("offset_seconds") is not None:
                place[slot] = int(round(al["offset_seconds"] * audio[slot].rate))
        al = (t["results"].get("ab") or {}).get("alignment") or {}
        if al.get("steps") and len(al["steps"]) == 1:
            ab = al["steps"][0][1]
            if "a" in place and "b" in audio and "b" not in place and not isinstance(place["a"], int):
                place["b"] = [(s, lag + ab) for s, lag in place["a"]]
            elif "b" in place and "a" in audio and "a" not in place and not isinstance(place["b"], int):
                place["a"] = [(s, lag - ab) for s, lag in place["b"]]
        for slot in SLOTS:
            if slot in audio and slot not in place:
                place[slot] = 0
        return t, audio, place

    def _diff_fetch(self, t: dict, audio: dict, place: dict, name: str, matched: bool):
        plus, minus, key = DIFFS[name]
        if plus not in audio or minus not in audio:
            raise KeyError(name)
        a, b = audio[plus], audio[minus]
        if a.rate != b.rate:
            raise ValueError("%s and %s have different sample rates (%d and %d Hz): the samples cannot be "
                             "subtracted" % (self._who(t, plus), self._who(t, minus), a.rate, b.rate))
        if a.channels != b.channels:
            raise ValueError("%s and %s have different channel counts"
                             % (self._who(t, plus), self._who(t, minus)))
        model = None
        if matched:
            m = ((t["results"].get(key) or {}).get("alignment") or {}).get("model")
            model = np.array(m) if m is not None else None
        return difference(fetch_from(a, place[plus]), fetch_from(b, place[minus]), model), a

    @staticmethod
    def _who(t: dict, w: str) -> str:
        return "the source" if w == "source" else t["players"][w]

    def spectrogram(self, tid: str, which: str, t0: float, t1: float, width: int, height: int,
                    scale: str = "log", db_low: float = -150.0, matched: bool = False) -> Tuple[bytes, dict]:
        """A spectrogram of the source, a capture or a difference, on the source's timeline."""
        if which not in ITEMS and which not in DIFFS:
            raise KeyError(which)
        scale = "linear" if scale == "linear" else "log"
        db_low = float(min(-20.0, max(-300.0, db_low)))
        width = int(min(max(width, 64), 4096))
        height = int(min(max(height, 64), 2048))
        t, audio, place = self._placement(tid)
        fmax = max(a.rate for a in audio.values()) / 2.0
        if which in DIFFS:
            fetch, item = self._diff_fetch(t, audio, place, which, matched)
            used = [DIFFS[which][0], DIFFS[which][1]]
        else:
            if which not in audio:
                raise KeyError(which)
            item = audio[which]
            fetch = fetch_from(item, place[which])
            used = [which]
            matched = False
        src = audio["source"]
        least = 64.0 / item.rate
        t0 = min(max(0.0, float(t0)), max(0.0, src.duration - least))
        t1 = float(t1) if t1 and t1 > t0 else src.duration
        t1 = max(min(t1, src.duration), t0 + least)
        sig = json.dumps([which, round(t0, 6), round(t1, 6), width, height, scale, db_low, matched, fmax,
                          [place[u] for u in used], [os.path.getmtime(a.path) for a in audio.values() if a.path]],
                         default=list)
        key = hashlib.sha1(sig.encode()).hexdigest()[:20]
        cache = os.path.join(self._dir(tid), "cache", key + ".png")
        info_path = cache[:-4] + ".json"
        if os.path.exists(cache) and os.path.exists(info_path):
            with open(cache, "rb") as f:
                return f.read(), _read_json(info_path)
        start, end = int(round(t0 * item.rate)), int(round(t1 * item.rate))
        with self._render_slots:
            png, info = render(fetch, item.rate, start, end, width, height, scale, 20.0, db_low, 0.0, fmax)
        info.update({"which": which, "t0": start / item.rate, "t1": end / item.rate,
                     "rate": item.rate, "scale": scale, "db_low": db_low, "db_high": 0.0, "matched": matched,
                     "width": width, "height": height})
        tmp = cache + ".%s.tmp" % uuid.uuid4().hex[:6]
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, cache)
        _write_json(info_path, info)
        self._trim_cache(os.path.dirname(cache))
        return png, info

    @staticmethod
    def _trim_cache(d: str, keep: int = 400) -> None:
        """Forget the oldest rendered spectrograms once there are many (every zoom makes new ones)."""
        try:
            names = os.listdir(d)
            if len(names) <= keep:
                return
            paths = sorted((os.path.join(d, n) for n in names), key=os.path.getmtime)
            for path in paths[:len(paths) - keep // 2]:
                os.unlink(path)
        except OSError:
            pass

    def difference_wav(self, tid: str, key: str, matched: bool = False) -> Tuple[Iterator[bytes], int, str]:
        """A difference on the source's timeline as a 32-bit float WAV (exact unless `matched`)."""
        name = {"a": "diff-a", "b": "diff-b", "ab": "diff-ab"}.get(key)
        if name is None:
            raise KeyError(key)
        t, audio, place = self._placement(tid)
        fetch, item = self._diff_fetch(t, audio, place, name, matched)
        frames = int(round(audio["source"].duration * item.rate))
        header = float_wav_header(item.rate, item.channels, frames)

        def chunks() -> Iterator[bytes]:
            yield header
            for s in range(0, frames, 1 << 16):
                yield fetch(s, min(frames, s + (1 << 16))).astype("<f4").tobytes()

        plus, minus, _ = DIFFS[name]
        filename = _filename("%s - %s minus %s%s.wav" % (t.get("title") or tid, self._who(t, plus),
                                                          self._who(t, minus), " (levels matched)" if matched else ""))
        return chunks(), len(header) + frames * item.channels * 4, filename


def _filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip() or "audio.wav"


def _zoom(ref: Audio, cap: Audio, res: dict, half: int = 96) -> Optional[dict]:
    """A few hundred samples of both waveforms around the largest (or first) difference."""
    exact, approx = res.get("exact"), res.get("approx")
    if res["verdict"] == "IDENTICAL" or ref.rate != cap.rate or ref.channels != cap.channels:
        return None
    why = "first"
    if exact and exact.get("events"):
        ev = exact["events"][0]
        at, lag = ev["ref"], ev["lag_before"]
    elif exact and exact.get("differs_from") is not None:
        at, lag = exact["differs_from"], exact["segments"][-1]["lag"] if exact["segments"] else exact["lag"]
    elif approx and approx.get("lag") is not None:
        why = "largest"
        lag = approx["lag"]
        a, b = max(0, -lag), min(ref.frames, cap.frames - lag)
        best, at = -1.0, None
        for s, e in _chunks(a, b):
            diff = np.abs(to_float(cap, s + lag, e + lag) - to_float(ref, s, e)).max(axis=1)
            k = int(np.argmax(diff))
            if diff[k] > best:
                best, at = float(diff[k]), s + k
        if at is None:
            return None
    else:
        return None
    s = max(0, max(-lag, at - half))
    e = min(ref.frames, cap.frames - lag, at + half)
    if e - s < 8:
        return None
    r = to_float(ref, s, e)
    c = to_float(cap, s + lag, e + lag)
    ch = int(np.argmax(np.abs(c - r).max(axis=0)))
    return {"start_seconds": s / ref.rate, "rate": ref.rate, "channel": ch + 1, "at_seconds": at / ref.rate,
            "why": why, "ref": [round(float(v), 9) for v in r[:, ch]], "cap": [round(float(v), 9) for v in c[:, ch]]}


# --------------------------------------------------------------------------
# Recording


NO_LOOPBACK = ("no Loopback sound card found. On the server, run: sudo modprobe snd-aloop "
               "(or start the container with -v /lib/modules:/lib/modules:ro so SQ-tool can load it)")


def resolve_device(setting: str = "auto") -> str:
    """The device a recording uses for the "Record from" setting: auto, loopback or usb[:card].

    Automatic means the Loopback card or, failing that, a USB DAC if usbmon is there to record it.
    """
    kind, _, ref = (setting or "auto").partition(":")
    if kind in ("auto", "loopback"):
        loop = find_loopback(list_cards())
        if loop:
            return "loopback:%d" % loop.index
        if kind == "loopback":
            raise CaptureError(NO_LOOPBACK)
    if kind in ("auto", "usb"):
        dacs = usb_dacs()
        if kind == "auto":
            dacs = [d for d in dacs if usbmon_state(d.bus) == "ready"]
        if dacs:
            pick = [d for d in dacs if str(d.card.index) == ref] or dacs
            return "usb:%d" % pick[0].card.index
        if kind == "usb":
            raise CaptureError("no USB DAC found. Is it connected and switched on?")
    raise CaptureError(NO_LOOPBACK)


class Recorder:
    """Records one player at a time for a test, then hands the recording to Tests."""

    def __init__(self, tests: Tests, log: Callable[[str], None] = print, arecord: Optional[str] = None):
        self.tests = tests
        self.log = log
        self.arecord = arecord or os.environ.get("SQTOOL_ARECORD", "arecord")
        self.capture = None
        self.info: dict = {}
        self.max_wait_audio = 600.0  # stop waiting for the music after 10 minutes
        self._out_dir: Optional[str] = None
        self._runner: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _filing(self) -> bool:
        """The recording has ended but is still being filed (or its error noted)."""
        return self._runner is not None and self._runner.is_alive()

    def active(self) -> bool:
        if self.capture is None:
            return False
        return self._filing() or self.capture.state not in ("done", "error")

    def start(self, tid: str, slot: str) -> dict:
        if slot not in SLOTS:
            raise ValueError("unknown player %r" % slot)
        test = self.tests.get(tid)
        src = test["source"]
        if src.get("state") != "ready":
            # Its length sets when the recording stops: wait for it.
            raise ValueError("the song is still being analysed: try again in a moment"
                             if src.get("state") == "preparing" else
                             "the song could not be read, so there is nothing to compare with")
        with self._lock:
            if self.active():
                raise CaptureError("a recording is already running: stop it first")
            self._discard_unsaved()
            settings = self.tests.settings()
            device = resolve_device(settings.get("device") or "auto")
            # Stop at the end of the song: its length after the music starts, with a margin for
            # silence the player adds; and after a silence longer than any inside the song.
            expected = src.get("duration")
            limit = expected + 5.0 if expected else None
            idle = float(settings.get("idle_stop", 5) or 0)
            if idle and src.get("longest_silence"):
                idle = max(idle, src["longest_silence"] + 2.0)
            out_dir = os.path.join(self.tests.tmp, "rec-%s" % uuid.uuid4().hex[:8])
            os.makedirs(out_dir)
            self._out_dir = out_dir
            kind, _, ref = device.partition(":")
            # With the song's length known, its end is timed from the first sound: a player
            # may send silence for a long time first (Squeezelite keeps its output open).
            cap_seconds = None if limit else 1800.0
            if kind == "usb":
                cap = UsbCapture(find_usb_dac(ref or None), out_dir, name=slot, idle_stop=idle,
                                 max_seconds=cap_seconds, log=self.log, stop_after_audio=limit,
                                 max_wait_audio=self.max_wait_audio)
                cap.on_take = lambda path, meta, cap=cap: self._take(tid, slot, path, cap)
            elif kind == "loopback":
                cap = Capture(os.path.join(out_dir, "%s.wav" % slot), card=ref or None, silence_stop=idle,
                              arecord=self.arecord, log=self.log, handle_sigint=False,
                              stop_after_audio=limit, duration=cap_seconds, wait=self.max_wait_audio,
                              max_wait_audio=self.max_wait_audio)
            else:
                raise CaptureError("unknown recording device %r" % device)
            self.capture = cap
            self.info = {"test": tid, "slot": slot, "player": test["players"][slot], "device": device,
                         "expected_seconds": expected, "saved": False, "error": None}
            if kind == "usb":
                cap.start()
                self._runner = cap._thread
            else:
                self._runner = threading.Thread(target=self._run_loopback, args=(cap, tid, slot),
                                                name="loopback-capture", daemon=True)
                self._runner.start()
        return self.status()

    def _take(self, tid: str, slot: str, path: str, cap) -> None:
        if self.info.get("saved"):
            return  # one recording per player: keep the first take
        self.info["saved"] = True
        try:
            self.tests.add_capture(tid, slot, path)
        except KeyError:
            self.info["error"] = "the test was deleted"
        cap.stop("recorded")

    def _run_loopback(self, cap: Capture, tid: str, slot: str) -> None:
        try:
            cap.run()
            self.info["saved"] = True
            self.tests.add_capture(tid, slot, cap.out_path)
        except CaptureStopped:
            pass
        except KeyError:
            self.info["error"] = "the test was deleted"
        except Exception as exc:
            self.info["error"] = str(exc)
            cap.message = str(exc)
            cap.state = "error"
            return
        finally:
            if not self.info.get("saved") or self.info.get("error"):
                shutil.rmtree(os.path.dirname(cap.out_path), ignore_errors=True)
        cap.state = "done"

    def _discard_unsaved(self) -> None:
        """Remove what the previous recording left behind if it saved nothing (stopped early, say)."""
        if self._out_dir and not self.info.get("saved"):
            shutil.rmtree(self._out_dir, ignore_errors=True)
        self._out_dir = None

    def stop(self, tid: Optional[str] = None) -> Optional[dict]:
        """Stop the recording (only if it belongs to test `tid`, when given)."""
        if self.capture is not None and (tid is None or self.info.get("test") == tid):
            self.capture.stop()
        return self.status()

    def status(self) -> Optional[dict]:
        cap = self.capture
        if cap is None:
            return None
        st = cap.status()
        if st["state"] in ("done", "error") and self._filing():
            st["state"] = "stopping"
        st.update(self.info)
        if st.get("error") is None and cap.state == "error":
            st["error"] = getattr(cap, "error", None) or cap.message
        return st


__all__ = ["Tests", "Recorder", "resolve_device", "clean"]
