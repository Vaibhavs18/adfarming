FROM python:3.11-slim

# ffmpeg for cuts; fontconfig + fribidi/harfbuzz so Pillow can shape Devanagari (raqm) in captions
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig libfribidi0 libharfbuzz0b libraqm0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code, schemas and bundled fonts only. No .env, no private/ (see .dockerignore).
COPY src ./src
COPY scripts ./scripts
COPY schemas ./schemas
COPY assets ./assets

# Mounted at /data: footage/, asr/, audio/ (client footage, brief.md, subject.txt) and out/ (results).
# API keys come from the environment (META_MODEL_API_KEY, BASETEN_API_KEY, BASETEN_MODEL).
RUN useradd --create-home --uid 1000 adf \
    && mkdir -p /data/out \
    && chown -R adf:adf /app /data
USER adf

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src ADF_PRIVATE=/data ADF_OUT=/data/out
VOLUME ["/data"]
ENTRYPOINT ["python", "scripts/run_pipeline.py"]
