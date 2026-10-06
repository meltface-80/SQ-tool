"""The saved captures and reference files behind the web interface.

Layout under the data directory:

    library/<id>/audio.wav        the samples (captures exactly as the DAC received them)
    library/<id>/audio.wav.json   capture details (format, player, USB statistics...), if a capture
    library/<id>/meta.json        name, kind (capture or reference), notes, creation time
    library/<id>/analysis.json    the analysis and the curves the web page draws
    nulls/<a>__<b>.json           cached null tests
    test-tracks/                  generated test tracks (mount this into your music library)
    tmp/                          captures in progress and uploads
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import shutil
import threading
import uuid
from typing import Iterator, List, Optional, Tuple

import numpy as np

from .analysis import analyze_file, compare, to_float
from .generate import DEFAULT_SET, test_track_name, write_test_track
from .plots import exact_timeline, item_plots, null_plots
from .report import comparison_lines, headline, short_verdict
from .wavio import SIDECAR_SUFFIX, AudioFileError, float_wav_header, load_audio, read_wav, write_wav

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
SUMMARY_KEYS = ("label", "rate", "channels", "duration", "bits", "is_float", "resolution", "fingerprint",
                "dop", "silent")


def clean(obj):
    """JSON-safe copy: no NaN/inf (they become None), no numpy scalars or arrays."""
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


class Library:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.items_dir = os.path.join(self.root, "library")
        self.nulls_dir = os.path.join(self.root, "nulls")
        self.tmp_dir = os.path.join(self.root, "tmp")
        self.tracks_dir = os.path.join(self.root, "test-tracks")
        for d in (self.items_dir, self.nulls_dir, self.tmp_dir, self.tracks_dir):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        self.compute_lock = threading.Lock()  # one heavy analysis at a time keeps memory in check

    # -- items ------------------------------------------------------------------

    def _dir(self, item_id: str) -> str:
        if not ID_RE.match(item_id or ""):
            raise KeyError(item_id)
        d = os.path.join(self.items_dir, item_id)
        if not os.path.isdir(d):
            raise KeyError(item_id)
        return d

    def audio_path(self, item_id: str) -> str:
        return os.path.join(self._dir(item_id), "audio.wav")

    def _new_id(self, name: str) -> str:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "item"
        base = "%s-%s" % (stamp, slug)
        item_id, n = base, 2
        while os.path.exists(os.path.join(self.items_dir, item_id)):
            item_id = "%s-%d" % (base, n)
            n += 1
        return item_id

    def add_wav(self, wav_path: str, name: str, kind: str, extra: Optional[dict] = None) -> str:
        """Move a WAV file (and its capture sidecar, if any) into the library and analyse it."""
        with self._lock:
            item_id = self._new_id(name)
            d = os.path.join(self.items_dir, item_id)
            os.makedirs(d)
        dest = os.path.join(d, "audio.wav")
        shutil.move(wav_path, dest)
        if os.path.exists(wav_path + SIDECAR_SUFFIX):
            shutil.move(wav_path + SIDECAR_SUFFIX, dest + SIDECAR_SUFFIX)
        meta = {"id": item_id, "name": name, "kind": kind, "created": _now(), "notes": ""}
        meta.update(extra or {})
        _write_json(os.path.join(d, "meta.json"), meta)
        try:
            self.analyze(item_id)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return item_id

    def import_file(self, src_path: str, name: str, filename: str = "") -> str:
        """Add a source file (WAV, FLAC, ...) as a reference, stored as WAV at its own bit depth."""
        audio = load_audio(src_path)
        tmp = os.path.join(self.tmp_dir, "import-%s.wav" % uuid.uuid4().hex)
        write_wav(tmp, audio)
        extra = {"source": {"filename": filename or os.path.basename(src_path), "format": audio.label}}
        if audio.meta.get("lossy_source"):
            extra["source"]["lossy"] = audio.meta["lossy_source"]
        return self.add_wav(tmp, name or os.path.splitext(filename)[0] or "reference", "reference", extra)

    def analyze(self, item_id: str) -> dict:
        path = self.audio_path(item_id)
        with self.compute_lock:
            audio = read_wav(path)
            result = analyze_file(audio)
            result["plots"] = item_plots(audio)
        _write_json(os.path.join(self._dir(item_id), "analysis.json"), result)
        return result

    def get(self, item_id: str) -> dict:
        d = self._dir(item_id)
        return {"meta": _read_json(os.path.join(d, "meta.json")),
                "analysis": _read_json(os.path.join(d, "analysis.json"))}

    def summary(self, item_id: str) -> dict:
        d = self._dir(item_id)
        meta = _read_json(os.path.join(d, "meta.json"))
        analysis = _read_json(os.path.join(d, "analysis.json"))
        out = dict(meta)
        out.update({k: analysis.get(k) for k in SUMMARY_KEYS})
        cap = analysis.get("capture") or {}
        out["player"] = (cap.get("player") or {}).get("name")
        out["alsa_format"] = cap.get("alsa_format")
        out["method"] = cap.get("method")
        return out

    def list(self) -> List[dict]:
        items = []
        for item_id in os.listdir(self.items_dir):
            try:
                if os.path.exists(os.path.join(self.items_dir, item_id, "analysis.json")):
                    items.append(self.summary(item_id))
            except KeyError:
                continue
        items.sort(key=lambda x: x.get("created", ""), reverse=True)
        return items

    def update(self, item_id: str, name: Optional[str] = None, notes: Optional[str] = None) -> dict:
        path = os.path.join(self._dir(item_id), "meta.json")
        with self._lock:
            meta = _read_json(path)
            if name is not None and name.strip():
                meta["name"] = name.strip()[:200]
            if notes is not None:
                meta["notes"] = notes[:5000]
            _write_json(path, meta)
        return meta

    def delete(self, item_id: str) -> None:
        d = self._dir(item_id)
        with self._lock:
            shutil.rmtree(d)
            for f in os.listdir(self.nulls_dir):
                if item_id in f[:-len(".json")].split("__"):
                    os.unlink(os.path.join(self.nulls_dir, f))

    # -- null tests -------------------------------------------------------------

    def _null_path(self, a: str, b: str) -> str:
        return os.path.join(self.nulls_dir, "%s__%s.json" % (a, b))

    def null_test(self, a: str, b: str) -> dict:
        """Compare item b against item a (the reference) sample by sample. Cached."""
        pa, pb = self.audio_path(a), self.audio_path(b)
        cache = self._null_path(a, b)
        cached = _read_json(cache)
        if cached:
            return cached
        with self.compute_lock:
            ref, cap = read_wav(pa), read_wav(pb)
            res = compare(ref, cap)
            plots: dict = {}
            exact = res.get("exact")
            if exact and res["verdict"] in ("IDENTICAL", "PARTIAL", "GAPS", "ALTERED"):
                plots["timeline"] = exact_timeline(exact, ref.rate)
            approx = res.get("approx")
            # How b lines up with a: the offset, and the level/channel match used for the difference.
            alignment = {"lag": exact["lag"] if exact else 0, "model": np.eye(ref.channels).tolist(),
                         "matched": False}
            if approx and approx.get("model") is not None:
                plots.update(null_plots(ref, cap, approx["lag"], np.array(approx["model"])))
                alignment = {"lag": approx["lag"], "model": approx["model"], "matched": True}
        out = {
            "a": a, "b": b,
            "verdict": res["verdict"],
            "short": short_verdict(res),
            "headline": headline(res),
            "lines": comparison_lines(res),
            "ref_is_capture": res.get("ref_is_capture"),
            "null_db": (approx or {}).get("null_depth_db"),
            "gain_db": (approx or {}).get("gain_db"),
            "residual_rms_db": (approx or {}).get("residual_rms_db"),
            "exact": {k: v for k, v in (exact or {}).items() if k not in ("segments",)} or None,
            "plots": plots,
            "alignment": alignment,
            "created": _now(),
        }
        _write_json(cache, out)
        return clean(out)

    def difference_wav(self, a: str, b: str) -> Tuple[Iterator[bytes], int, str]:
        """b minus the reference a, as 32-bit float WAV: the audible part of a null test.

        Where the levels or channels differ, a is first matched to b, so the file holds only
        what a level change cannot explain. Returns (chunks, total size, file name)."""
        null = self.null_test(a, b)
        ref, cap = read_wav(self.audio_path(a)), read_wav(self.audio_path(b))
        if ref.rate != cap.rate or ref.channels != cap.channels:
            raise ValueError("the two files have different sample rates or channel counts")
        lag = int(null["alignment"]["lag"])
        model = np.array(null["alignment"]["model"], dtype=np.float64)
        start, end = max(0, -lag), min(ref.frames, cap.frames - lag)
        frames = max(0, end - start)
        header = float_wav_header(ref.rate, ref.channels, frames)

        def chunks() -> Iterator[bytes]:
            yield header
            for s in range(start, end, 1 << 16):
                e = min(end, s + (1 << 16))
                diff = to_float(cap, s + lag, e + lag) - to_float(ref, s, e) @ model
                yield diff.astype("<f4").tobytes()

        names = [self.get(x)["meta"].get("name", x) for x in (b, a)]
        filename = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", "difference - %s vs %s.wav" % tuple(names))
        return chunks(), len(header) + frames * ref.channels * 4, filename

    # -- test tracks -------------------------------------------------------------

    def test_tracks(self) -> List[dict]:
        out = []
        for name in sorted(os.listdir(self.tracks_dir)):
            path = os.path.join(self.tracks_dir, name)
            if name.endswith(".wav") and os.path.isfile(path):
                out.append({"name": name, "size": os.path.getsize(path)})
        return out

    def make_test_tracks(self) -> List[str]:
        """Write the standard test tracks and add each one to the library as a reference."""
        known = {(i.get("source") or {}).get("test_track") for i in self.list()}
        added = []
        for bits, rate in DEFAULT_SET:
            name = test_track_name(bits, rate)
            path = os.path.join(self.tracks_dir, name)
            if not os.path.exists(path):
                write_test_track(self.tracks_dir, bits, rate)
            if name not in known:
                tmp = os.path.join(self.tmp_dir, "track-%s.wav" % uuid.uuid4().hex)
                shutil.copyfile(path, tmp)
                label = "Test track %d-bit %g kHz" % (bits, rate / 1000)
                added.append(self.add_wav(tmp, label, "reference",
                                          {"source": {"filename": name, "test_track": name}}))
        return added


__all__ = ["Library", "clean", "AudioFileError"]
