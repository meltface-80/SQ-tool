"""ALSA side: find the loopback card, see what players send, and capture it.

How the capture works: the snd-aloop kernel module provides a virtual sound
card whose playback side is wired to its capture side. A player plays to
hw:Loopback,0 (or ,1) exactly as it would to a USB DAC, and arecord records the
other end, sample for sample.

One detail matters for honest results: the first side of a loopback "cable"
to be set up dictates the sample format and rate to the other side. So by
default sq-tool waits until the player has opened its end and then opens the
capture end with exactly the same parameters. It closes again as soon as the
player stops, so the player is free to choose a new format for the next track.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import __version__
from .wavio import ALSA_FORMATS, SIDECAR_SUFFIX, WavWriter

ACTIVE_STATES = {"PREPARED", "RUNNING", "XRUN", "DRAINING", "PAUSED"}

LOOPBACK_HELP = """No ALSA loopback card found. Load the loopback driver with:

    sudo modprobe snd-aloop

To load it at every boot as well:

    echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf

Then restart Roon Server (or your player) so it lists the new "Loopback" device."""


class CaptureError(Exception):
    pass


def proc_asound() -> str:
    return os.environ.get("SQTOOL_PROC_ASOUND", "/proc/asound")


def proc_root() -> str:
    return os.environ.get("SQTOOL_PROC", "/proc")


def _read(path: str) -> Optional[str]:
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------
# Cards and PCM substreams


@dataclass
class Card:
    index: int
    id: str
    driver: str
    name: str
    longname: str = ""

    @property
    def is_loopback(self) -> bool:
        return self.driver == "Loopback"

    @property
    def is_usb(self) -> bool:
        return self.driver == "USB-Audio"


_CARD_RE = re.compile(r"^\s*(\d+)\s+\[(.*?)\s*\]:\s*(.*?)\s+-\s+(.*?)\s*$")


def list_cards() -> List[Card]:
    text = _read(os.path.join(proc_asound(), "cards")) or ""
    lines = text.splitlines()
    cards = []
    for i, line in enumerate(lines):
        m = _CARD_RE.match(line)
        if m:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            longname = "" if _CARD_RE.match(nxt) else nxt.strip()
            cards.append(Card(int(m.group(1)), m.group(2), m.group(3), m.group(4), longname))
    return cards


def find_loopback(cards: List[Card], want: Optional[str] = None) -> Optional[Card]:
    """The loopback card, or the card named by `want` (index or id)."""
    if want is not None:
        for c in cards:
            if str(c.index) == str(want) or c.id == want:
                return c
        raise CaptureError("no ALSA card %r (see `sq-tool status`)" % want)
    for c in cards:
        if c.is_loopback:
            return c
    return None


def parse_hw_params(text: Optional[str]) -> Optional[dict]:
    """Parse /proc/asound/cardN/pcmDp/subS/hw_params. None if closed or not set up."""
    if not text or text.strip() in ("closed", "no setup"):
        return None
    out: dict = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if key in ("channels", "period_size", "buffer_size"):
            out[key] = int(val)
        elif key == "rate":
            out[key] = int(val.split()[0])
        elif key in ("access", "format", "subformat"):
            out[key] = val
    return out if "format" in out and "rate" in out and "channels" in out else None


def parse_status(text: Optional[str]) -> Optional[dict]:
    """Parse /proc/asound/cardN/pcmDp/subS/status. None if closed."""
    if not text or text.strip() == "closed":
        return None
    out: dict = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if key == "state":
            out[key] = val
        elif key in ("owner_pid", "delay", "avail", "avail_max", "hw_ptr", "appl_ptr"):
            try:
                out[key] = int(val)
            except ValueError:
                pass
    return out if "state" in out else None


@dataclass
class PcmSub:
    card: int
    device: int
    stream: str  # "p" (playback) or "c" (capture)
    sub: int

    @property
    def path(self) -> str:
        return os.path.join(proc_asound(), "card%d" % self.card,
                            "pcm%d%s" % (self.device, self.stream), "sub%d" % self.sub)

    @property
    def hw_name(self) -> str:
        return "hw:%d,%d,%d" % (self.card, self.device, self.sub)

    def hw_params(self) -> Optional[dict]:
        return parse_hw_params(_read(os.path.join(self.path, "hw_params")))

    def status(self) -> Optional[dict]:
        return parse_status(_read(os.path.join(self.path, "status")))


def list_subs(card: int, stream: str = "p", device: Optional[int] = None) -> List[PcmSub]:
    subs = []
    base = os.path.join(proc_asound(), "card%d" % card)
    for d in sorted(glob.glob(os.path.join(base, "pcm*%s" % stream))):
        m = re.search(r"pcm(\d+)[pc]$", d)
        if not m or (device is not None and int(m.group(1)) != device):
            continue
        for s in sorted(glob.glob(os.path.join(d, "sub*"))):
            ms = re.search(r"sub(\d+)$", s)
            if ms:
                subs.append(PcmSub(card, int(m.group(1)), stream, int(ms.group(1))))
    return subs


def process_info(pid: Optional[int]) -> dict:
    if not pid:
        return {}
    base = os.path.join(proc_root(), str(pid))
    comm = (_read(os.path.join(base, "comm")) or "").strip()
    cmdline = (_read(os.path.join(base, "cmdline")) or "").replace("\0", " ").strip()
    return {"pid": pid, "name": comm or "?", "cmdline": cmdline}


def describe_params(hw: dict) -> str:
    rate = hw["rate"]
    text = "%s, %d Hz, %d ch" % (hw["format"], rate, hw["channels"])
    if hw.get("period_size") and hw.get("buffer_size"):
        text += ", period %d frames (%.1f ms), buffer %d frames (%.1f ms)" % (
            hw["period_size"], 1000.0 * hw["period_size"] / rate,
            hw["buffer_size"], 1000.0 * hw["buffer_size"] / rate)
    return text


def status_report(verbose: bool = False) -> str:
    """What every ALSA card is doing right now: the `sq-tool status` command."""
    cards = list_cards()
    if not cards:
        return "No ALSA sound cards found (nothing in %s/cards).\n\n%s" % (proc_asound(), LOOPBACK_HELP)
    lines = ["ALSA cards:"]
    for c in cards:
        tag = "  <- loopback: sq-tool captures here" if c.is_loopback else (
            "  (USB audio)" if c.is_usb else "")
        lines.append("  %2d  %-12s %s [%s]%s" % (c.index, c.id, c.name, c.driver, tag))
    lines.append("")
    lines.append("Playback streams in use:")
    active = 0
    for c in cards:
        for sub in list_subs(c.index, "p"):
            st = sub.status()
            if not st:
                continue
            active += 1
            hw = sub.hw_params()
            owner = process_info(st.get("owner_pid"))
            lines.append("  %-12s %-9s %s" % (sub.hw_name, st["state"],
                                             describe_params(hw) if hw else "(not set up yet)"))
            if owner:
                lines.append("               player: %s (pid %d)" % (owner["name"], owner["pid"]))
        if verbose and c.is_usb:
            stream0 = _read(os.path.join(proc_asound(), "card%d" % c.index, "stream0"))
            if stream0:
                lines.append("  card %d stream0:" % c.index)
                lines.extend("    " + l for l in stream0.rstrip().splitlines())
    if not active:
        lines.append("  none (start playback to see what a player sends)")
    if not any(c.is_loopback for c in cards):
        lines.append("")
        lines.append(LOOPBACK_HELP)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CPU accounting during a capture


def cpu_snapshot() -> dict:
    root = proc_root()
    snap: dict = {"time": time.monotonic(), "procs": {}}
    first = (_read(os.path.join(root, "stat")) or "").split("\n", 1)[0].split()
    if first and first[0] == "cpu":
        vals = [int(v) for v in first[1:9]]
        snap["idle"] = vals[3] + (vals[4] if len(vals) > 4 else 0)
        snap["total"] = sum(vals)
    try:
        pids = [p for p in os.listdir(root) if p.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        text = _read(os.path.join(root, pid, "stat"))
        if not text or ")" not in text:
            continue
        r = text.rfind(")")
        fields = text[r + 2:].split()
        try:
            snap["procs"][int(pid)] = (text[text.find("(") + 1:r], int(fields[11]) + int(fields[12]))
        except (IndexError, ValueError):
            continue
    return snap


def cpu_usage(before: dict, after: dict, top: int = 6) -> dict:
    dt = after["time"] - before["time"]
    out: dict = {"seconds": round(dt, 2), "cpus": os.cpu_count()}
    if "total" in before and "total" in after and after["total"] > before["total"]:
        busy = 1 - (after["idle"] - before["idle"]) / (after["total"] - before["total"])
        out["system_percent"] = round(100 * busy, 2)
    if dt <= 0:
        return out
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    usage = []
    for pid, (name, ticks) in after["procs"].items():
        used = ticks - before["procs"].get(pid, (name, 0))[1]
        if used > 0:
            usage.append((100.0 * used / hz / dt, name, pid))
    usage.sort(reverse=True)
    out["top"] = [{"name": n, "pid": p, "percent_of_one_cpu": round(u, 2)} for u, n, p in usage[:top]]
    return out


# --------------------------------------------------------------------------
# Capture


class Capture:
    """Record what a player sends to the loopback card into a WAV file."""

    def __init__(self, out_path: str, card: Optional[str] = None, duration: Optional[float] = None,
                 silence_stop: float = 5.0, wait: Optional[float] = None,
                 prearm: Optional[Tuple[str, int, int]] = None, device: Optional[int] = None,
                 subdevice: Optional[int] = None, arecord: str = "arecord", poll: float = 0.01,
                 log: Callable[[str], None] = print):
        self.out_path = out_path
        self.card_want = card
        self.duration = duration
        self.silence_stop = silence_stop
        self.wait = wait
        self.prearm = prearm
        self.device = device
        self.subdevice = subdevice
        self.arecord = arecord
        self.poll = poll
        self.log = log

    # -- finding the player -------------------------------------------------

    def _card(self) -> Card:
        cards = list_cards()
        card = find_loopback(cards, self.card_want)
        if card is None:
            raise CaptureError(LOOPBACK_HELP)
        return card

    def _candidates(self, card: Card) -> List[PcmSub]:
        subs = [s for s in list_subs(card.index, "p", self.device)
                if s.device in (0, 1) and (self.subdevice is None or s.sub == self.subdevice)]
        if not subs:
            raise CaptureError("card %d has no loopback playback devices in %s"
                               % (card.index, proc_asound()))
        return subs

    def _wait_for_player(self, card: Card, started: float) -> Tuple[PcmSub, dict]:
        subs = self._candidates(card)
        self.log("Waiting for a player on the loopback card (%s or %s). Start playback now; "
                 "Ctrl+C gives up." % ("hw:%d,0" % card.index, "hw:%d,1" % card.index))
        while True:
            for sub in subs:
                st = sub.status()
                if st and st["state"] in ACTIVE_STATES:
                    hw = sub.hw_params()
                    if hw:
                        return sub, hw
            if self.wait and time.monotonic() - started > self.wait:
                raise CaptureError("no playback started within %g s" % self.wait)
            time.sleep(self.poll)

    # -- the capture itself ---------------------------------------------------

    def run(self) -> dict:
        card = self._card()
        if not shutil.which(self.arecord):
            raise CaptureError("arecord not found. Install alsa-utils "
                               "(e.g. sudo apt install alsa-utils).")
        started = time.monotonic()
        while True:
            if self.prearm:
                fmt, rate, channels = self.prearm
                play = PcmSub(card.index, self.device or 0, "p", self.subdevice or 0)
                params = {"format": fmt, "rate": rate, "channels": channels}
                self.log("Pre-armed: capturing %s %d Hz %d ch from hw:%d,%d,%d. This forces the "
                         "player on %s to use exactly that format. Start playback now."
                         % (fmt, rate, channels, card.index, 1 - play.device, play.sub, play.hw_name))
            else:
                play, params = self._wait_for_player(card, started)
                owner = process_info((play.status() or {}).get("owner_pid"))
                self.log("Player found on %s%s: %s" % (
                    play.hw_name, " (%s, pid %d)" % (owner["name"], owner["pid"]) if owner else "",
                    describe_params(params)))
            meta = self._session(card, play, params)
            if meta is not None:
                return meta
            self.log("The player stopped before any audio arrived; waiting again.")

    def _session(self, card: Card, play: PcmSub, params: dict) -> Optional[dict]:
        fmt = ALSA_FORMATS.get(params["format"])
        if fmt is None:
            raise CaptureError("the player is sending %s, which sq-tool cannot capture "
                               "(supported: %s)" % (params["format"], ", ".join(ALSA_FORMATS)))
        if "NONINTERLEAVED" in params.get("access", ""):
            raise CaptureError("the player uses non-interleaved access, which sq-tool cannot capture")
        rate, channels = params["rate"], params["channels"]
        cap_dev = "hw:%d,%d,%d" % (card.index, 1 - play.device, play.sub)
        cmd = [self.arecord, "-D", cap_dev, "-f", params["format"], "-r", str(rate),
               "-c", str(channels), "-t", "raw", "-B", "500000"]
        frame_bytes = fmt.width * channels
        part = self.out_path + ".part"
        writer = WavWriter(part, rate, channels, fmt.wav_bits, fmt.is_float)
        cpu0 = cpu_snapshot()
        wall_start = datetime.datetime.now().astimezone()
        self.t0 = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.RLock()  # re-entrant: the SIGINT handler may run inside it
        self.stop_reason = None  # type: Optional[str]
        self.frames = 0
        self.state_log = []  # type: List[dict]
        self.player_xruns = 0
        self.seen_player = not self.prearm
        self.owner = {}  # type: dict
        self.interrupted = False
        stderr_lines = []  # type: List[str]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc = self._proc

        def on_sigint(signum, frame):
            self.interrupted = True
            self._request_stop("stopped with Ctrl+C")

        def read_stderr():
            for raw in proc.stderr:
                stderr_lines.append(raw.decode(errors="replace").rstrip())

        old_sigint = signal.signal(signal.SIGINT, on_sigint)
        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_mon = threading.Thread(target=self._monitor, args=(play, params), daemon=True)
        t_err.start()
        t_mon.start()
        first_sound = last_sound = None
        missed = None
        limit = int(self.duration * rate) if self.duration else None
        pending = b""
        fd = proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                pending += chunk
                usable = len(pending) - len(pending) % frame_bytes
                if limit is not None:
                    usable = max(0, min(usable, (limit - writer.frames) * frame_bytes))
                if usable == 0:
                    continue
                raw, pending = pending[:usable], pending[usable:]
                payload = fmt.convert(raw) if fmt.convert else raw
                before = writer.frames
                writer.write(payload)
                self.frames = writer.frames
                if missed is None:
                    # Frames the player had already played when the capture's first data arrived.
                    hw_ptr = (play.status() or {}).get("hw_ptr")
                    missed = max(0, hw_ptr - self.frames) if hw_ptr is not None else -1
                if np.frombuffer(payload, dtype=np.uint8).any():
                    if first_sound is None:
                        first_sound = before
                    last_sound = self.frames
                if limit is not None and self.frames >= limit:
                    self._request_stop("reached --duration")
                if (self.silence_stop and last_sound is not None
                        and self.frames - last_sound >= self.silence_stop * rate):
                    self._request_stop("%g s of digital silence after the music" % self.silence_stop)
        finally:
            self._request_stop("arecord exited")
            proc.wait()
            t_err.join(timeout=2)
            t_mon.join(timeout=2)
            writer.close()
            signal.signal(signal.SIGINT, old_sigint)
        cpu1 = cpu_snapshot()
        player_left = self.stop_reason.startswith("the player")
        if writer.frames == 0 or (first_sound is None and player_left and not self.prearm):
            os.unlink(part)
            if writer.frames == 0 and proc.returncode not in (0, None) and not player_left \
                    and not self.interrupted:
                raise CaptureError("arecord failed (%s):\n%s" % (" ".join(cmd), "\n".join(stderr_lines)))
            if self.interrupted:
                raise KeyboardInterrupt
            if self.prearm:
                raise CaptureError("nothing was captured: %s" % self.stop_reason)
            return None  # the player went away before sending audio: wait for it again
        os.replace(part, self.out_path)
        overruns = [l for l in stderr_lines if "overrun" in l]
        meta = {
            "tool": "sq-tool %s" % __version__,
            "capture": {
                "alsa_format": params["format"],
                "rate": rate,
                "channels": channels,
                "access": params.get("access"),
                "period_size": params.get("period_size"),
                "buffer_size": params.get("buffer_size"),
                "playback_device": play.hw_name,
                "capture_device": cap_dev,
                "card": {"index": card.index, "id": card.id, "name": card.name},
                "player": self.owner,
                "mode": "prearm" if self.prearm else "auto",
                "started": wall_start.isoformat(timespec="seconds"),
                "frames": writer.frames,
                "seconds": round(writer.frames / rate, 3),
                "first_sound_frame": first_sound,
                "stop_reason": self.stop_reason,
                "player_frames_before_capture": missed,
                "capture_overruns": len(overruns),
                "player_xruns_seen": self.player_xruns,
                "player_states": self.state_log[:200],
                "arecord": cmd,
                "arecord_messages": stderr_lines[-50:],
            },
            "cpu": cpu_usage(cpu0, cpu1),
        }
        with open(self.out_path + SIDECAR_SUFFIX, "w") as f:
            json.dump(meta, f, indent=2)
        return meta

    def _request_stop(self, reason: str) -> None:
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason
            self._stop.set()
            if self._proc.poll() is None:
                try:
                    self._proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass

    def _monitor(self, play: PcmSub, params: dict) -> None:
        last = None
        while not self._stop.is_set():
            st = play.status()
            hw = play.hw_params()
            state = st["state"] if st else "CLOSED"
            if state != last:
                self.state_log.append({"time": round(time.monotonic() - self.t0, 3),
                                       "frame": self.frames, "state": state})
                if state == "XRUN":
                    self.player_xruns += 1
                last = state
            if st and not self.owner:
                self.owner = process_info(st.get("owner_pid"))
            if st and state in ACTIVE_STATES:
                self.seen_player = True
            if self.seen_player:
                if st is None or hw is None:
                    self._request_stop("the player closed the device")
                elif any(hw.get(k) != params.get(k) for k in ("format", "rate", "channels")):
                    self._request_stop("the player switched to %s" % describe_params(hw))
            if self.wait and not self.seen_player and time.monotonic() - self.t0 > self.wait:
                self._request_stop("no playback started within %g s" % self.wait)
            time.sleep(self.poll)
