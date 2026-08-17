FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG AWS_SIGNING_HELPER_VERSION=1.8.4
ARG AWS_SIGNING_HELPER_SHA256=b7568acd6e1517a4e1adaee68d52bfd6284a0e5305677166cd83d43a07c815c9

WORKDIR /app

# Install system dependencies. curl and openssl are what the enrollment
# bootstrap speaks to the trust appliance with.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gcc \
    libc6-dev \
    openssl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
    "https://rolesanywhere.amazonaws.com/releases/${AWS_SIGNING_HELPER_VERSION}/X86_64/Linux/Amzn2023/aws_signing_helper" \
    -o /usr/local/bin/aws_signing_helper \
    && echo "${AWS_SIGNING_HELPER_SHA256}  /usr/local/bin/aws_signing_helper" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/aws_signing_helper

# Copy requirements first for better Docker layer caching
COPY requirements.txt ./

# Install Python dependencies with verbose output
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

# The collector reads its own secrets at start with the identity the trust
# appliance issues it, so the AWS CLI is a runtime dependency rather than a
# build one (ahara-trust ADR-0002).
RUN python -m pip install awscli

# Copy application code
COPY app_telemetry.py ./
COPY env_sensor_collector.py ./
COPY truenas-roles-anywhere-bootstrap /usr/local/bin/truenas-roles-anywhere-bootstrap
RUN chmod 0755 /usr/local/bin/truenas-roles-anywhere-bootstrap

# Add healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; import env_sensor_collector; print('All imports OK')" || exit 1

# Run the collector
CMD ["python", "-u", "env_sensor_collector.py"]
