# syntax=docker/dockerfile:1

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install OS-level tools needed by the app and dependency build steps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies before copying the rest of the project to keep
# Docker layer caching efficient for repeated rebuilds.
COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -r requirements.txt

# Copy the current project structure: gateway + orchestration, frontend, data,
# microservices, and scripts used by the launcher.
COPY backend ./backend
COPY frontend ./frontend
COPY data_and_model ./data_and_model
COPY data_and_model_meghalaya ./data_and_model_meghalaya
COPY data_and_model_rajasthan ./data_and_model_rajasthan
COPY micro_services ./micro_services
COPY scripts ./scripts
COPY test ./test

# Public gateway port plus internal service ports used by the launcher.
EXPOSE 8085 6000 6001 6002 6003 6004 6005 6006 6007 6008 6009 6010 6011 6012 6013 6100 6200

# Check that the gateway is serving requests before the container is considered healthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
  CMD curl -fsS http://localhost:8085/health || curl -fsS http://localhost:8085/api/disease/health?state=tripura || exit 1

# Launch the unified stack in headless mode.
CMD ["python", "backend/main.py", "--no-browser"]
