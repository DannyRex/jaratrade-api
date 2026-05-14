# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system packages needed for psycopg + cryptography wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt psycopg[binary]

# Then app code
COPY . .

EXPOSE 8000

# In production: run migrations, then start uvicorn with workers
CMD sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_WORKERS:-2}"
