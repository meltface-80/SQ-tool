# SQ-tool

**Is your music player bit-perfect? SQ-tool records exactly what Roon, Mandarin,
Lyrion (Squeezelite) or any other player sends to its output, and compares it
sample by sample with the original file and with each other.**

It runs in Docker on the computer your players run on. You use it from a phone
or tablet at `http://<server>:3400`:

1. **Choose a song** from your music folder, for example a Qobuz download.
   SQ-tool reads and analyses the file.
2. **Record Roon.** Press *Record*, then play the song in Roon to the
   **Loopback** output. SQ-tool records every sample Roon sends.
3. **Record Mandarin** the same way.
4. **Read the results.**
   * Is Roon bit-perfect? Is Mandarin? In other words, is every sample they
     send identical to the file?
   * Do both send the same data? That is, would your DAC convert exactly the
     same numbers from both?
   * Detailed, zoomable **spectrograms**: the file, what each player sent, and
     the differences between them.
   * **Charts**: spectrum, level over time, what remains after matching levels,
     a sample-by-sample close-up of the waveforms, and bit usage.
   * **Details** of each: format, bits in use, peak, loudness, a fingerprint of
     the sample data, the player's buffer sizes, and CPU load.
   * **Downloads**: each recording, and the differences as WAV files.

```
song.flac ──► Roon ──────┐
                         ├──► Loopback (a virtual sound card) ──► SQ-tool ──► :3400 on your tablet
song.flac ──► Mandarin ──┘
```

## Install

You need the Linux computer that runs Roon Server and Mandarin, with Docker,
and your music folder on it. Run:

```sh
docker run -d --name sq-tool --restart unless-stopped \
  --privileged --pid=host \
  -v /dev/snd:/dev/snd \
  -v /lib/modules:/lib/modules:ro \
  -v /path/to/your/music:/music:ro \
  -v sq-tool-data:/data \
  -p 3400:3400 \
  ghcr.io/meltface-80/sq-tool:latest
```

Replace `/path/to/your/music` with your music folder, the one Roon and Mandarin
play from. Then open `http://<server-address>:3400` on your phone or tablet, for
example `http://192.168.1.20:3400`.

What the options do:

| Option | Why |
|---|---|
| `--privileged` | lets SQ-tool see the sound cards (`/proc/asound`) and load the Loopback driver |
| `--pid=host` | shows which program sent the audio (RAATServer, mandarin, …) |
| `-v /dev/snd:/dev/snd` | the sound devices, including the Loopback card once it is loaded |
| `-v /lib/modules:/lib/modules:ro` | your kernel's drivers, so SQ-tool can load the Loopback driver (`snd-aloop`) itself |
| `-v /path/to/your/music:/music:ro` | your music, read-only, to choose the song from |
| `-v sq-tool-data:/data` | keeps your tests when the container is updated |
| `-p 3400:3400` | the web page |

**The Loopback card.** SQ-tool loads it when it starts, and there is a button
for it on the page. The first time it appears, **restart Roon Server** so Roon
lists the new output. To load it at every boot, before Roon starts:

```sh
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf
```

**Updating:**

```sh
docker pull ghcr.io/meltface-80/sq-tool:latest
docker rm -f sq-tool
# then run the docker run command again
```

Your tests stay in the `sq-tool-data` volume.

**The image.** GitHub builds it on every push:

* `:latest` comes from the `main` branch.
* Each other branch gets its own tag, named after the branch with `/` turned
  into `-`.

If `docker pull` asks you to log in, the package isn't public yet. Make it
public once: on GitHub, go to *Your profile → Packages → sq-tool → Package
settings → Change visibility → Public*. Or build the image yourself:

```sh
docker build -t sq-tool https://github.com/meltface-80/SQ-tool.git#main
```

Then use `sq-tool` instead of `ghcr.io/meltface-80/sq-tool:latest` in the run
command.

## Setting up the players

**Roon**

1. Open *Settings → Audio*. Under your Roon Server, find **Loopback** and press
   **Enable**. If it's listed twice, either one works. Name the zone, for
   example "SQ-tool".
2. In that zone's *Device Setup*, use the same settings as your DAC's zone, so
   the test shows what your DAC gets.
   * For a pure bit-perfect check, set *Volume control* to **Fixed volume**.
   * Turn off everything in the zone's *DSP Engine*: volume leveling, headroom
     management, sample rate conversion, EQ, crossfeed, convolution.
3. When testing, play the song to that zone.

**Lyrion Music Server.** Lyrion plays through a player, usually Squeezelite.
Run a second Squeezelite on the server that plays to the Loopback card:

```sh
squeezelite -n SQ-tool -m 02:00:00:00:00:01 -o hw:CARD=Loopback,DEV=0 -s 127.0.0.1
```

`-n` is the name it gets in Lyrion, `-m` a MAC address no other player uses,
`-o` the Loopback output, and `-s` the Lyrion server (127.0.0.1 when it runs on
the same computer).

1. In Lyrion, give the "SQ-tool" player the same settings as your DAC's
   player, so the test shows what your DAC gets.
2. For a pure bit-perfect check, set these in the player's *Audio* settings:
   * *Volume Control*: output level fixed at 100%;
   * *Replay Gain*: off;
   * *Crossfade*: no fade;
   * *Bitrate Limiting*: no limit.
3. Play the song to that player.

Squeezelite keeps its output open, sending silence, while the player is on.
SQ-tool waits for the music and times the song from there.

**Mandarin** (or any other player): choose the output device **Loopback**
(`hw:Loopback,0`, or `hw:N,0` with the card number the page shows), the same
way you would choose your DAC.

Each test compares two players. Their names are set in *Settings*, or with **⋯**
on a test. To compare, say, Roon against Lyrion, name the second player
"Lyrion".

## Using it

* **Start a new test** and pick the song: browse your music folder, search it,
  or upload the file from your phone or tablet. SQ-tool reads FLAC, WAV and
  AIFF files.
* Press **Record Roon**. SQ-tool waits for playback on the Loopback card. Then
  play the song in Roon, from the beginning. Recording starts when Roon starts
  sending audio.
* Recording stops by itself **at the song's last sample**. SQ-tool recognises
  the song in what Roon sends, so it stops there even when Roon goes straight
  on to the next track in its queue.
  * From a bit-perfect player, SQ-tool follows the song sample by sample, so a
    dropout, a pause, a repeat or a seek moves the end with it.
  * From a player that isn't bit-perfect (volume, DSP, another sample rate),
    SQ-tool lines the song up by its sound, then finds the song's last notes to
    place the end, after any dropout. If they can't be found (crossfaded into
    the next track, say), the recording ends a quarter of a second after where
    the song should end.
  * It also stops when Roon closes the output, or after 5 seconds of digital
    silence. The silence time can be changed in Settings; if the song itself
    has a longer silent passage, SQ-tool waits longer.
  * You can also press **Stop now**.
* Press **Record Mandarin** and do the same.
* **Results** appear as soon as each recording is analysed: one card for each
  player against the file, and one for the two players against each other.
* **Record again** replaces a player's recording, for example after you change
  a setting in Roon.
* The player names can be changed in Settings (for new tests) or with **⋯** on
  a test. **⋯** also deletes a test.

### The spectrograms

There is one picture per recording, all lined up on the song's timeline and on
one colour scale, from dark graphite (the lowest level, −150 dB by default)
through bronze and brass to pale gold (0 dB, full scale).

* **What each one sent** shows the file, Roon and Mandarin. If they are
  identical, the pictures are identical.
* **Differences** shows what is left when one recording is subtracted from the
  other, sample by sample: Roon − file, Mandarin − file and Mandarin − Roon.
  **Dark graphite means the samples are identical.**
  * **Match levels first** removes a plain volume difference before
    subtracting. It shows what else changed, such as dither or EQ.
* **Red lines** mark dropouts: gaps or jumps in a player's stream. The pictures
  and the difference files skip over a dropout to keep everything lined up with
  the song, so a dropout shows as a red line rather than as a difference.
* **Drag** across the pictures to zoom into that part of the song, down to a
  few hundredths of a second. Use the buttons to zoom out, move, or go back to
  the whole song.
* **Tap** (or hover with a mouse) to read the time and frequency.
* **Log / Linear** sets the frequency scale, and the menu sets the lowest level
  shown. Lower it (−180 or −210 dB) to see dither at 24 bits.
* Each column of pixels covers its whole stretch of time: SQ-tool analyses
  every part of it and shows the loudest value, so a short click can't fall
  between pixels.
  * Some differences are far below the colour scale, such as one sample off
    by its last bit. The verdict and the timeline under it still catch them.

## Reading the results

| Verdict | Meaning |
|---|---|
| **BIT-PERFECT** | Every sample the player sent equals the sample in the file. Silence before and after the song is allowed, and so is a bigger container: 16-bit samples sent as 32-bit words keep their values. |
| **BIT-PERFECT** with part of the song missing | Every recorded sample matches, but part of the song isn't in the recording, for example because playback was stopped early. |
| **DROPOUTS** | The samples are unchanged, but the stream has gaps or jumps: silence inserted (an underrun), samples lost, or audio repeated. |
| **ALTERED** | Most of the stream matches the file; some parts differ. |
| **NOT BIT-PERFECT** | The samples differ throughout. SQ-tool says how: a level change, dither, rounding, EQ, channel changes. |
| **RESAMPLED** | The player sent a different sample rate from the file's. |
| **SAME DATA / DIFFERENT DATA** | Roon against Mandarin: whether your DAC would convert exactly the same numbers from both. |

For a player that is not bit-perfect, the numbers mean:

* **level**: the volume difference, for example −0.50 dB.
* **what remains after matching the level**: how far below the music the rest
  of the difference lies, and its level in dBFS rms. It is typically one of
  these:
  * dither: about −144 dBFS at 24 bits, or −96 dBFS at 16 bits;
  * plain rounding: about −149 dBFS at 24 bits, or −101 dBFS at 16 bits;
  * or, if higher, a sign of heavier processing.

## What it can and cannot tell you

* The Loopback card receives exactly the samples a player sends to an ALSA
  output. If the Loopback zone has the same settings as your DAC's zone, those
  are the samples your DAC gets.
* A player may package the same samples differently for different devices:
  16-bit samples in 32-bit words, say, or packed 24-bit. SQ-tool compares the
  sample values, so the same values in another container count as identical.
  The *Details* table shows each format.
* It cannot measure what happens electrically: jitter, noise on the USB cable
  or ground, or the DAC's own behaviour.

If both players send identical data, your DAC converts the same numbers from
both. Any audible difference then comes from somewhere else: electrical or
system-load effects, or the listening test itself. Level-matched blind
listening is the way to check that. Even a few tenths of a dB of level
difference can make one player sound "better" in a sighted comparison.

**Recording the USB DAC itself.** With a USB DAC, SQ-tool can also record the
USB packets on their way to the DAC (Linux's `usbmon`), instead of the Loopback
card. Choose your DAC in *Settings → Record from*, and load usbmon on the
server first:

```sh
sudo modprobe usbmon
```

The players then play to the DAC as usual.

## Getting a fair comparison

* Play the same file in both players, from the beginning.
* Record the same player twice. The two recordings should be identical, which
  shows the measurement itself is consistent.
* Roon may still be playing its next track when you record Mandarin. SQ-tool
  leaves that stream alone, playing or paused, and records Mandarin.
* Recording starts a moment after the player starts. Most songs begin with
  digital silence, so nothing is lost. If a song starts with sound right away,
  its first few milliseconds may be missing. The verdict then says so, and
  everything recorded is still compared.

## Troubleshooting

* **"The Loopback sound card is not loaded"**: press *Load the Loopback driver
  now*. If there is no button, add `-v /lib/modules:/lib/modules:ro` and
  `--privileged` to the run command, or run `sudo modprobe snd-aloop` on the
  server.
* **"Module snd-aloop not found"**: on Ubuntu the driver is in a separate
  package. Install it on the server with
  `sudo apt install linux-modules-extra-$(uname -r)`.
* **Roon doesn't list Loopback**: restart Roon Server after the Loopback card
  appears.
* **The recording never starts**: the player isn't playing to the Loopback
  card. The start page shows what is playing on it right now. Check that the
  player's output is Loopback, device 0 (`hw:N,0`).
* **"Your music folder is not connected"**: add `-v /path/to/your/music:/music:ro`
  to the run command, or upload the song from your phone or tablet instead.
* **"The sound cards are hidden from this container"**: add `--privileged`.
* **The sending program isn't shown**: add `--pid=host`.

## Security

The web page has no login, so keep port 3400 on your home network. SQ-tool only
reads your music folder.

## Command line

The same code runs without Docker:

```sh
sudo apt install python3-numpy alsa-utils flac   # Debian/Ubuntu; Python 3.8+
sudo modprobe snd-aloop
./sq-tool serve --music ~/Music                  # the web interface, as in Docker
./sq-tool status                                 # sound cards and what they are being sent
./sq-tool capture roon.wav                       # record a player on the loopback card
./sq-tool compare song.flac roon.wav mandarin.wav
./sq-tool analyze roon.wav
```

## Tests

```sh
pip install numpy
python3 -m unittest discover -s tests
```

The tests need no audio hardware. They simulate:

* the Loopback card (`/proc/asound` and `arecord`);
* players that play a song to it;
* a USB DAC's packets, as usbmon delivers them.

They cover recording, comparisons against known changes (gain, dither,
truncation, EQ, crossfeed, channel swaps, dropouts, repeats, resampling), the
spectrograms, and the whole web API.
