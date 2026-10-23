FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# Xvfb: headed Chrome. Tailscale: home exit.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth tini curl ca-certificates \
 && curl -fsSL https://tailscale.com/install.sh | sh \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies layer, reused across code changes.
COPY pyproject.toml ./
RUN mkdir -p src/grocerybot && touch src/grocerybot/__init__.py && pip install --no-cache-dir ".[runtime]"

ARG WHISPER_MODEL=small
ENV HF_HOME=/opt/hf PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
 && chmod -R a+rX /opt/hf
ENV HF_HUB_OFFLINE=1

# Non-root: Tailscale runs in userspace.
RUN mkdir -p /data && chown pwuser:pwuser /data

# Code last, as --link layers: a code change rebuilds without refetching dependencies or the model.
# Runs from source, ahead of the empty package installed above.
COPY --link src ./src
COPY --link --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint
ENV PYTHONPATH=/app/src
ARG REVISION=dev
LABEL org.opencontainers.image.source=https://github.com/vaari-dev/grocery-bot org.opencontainers.image.revision=$REVISION

USER pwuser
VOLUME /data
HEALTHCHECK --interval=1m --timeout=5s --start-period=2m \
  CMD find /tmp/heartbeat -mmin -3 | grep -q . || exit 1
ENTRYPOINT ["tini", "--", "entrypoint"]
CMD ["python", "-m", "grocerybot"]
