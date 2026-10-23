"""The opt-in ``lauren.testing.pytest_plugin`` fixtures, exercised with ``pytester``."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

APP_SOURCE = """
from lauren import LaurenFactory, controller, get, injectable, module, RequestComplete


@injectable()
class Greeter:
    def greet(self) -> str:
        return "real"


@controller("/")
class Hello:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter

    @get("/hello")
    async def hello(self) -> dict:
        return {"greeting": self.greeter.greet()}


@module(controllers=[Hello], providers=[Greeter])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)
"""


INI_HEAD = "[pytest]\nasyncio_default_fixture_loop_scope = function\n"


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(myapp=APP_SOURCE)
    return pytester


def test_lauren_app_ini_option_and_client(project: pytest.Pytester) -> None:
    project.makeini(INI_HEAD + "lauren_app = myapp:AppModule\nasyncio_mode = auto\n")
    project.makeconftest('pytest_plugins = ["lauren.testing.pytest_plugin"]')
    project.makepyfile(
        test_it="""
        def test_hello(lauren_client):
            assert lauren_client.get("/hello").json() == {"greeting": "real"}

        def test_capture(lauren_client, lauren_capture):
            from lauren import RequestComplete
            with lauren_capture(RequestComplete) as events:
                lauren_client.get("/hello")
            assert len(events) == 1

        def test_ws_fixture_is_the_connect_method(lauren_ws, lauren_client):
            assert lauren_ws == lauren_client.websocket_connect

        async def test_async_client(lauren_aclient):
            assert (await lauren_aclient.get("/hello")).status_code == 200
        """
    )
    result = project.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=4)


def test_built_app_target_needs_session_scope(project: pytest.Pytester) -> None:
    project.makeini(INI_HEAD + "lauren_app = myapp:app\nlauren_client_scope = session\n")
    project.makeconftest('pytest_plugins = ["lauren.testing.pytest_plugin"]')
    project.makepyfile(
        test_it="""
        def test_one(lauren_client):
            assert lauren_client.get("/hello").status_code == 200

        def test_two(lauren_client):
            assert lauren_client.get("/hello").status_code == 200
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=2)


def test_built_app_target_with_function_scope_is_refused(project: pytest.Pytester) -> None:
    project.makeini(INI_HEAD + "lauren_app = myapp:app\n")
    project.makeconftest('pytest_plugins = ["lauren.testing.pytest_plugin"]')
    project.makepyfile(test_it="def test_hello(lauren_client):\n    pass\n")
    result = project.runpytest("-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*can be started only once*"])


def test_testing_module_fixture_wins(project: pytest.Pytester) -> None:
    project.makeconftest(
        """
        import pytest
        from lauren.testing import Test
        from myapp import AppModule, Greeter

        pytest_plugins = ["lauren.testing.pytest_plugin"]


        class Fake:
            def greet(self):
                return "fake"


        @pytest.fixture(scope="module")
        def lauren_testing_module():
            return Test.create_testing_module(AppModule).override_provider(Greeter).use_value(Fake())
        """
    )
    project.makeini(INI_HEAD + "lauren_client_scope = module\n")
    project.makepyfile(
        test_it="""
        def test_first(lauren_client):
            assert lauren_client.get("/hello").json() == {"greeting": "fake"}

        def test_same_client(lauren_client, lauren_app):
            assert lauren_client.app is lauren_app
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=2)


def test_builder_ini_target_is_compiled(project: pytest.Pytester) -> None:
    project.makepyfile(
        testing_target="""
        from lauren.testing import Test
        from myapp import AppModule

        builder = Test.create_testing_module(AppModule)
        """
    )
    project.makeini(INI_HEAD + "lauren_app = testing_target:builder\n")
    project.makeconftest('pytest_plugins = ["lauren.testing.pytest_plugin"]')
    project.makepyfile(
        test_it="def test_hello(lauren_client):\n    assert lauren_client.get('/hello').status_code == 200\n"
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("ini", "message"),
    [
        ("", "define a `lauren_testing_module` fixture"),
        ("lauren_app = not-a-target\n", "package.module:attribute"),
        ("lauren_client_scope = galaxy\n", "lauren_client_scope must be one of"),
    ],
)
def test_misconfiguration_is_reported(project: pytest.Pytester, ini: str, message: str) -> None:
    project.makeini(INI_HEAD + ini)
    project.makeconftest('pytest_plugins = ["lauren.testing.pytest_plugin"]')
    project.makepyfile(test_it="def test_hello(lauren_client):\n    pass\n")
    result = project.runpytest("-p", "no:cacheprovider")
    result.stdout.fnmatch_lines([f"*{message}*"])
    assert result.ret != 0


def test_plugin_is_not_auto_registered(project: pytest.Pytester) -> None:
    project.makeini(INI_HEAD)
    project.makepyfile(test_it="def test_hello(lauren_client):\n    pass\n")
    result = project.runpytest("-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*fixture 'lauren_client' not found*"])


def test_load_app_helper_builds_module_targets(project: pytest.Pytester) -> None:
    import sys

    from lauren import LaurenApp
    from lauren.testing.pytest_plugin import _needs_pytest_asyncio, load_app

    sys.path.insert(0, str(project.path))
    try:
        assert isinstance(load_app("myapp:AppModule"), LaurenApp)
    finally:
        sys.path.remove(str(project.path))
        sys.modules.pop("myapp", None)
    with pytest.raises(pytest.UsageError, match="pytest-asyncio"):
        _needs_pytest_asyncio()
