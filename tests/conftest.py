import importlib.util
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "pydantic: tests that require pydantic")
    config.addinivalue_line("markers", "msgspec: tests that require msgspec")
    config.addinivalue_line("markers", "dataclass: tests using stdlib dataclasses")
    config.addinivalue_line("markers", "typeddict: tests using stdlib TypedDict")
    config.addinivalue_line("markers", "slow: tests that take >5 seconds")


def pytest_collection_modifyitems(items):
    """Auto-skip pydantic/msgspec tests if the library is not installed."""
    for item in items:
        if item.get_closest_marker("pydantic"):
            if importlib.util.find_spec("pydantic") is None:
                item.add_marker(pytest.mark.skip(reason="pydantic not installed"))
        if item.get_closest_marker("msgspec"):
            if importlib.util.find_spec("msgspec") is None:
                item.add_marker(pytest.mark.skip(reason="msgspec not installed"))


@pytest.fixture(scope="session", autouse=True)
def _preload_lauren():
    """Pre-import Lauren modules before any test blocks pydantic/msgspec.

    Tests that monkeypatch sys.modules to block optional dependencies must
    not be the *first* to import a Lauren module, otherwise the module-level
    _PYDANTIC_AVAILABLE / _MSGSPEC_AVAILABLE flags are set to False for the
    entire session even after the block is lifted.  This session-scoped
    fixture ensures Lauren's core modules are imported once (with all
    optional deps available) before any blocking fixture runs.
    """
    import lauren  # noqa: F401
    import lauren.streaming  # noqa: F401
    import lauren.extractors  # noqa: F401
