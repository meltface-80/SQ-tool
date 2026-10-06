# SQ-tool

SQ-tool records the exact PCM samples an audio player hands to the sound driver
and compares them, sample by sample, with the source file or with another
player's recording. It answers the "bits are bits" question with data:
does Roon (or any other player) deliver the same samples as Mandarin, and if
not, exactly how do they differ?

```
player ──► hw:Loopback,0 ═══ snd-aloop ═══ hw:Loopback,1 ──► arecord ──► sq-tool ──► WAV + JSON
```

The player plays to Linux's loopback sound card just as it would play to a USB
DAC. Whatever it writes comes out of the other end of the loopback untouched,
and SQ-tool records it in the exact format the player chose.

## What it can and cannot tell you

It **can** tell you:

* whether the samples are **bit-identical** to the source file (bit-perfect), or
  to another player's capture;
* if they are not, **how** they differ:
  * level changes (volume, leveling, headroom management);
  * dither, rounding or truncation, and to how many bits;
  * resampling;
  * EQ or filtering (the frequency response relative to the source);
  * polarity, swapped or mixed channels;
  * inserted silence (player buffer underruns);
  * lost samples;
* what the player actually sends: the sample format (S16/S24/S32/float), the
  sample rate, and the period and buffer sizes it uses;
* how busy the machine was during playback (CPU use per process).

It **cannot** see anything after the samples leave the computer: the electrical
signal on the USB cable, timing jitter on the wire, noise coupled through USB
or ground, or what the DAC does. With a USB DAC, Linux's USB audio driver
copies these same samples into USB packets unchanged. So if two players
produce identical captures, the DAC receives identical data from both. Any
audible difference then has to come from somewhere else: electrical or
system-load effects, or the listening test itself. Matching levels and
listening blind is the way to check that.

## Requirements

* Linux with ALSA (any distribution), Python 3.8 or newer
* numpy, and `arecord` from alsa-utils
* optional: ffmpeg (or `flac`) to use FLAC, ALAC, AIFF and similar files as references

```sh
sudo apt install python3-numpy alsa-utils ffmpeg     # Debian / Ubuntu
sudo dnf install python3-numpy alsa-utils ffmpeg     # Fedora
sudo pacman -S python-numpy alsa-utils ffmpeg        # Arch
```

To open the loopback device, your user must be in the `audio` group, or you
can run `capture` with `sudo`.

Get the tool:

```sh
git clone https://github.com/meltface-80/SQ-tool.git
cd SQ-tool
./sq-tool --help          # runs straight from the checkout (or: pip install .)
```

## Step by step: Roon vs Mandarin

### 1. Load the loopback sound card

```sh
sudo modprobe snd-aloop
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf   # also load it at boot
sudo systemctl restart roonserver                              # so Roon sees the new device
./sq-tool status
```

`status` should list a card called `Loopback`. It also shows what every card is
receiving right now, so it is worth running while you play to your real DAC
too (see [Match your DAC](#match-your-dac)).

### 2. Make test tracks

```sh
./sq-tool gen -o ~/Music/sq-test
```

This writes four 15.5-second WAV files: 16-bit/44.1 kHz, 24/44.1, 24/96 and
24/192. Each contains:

* 2 s of digital silence;
* white noise;
* loud and very quiet tones (997 Hz left, 1499 Hz right);
* sweeps from 20 Hz to 20 kHz;
* 2 s of silence at the end.

Every one of those sections exposes a different kind of change. Add the folder
to both Roon's and Mandarin's library. These are test signals, not music: keep
the volume down if they ever play through speakers.

You can also use your own music as the reference (WAV directly; FLAC and
others when ffmpeg or flac is installed).

### 3. Set up the Loopback zone in Roon

In *Settings → Audio*, the Loopback card appears among the Roon Server's
devices, possibly twice (`hw:…,0` and `hw:…,1`). Enable one of them. SQ-tool
watches both.

For a bit-perfect baseline:

* set the zone's volume control to *Fixed*;
* turn off everything in its DSP engine: volume leveling, headroom management,
  sample rate conversion, EQ, crossfeed, convolution.

Roon's signal path view should then show the stream as lossless. After that,
switch features back on one at a time if you want to see exactly what each
one does to the data.

### 4. Capture Roon

```sh
./sq-tool capture roon-96k.wav
```

SQ-tool waits for a player. Now play `sq-test_24bit_96000Hz.wav` in the
Loopback zone. SQ-tool picks up Roon's format, records, and stops by itself
when one of these happens:

* Roon closes the device;
* 5 s of digital silence follow the music;
* you press Ctrl+C.

It then prints an analysis of what it recorded and writes `roon-96k.wav` plus
`roon-96k.wav.json`, which holds the capture details.

### 5. Capture Mandarin

Point Mandarin at `hw:Loopback,0` (or `hw:CARD=Loopback,DEV=0`). Use a plain
`hw:` device, not `plughw:`, `default` or PulseAudio/PipeWire: those layers can
resample or change the volume, and SQ-tool will faithfully report that they
did.

```sh
./sq-tool capture mandarin-96k.wav     # then press play in Mandarin
```

### 6. Compare

```sh
./sq-tool compare ~/Music/sq-test/sq-test_24bit_96000Hz.wav roon-96k.wav mandarin-96k.wav
```

Each capture is compared with the source, and then the captures are compared
with each other. Below is example output, made from simulated captures by the
test suite. It is not a real Roon or Mandarin result. `player-a` is
bit-perfect, and `player-b` applied -0.5 dB of digital volume with dither:

```
Reference  sq-test_24bit_96000Hz.wav
           WAV PCM 24-bit: 2 ch, 96000 Hz, 15.500 s
Capture    player-a.wav
           captured ALSA S32_LE: 2 ch, 96000 Hz, 16.000 s

BIT-PERFECT: every sample of the reference arrives unchanged.
  - the reference starts 0.250 s into the capture
  - 1,488,000 frames compared bit for bit and identical (100.00% of the reference's audio is in the capture)
  - 24-bit samples carried in a 32-bit container (low bits zero): lossless padding
  - the capture also holds 0.250 s of digital silence before the reference
  - the capture also holds 0.250 s of digital silence after the reference

Reference  sq-test_24bit_96000Hz.wav
           WAV PCM 24-bit: 2 ch, 96000 Hz, 15.500 s
Capture    player-b.wav
           captured ALSA S32_LE: 2 ch, 96000 Hz, 16.000 s

NOT BIT-PERFECT: the samples differ throughout.
  - best alignment: the reference starts 0.250 s into the capture (correlation 1.0000)
  - level changed: -0.500 dB, -0.500 dB (by channel)
  - frequency response flat within 0.000 dB across 39 Hz-32.0 kHz
  - remaining difference -144.5 dBFS rms: consistent with dither at 24-bit output (about 0.50 LSB rms)
  - where the reference is digital silence, the capture is not: -144.5 dBFS rms (dither or noise added)
...
Summary
  player-a.wav  vs  sq-test_24bit_96000Hz.wav : BIT-PERFECT
  player-b.wav  vs  sq-test_24bit_96000Hz.wav : NOT BIT-PERFECT
  player-b.wav  vs  player-a.wav : DIFFERENT
```

The exit status is 0 when everything is identical, 1 when something differs and
2 on errors, so the comparison can be scripted. `--json FILE` saves all the
numbers.

## Reading the verdicts

| Verdict | Meaning |
|---|---|
| **BIT-PERFECT** / **IDENTICAL** | Every sample matches. Leading and trailing silence and container padding are allowed: a 16-bit sample sent as S32_LE has the same value. |
| **BIT-PERFECT WHERE CAPTURED** | Everything captured matches, but some of the reference's audio was not captured, usually because the capture started late or stopped early. |
| **SAME SAMPLES, WITH INTERRUPTIONS** | The data matches, but the stream has gaps. *Inserted silence* is a player buffer underrun, which would be a dropout on a real DAC. *Missing frames* means samples were lost. |
| **ALTERED** | Most of the stream matches, but listed places differ. |
| **NOT BIT-PERFECT** / **DIFFERENT** | The samples differ throughout. The findings say how: level, dither or rounding, frequency response, channel mixing, noise in silent parts, reduced resolution. |
| **RESAMPLED** / **CHANNELS** | The sample rate or channel count changed, so the player converted the audio. |

A level difference is worth knowing about even when it is tiny. In a listening
comparison, the louder of two otherwise identical sources tends to sound
"better", and differences of a few tenths of a dB are enough.

## Other commands

* `./sq-tool analyze FILE...` describes captures or files:
  * format, and how many bits are really in use (for example "16 significant
    bits: the low 16 bits are zero");
  * silence at the start and end, peak and RMS levels, clipping;
  * DoP (DSD over PCM) detection;
  * a **fingerprint**: a SHA-256 of the sample values. Files carrying the same
    samples have the same fingerprint even in different containers, such as a
    16-bit WAV and an S32_LE capture.

  For captures it also shows:
  * who sent the data, and their period and buffer sizes;
  * whether the capture had overruns;
  * CPU use during playback. Roon's work is spread over several processes
    (RoonAppliance, RAATServer…); the busiest ones are listed.
* `./sq-tool status [-v]` lists the sound cards and every playback stream that
  is open right now: format, rate, period and buffer, and which process owns
  it. `-v` adds the USB stream details of USB DACs.

## Match your DAC

The loopback accepts almost any format: 8 kHz to 768 kHz, 16/24/32-bit integer
and 32-bit float. A player may therefore choose a different output format for
it than for your real DAC.

To see what the player really sends your DAC, play to the DAC and run
`./sq-tool status` (for example `S32_LE, 96000 Hz, 2 ch` and `player:
RAATServer`). Then make the Loopback zone use the same format. In Roon, set
the device's maximum sample rate and bit depth to what the DAC supports.

## Good to know

* **The first milliseconds.** SQ-tool starts recording when the player opens
  the device, a few milliseconds after it starts. The test tracks begin with 2
  s of silence, so nothing is lost. For your own music, the report says
  whether any audio at the start was missed.
  `--prearm S32_LE:44100:2` starts recording before the player does, but it
  forces the player to use exactly that format. Use it only once you know
  what the player picks.
* **One format per capture.** While a capture is open, the loopback holds both
  ends to one sample format and rate. SQ-tool lets go as soon as the player
  closes the device. Capture one track, or an album at a single sample rate,
  per run. If the next track in the queue has a different rate, the player
  may complain or resample it.
* **Overruns.** If the machine is so busy that the capture falls behind, the
  report says so. Re-run the capture rather than trust it.
* **DSD.** DoP streams are recognised and compared like PCM (captures from two
  players can be compared with each other). Native DSD formats are not
  captured.

## Troubleshooting

* **Roon does not list the Loopback device.** Check that `aplay -l` shows the
  Loopback card, then restart Roon Server (`sudo systemctl restart roonserver`).
* **`arecord ... Permission denied`.** Add yourself to the audio group
  (`sudo usermod -aG audio $USER`, then log out and back in), or run the
  capture with `sudo`.
* **`Device or resource busy`.** Something else already has the capture side of
  that loopback device open, for example another capture or a DSP program
  such as CamillaDSP. Stop it first.
* **"Player found" appears before you press play.** Some players keep the
  device open while idle. Press Ctrl+C, press play, then start the capture
  within the first two seconds (the test tracks start with 2 s of silence).
* **The captured format is not what you expected.** See
  [Match your DAC](#match-your-dac). `sq-tool analyze` shows the format, period
  and buffer the player used.

## How the comparison works

SQ-tool first looks for an exact match: a distinctive stretch of the reference
that appears unchanged in the capture. From there it walks along both files
and records every place where they stop matching. If matching resumes later,
the jump is classified as:

* inserted frames: an underrun when they are silence;
* dropped frames;
* altered samples.

If no exact match exists at all, it aligns the files by correlation instead.
It then fits the capture to the reference with a gain and channel matrix and
analyses what is left over:

* the size and spectrum of the residual, and its relation to the output's
  least significant bit;
* the frequency response;
* how the level changes over time;
* what happens in passages where the reference is digital silence.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests need no sound hardware. They simulate the loopback card's `/proc`
entries and `arecord`, and check the comparison against known manipulations
(gain, dither, truncation, EQ, crossfeed, swaps, underruns, dropouts).
