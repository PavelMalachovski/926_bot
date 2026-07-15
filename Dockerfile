# SMC Watcher — single lightweight worker (no web server, no DB)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/app/
COPY smc_watcher.py /app/

# Run as root: Railway volumes are mounted root-owned, and a non-root user
# cannot write the SQLite database to them.

CMD ["python", "smc_watcher.py"]
