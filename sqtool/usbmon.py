"""Record exactly what a USB DAC receives, straight off the USB bus (Linux usbmon).

usbmon is the kernel's USB packet monitor. For a USB audio DAC, every
isochronous OUT packet on the DAC's audio endpoint carries whole PCM frames
that snd-usb-audio copied unchanged from the player's ALSA buffer. Joining the
packet payloads in order therefore gives the exact sample stream the DAC got.

Capturing is passive: unlike the loopback method it cannot influence the
player, which keeps playing to the real DAC with whatever format it chooses.

The binary usbmon interface (/dev/usbmonN, one per USB bus) returns one event
per read(2): a 48-byte header (struct mon_bin_hdr), then for isochronous
transfers one 16-byte descriptor per packet (struct mon_bin_isodesc: status,
offset, length), then the transfer data. Layout as in the kernel's
drivers/usb/mon/mon_bin.c.
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import json
import os
import select
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import __version__
from .alsa import CaptureError, UsbAudio, cpu_snapshot, cpu_usage, list_subs, process_info
from .wavio import ALSA_FORMATS, SIDECAR_SUFFIX, WavWriter

HEADER = struct.Struct("<QBBBBHccqiiII8s")  # struct mon_bin_hdr, first 48 bytes (what read() returns)
ISO_DESC = struct.Struct("<iIII")  # struct mon_bin_isodesc
XFER_ISO, XFER_INT, XFER_CONTROL, XFER_BULK = 0, 1, 2, 3
ISODESC_MAX = 128
MON_IOCG_STATS = 0x80089203  # _IOR(0x92, 3, struct mon_bin_stats)
MON_IOCT_RING_SIZE = 0x9204  # _IO(0x92, 4)
RING_SIZES = (16 << 20, 4 << 20, 1200 * 1024, 300 * 1024)  # newest kernels allow 64 MiB, old ones 1.2 MB
READ_SIZE = 4 << 20

# Endpoint stop: snd-usb-audio kills or unlinks its URBs (-ENOENT / -ECONNRESET / -ESHUTDOWN).
STOP_STATUSES = {-errno.ENOENT, -errno.ECONNRESET, -errno.ESHUTDOWN}

# Native DSD is passed through byte for byte: bit-exact comparisons only.
DSD_WIDTHS = {"DSD_U8": 1, "DSD_U16_LE": 2, "DSD_U16_BE": 2, "DSD_U32_LE": 4, "DSD_U32_BE": 4}

USBMON_HELP = """usbmon is not available. On the host, run:

    sudo modprobe usbmon
    echo usbmon | sudo tee /etc/modules-load.d/usbmon.conf    # load it at every boot

In Docker, start the container with --privileged (it then finds the device even
if usbmon is loaded later)."""


@dataclass
class UsbEvent:
    kind: str  # "S" submission, "C" completion, "E" error
    xfer: int
    ep: int  # endpoint address including the direction bit (0x80 = IN)
    dev: int
    bus: int
    ts: float
    status: int
    flag_setup: bytes
    flag_data: bytes
    setup: bytes
    iso_errors: int
    iso_numdesc: int
    body: memoryview  # ISO descriptors, then data

    @staticmethod
    def parse(buf) -> "UsbEvent":
        (_id, kind, xfer, ep, dev, bus, fsetup, fdata, sec, usec, status, _len_urb, len_cap,
         s) = HEADER.unpack_from(buf, 0)
        iso_errors, numdesc = struct.unpack("<ii", s)
        return UsbEvent(chr(kind), xfer, ep, dev, bus, sec + usec / 1e6, status, fsetup, fdata, s,
                        iso_errors, numdesc, memoryview(buf)[HEADER.size:HEADER.size + len_cap])

    def iso_packets(self) -> List[Tuple[int, int, memoryview]]:
        """(status, length, captured payload) for each isochronous packet of this transfer."""
        ndesc = min(max(self.iso_numdesc, 0), ISODESC_MAX)
        data = self.body[ndesc * ISO_DESC.size:]
        packets = []
        for i in range(ndesc):
            status, offset, length, _pad = ISO_DESC.unpack_from(self.body, i * ISO_DESC.size)
            packets.append((status, length, data[offset:offset + length]))
        return packets


def build_event(kind: str, xfer: int, ep: int, dev: int, bus: int, ts: float = 0.0, status: int = 0,
                packets: Optional[List[bytes]] = None, setup: bytes = b"\0" * 8, data: bytes = b"",
                iso_errors: int = 0, flag_setup: bytes = b"-", flag_data: bytes = b"\0",
                packet_status: Optional[List[int]] = None) -> bytes:
    """Encode an event exactly as read(2) on /dev/usbmonN returns it (used by the tests)."""
    body = b""
    numdesc = 0
    if packets is not None:
        numdesc = len(packets)
        off = 0
        descs = b""
        for i, p in enumerate(packets):
            st = packet_status[i] if packet_status else 0
            descs += ISO_DESC.pack(st, off, len(p), 0)
            off += len(p)
        body = descs + b"".join(packets)
        setup = struct.pack("<ii", iso_errors, numdesc)
    else:
        body = data
    sec = int(ts)
    usec = int(round((ts - sec) * 1e6))
    return HEADER.pack(0, ord(kind), xfer, ep, dev, bus, flag_setup, flag_data, sec, usec, status,
                       len(body), len(body), setup) + body


# --------------------------------------------------------------------------
# The usbmon device


def usbmon_path(bus: int) -> str:
    return os.path.join(os.environ.get("SQTOOL_DEV", "/dev"), "usbmon%d" % bus)


def usbmon_state(bus: int) -> str:
    """"ready", "no-permission" or "missing" (the usbmon driver isn't loaded)."""
    path = usbmon_path(bus)
    if os.path.exists(path):
        return "ready" if os.access(path, os.R_OK) else "no-permission"
    return "ready" if os.path.exists("/sys/class/usbmon/usbmon%d" % bus) else "missing"


def ensure_usbmon_node(bus: int) -> str:
    """Path of /dev/usbmonN, creating the node from sysfs if a container lacks it."""
    path = usbmon_path(bus)
    if os.path.exists(path):
        return path
    sysdev = "/sys/class/usbmon/usbmon%d/dev" % bus
    try:
        with open(sysdev) as f:
            major, minor = (int(x) for x in f.read().strip().split(":"))
        os.mknod(path, 0o600 | 0o020000, os.makedev(major, minor))  # S_IFCHR
    except (OSError, ValueError):
        raise CaptureError(USBMON_HELP)
    return path


class UsbmonSource:
    """Reads events from /dev/usbmonN with a large kernel ring buffer."""

    def __init__(self, bus: int):
        path = ensure_usbmon_node(bus)
        try:
            self.f = open(path, "rb", buffering=0)
        except PermissionError:
            raise CaptureError("permission denied opening %s: run as root (in Docker: --privileged)" % path)
        except OSError as exc:
            raise CaptureError("cannot open %s: %s\n\n%s" % (path, exc, USBMON_HELP))
        self.ring = 0
        for size in RING_SIZES:
            try:
                fcntl.ioctl(self.f, MON_IOCT_RING_SIZE, size)
                self.ring = size
                break
            except OSError:
                continue
        self.dropped()  # reset the counter

    def wait(self, timeout: float) -> bool:
        return bool(select.select([self.f], [], [], timeout)[0])

    def readinto(self, buf) -> int:
        try:
            return self.f.readinto(buf) or 0
        except InterruptedError:
            return 0

    def dropped(self) -> int:
        """Events the kernel had to drop since the last call (ring buffer full)."""
        stats = bytearray(8)
        try:
            fcntl.ioctl(self.f, MON_IOCG_STATS, stats, True)
        except OSError:
            return 0
        return struct.unpack("<II", stats)[1]

    def close(self) -> None:
        self.f.close()


# --------------------------------------------------------------------------
# Capture


@dataclass
class Take:
    """One continuous stream in one format: becomes one WAV file."""
    params: dict
    path: str
    writer: WavWriter
    stride: int
    convert: Optional[Callable[[bytes], bytes]]
    dsd: bool
    started_wall: str
    cpu0: dict
    player: dict
    pending: bytes = b""
    frames: int = 0
    urbs: int = 0
    packets: int = 0
    packet_sizes: Dict[int, int] = field(default_factory=dict)
    bad_packets: int = 0
    uncaptured: int = 0
    packet_errors: int = 0
    iso_errors: int = 0
    dropped: int = 0
    has_audio: bool = False
    first_audio_frame: Optional[int] = None
    interruptions: List[dict] = field(default_factory=list)
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    dac_rate: Optional[int] = None
    tracker: object = None  # a songend.SongEnd: where the song ends in this take
    wav_frame: int = 0  # bytes per frame in the WAV file
    ended: bool = False  # the song ended: nothing more is kept


class UsbCapture:
    """Record the stream a USB DAC receives. One session can produce several takes."""

    def __init__(self, dac: UsbAudio, out_dir: str, name: str = "capture", idle_stop: float = 5.0,
                 max_seconds: Optional[float] = None, on_take: Optional[Callable[[str, dict], None]] = None,
                 log: Callable[[str], None] = print, source=None, gap: float = 0.25,
                 stop_after_audio: Optional[float] = None, max_wait_audio: Optional[float] = None,
                 song_end: Optional[Callable[[dict], object]] = None):
        self.dac = dac
        self.song_end = song_end  # makes a songend.SongEnd for a take's format (see alsa.Capture)
        self.stop_after_audio = stop_after_audio  # seconds after the music starts (the song's length)
        self.max_wait_audio = max_wait_audio  # give up when no music arrives within this many seconds
        self.out_dir = out_dir
        self.name = name
        self.idle_stop = idle_stop
        self.max_seconds = max_seconds
        self.on_take = on_take
        self.log = log
        self.source = source
        self.gap = gap
        self.state = "starting"
        self.message = ""
        self.error: Optional[str] = None
        self.takes: List[dict] = []
        self.take: Optional[Take] = None
        self.started = time.monotonic()
        self.events = 0
        self.dropped_total = 0
        self.stop_reason: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._new_run = True  # the next data transfer starts a new run (stream (re)start)
        self._last_sound: Optional[float] = None  # monotonic time of the last non-silent audio
        self._dac_rate: Optional[int] = None  # last sample rate the host set on the DAC
        self._xruns = 0
        self._states: List[dict] = []
        self._last_state = None

    # -- control ----------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_safe, name="usb-capture", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "stopped by user") -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self._stop.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run_safe(self) -> None:
        try:
            self.run()
        except Exception as exc:  # reported to the UI
            self.error = str(exc)
            self.state = "error"
            self.message = str(exc)
        finally:
            self.song_end = None  # done with the song (it can be large)

    def status(self) -> dict:
        take = self.take
        out = {
            "method": "usbmon",
            "state": self.state,
            "message": self.message,
            "error": self.error,
            "device": self.dac.describe(),
            "elapsed": round(time.monotonic() - self.started, 1),
            "takes": list(self.takes),
            "events": self.events,
            "usbmon_dropped": self.dropped_total,
            "stop_reason": self.stop_reason,
        }
        if take:
            tracker = take.tracker
            at = tracker.song_seconds(take.frames) if tracker is not None else None
            out["take"] = {
                "format": take.params.get("format"), "rate": take.params.get("rate"),
                "channels": take.params.get("channels"), "player": take.player,
                "seconds": round(take.frames / take.params["rate"], 2),
                "music_seconds": None if take.first_audio_frame is None else
                round((take.frames - take.first_audio_frame) / take.params["rate"], 2),
                "song_seconds": None if at is None else round(at, 2),  # where in the song
                "interruptions": len(take.interruptions), "packets": take.packets,
            }
        return out

    # -- main loop --------------------------------------------------------------

    def run(self) -> List[dict]:
        source = self.source or UsbmonSource(self.dac.bus)
        buf = bytearray(READ_SIZE)
        self.state = "waiting"
        self.message = "Waiting for playback on %s" % self.dac.card.name
        self.log(self.message + " (start playback now)")
        last_poll = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_poll >= 0.05:
                    last_poll = now
                    self._poll(source)
                if not source.wait(0.05):
                    continue
                n = source.readinto(buf)
                if n <= 0:
                    continue
                self.events += 1
                self._handle(UsbEvent.parse(memoryview(buf)[:n]))
        finally:
            self.state = "stopping"
            if hasattr(source, "dropped"):
                self._account_dropped(source.dropped())
            self._finish_take()
            source.close()
            self.state = "done"
            self.message = "Finished: %d take(s)" % len(self.takes)
        return self.takes

    def _poll(self, source) -> None:
        if hasattr(source, "dropped"):
            self._account_dropped(source.dropped())
        st, _hw = self._alsa()
        state = st["state"] if st else "CLOSED"
        if state != self._last_state:
            self._last_state = state
            self._states.append({"time": round(time.monotonic() - self.started, 3), "state": state,
                                 "frame": self.take.frames if self.take else None})
            if state == "XRUN":
                self._xruns += 1
        if self.idle_stop and self._last_sound is not None:
            if time.monotonic() - self._last_sound >= self.idle_stop:
                self.stop("playback ended (%g s without music)" % self.idle_stop)
        if (self.max_wait_audio and self._last_sound is None
                and time.monotonic() - self.started >= self.max_wait_audio):
            self.stop("no music arrived within %g s" % self.max_wait_audio)
        take = self.take
        if self.max_seconds and take and take.frames >= self.max_seconds * take.params["rate"]:
            self.stop("reached the maximum length")
        if (self.stop_after_audio and take and take.first_audio_frame is not None
                and (take.tracker is None or take.tracker.end_frame() is None)
                and take.frames - take.first_audio_frame >= self.stop_after_audio * take.params["rate"]):
            self.stop("reached the end of the song")

    def _account_dropped(self, n: int) -> None:
        if n:
            self.dropped_total += n
            if self.take:
                self.take.dropped += n

    def _alsa(self) -> Tuple[Optional[dict], Optional[dict]]:
        """Status and hw_params of the DAC's open playback stream, if any."""
        for sub in list_subs(self.dac.card.index, "p"):
            st = sub.status()
            if st:
                return st, sub.hw_params()
        return None, None

    def _handle(self, ev: UsbEvent) -> None:
        if ev.bus != self.dac.bus or ev.dev != self.dac.dev:
            return
        if ev.xfer == XFER_CONTROL:
            if ev.kind == "S":
                self._control(ev)
            return
        if ev.xfer != XFER_ISO or ev.ep not in self.dac.endpoints:
            return
        if ev.kind == "C":
            if ev.status in STOP_STATUSES:
                self._mark_stop()
            elif self.take:
                self.take.iso_errors += max(0, ev.iso_errors)
                self.take.packet_errors += sum(1 for st, _, _ in ev.iso_packets() if st)
            return
        if ev.kind != "S":
            return
        if self.take and self.take.last_ts is not None and ev.ts - self.take.last_ts > self.gap:
            self._mark_stop()
        if self._new_run or self.take is None:
            self._begin_run(ev.ts)
        take = self.take
        if take is None or take.ended:
            return
        if ev.flag_data != b"\0":  # usbmon could not copy this transfer's data
            take.uncaptured += 1
            return
        packets = ev.iso_packets()
        take.urbs += 1
        for _, length, p in packets:
            take.packets += 1
            if len(p) < length:
                take.uncaptured += 1
            frames, rest = divmod(len(p), take.stride)
            take.packet_sizes[frames] = take.packet_sizes.get(frames, 0) + 1
            if rest:
                take.bad_packets += 1
        payload = b"".join(bytes(p) for _, _, p in packets)
        if take.first_ts is None:
            take.first_ts = ev.ts
        take.last_ts = ev.ts
        data = take.pending + payload
        usable = len(data) - len(data) % take.stride
        take.pending = data[usable:]
        raw = data[:usable]
        if not raw:
            return
        payload = take.convert(raw) if take.convert else raw
        n = usable // take.stride
        if take.tracker is not None:
            try:
                end = take.tracker.feed(payload)
            except Exception as exc:  # never lose a recording over this: just stop following
                self.log("Can't follow the song any more (%s): stopping at silence instead" % exc)
                take.tracker, end = None, None
            if end is not None and end < take.frames + n:  # the song ends in this transfer
                if end < take.frames:  # known only once the stream had gone past it: cut back
                    take.writer.truncate(end)
                    take.frames = take.writer.frames
                n = max(0, end - take.frames)
                payload, raw = payload[:n * take.wav_frame], raw[:n * take.stride]
                take.ended = True
                self.stop("reached the end of the song")
        take.writer.write(payload)
        take.frames += n
        if np.frombuffer(raw, dtype=np.uint8).any():
            if take.first_audio_frame is None:
                take.first_audio_frame = take.frames - n
            take.has_audio = True
            self._last_sound = time.monotonic()
        if self.state != "recording":
            self.state = "recording"
            self.message = "Recording"

    def _control(self, ev: UsbEvent) -> None:
        """Note interface and sample-rate changes: the stream is being reconfigured."""
        if ev.flag_setup != b"\0":
            return
        req_type, request, value, _index, length = struct.unpack("<BBHHH", ev.setup)
        if request == 0x0B and req_type == 0x01:  # SET_INTERFACE: the stream is being reconfigured
            self._mark_stop()
        elif request == 0x01 and req_type in (0x21, 0x22) and value >> 8 == 0x01 and length in (3, 4):
            # SET_CUR on a sampling-frequency control (UAC2: 4 bytes, UAC1: 3 bytes). Volume and
            # mute requests have other control selectors or lengths.
            rate = int.from_bytes(bytes(ev.body[:length]), "little")
            if 8000 <= rate <= 3072000:
                self._dac_rate = rate

    def _mark_stop(self) -> None:
        self._new_run = True

    def _begin_run(self, ts: float) -> None:
        """First transfer of a (re)started stream: continue the take or start a new one."""
        self._new_run = False
        st, hw = self._alsa()
        if hw is None:
            if self.take:
                hw = self.take.params
            else:
                self.message = "Audio is flowing but the DAC's stream format is unknown (is /proc/asound readable?)"
                self._new_run = True
                return
        if self.take and all(hw.get(k) == self.take.params.get(k) for k in ("format", "rate", "channels")):
            if self.take.frames:
                gap = ts - self.take.last_ts if self.take.last_ts is not None else 0.0
                self.take.interruptions.append({"frame": self.take.frames,
                                                "seconds": round(self.take.frames / hw["rate"], 3),
                                                "gap": round(gap, 3)})
            return
        self._finish_take()
        self._open_take(hw, st)

    def _open_take(self, hw: dict, st: Optional[dict]) -> None:
        fmt = hw["format"]
        if fmt in ALSA_FORMATS:
            af = ALSA_FORMATS[fmt]
            width, bits, is_float, convert, dsd = af.width, af.wav_bits, af.is_float, af.convert, False
        elif fmt in DSD_WIDTHS:
            width = DSD_WIDTHS[fmt]
            bits, is_float, convert, dsd = width * 8, False, None, True
        else:
            self.message = "The DAC is receiving %s, which sq-tool cannot record" % fmt
            return
        os.makedirs(self.out_dir, exist_ok=True)
        index = len(self.takes) + 1
        path = os.path.join(self.out_dir, "usb-take-%d-%d.wav" % (int(time.time()), index))
        writer = WavWriter(path + ".part", hw["rate"], hw["channels"], bits, is_float)
        player = process_info((st or {}).get("owner_pid"))
        self.take = Take(params=dict(hw), path=path, writer=writer, stride=width * hw["channels"],
                         convert=convert, dsd=dsd,
                         started_wall=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                         cpu0=cpu_snapshot(), player=player, dac_rate=self._dac_rate,
                         tracker=self.song_end(hw) if self.song_end and not dsd else None,
                         wav_frame=bits // 8 * hw["channels"])
        self.log("Recording %s, %d Hz, %d ch from %s%s" % (
            fmt, hw["rate"], hw["channels"], self.dac.card.name,
            " (sent by %s)" % player["name"] if player else ""))

    def _finish_take(self) -> None:
        take, self.take = self.take, None
        if take is None:
            return
        if take.tracker is not None and not take.ended:
            # Stopped some other way past the song's end (waiting out a dropout, say): cut there.
            end = take.tracker.end_frame()
            if end is not None and take.first_audio_frame is not None and take.first_audio_frame < end < take.frames:
                take.writer.truncate(end)
                take.frames = take.writer.frames
        take.writer.close()
        if not take.has_audio:  # nothing but digital silence, e.g. a player probing the device
            os.unlink(take.path + ".part")
            return
        os.replace(take.path + ".part", take.path)
        rate = take.params["rate"]
        meta = {
            "tool": "sq-tool %s" % __version__,
            "capture": {
                "method": "usbmon",
                "alsa_format": take.params["format"],
                "rate": rate,
                "channels": take.params["channels"],
                "access": take.params.get("access"),
                "period_size": take.params.get("period_size"),
                "buffer_size": take.params.get("buffer_size"),
                "dsd": take.dsd,
                "device": self.dac.describe(),
                "playback_device": "hw:%d,0" % self.dac.card.index,
                "player": take.player,
                "started": take.started_wall,
                "frames": take.frames,
                "seconds": round(take.frames / rate, 3),
                "stop_reason": self.stop_reason or "stream format changed",
                "song_end": take.tracker.summary() if take.tracker is not None else None,
                "interruptions": take.interruptions[:500],
                "player_xruns_seen": self._xruns,
                "player_states": self._states[-200:],
                "usb": {
                    "urbs": take.urbs,
                    "packets": take.packets,
                    "frames_per_packet": {str(k): v for k, v in sorted(take.packet_sizes.items())},
                    "bad_packets": take.bad_packets,
                    "uncaptured_transfers": take.uncaptured,
                    "iso_errors": take.iso_errors,
                    "packet_errors": take.packet_errors,
                    "usbmon_dropped": take.dropped,
                    "dac_rate_set": take.dac_rate,
                    "stream_seconds": round((take.last_ts or 0) - (take.first_ts or 0), 3),
                },
            },
            "cpu": cpu_usage(take.cpu0, cpu_snapshot()),
        }
        with open(take.path + SIDECAR_SUFFIX, "w") as f:
            json.dump(meta, f, indent=2)
        info = {"path": take.path, "name": self.name, "format": take.params["format"], "rate": rate,
                "channels": take.params["channels"], "seconds": meta["capture"]["seconds"],
                "interruptions": len(take.interruptions), "usbmon_dropped": take.dropped,
                "complete": not (take.dropped or take.uncaptured)}
        self.takes.append(info)
        self.log("Saved %.1f s to %s" % (info["seconds"], take.path))
        if self.on_take:
            self.on_take(take.path, meta)
