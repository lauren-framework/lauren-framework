"""DI-aware commands (private package; the public surface is ``lauren.commands``).

* ``_context`` — ``CommandContext`` and ``CommandResult``.
* ``_meta`` — ``@command``, ``Opt`` and the discovery marker registration.
* ``_compile`` — Phase 6a compilation (argument specs, ``argparse`` parsers),
  ``CommandRegistry`` and command execution in an ``OperationScope``.
* ``_runner`` — ``python -m lauren``: target resolution, ``exec``,
  ``commands`` and ``console``.

This package ``__init__`` deliberately imports nothing.
"""

from __future__ import annotations
