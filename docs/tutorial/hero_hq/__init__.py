"""Hero HQ — the companion app built by the docs Tutorial.

This is the *finished* application the tutorial assembles step by step (it
reflects the latest built state — guards, sessions, and Mission Control
included). It is a real, importable package and is exercised by
``tests/integration/test_tutorial_hero_hq.py`` so the tutorial's code can
never silently rot.

Run it::

    uvicorn docs.tutorial.hero_hq.main:app --reload
"""

from __future__ import annotations

from .main import app, build_app

__all__ = ["app", "build_app"]
