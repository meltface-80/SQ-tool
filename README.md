# SQ-tool

**SQ-tool records exactly what your music player sends to your DAC, and shows
whether two players send the same thing.**

It runs in Docker on the computer the DAC is plugged into. You use it from a
phone or tablet at `http://<server>:3400`:

1. **Capture.** Press *Start*, then play a track in Roon, Mandarin or any other
   player, to your USB DAC as usual. SQ-tool records the USB audio packets on
   their way to the DAC. You don't need to change anything in the players.
2. **Library.** Every capture is saved and analysed:
   * format and sample rate;
   * how many bits are really used;
   * levels and silence;
   * a fingerprint of the sample data;
   * which program sent it;
   * USB statistics and CPU load.
3. **Compare.** Pick two or more captures (and the original file, if you like)
   to see them side by side. Each pair is **nulled**: aligned sample by sample
   and subtracted. Identical data leaves nothing behind. If the data differs,
   SQ-tool says how:
   * level;
   * dither or rounding;
   * frequency response;
   * channel mixing;
   * dropouts, or audio that stopped and restarted.

   You can also download the difference as a WAV file and listen to it.

```
Roon / Mandarin ──► ALSA ──► USB audio driver ──► USB cable ──► SMSL SU-1
                                                     │
                                         usbmon (kernel USB monitor)
                                                     │
                                                SQ-tool :3400 ──► your tablet
```

## What it can and cannot tell you

SQ-tool sees the exact bytes in every USB audio packet sent to the DAC. With
those it shows:

* whether two players deliver **bit-identical** audio;
* **how** the data differs when it does: volume, leveling, headroom, dither,
  resampling, EQ, crossfeed, polarity, channel swaps;
* whether the stream had **interruptions**: underruns, or playback that stopped
  and restarted;
* how each player uses ALSA: sample format, period and buffer sizes;
* how busy the computer was while playing.

It **cannot** measure what happens electrically: jitter on the cable, noise
coupled through USB or ground, or the DAC's own behaviour.

If two players produce identical captures, the DAC received the same data from
both. Any audible difference then comes from somewhere else: electrical or
system-load effects, or the listening test itself. Level-matched blind
listening is the way to check that.

## Install

You need a Linux machine with Docker, and the USB DAC plugged into it.

**1. Load the kernel's USB monitor** (once, on the host):

```sh
sudo modprobe usbmon
echo usbmon | sudo tee /etc/modules-load.d/usbmon.conf   # also load it at every boot
```

**2. Start SQ-tool:**

```sh
docker run -d --name sq-tool --restart unless-stopped \
  --privileged --pid=host \
  -p 3400:3400 \
  -v sq-tool-data:/data \
  ghcr.io/meltface-80/sq-tool:latest
```

**3. Open `http://<server-address>:3400`** on your phone or tablet, for
example `http://192.168.1.20:3400`.

What the options do:

| Option | Why |
|---|---|
| `--privileged` | lets it read the USB monitor (`/dev/usbmon*`) and the sound cards' status (`/proc/asound`), which Docker normally hides |
| `--pid=host` | lets it name the program sending the audio (RAATServer, mandarin, …) and measure CPU use |
| `-p 3400:3400` | the web page |
| `-v sq-tool-data:/data` | keeps your captures when the container is updated or restarted |

**Test tracks in your music library (optional).** SQ-tool can generate test
tracks: 16/44.1, 24/44.1, 24/96 and 24/192. To have them appear in Roon's and
Mandarin's libraries by themselves, add this option to the command above:

```sh
-v /path/to/your/music/sq-test:/data/test-tracks
```

**Updating:**

```sh
docker pull ghcr.io/meltface-80/sq-tool:latest
docker rm -f sq-tool
# then run the docker run command again
```

Your captures stay in the `sq-tool-data` volume.

**Building the image yourself.** The image is published from the `main`
branch. To build it from a branch, or if you prefer to build your own:

```sh
docker build -t sq-tool https://github.com/meltface-80/SQ-tool.git#main
```

Then use `sq-tool` instead of `ghcr.io/meltface-80/sq-tool:latest` in the run
command.

If `docker pull` asks you to log in, the package isn't public yet. Make it
public once: on GitHub, go to *Your profile → Packages → sq-tool → Package
settings → Change visibility → Public*.

## Using it

### Capture

1. Pick the DAC in *1. Output device*. It shows what is playing right now:
   format, rate and sending program.
2. Give the capture a name, for example *Roon – Track 3*, and press
   **Start capture**.
3. Play the track to the DAC from the player. SQ-tool records it, and saves it
   when the music has stopped for 5 seconds (you can change that). You can
   also press **Stop**.
4. Repeat with the other player and the same track.

If the player changes sample rate between tracks, each format becomes a
separate capture.

### Library

Captures are listed newest first. The coloured square is a fingerprint of the
sample data, so **the same colour means identical audio data**, whatever the
container.

* **Import source file** adds an original file (WAV or FLAC) as a reference.
* **Test tracks** creates the test signals and adds them to the library as
  references.

Tap an item for its full analysis and charts. From there you can download the
captured audio as WAV, rename it, add notes, or delete it.

### Compare

Tick two or more items and press **Compare**. You get:

* **Side by side**: one column per capture. Cells that differ from the first
  column are highlighted.
* **Null tests** for every pair:
  * the verdict, with a plain-language explanation;
  * a timeline of where the data matches;
  * how the difference behaves over time and frequency;
  * a **difference file** to download.
* **Bit usage**: how often each bit of the sample word is set. Padding shows as
  empty bits; dither fills the low bits.
* **Spectrum** and **level over time** of each capture, overlaid.

## Getting a fair comparison

* Play **the same track** from both players, to **the same DAC**.
* Compare format and sample rate first, in the side-by-side table. If the
  players send different formats, the data cannot be identical.
* For a bit-perfect baseline in Roon:
  * set the zone's volume to *Fixed*, or to the DAC's own (device) volume;
  * turn off everything in its DSP engine: volume leveling, headroom
    management, sample rate conversion, EQ, crossfeed, convolution.

  Then switch features on one at a time to see exactly what each does to the
  data.
* Capture the same player twice. The two captures should be identical to each
  other: that shows the measurement itself is consistent.
* If the levels differ, even by a few tenths of a dB, that alone can make one
  player sound "better" in a sighted comparison.

## Reading the verdicts

| Verdict | Meaning |
|---|---|
| **BIT-PERFECT / IDENTICAL** | Every sample matches. Silence before and after is allowed, and so is padding: 16-bit samples sent in a 32-bit container keep the same values. |
| **BIT-PERFECT WHERE CAPTURED** | Everything captured matches, but part of the track is missing, e.g. playback was stopped early. |
| **SAME SAMPLES, WITH INTERRUPTIONS** | The data matches, but the stream has gaps: inserted silence (an underrun), lost samples, or repeated audio. |
| **ALTERED** | Most of the stream matches; the listed places differ. |
| **NOT BIT-PERFECT / DIFFERENT** | The samples differ throughout; the findings say how. |
| **RESAMPLED / CHANNELS** | The sample rate or the channel count differs. |

For a null test that does not cancel, the numbers mean:

* **difference … dB below the music**: how deep the null is. Rounding or dither
  at 24 bits leaves the difference roughly 130–140 dB below typical music.
* **level changed**: the volume difference.
* **remaining difference … dBFS**: what is left once that level difference is
  removed. It is typically:
  * dither, about 0.5 LSB;
  * plain rounding, about 0.29 LSB;
  * or a sign of heavier processing.

## How it works

Linux's USB audio driver (`snd-usb-audio`) copies the samples the player
writes into USB packets unchanged. With `usbmon`, the kernel's USB packet
monitor, SQ-tool reads every packet sent to the DAC's audio endpoint and joins
them back into the sample stream. This is passive: it cannot change what the
player or the DAC does.

Along the way it records:

* the stream format, from `/proc/asound`;
* the sample rate the host set on the DAC;
* how many frames each USB packet carried;
* packet errors;
* when the stream stopped and restarted.

Comparisons first look for a bit-exact alignment and follow it through the
whole file. Every place where it breaks is classified as an insertion, a drop,
a repeat or an alteration. If there is no exact match at all, the files are
aligned by correlation instead. The capture is then fitted to the reference
with a gain and channel matrix, and SQ-tool analyses what remains.

## Command line

The same code also runs without Docker:

```sh
sudo apt install python3-numpy flac      # Debian/Ubuntu; Python 3.8+
./sq-tool status                         # sound cards and what they are being sent right now
sudo ./sq-tool capture --usb roon.wav    # record what the USB DAC receives
./sq-tool compare source.flac roon.wav mandarin.wav
./sq-tool analyze roon.wav
./sq-tool serve --data ./sq-data         # the web interface, as in Docker
```

There is also a second capture method that needs no USB DAC: the ALSA loopback
card (`sudo modprobe snd-aloop`). The player plays to `hw:Loopback,0` and
SQ-tool records the other end. It also appears in the web interface's device
list. Prefer the USB method when you have a USB DAC, because the player then
sees the real device.

## Troubleshooting

* **"usbmon is not loaded"**: run `sudo modprobe usbmon` on the host. The
  container picks it up without a restart.
* **"The sound cards are hidden from this container"**: start the container
  with `--privileged`.
* **The DAC isn't listed**: check that it is connected and switched on, and
  that `aplay -l` on the host lists it.
* **"Audio is flowing but the DAC's stream format is unknown"**: SQ-tool can
  see the USB packets but not `/proc/asound`. Use `--privileged`.
* **"USB events were lost"**: the computer was too busy for the monitor to keep
  up. Other heavy USB traffic on the same bus can cause it, such as a USB disk
  on a USB 2 port. Plug the DAC into another port or controller, and capture
  again.
* **The sending program is not shown**: add `--pid=host`.

## Security

The web page has no login, so keep port 3400 on your home network. The USB
monitor can see all traffic on the DAC's USB bus; SQ-tool keeps only the DAC's
audio data.

## Tests

```sh
pip install numpy
python3 -m unittest discover -s tests
```

The tests need no audio hardware. They simulate the DAC: `/proc/asound`
entries, plus usbmon events encoded byte for byte as the kernel delivers them.
They cover:

* exact capture;
* stream restarts and format changes;
* comparisons against known manipulations (gain, dither, truncation, EQ,
  crossfeed, swaps, underruns, dropouts, repeats);
* the whole web API.
