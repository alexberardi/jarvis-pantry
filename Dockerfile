FROM python:3.11-slim

WORKDIR /app

# Install git + Docker CLI (for sibling-container test runner)
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && \
    curl -fsSL https://download.docker.com/linux/static/stable/$(uname -m)/docker-27.5.1.tgz | \
    tar xz --strip-components=1 -C /usr/local/bin docker/docker && \
    rm -rf /var/lib/apt/lists/*

# Install SDK (for forge spec generation)
COPY jarvis-command-sdk/ /tmp/jarvis-command-sdk/
RUN pip install --no-cache-dir /tmp/jarvis-command-sdk/ && rm -rf /tmp/jarvis-command-sdk/

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini .

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PANTRY_PORT=7721

CMD ["bash", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PANTRY_PORT:-7721}"]
