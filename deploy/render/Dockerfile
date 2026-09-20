FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/home/app/.cache/fastembed \
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072 \
    OMP_NUM_THREADS=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/render/requirements.txt ./deploy/render/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r deploy/render/requirements.txt

COPY pyproject.toml README.md ./
COPY api ./api
COPY pipeline ./pipeline
RUN pip install --no-deps .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p "$FASTEMBED_CACHE_PATH" \
    && chown -R app:app /app /home/app

USER app

# Bake the two compact ONNX models into the image so a cold start never has to
# download weights into Render's ephemeral filesystem.
RUN python -c "from fastembed import TextEmbedding; list(TextEmbedding('sentence-transformers/all-MiniLM-L6-v2').embed(['warmup']))" \
    && python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; list(TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2').rerank('warmup', ['warmup']))"

EXPOSE 8000

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
