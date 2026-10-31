FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/media-agent

# Copy application source and configuration
COPY pyproject.toml ./
COPY app/ ./app/
COPY config/ ./config/

# Install application dependencies
RUN pip install --no-cache-dir .

# Persistent journal directory
RUN mkdir -p /var/lib/media-agent

CMD ["python", "-m", "app.main"]
