# ---- Base image ----
FROM python:3.11-slim

# Prevents .pyc files and enables unbuffered logging (useful for docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ---- Install system deps (uncomment if any package needs build tools) ----
# RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# ---- Install Python deps first (better layer caching) ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Copy application code ----
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY data_and_model/ ./data_and_model/
COPY data_and_model_meghalaya/ ./data_and_model_meghalaya/
COPY data_and_model_rajasthan/ ./data_and_model_rajasthan/

# NOTE: .env and api_keys.txt are NOT copied here on purpose — pass secrets
# at runtime via `docker run --env-file` or a Docker secret, don't bake them in.

# Flask default port (change if your main.py binds elsewhere)
EXPOSE 5000

# ---- Run the app ----
CMD ["python", "backend/main.py"]
