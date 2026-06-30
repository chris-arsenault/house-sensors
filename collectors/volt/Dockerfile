FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt ./

# Install Python dependencies with verbose output
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

# Copy application code
COPY voltage_collector.py ./
COPY config.yaml ./

# Add healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import kasa, influxdb_client, prometheus_client; print('All imports OK')" || exit 1

# Run the collector
CMD ["python", "-u", "voltage_collector.py"]
