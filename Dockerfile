# ---- Base image ----
FROM python:3.11-slim

# Prevents .pyc files and enables unbuffered logging (useful for docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ---- Install system dependencies (curl for healthchecks, gcc if packages need C extensions) ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# ---- Install Python dependencies first (leverages Docker layer caching) ----
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- Copy application code & data models ----
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY data_and_model/ ./data_and_model/
COPY data_and_model_meghalaya/ ./data_and_model_meghalaya/
COPY data_and_model_rajasthan/ ./data_and_model_rajasthan/
COPY micro_services/ ./micro_services/
COPY scripts/ ./scripts/

# Gateway entry port
EXPOSE 8085

# Container healthcheck via the gateway disease health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:8085/api/disease/health?state=tripura || exit 1

# ---- Run the unified launcher in headless container mode ----
CMD ["python", "backend/main.py", "--no-browser"]
