"""Re-export WsConnectionContext and WsUpgradeRequest from their canonical
location in :mod:`lauren._ws_runtime`.

These types live in ``_ws_runtime`` because that is the module where
WebSocket connection handling is implemented.  They are re-exported from
``lauren.reflect`` so that guard/interceptor authors can import them from
the stable ``lauren.reflect`` public surface without depending on an
underscore-prefixed private module.
"""

from __future__ import annotations

# Re-export from canonical location.
from .._ws_runtime import WsConnectionContext, WsUpgradeRequest

__all__ = [
    "WsConnectionContext",
    "WsUpgradeRequest",
]
