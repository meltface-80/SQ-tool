# SQ-tool: capture and compare exactly what audio players send to a DAC.
# Web interface on port 3400. See README.md for the docker run command.
FROM python:3.12-slim

# alsa-utils: arecord, for the ALSA loopback method. flac: FLAC files as references.
RUN apt-get update \
 && apt-get install -y --no-install-recommends alsa-utils flac \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "numpy>=1.17"

WORKDIR /app
COPY sqtool ./sqtool
COPY sq-tool pyproject.toml README.md LICENSE ./

ENV SQTOOL_DATA=/data PORT=3400 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 3400
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT', '3400'), timeout=4)"
CMD ["python", "-m", "sqtool", "serve", "--data", "/data"]
