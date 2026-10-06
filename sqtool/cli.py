"""Command line: sq-tool status | gen | capture | analyze | compare."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys

from . import __version__

DESCRIPTION = """\
Capture the exact PCM data an audio player sends to an ALSA device (through the
snd-aloop loopback card) and compare it, sample by sample, with the source file
or with another player's capture.

Typical session:
  sq-tool status                     check that the Loopback card is there
  sq-tool gen -o ~/Music/sq-test     write test tracks, add them to your library
  sq-tool capture roon.wav           ...then press play in Roon (Loopback zone)
  sq-tool capture mandarin.wav       ...then press play in Mandarin (hw:Loopback,0)
  sq-tool compare ~/Music/sq-test/sq-test_24bit_96000Hz.wav roon.wav mandarin.wav
"""


def _prearm(value: str):
    try:
        fmt, rate, channels = value.split(":")
        return fmt.upper(), int(rate), int(channels)
    except ValueError:
        raise argparse.ArgumentTypeError("expected FORMAT:RATE:CHANNELS, e.g. S32_LE:44100:2")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sq-tool", description=DESCRIPTION,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="sq-tool %s" % __version__)
    sub = p.add_subparsers(dest="command", metavar="command")

    s = sub.add_parser("status", help="list sound cards and what players are sending right now")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="also show the USB audio stream details of USB DACs")

    g = sub.add_parser("gen", help="write deterministic test tracks (WAV)")
    g.add_argument("-o", "--outdir", default=".", help="directory to write to (default: current)")
    g.add_argument("--bits", type=int, choices=(16, 24, 32), help="one track with this bit depth...")
    g.add_argument("--rate", type=int, help="...and this sample rate (default: a standard set)")

    c = sub.add_parser("capture", help="record what a player sends to the Loopback card",
                       description="Waits for a player to start on the Loopback card, then records "
                       "exactly what it sends until it stops.")
    c.add_argument("output", help="WAV file to write (metadata goes to OUTPUT.json)")
    c.add_argument("--duration", type=float, help="stop after this many seconds")
    c.add_argument("--silence-stop", type=float, default=5.0, metavar="SECONDS",
                   help="stop after this much digital silence following the music "
                   "(default 5, 0 = never)")
    c.add_argument("--wait", type=float, metavar="SECONDS",
                   help="give up if no playback starts within this time")
    c.add_argument("--card", help="loopback card index or id (default: the first Loopback card)")
    c.add_argument("--device", type=int, choices=(0, 1),
                   help="only watch the player on this loopback device (default: both)")
    c.add_argument("--subdevice", type=int, help="only watch this loopback subdevice")
    c.add_argument("--prearm", type=_prearm, metavar="FORMAT:RATE:CHANNELS",
                   help="start recording immediately in this format, to catch the very first "
                   "sample. This forces the player to use exactly this format.")
    c.add_argument("--arecord", default="arecord", help="arecord binary to use")

    a = sub.add_parser("analyze", help="describe captures or audio files")
    a.add_argument("files", nargs="+")
    a.add_argument("--json", metavar="FILE", help="also write the results as JSON")

    m = sub.add_parser("compare", help="compare captures with a reference, sample by sample",
                       description="Compares each FILE with REFERENCE (the source track or another "
                       "capture), and the FILEs with each other. Exit status: 0 if everything is "
                       "identical, 1 if anything differs, 2 on errors.")
    m.add_argument("reference", help="source track (WAV, or FLAC etc. with ffmpeg) or a capture")
    m.add_argument("files", nargs="+", metavar="FILE")
    m.add_argument("--json", metavar="FILE", help="also write the results as JSON")
    return p


def _clean(obj):
    """Make results JSON-safe (no NaN/inf, no numpy scalars)."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item"):
        obj = obj.item()
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _write_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(_clean(data), f, indent=2)
    print("Results written to %s" % path)


def cmd_status(args) -> int:
    from .alsa import status_report
    print(status_report(args.verbose))
    return 0


def cmd_gen(args) -> int:
    from .generate import DEFAULT_SET, write_test_track
    os.makedirs(args.outdir, exist_ok=True)
    if args.bits or args.rate:
        todo = [(args.bits or 24, args.rate or 96000)]
    else:
        todo = DEFAULT_SET
    for bits, rate in todo:
        print("wrote %s" % write_test_track(args.outdir, bits, rate))
    print("Add these to your player's library. They are test signals, not music: keep the volume "
          "low if they ever play through speakers.")
    return 0


def cmd_capture(args) -> int:
    from .alsa import Capture
    from .analysis import analyze_file
    from .report import format_analysis
    from .wavio import read_wav
    cap = Capture(args.output, card=args.card, duration=args.duration,
                  silence_stop=args.silence_stop, wait=args.wait, prearm=args.prearm,
                  device=args.device, subdevice=args.subdevice, arecord=args.arecord)
    meta = cap.run()
    info = meta["capture"]
    print("Captured %.3f s to %s (stopped: %s)" % (info["seconds"], args.output, info["stop_reason"]))
    if info["capture_overruns"]:
        print("WARNING: the capture overran %d time(s), so samples are missing. Re-run it with "
              "less load on the machine." % info["capture_overruns"])
    print()
    print(format_analysis(analyze_file(read_wav(args.output))))
    return 0


def cmd_analyze(args) -> int:
    from .analysis import analyze_file
    from .report import format_analysis
    from .wavio import load_audio
    results = []
    for path in args.files:
        res = analyze_file(load_audio(path))
        results.append(res)
        print(format_analysis(res))
        print()
    _print_fingerprint_groups(results)
    if args.json:
        _write_json(args.json, results)
    return 0


def _print_fingerprint_groups(results) -> None:
    if len(results) < 2:
        return
    groups = {}
    for r in results:
        if r.get("fingerprint"):
            groups.setdefault(r["fingerprint"], []).append(r["path"])
    same = [paths for paths in groups.values() if len(paths) > 1]
    for paths in same:
        print("Identical sample data (same fingerprint): %s" % ", ".join(paths))
    if not same:
        print("No two files have the same fingerprint (use `compare` to see how they differ).")


def cmd_compare(args) -> int:
    from .analysis import compare
    from .report import format_comparison, format_summary, short_verdict
    from .wavio import load_audio
    ref = load_audio(args.reference)
    files = [load_audio(path) for path in args.files]
    pairs = [(ref, f) for f in files] + list(itertools.combinations(files, 2))
    results, rows = [], []
    for a, b in pairs:
        res = compare(a, b)
        results.append(res)
        print(format_comparison(res))
        print()
        rows.append((os.path.basename(b.path), os.path.basename(a.path), short_verdict(res)))
    if len(rows) > 1:
        print(format_summary(rows))
    if args.json:
        _write_json(args.json, results)
    return 0 if all(r["verdict"] == "IDENTICAL" for r in results) else 1


COMMANDS = {"status": cmd_status, "gen": cmd_gen, "capture": cmd_capture,
            "analyze": cmd_analyze, "compare": cmd_compare}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("sq-tool needs numpy. Install it with:  sudo apt install python3-numpy  "
              "(Fedora: sudo dnf install python3-numpy, or: pip install numpy)", file=sys.stderr)
        return 2
    from .alsa import CaptureError
    from .wavio import AudioFileError
    try:
        return COMMANDS[args.command](args)
    except (CaptureError, AudioFileError, OSError) as exc:
        print("sq-tool: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130
