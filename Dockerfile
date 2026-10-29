FROM python:3.13-slim-trixie

# uid 1001 owns the existing /data volume
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1001 --create-home bot

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
COPY pyproject.toml uv.lock ./
ENV UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never PATH=/app/.venv/bin:$PATH
RUN uv sync --locked --no-install-project --no-dev --extra runtime

ARG WHISPER_MODEL=small
ENV HF_HOME=/opt/hf PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
 && chmod -R a+rX /opt/hf
ENV HF_HUB_OFFLINE=1
RUN mkdir -p /data && chown bot:bot /data

# Code last: a code change rebuilds without refetching dependencies or the model.
COPY --link src ./src
ENV PYTHONPATH=/app/src
ARG REVISION=dev
LABEL org.opencontainers.image.source=https://github.com/vaari-dev/pantry org.opencontainers.image.revision=$REVISION

USER bot
VOLUME /data
HEALTHCHECK --interval=1m --timeout=5s --start-period=2m \
  CMD find /tmp/heartbeat -mmin -3 | grep -q . || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "pantry"]
