"""Private typed-configuration runtime (public surface: :mod:`lauren.config`).

* ``_secret`` — :class:`Secret` and the redaction helpers (no lauren imports,
  so the JSON encoder and the session signer can use it).
* ``_coerce`` — the lax / strict string-to-type coercion table.
* ``_sources`` — environment, ``.env``, TOML, YAML, secrets-directory and
  async sources, plus the stdlib ``.env`` parser.
* ``_loader`` — settings-class walk, layered resolution, aggregated errors,
  :func:`load_settings`.
* ``_module`` — :class:`ConfigModule` and Phase 2b of ``LaurenFactory.create``.

This package ``__init__`` deliberately imports nothing, so importing
``lauren._config._secret`` never drags in the module or DI machinery.
"""
