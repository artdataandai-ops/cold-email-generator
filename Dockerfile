# Use the official Playwright Python image — has Chromium + system deps preinstalled.
# Pin to a specific version so builds are reproducible.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY templates/ ./templates/
COPY app.py ./

# Persistent paths (mount as volumes in compose)
RUN mkdir -p /app/kb /app/logs /app/.cache

EXPOSE 8000

# Healthcheck hits the existing /api/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health').read()" || exit 1

# 2 workers handles ~10 teammates with headroom; SQLite WAL keeps writes safe.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
