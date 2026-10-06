"""Plain-text reports for `sq-tool analyze` and `sq-tool compare`."""

from __future__ import annotations

import math
from typing import List, Optional

HEADLINES = {
    "IDENTICAL": ("BIT-PERFECT: every sample of the reference arrives unchanged.",
                  "IDENTICAL: both files carry exactly the same samples."),
    "PARTIAL": ("BIT-PERFECT WHERE CAPTURED: every captured sample is unchanged, but part of the "
                "reference's audio is missing from the capture.",
                "IDENTICAL WHERE THEY OVERLAP: the samples match, but one file covers less audio."),
    "GAPS": ("SAME SAMPLES, WITH INTERRUPTIONS: the samples are unchanged, but the stream has "
             "gaps or jumps.",) * 2,
    "ALTERED": ("ALTERED: parts of the stream differ from the reference.",) * 2,
    "DIFFERENT": ("NOT BIT-PERFECT: the samples differ throughout.",
                  "DIFFERENT: the samples differ throughout."),
    "RESAMPLED": ("NOT BIT-PERFECT: different sample rate.",) * 2,
    "CHANNELS": ("NOT BIT-PERFECT: different number of channels.",) * 2,
    "NO MATCH": ("NO MATCH: the reference could not be found in the capture.",) * 2,
    "NO SIGNAL": ("NOTHING TO COMPARE: the reference is digital silence.",) * 2,
}

SHORT = {"IDENTICAL": ("BIT-PERFECT", "IDENTICAL"),
         "PARTIAL": ("BIT-PERFECT (partial)", "IDENTICAL (partial)"),
         "DIFFERENT": ("NOT BIT-PERFECT", "DIFFERENT")}


def short_verdict(res: dict) -> str:
    pair = SHORT.get(res["verdict"])
    if pair:
        return pair[1] if res.get("ref_is_capture") else pair[0]
    return res["verdict"]


def _secs(frames: float, rate: int) -> str:
    return "%.3f s" % (frames / rate)


def _dur(frames: int, rate: int) -> str:
    ms = 1000.0 * frames / rate
    return "%s frames (%s)" % (format(frames, ","), "%.1f ms" % ms if ms < 1000 else "%.3f s" % (ms / 1000))


def _db(v: Optional[float]) -> str:
    if v is None or (isinstance(v, float) and math.isinf(v)):
        return "-inf"
    return "%.1f" % v


def format_analysis(a: dict) -> str:
    rate = a["rate"]
    lines = [a["path"] or "(file)"]

    def row(name: str, text: str) -> None:
        lines.append("  %-12s %s" % (name, text))

    container = "float" if a["is_float"] else "PCM"
    row("Format", "%s: %d ch, %d Hz, %s %d-bit" % (a["label"], a["channels"], rate, container, a["bits"]))
    row("Length", "%.3f s (%s frames)" % (a["duration"], format(a["frames"], ",")))
    if a["silent"]:
        row("Content", "digital silence only")
        return "\n".join(lines)
    res = a["resolution"]
    if a["is_float"]:
        row("Resolution", "every value is an exact %d-bit integer sample" % res if res
            else "uses full floating-point resolution (not integer sample values)")
    elif res is not None:
        text = "%d significant bits" % res
        if res < a["bits"]:
            text += ": the low %d bits of every sample are zero (padding)" % (a["bits"] - res)
        elif res < 32 and a["bits"] == 32:
            text += " in a 32-bit container"
        row("Resolution", text)
    if a.get("dop"):
        row("DSD", a["dop"])
    row("Silence", "%s of digital silence at the start, %s at the end" % (
        _secs(a["lead_silence"], rate), _secs(a["trail_silence"], rate)))
    stats = a["channel_stats"]
    row("Peak", " | ".join("ch%d %s dBFS" % (i + 1, _db(s["peak_db"])) for i, s in enumerate(stats)))
    row("RMS", " | ".join("ch%d %s dBFS" % (i + 1, _db(s["rms_db"])) for i, s in enumerate(stats)))
    clipped = sum(s["clipped"] for s in stats)
    row("Clipping", "%s samples at full scale" % format(clipped, ",") if clipped else "none")
    dc = max(abs(s["dc"]) for s in stats)
    if dc > 1e-3:  # above -60 dBFS: a real offset rather than the average of the music
        row("DC offset", "%.1f dBFS" % (20 * math.log10(dc)))
    q = a.get("quietest_block_db")
    row("Quiet parts", "%d of %d 50 ms blocks are digital silence; quietest other block %s dBFS"
        % (a["zero_blocks"], a["blocks"], _db(q) if q is not None else "-"))
    row("Fingerprint", "%s  (SHA-256 of the samples from the first to the last non-zero one)"
        % a["fingerprint"][:16])
    if a.get("lossy_source"):
        row("Note", "lossy source (%s): not suitable as a bit-perfect reference" % a["lossy_source"])
    cap = a.get("capture")
    if cap:
        lines.append("")
        player = cap.get("player") or {}
        who = "%s (pid %s)" % (player.get("name"), player.get("pid")) if player else "unknown player"
        row("Captured", "%s from %s, sent by %s" % (cap.get("started", "?"), cap.get("playback_device"), who))
        p = cap.get("period_size")
        b = cap.get("buffer_size")
        if p and b:
            row("Player I/O", "%s, period %d frames (%.1f ms), buffer %d frames (%.1f ms), %s" % (
                cap["alsa_format"], p, 1000.0 * p / rate, b, 1000.0 * b / rate, cap.get("access")))
        row("Stopped", cap.get("stop_reason", "?"))
        missed = cap.get("player_frames_before_capture")
        if missed:
            row("Start", "the player had already played about %s of this stream when the capture "
                "began" % _dur(missed, rate))
        over = cap.get("capture_overruns", 0)
        row("Integrity", "capture overruns: %d%s; player underruns seen: %d" % (
            over, " (CAPTURE INCOMPLETE: re-run it)" if over else "", cap.get("player_xruns_seen", 0)))
    cpu = a.get("cpu")
    if cpu:
        _cpu_rows(cpu, row)
    return "\n".join(lines)


def _cpu_rows(cpu: dict, row) -> None:
    parts = []
    if "system_percent" in cpu:
        parts.append("whole system %.1f%% of %s CPUs" % (cpu["system_percent"], cpu.get("cpus", "?")))
    top = ", ".join("%s %.1f%%" % (t["name"], t["percent_of_one_cpu"]) for t in cpu.get("top", [])[:5])
    if top:
        parts.append("busiest: " + top + " (of one CPU)")
    if parts:
        row("CPU", "; ".join(parts))


def _event_text(ev: dict, rate: int) -> str:
    at = "at %.3f s into the reference" % (ev["ref"] / rate)
    kind = ev["kind"]
    if kind == "inserted":
        what = "silence" if ev.get("cap_silent") else "extra audio"
        hint = " (typical of a player buffer underrun)" if ev.get("cap_silent") else ""
        return "%s: %s of %s inserted%s" % (at, _dur(ev["cap_frames"], rate), what, hint)
    if kind == "dropped":
        return "%s: %s of the reference missing (samples lost)" % (at, _dur(ev["ref_frames"], rate))
    if kind == "altered":
        return "%s: %s differ (difference %s dBFS rms, peak %s dBFS)" % (
            at, _dur(ev["ref_frames"], rate), _db(ev.get("diff_rms_db")), _db(ev.get("diff_peak_db")))
    if kind == "replaced":
        return "%s: %s of the reference replaced by %s of different audio" % (
            at, _dur(ev["ref_frames"], rate), _dur(ev["cap_frames"], rate))
    if kind == "repeated":
        return "%s: the stream jumped back and repeated %s" % (at, _dur(-ev["ref_frames"], rate))
    return "%s: %s" % (at, kind)


def format_comparison(res: dict) -> str:
    ref, cap = res["ref"], res["cap"]
    rate = ref["rate"]
    lines = []
    second = "Compared" if res.get("ref_is_capture") else "Capture"
    for name, f in (("Reference", ref), (second, cap)):
        lines.append("%-10s %s" % (name, f["path"] or "(file)"))
        lines.append("%-10s %s: %d ch, %d Hz, %.3f s" % ("", f["label"], f["channels"], f["rate"],
                                                        f["frames"] / f["rate"] if f["rate"] else 0))
    lines.append("")
    pair = HEADLINES.get(res["verdict"], (res["verdict"],) * 2)
    lines.append(pair[1] if res.get("ref_is_capture") else pair[0])
    other = "compared file" if res.get("ref_is_capture") else "capture"
    out = []
    if res.get("summary"):
        out.append(res["summary"])
    out.extend(res.get("notes", []))
    exact = res.get("exact")
    if exact and res["verdict"] in ("IDENTICAL", "PARTIAL", "GAPS", "ALTERED"):
        if exact["lag"] >= 0:
            out.append("the reference starts %s into the %s" % (_secs(exact["lag"], rate), other))
        compared = exact["exact_frames"]
        out.append("%s frames compared bit for bit and identical (%.2f%% of the reference's audio "
                   "is in the capture)" % (format(compared, ","), 100 * exact["covered_audio_fraction"]))
        if exact["missing_start"]:
            out.append("the first %s of the reference %s not captured" % (
                _secs(exact["missing_start"], rate),
                "(digital silence only) were" if exact["missing_start_silent"] else "(with audio) were"))
        if exact["missing_end"]:
            out.append("the last %s of the reference %s not captured" % (
                _secs(exact["missing_end"], rate),
                "(digital silence only) were" if exact["missing_end_silent"] else "(with audio) were"))
        for ev in exact["events"][:20]:
            out.append(_event_text(ev, rate))
        if len(exact["events"]) > 20:
            out.append("... and %d more" % (len(exact["events"]) - 20))
        if exact.get("differs_from") is not None:
            out.append("from %.3f s into the reference onwards the capture no longer matches "
                       "(%s)" % (exact["differs_from"] / rate, _dur(exact.get("differing_frames", 0), rate)))
        ref_res, cap_bits = res.get("ref_resolution"), cap["bits"]
        if (res["verdict"] in ("IDENTICAL", "PARTIAL") and not cap["is_float"] and ref_res
                and ref_res < cap_bits):
            out.append("%d-bit samples carried in a %d-bit container (low bits zero): lossless padding"
                       % (ref_res, cap_bits))
        if cap["is_float"] and not ref["is_float"]:
            out.append("integer samples carried as floating point without any change in value")
        for side in ("before", "after"):
            frames = exact.get("cap_" + side, 0)
            if frames:
                out.append("the capture also holds %s of %s %s the reference" % (
                    _secs(frames, rate),
                    "digital silence" if exact["cap_%s_silent" % side] else "other audio", side))
    approx = res.get("approx")
    if approx:
        if approx.get("lag") is not None:
            out.append("best alignment: the reference starts %s into the %s (correlation %.4f)"
                       % (_secs(approx["lag"], rate), other, approx.get("correlation", 0)))
        out.extend(approx.get("findings", []))
    lines.extend("  - " + o for o in out)
    return "\n".join(lines)


def format_summary(rows: List[tuple]) -> str:
    width = max(len(a) for a, _, _ in rows)
    lines = ["Summary"]
    for a, b, verdict in rows:
        lines.append("  %-*s  vs  %s : %s" % (width, a, b, verdict))
    return "\n".join(lines)
