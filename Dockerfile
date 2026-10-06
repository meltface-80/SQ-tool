# SQ-tool: is your music player bit-perfect? Records what players send to the ALSA
# loopback card and compares it with the original file and with each other.
# Web interface on port 3400. See README.md for the docker run command.
FROM python:3.12-slim

# alsa-utils: arecord, to record the loopback card. kmod: modprobe, to load the
# loopback driver (snd-aloop) when it isn't loaded yet. flac: to read FLAC files.
RUN apt-get update \
 && apt-get install -y --no-install-recommends alsa-utils flac kmod \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "numpy>=1.17"

WORKDIR /app
COPY sqtool ./sqtool
COPY sq-tool pyproject.toml README.md LICENSE ./

# /data: tests, recordings and analyses. /music: your music folder (mount it read-only).
ENV SQTOOL_DATA=/data SQTOOL_MUSIC=/music SQTOOL_LOAD_LOOPBACK=1 PORT=3400 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 3400
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT', '3400'), timeout=4)"
CMD ["python", "-m", "sqtool", "serve"]
