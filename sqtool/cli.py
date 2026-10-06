"""Command line: sq-tool status | gen | capture | analyze | compare."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

from . import __version__

DESCRIPTION = """\
Capture exactly what audio players send to a DAC and compare it, sample by
sample, with the source file or with another player's capture.

The web interface (what the Docker image runs):
  sq-tool serve --data ./sq-data         then open http://<this machine>:3400

From the command line:
  sq-tool status                         sound cards and what they are being sent
  sq-tool capture --usb roon.wav         record what the USB DAC receives (usbmon)
  sq-tool capture roon.wav               record a player on the ALSA loopback card
  sq-tool compare source.flac roon.wav mandarin.wav
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

    w = sub.add_parser("serve", help="run the web interface (port 3400)")
    w.add_argument("--data", default=os.environ.get("SQTOOL_DATA", "sq-data"),
                   help="folder for captures, analyses and test tracks (default: ./sq-data)")
    w.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    w.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3400")),
                   help="port (default 3400)")

    c = sub.add_parser("capture", help="record what a player sends to a DAC",
                       description="Waits for playback, then records exactly what the player sends "
                       "until it stops. With --usb it records the USB packets going to a USB DAC "
                       "(usbmon, needs root); otherwise it records from the ALSA loopback card.")
    c.add_argument("output", help="WAV file to write (metadata goes to OUTPUT.json)")
    c.add_argument("--usb", nargs="?", const="", metavar="CARD",
                   help="record what a USB DAC receives (optionally which card, by index or id)")
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


def _write_json(path: str, data) -> None:
    from .library import clean
    with open(path, "w") as f:
        json.dump(clean(data), f, indent=2)
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


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.data, args.host, args.port)
    return 0


def cmd_capture_usb(args) -> int:
    import shutil
    import tempfile
    from .alsa import find_usb_dac
    from .analysis import analyze_file
    from .report import format_analysis
    from .usbmon import UsbCapture
    from .wavio import SIDECAR_SUFFIX, read_wav
    dac = find_usb_dac(args.usb or None)
    base, ext = os.path.splitext(args.output)
    saved = []

    def on_take(path, meta):
        dest = args.output if not saved else "%s-%d%s" % (base, len(saved) + 1, ext or ".wav")
        shutil.move(path, dest)
        shutil.move(path + SIDECAR_SUFFIX, dest + SIDECAR_SUFFIX)
        saved.append(dest)

    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(args.output))) as tmp:
        cap = UsbCapture(dac, tmp, name=os.path.basename(base), idle_stop=args.silence_stop,
                         max_seconds=args.duration, on_take=on_take)
        try:
            cap.run()
        except KeyboardInterrupt:
            cap.stop("stopped with Ctrl+C")
    if not saved:
        print("Nothing was recorded (%s)." % (cap.stop_reason or "no audio"))
        return 1
    for path in saved:
        print()
        print(format_analysis(analyze_file(read_wav(path))))
    return 0


def cmd_capture(args) -> int:
    if args.usb is not None:
        return cmd_capture_usb(args)
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
            "analyze": cmd_analyze, "compare": cmd_compare, "serve": cmd_serve}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    import importlib.util
    if importlib.util.find_spec("numpy") is None:
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
