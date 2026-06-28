"""Hero HQ entry point — assembles the app via the factory (tutorial steps 4-7)."""

from __future__ import annotations

from lauren import LaurenFactory, SessionConfig

from .teams import HeroHQModule

# In production, load the secret from the environment — never hardcode it.
SESSION_SECRET = "change-me-hero-hq-secret-please-32-bytes"


def build_app():
    """Compile the Hero HQ app. Every misconfiguration fails here, not in prod."""
    return LaurenFactory.create(
        HeroHQModule,
        sessions=SessionConfig(
            secret=SESSION_SECRET,
            secure=False,  # local dev is HTTP; flip to True (the default) in production
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )


# `app` is a ready-to-serve ASGI callable: `uvicorn ...hero_hq.main:app`.
app = build_app()
