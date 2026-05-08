---
name: docker-compose-setup
description: Shows a production-ready multi-stage Dockerfile and docker-compose stack for a Lauren application with PostgreSQL and Redis. Use when containerising a Lauren service for local development, CI, or production deployment.
---

> Use `codemap find "LaurenFactory"` to verify the application entrypoint before adjusting the Dockerfile CMD.

# Docker Multi-Stage Build & docker-compose Stack

## Dockerfile (multi-stage)

```dockerfile
# syntax=docker/dockerfile:1
# Stage 1: dependency builder — installs packages into an isolated venv
FROM python:3.11-slim AS builder
WORKDIR /app

# Install uv for fast dependency resolution
COPY requirements.txt ./
RUN pip install --no-cache-dir uv \
    && uv venv .venv \
    && uv pip install --no-cache -r requirements.txt

# Stage 2: lean runtime image
FROM python:3.11-slim AS runtime
WORKDIR /app

# Copy the pre-built venv from the builder stage
COPY --from=builder /app/.venv ./.venv

# Copy application source
COPY ./app ./app
COPY ./main.py .

# Activate the venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Use uvicorn with the ASGI lifespan protocol enabled so @pre_destruct fires on SIGTERM
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--lifespan", "on"]
```

## main.py

```python
from lauren import LaurenFactory
from app.module import AppModule

app = LaurenFactory.create(AppModule)
```

## docker-compose.yml

```yaml
version: "3.9"

services:
  api:
    build:
      context: .
      target: runtime
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql://app:secret@db:5432/appdb
      REDIS_URL: redis://redis:6379/0
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: secret
      POSTGRES_DB: appdb
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d appdb"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

## Development overrides (docker-compose.override.yml)

```yaml
version: "3.9"

services:
  api:
    build:
      target: builder   # use the builder stage to include dev tools
    volumes:
      - ./app:/app/app   # live-reload without rebuilding
      - ./main.py:/app/main.py
    command: >
      uvicorn main:app
        --host 0.0.0.0
        --port 8000
        --reload
        --lifespan on
    environment:
      DEBUG: "true"
```

## Environment variable injection via pydantic-settings

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "sqlite:///./dev.db"
    redis_url: str = "redis://localhost:6379/0"
    debug: bool = False

    class Config:
        env_file = ".env"

settings = Settings()
```

## Health check endpoint (required by depends_on)

Add a `/health/live` endpoint (see the `health-check-probes` skill) so Docker can verify the API is up before routing traffic:

```yaml
api:
  healthcheck:
    test: ["CMD-SHELL", "curl -f http://localhost:8000/health/live || exit 1"]
    interval: 10s
    timeout: 5s
    retries: 3
    start_period: 15s
```

## Tips

- Always use `--lifespan on` with uvicorn so ASGI lifespan events fire and `@post_construct` / `@pre_destruct` hooks run on container start/stop.
- Pin image tags (`python:3.11-slim`, `postgres:16-alpine`) in production to avoid surprise upgrades.
- Store secrets in Docker secrets or a secrets manager — never in `docker-compose.yml`.
- The multi-stage build keeps the runtime image small (~120 MB vs ~600 MB for a full builder image) by leaving build tools and the pip cache in the `builder` stage.
