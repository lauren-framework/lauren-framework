from __future__ import annotations

from workers import asgi

from app import app

# Cloudflare's Python Workers runtime adapts ASGI requests to the Worker
# fetch handler. Lauren remains a regular ASGI application.
Default = asgi.entrypoint(app)
