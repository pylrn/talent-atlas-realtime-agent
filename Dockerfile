FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/home/app/.cache/huggingface \
    PORT=8000

RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.7,<3"

COPY pyproject.toml README.md ./
COPY api ./api
COPY pipeline ./pipeline

RUN pip install .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p "$HF_HOME" \
    && chown -R app:app /app /home/app

USER app

# Cache the exact retrieval and reranking models used by the deployed demo.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); CrossEncoder('cross-encoder/ms-marco-MiniLM-L6-v2')"

EXPOSE 8000

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
