"""Shared fixtures: run the real integration against a fake RepRapFirmware."""
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


def _expose_repo_components() -> None:
    """Make ``custom_components.duet3d`` importable from this repository.

    The plugin ships its own ``custom_components`` package, which wins the import;
    extend its search path so the integration under test is found as well.
    """
    import custom_components

    repo_components = str(Path(__file__).parent.parent / "custom_components")
    if repo_components not in custom_components.__path__:
        custom_components.__path__.append(repo_components)


_expose_repo_components()


def pytest_configure(config):
    """Windows only: asyncio's event loop needs a loopback socketpair.

    pytest-homeassistant-custom-component blocks all sockets, which is fine on
    Linux (unix socketpair) but breaks loop creation on Windows. Only patch when
    the platform needs it, so CI on Linux keeps the guard.
    """
    import sys

    if sys.platform != "win32":
        return
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load custom_components/duet3d."""
    yield


@pytest.fixture(autouse=True)
def allow_local_sockets(socket_enabled):
    """The fake Duet listens on localhost; the plugin blocks sockets by default."""
    yield


@pytest.fixture
async def fake_duet():
    """A standalone-mode Duet whose model tests can mutate."""
    from fake_duet import start_server

    server = await start_server()
    server.expect_refusals = False
    yield server
    await server.close()
    # The firmware would have ignored these; no code the integration sends may be one.
    if not server.expect_refusals:
        assert server.refused == []


@pytest.fixture
async def fake_dsf():
    """A fake DSF (SBC mode) whose model tests can mutate."""
    from fake_duet import start_sbc_server

    server = await start_sbc_server()
    yield server
    await server.close()
