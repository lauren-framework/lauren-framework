"""``python -m lauren``: run the application's commands without serving HTTP.

::

    python -m lauren exec    [TARGET] COMMAND [ARGS...] [--yes] [--verbose]
    python -m lauren commands [TARGET]
    python -m lauren console  [TARGET]

``TARGET`` is ``pkg.module:attr`` (a ``@module`` class, a ``LaurenApp``, a
``Lauren`` app, an ``ApplicationContext`` or a zero-argument factory returning
one) and defaults to ``$LAUREN_TARGET``. ``python -m lauren --help`` lists the
options and exit codes.
"""

from __future__ import annotations

import sys

from ._commands._runner import main

if __name__ == "__main__":
    sys.exit(main())
