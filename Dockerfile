# AdFarming Studio: web app (default) and pipeline CLI (override the entrypoint).
#   web:  docker run -p 8000:8000 --env-file .env adfarming        (uvicorn adfarming.web:app)
#   CLI:  docker run --rm --entrypoint python -v ...:/data adfarming scripts/run_pipeline.py ...
# Build for the Vultr VM (amd64) from an arm64 Mac:  docker buildx build --platform linux/amd64 -t adfarming:test .
FROM python:3.11-slim

# ffmpeg for cuts; fontconfig + fribidi/harfbuzz so Pillow can shape Devanagari (raqm) in captions.
# Captions use the Noto fonts bundled under assets/fonts (OFL), so no apt font packages are needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig libfribidi0 libharfbuzz0b libraqm0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code, schemas, web UI and bundled fonts only. No .env, no private/, no out/ (see .dockerignore).
COPY src ./src
COPY scripts ./scripts
COPY schemas ./schemas
COPY assets ./assets
COPY web ./web

# Volumes (mounted at runtime, never baked in):
#   /data/private  client footage, asr/, brief.md, subject.txt   (ADF_PRIVATE)
#   /data/out      run outputs and artifacts                     (ADF_OUT and ADFARMING_OUT)
#   /data/db       SQLite studio.db                              (ADFARMING_DB)
# Secrets (META_MODEL_API_KEY, BASETEN_API_KEY, STUDIO_PASSWORD, ...) come from the environment only.
RUN useradd --create-home --uid 1000 adf \
    && mkdir -p /data/private /data/out /data/db \
    && chown -R adf:adf /app /data
USER adf

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    ADF_PRIVATE=/data/private \
    ADF_OUT=/data/out \
    ADFARMING_OUT=/data/out \
    ADFARMING_DB=/data/db/studio.db

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# --forwarded-allow-ips '*': the app port is never published, only Caddy can reach it, so trust its
# X-Forwarded-* headers (real client IP for the login rate limit, https scheme for the secure cookie).
CMD ["uvicorn", "adfarming.web:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
