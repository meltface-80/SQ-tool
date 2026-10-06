#!/usr/bin/env python3
"""Stand-in for arecord on a simulated snd-aloop card (used by the tests only).

Streams the bytes in $FAKE_PCM (what the "player" sent, already in the ALSA
format; a list of files plays one per run) to stdout, skipping $FAKE_SKIP_BYTES at the start like a capture that
began late. When the data runs out it marks the player's /proc entries closed
($FAKE_CLOSE_DIR) and keeps sending silence, as the real loopback does, until
it is stopped with SIGTERM or SIGINT.
"""
import os
import signal
import sys
import time

args = sys.argv[1:]
opts = {}
i = 0
while i < len(args):
    if args[i] in ("-D", "-f", "-r", "-c", "-t", "-B"):
        opts[args[i]] = args[i + 1]
        i += 2
    else:
        i += 1
with open(os.environ["FAKE_ARGS_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
expect = os.environ.get("FAKE_EXPECT")
if expect and expect != "%s:%s:%s" % (opts.get("-f"), opts.get("-r"), opts.get("-c")):
    sys.stderr.write("arecord: set_params:1343: Sample format non available\n")
    sys.exit(1)

stop = False


def on_signal(signum, frame):
    global stop
    stop = True


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)
# $FAKE_PCM may list several files: each run of this program plays the next one.
runs = os.environ["FAKE_PCM"].split(os.pathsep)
counter = os.environ["FAKE_ARGS_LOG"] + ".runs"
run = int(open(counter).read()) if os.path.exists(counter) else 0
with open(counter, "w") as f:
    f.write(str(run + 1))
with open(runs[min(run, len(runs) - 1)], "rb") as f:
    data = f.read()[int(os.environ.get("FAKE_SKIP_BYTES", "0")):]
frame = int(os.environ["FAKE_FRAME_BYTES"])
chunk = frame * 4096
overrun_at = int(os.environ.get("FAKE_OVERRUN_AT", "-1"))
out = sys.stdout.buffer
sys.stderr.write("Recording raw data 'stdin' : simulated\n")
sys.stderr.flush()
try:
    pos = 0
    while not stop and pos < len(data):
        out.write(data[pos:pos + chunk])
        out.flush()
        pos += chunk
        if 0 <= overrun_at <= pos:
            sys.stderr.write("overrun!!! (at least 2.000 ms long)\n")
            sys.stderr.flush()
            overrun_at = -1
        time.sleep(0.001)
    close_dir = os.environ.get("FAKE_CLOSE_DIR")
    if close_dir and not stop:
        for name in ("hw_params", "status"):
            with open(os.path.join(close_dir, name), "w") as f:
                f.write("closed\n")
    while not stop:
        out.write(b"\0" * chunk)
        out.flush()
        time.sleep(0.01)
except BrokenPipeError:
    pass
