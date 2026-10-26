# Production image: chat app and admin portal on port 8000.
# Each start creates or updates the tables first (--init is safe to re-run).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY sql ./sql
COPY samples ./samples

RUN useradd --system --uid 1001 --no-create-home appuser
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "python -m app.ingest --init && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
