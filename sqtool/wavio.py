"""Reading and writing audio files, and converting ALSA sample formats.

Integer audio is held in memory as int32 with every sample left-justified
(a 16-bit sample v is stored as v << 16, a 24-bit one as v << 8). That way
values from 16-, 24- and 32-bit containers compare directly: padding a 16-bit
sample into a 32-bit container does not change its value. Float audio is held
as float32 or float64 with full scale at 1.0.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

import numpy as np

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_* GUIDs share everything after the 2-byte format code.
_GUID_TAIL = bytes.fromhex("000000001000800000aa00389b71")
_CHANNEL_MASKS = {1: 0x4, 2: 0x3, 4: 0x33, 6: 0x3F, 8: 0x63F}

SIDECAR_SUFFIX = ".json"


class AudioFileError(Exception):
    pass


@dataclass
class Audio:
    data: np.ndarray  # shape (frames, channels): int32 left-justified, or float
    rate: int
    bits: int  # valid bits per sample (int) or 32/64 (float)
    is_float: bool = False
    label: str = ""  # human-readable description of where the samples came from
    path: str = ""
    meta: dict = field(default_factory=dict)  # sq-tool capture metadata, if any

    @property
    def frames(self) -> int:
        return int(self.data.shape[0])

    @property
    def channels(self) -> int:
        return int(self.data.shape[1])

    @property
    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    @property
    def is_capture(self) -> bool:
        return "capture" in self.meta


# --------------------------------------------------------------------------
# WAV reading


def _parse_fmt(body: bytes) -> dict:
    if len(body) < 16:
        raise AudioFileError("fmt chunk too short")
    tag, channels, rate, _byte_rate, block_align, bits = struct.unpack("<HHIIHH", body[:16])
    valid = bits
    if tag == WAVE_FORMAT_EXTENSIBLE:
        if len(body) < 40:
            raise AudioFileError("WAVE_FORMAT_EXTENSIBLE fmt chunk too short")
        _cb, valid, _mask = struct.unpack("<HHI", body[16:24])
        subformat = body[24:40]
        if subformat[2:] != _GUID_TAIL:
            raise AudioFileError("unsupported WAVE_FORMAT_EXTENSIBLE sub-format")
        tag = struct.unpack("<H", subformat[:2])[0]
        valid = valid or bits
    if tag not in (WAVE_FORMAT_PCM, WAVE_FORMAT_IEEE_FLOAT):
        raise AudioFileError("unsupported WAV format code 0x%04x (only PCM and float)" % tag)
    if channels < 1 or block_align < channels or block_align % channels:
        raise AudioFileError("inconsistent WAV header (channels/block align)")
    return {
        "tag": tag,
        "channels": channels,
        "rate": rate,
        "block_align": block_align,
        "container": block_align // channels,
        "bits": bits,
        "valid_bits": min(valid, bits),
    }


def _find_chunks(f) -> Tuple[dict, int, int]:
    """Return (fmt, data_offset, data_size) for a seekable WAV/RF64 stream."""
    head = f.read(12)
    if len(head) < 12:
        raise AudioFileError("file too short to be a WAV file")
    riff, _size, wave = struct.unpack("<4sI4s", head)
    if riff not in (b"RIFF", b"RF64", b"BW64") or wave != b"WAVE":
        raise AudioFileError("not a WAV file")
    ds64_data_size = None
    fmt = None
    pos = 12
    while True:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            raise AudioFileError("no data chunk found")
        cid, size = struct.unpack("<4sI", hdr)
        if cid == b"ds64":
            body = f.read(size)
            if len(body) >= 16:
                ds64_data_size = struct.unpack("<Q", body[8:16])[0]
        elif cid == b"fmt ":
            fmt = _parse_fmt(f.read(size))
        elif cid == b"data":
            if fmt is None:
                raise AudioFileError("data chunk before fmt chunk")
            if size == 0xFFFFFFFF and ds64_data_size is not None:
                size = ds64_data_size
            return fmt, pos + 8, size
        pos += 8 + size + (size & 1)


def _decode_samples(raw: bytes, fmt: dict) -> np.ndarray:
    """Decode interleaved WAV sample bytes into the in-memory representation."""
    width = fmt["container"]
    if fmt["tag"] == WAVE_FORMAT_IEEE_FLOAT:
        if width == 4:
            return np.frombuffer(raw, dtype="<f4").astype(np.float32)
        if width == 8:
            return np.frombuffer(raw, dtype="<f8").astype(np.float64)
        raise AudioFileError("unsupported float sample width: %d bytes" % width)
    if width == 1:
        u8 = np.frombuffer(raw, dtype=np.uint8)
        return (u8.astype(np.int32) - 128) << 24
    if width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.int32) << 16
    if width == 3:
        return packed24_to_int32(raw)
    if width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.int32)
    raise AudioFileError("unsupported PCM sample width: %d bytes" % width)


def packed24_to_int32(raw: bytes) -> np.ndarray:
    """Little-endian packed 24-bit samples -> left-justified int32."""
    b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
    out = np.zeros((b.shape[0], 4), dtype=np.uint8)
    out[:, 1:] = b
    return out.view("<i4").reshape(-1).astype(np.int32)


def read_wav(source) -> Audio:
    """Read a WAV/RF64 file (path or bytes) into an Audio object."""
    if isinstance(source, (bytes, bytearray)):
        f = io.BytesIO(source)
        path = ""
    else:
        path = os.fspath(source)
        f = open(path, "rb")
    with f:
        fmt, offset, size = _find_chunks(f)
        f.seek(0, io.SEEK_END)
        available = f.tell() - offset
        size = max(0, min(size, available))  # tolerate truncated files
        size -= size % fmt["block_align"]
        f.seek(offset)
        raw = f.read(size)
    samples = _decode_samples(raw, fmt)
    data = samples.reshape(-1, fmt["channels"])
    is_float = fmt["tag"] == WAVE_FORMAT_IEEE_FLOAT
    if is_float:
        label = "WAV float %d-bit" % fmt["bits"]
        bits = fmt["bits"]
    else:
        bits = fmt["valid_bits"]
        label = "WAV PCM %d-bit" % bits
        if fmt["container"] * 8 != bits:
            label += " (in %d-bit container)" % (fmt["container"] * 8)
    audio = Audio(data=data, rate=fmt["rate"], bits=bits, is_float=is_float, label=label, path=path)
    if path:
        audio.meta = read_sidecar(path)
        cap = audio.meta.get("capture") or {}
        if cap.get("alsa_format"):
            audio.label = "captured ALSA %s" % cap["alsa_format"]
    return audio


def read_sidecar(path: str) -> dict:
    side = path + SIDECAR_SUFFIX
    try:
        with open(side) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# Other formats (FLAC, ALAC, AIFF, ...) via ffmpeg or flac

# Codecs whose decoded output is exactly defined, so a sample comparison makes sense.
LOSSLESS_CODECS = {"flac", "alac", "wavpack", "ape", "tta", "mlp", "truehd", "shorten", "tak"}


def _is_wav(path: str) -> bool:
    with open(path, "rb") as f:
        head = f.read(12)
    return len(head) == 12 and head[:4] in (b"RIFF", b"RF64", b"BW64") and head[8:12] == b"WAVE"


def load_audio(path: str) -> Audio:
    """Load any audio file: WAV natively, everything else through ffmpeg (or flac)."""
    if not os.path.isfile(path):
        raise AudioFileError("no such file: %s" % path)
    if _is_wav(path):
        return read_wav(path)
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return _load_with_ffmpeg(path)
    with open(path, "rb") as f:
        is_flac = f.read(4) == b"fLaC"
    if is_flac and shutil.which("flac"):
        out = subprocess.run(["flac", "-d", "-c", "-s", "--", path], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False)
        if out.returncode != 0:
            raise AudioFileError("flac failed: %s" % out.stderr.decode(errors="replace").strip())
        audio = read_wav(out.stdout)
        audio.path = path
        audio.label = "FLAC %d-bit" % audio.bits
        return audio
    raise AudioFileError(
        "%s is not a WAV file; install ffmpeg (or flac for .flac files) so it can be decoded"
        % path)


def _load_with_ffmpeg(path: str) -> Audio:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate,channels,sample_fmt,bits_per_raw_sample,bits_per_sample",
         "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    try:
        info = json.loads(probe.stdout.decode())["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise AudioFileError("ffprobe could not read %s: %s"
                             % (path, probe.stderr.decode(errors="replace").strip()))
    codec = info.get("codec_name", "?")
    rate = int(info["sample_rate"])
    channels = int(info["channels"])
    sample_fmt = info.get("sample_fmt", "")
    is_float = sample_fmt.startswith(("flt", "dbl"))
    bits = 0
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            bits = int(info.get(key) or 0)
        except ValueError:
            bits = 0
        if bits:
            break
    if is_float:
        out_fmt, dtype = ("f64le", "<f8") if sample_fmt.startswith("dbl") else ("f32le", "<f4")
        bits = 64 if sample_fmt.startswith("dbl") else 32
    else:
        out_fmt, dtype = "s32le", "<i4"
        bits = bits or {"u8": 8, "s16": 16}.get(sample_fmt.rstrip("p"), 32)
    dec = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:a:0",
         "-f", out_fmt, "-acodec", "pcm_" + out_fmt, "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if dec.returncode != 0:
        raise AudioFileError("ffmpeg could not decode %s: %s"
                             % (path, dec.stderr.decode(errors="replace").strip()))
    raw = dec.stdout
    raw = raw[: len(raw) - len(raw) % (np.dtype(dtype).itemsize * channels)]
    data = np.frombuffer(raw, dtype=dtype).reshape(-1, channels)
    data = data.astype(np.float64 if dtype == "<f8" else np.float32 if is_float else np.int32)
    lossless = codec in LOSSLESS_CODECS or codec.startswith("pcm_")
    label = "%s %d-bit" % (codec.upper(), bits) if not is_float else "%s (float)" % codec.upper()
    audio = Audio(data=data, rate=rate, bits=bits, is_float=is_float, label=label, path=path)
    if not lossless:
        audio.meta["lossy_source"] = codec
    return audio


# --------------------------------------------------------------------------
# WAV writing


def int32_to_container(data: np.ndarray, bits: int) -> bytes:
    """Left-justified int32 samples -> little-endian WAV payload of the given width."""
    if bits == 16:
        return (data >> 16).astype("<i2").tobytes()
    if bits == 24:
        return np.ascontiguousarray(data, dtype="<i4").view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()
    if bits == 32:
        return np.ascontiguousarray(data, dtype="<i4").tobytes()
    if bits == 8:
        return ((data >> 24) + 128).astype(np.uint8).tobytes()
    raise ValueError("unsupported bit depth %d" % bits)


def info_chunk(tags: Dict[str, str]) -> bytes:
    """A LIST/INFO chunk body, e.g. {'INAM': 'title', 'IART': 'artist'}."""
    body = b"INFO"
    for key, value in tags.items():
        text = value.encode("utf-8") + b"\0"
        if len(text) & 1:
            text += b"\0"
        body += key.encode("ascii") + struct.pack("<I", len(text)) + text
    return body


class WavWriter:
    """Streaming WAV writer that switches to RF64 when the data passes 4 GiB."""

    def __init__(self, path: str, rate: int, channels: int, bits: int, is_float: bool = False,
                 valid_bits: Optional[int] = None, extra_chunks=()):
        self.path = path
        self.channels = channels
        self.width = bits // 8
        self.block_align = channels * self.width
        self.is_float = is_float
        self.nbytes = 0
        valid_bits = valid_bits or bits
        byte_rate = rate * self.block_align
        if is_float:
            fmt = struct.pack("<HHIIHHH", WAVE_FORMAT_IEEE_FLOAT, channels, rate, byte_rate,
                              self.block_align, bits, 0)
        elif bits > 16 or channels > 2 or valid_bits != bits:
            fmt = struct.pack("<HHIIHHHHI", WAVE_FORMAT_EXTENSIBLE, channels, rate, byte_rate,
                              self.block_align, bits, 22, valid_bits,
                              _CHANNEL_MASKS.get(channels, 0))
            fmt += struct.pack("<H", WAVE_FORMAT_PCM) + _GUID_TAIL
        else:
            fmt = struct.pack("<HHIIHH", WAVE_FORMAT_PCM, channels, rate, byte_rate,
                              self.block_align, bits)
        self.f = open(path, "wb")
        f = self.f
        f.write(b"RIFF\0\0\0\0WAVE")
        # Placeholder that becomes the ds64 chunk if the file outgrows plain RIFF.
        self._junk_pos = f.tell()
        f.write(b"JUNK" + struct.pack("<I", 28) + b"\0" * 28)
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        self._fact_pos = None
        if is_float:
            self._fact_pos = f.tell() + 8
            f.write(b"fact" + struct.pack("<I", 4) + b"\0\0\0\0")
        for cid, body in extra_chunks:
            f.write(cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b""))
        self._data_size_pos = f.tell() + 4
        f.write(b"data\0\0\0\0")

    def write(self, payload: bytes) -> None:
        self.f.write(payload)
        self.nbytes += len(payload)

    def write_array(self, data: np.ndarray, bits: int) -> None:
        if self.is_float:
            self.write(np.ascontiguousarray(data, dtype="<f4" if bits == 32 else "<f8").tobytes())
        else:
            self.write(int32_to_container(data, bits))

    @property
    def frames(self) -> int:
        return self.nbytes // self.block_align

    def close(self, force_rf64: bool = False) -> None:
        f = self.f
        if f.closed:
            return
        if self.nbytes & 1:
            f.write(b"\0")
        total = f.tell()
        frames = self.frames
        if force_rf64 or total - 8 > 0xFFFFFFFF:
            f.seek(0)
            f.write(b"RF64" + struct.pack("<I", 0xFFFFFFFF))
            f.seek(self._junk_pos)
            f.write(b"ds64" + struct.pack("<IQQQI", 28, total - 8, self.nbytes, frames, 0))
            f.seek(self._data_size_pos)
            f.write(struct.pack("<I", 0xFFFFFFFF))
            if self._fact_pos is not None:
                f.seek(self._fact_pos)
                f.write(struct.pack("<I", 0xFFFFFFFF))
        else:
            f.seek(4)
            f.write(struct.pack("<I", total - 8))
            f.seek(self._data_size_pos)
            f.write(struct.pack("<I", self.nbytes))
            if self._fact_pos is not None:
                f.seek(self._fact_pos)
                f.write(struct.pack("<I", frames))
        f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def write_wav(path: str, audio: Audio, extra_chunks=()) -> None:
    with WavWriter(path, audio.rate, audio.channels, audio.bits, audio.is_float,
                   extra_chunks=extra_chunks) as w:
        step = 1 << 18
        for start in range(0, audio.frames, step):
            w.write_array(audio.data[start:start + step], audio.bits)


# --------------------------------------------------------------------------
# ALSA sample formats -> WAV payload


def _swap(width: int) -> Callable[[bytes], bytes]:
    def convert(raw: bytes) -> bytes:
        return np.frombuffer(raw, dtype=np.uint8).reshape(-1, width)[:, ::-1].tobytes()
    return convert


def _s24_le(raw: bytes) -> bytes:
    # 24-bit value in the low three bytes of a little-endian 32-bit word.
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 4)[:, :3].tobytes()


def _s24_be(raw: bytes) -> bytes:
    # 24-bit value in the low three bytes of a big-endian 32-bit word.
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 4)[:, :0:-1].tobytes()


@dataclass(frozen=True)
class AlsaFormat:
    name: str
    width: int  # bytes per sample in the ALSA buffer
    wav_bits: int  # bits per sample in the WAV file we write
    is_float: bool
    convert: Optional[Callable[[bytes], bytes]] = None  # None: bytes are already WAV payload


ALSA_FORMATS = {f.name: f for f in (
    AlsaFormat("S16_LE", 2, 16, False),
    AlsaFormat("S16_BE", 2, 16, False, _swap(2)),
    AlsaFormat("S24_3LE", 3, 24, False),
    AlsaFormat("S24_3BE", 3, 24, False, _swap(3)),
    AlsaFormat("S24_LE", 4, 24, False, _s24_le),
    AlsaFormat("S24_BE", 4, 24, False, _s24_be),
    AlsaFormat("S32_LE", 4, 32, False),
    AlsaFormat("S32_BE", 4, 32, False, _swap(4)),
    AlsaFormat("FLOAT_LE", 4, 32, True),
    AlsaFormat("FLOAT_BE", 4, 32, True, _swap(4)),
    AlsaFormat("FLOAT64_LE", 8, 64, True),
    AlsaFormat("FLOAT64_BE", 8, 64, True, _swap(8)),
)}
