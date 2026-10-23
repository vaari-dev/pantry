# Telegram bot + real Chrome (Playwright) + Whisper, arm64 for the OCI box.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# Xvfb: the shop refuses headless Chrome, so Chrome runs normally on a virtual screen.
# Tailscale: Chrome's traffic leaves through the home connection (see docker/entrypoint.sh).
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth tini curl ca-certificates \
 && curl -fsSL https://tailscale.com/install.sh | sh \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first (from pyproject.toml alone, with an empty package) so code changes reuse this layer.
COPY pyproject.toml ./
RUN mkdir -p src/grocerybot && touch src/grocerybot/__init__.py && pip install --no-cache-dir ".[runtime]"

# The speech model is baked in so the first voice note is not slow and nothing is fetched at run time.
ARG WHISPER_MODEL=small
ENV HF_HOME=/opt/hf PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
 && chmod -R a+rX /opt/hf

COPY src ./src
RUN pip install --no-cache-dir --no-deps .

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint
ARG REVISION=dev
LABEL org.opencontainers.image.source=https://github.com/vaari-dev/grocery-bot org.opencontainers.image.revision=$REVISION

# Unprivileged: Tailscale runs in userspace mode, so no root or /dev/net/tun is needed.
RUN mkdir -p /data && chown pwuser:pwuser /data
USER pwuser
VOLUME /data
HEALTHCHECK --interval=1m --timeout=5s --start-period=2m \
  CMD find /tmp/heartbeat -mmin -3 | grep -q . || exit 1
ENTRYPOINT ["tini", "--", "entrypoint"]
CMD ["python", "-m", "grocerybot"]
