"""Unit tests for the signal-handler helper behind ``wait_for_shutdown_signal`` and the runner."""

from __future__ import annotations

import signal
import threading
from typing import Any

from lauren.context import _install_signal_handlers


class _LoopWithoutSignals:
    """An event loop like Windows' proactor: no ``add_signal_handler``."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[Any, ...]] = []

    def add_signal_handler(self, *args: Any) -> None:
        raise NotImplementedError

    def call_soon_threadsafe(self, *args: Any) -> None:
        self.scheduled.append(args)


def test_falls_back_to_the_signal_module_and_restores_the_previous_handler() -> None:
    loop = _LoopWithoutSignals()
    previous = signal.getsignal(signal.SIGINT)
    received: list[int] = []

    restore, installed = _install_signal_handlers(loop, [signal.SIGINT], received.append)  # type: ignore[arg-type]
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)
    restore()

    assert installed is True
    assert loop.scheduled == [(received.append, signal.SIGINT)]
    assert signal.getsignal(signal.SIGINT) == previous


def test_nothing_is_installed_outside_the_main_thread() -> None:
    results: list[bool] = []

    def worker() -> None:
        restore, installed = _install_signal_handlers(_LoopWithoutSignals(), [signal.SIGINT], print)  # type: ignore[arg-type]
        restore()
        results.append(installed)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert results == [False]
