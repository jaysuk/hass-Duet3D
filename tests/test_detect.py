"""Standalone and SBC mode are told apart from what the board answers."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.duet3d.detect import (
    CannotConnect,
    InvalidAuth,
    NotADuet,
    detect_standalone,
)
from fake_duet import start_server


def url(server):
    return f"http://{server.host}:{server.port}"


async def web_page(request):
    return web.Response(text="<html>Duet Web Control</html>", content_type="text/html")


@pytest.fixture
async def sbc_duet():
    """DSF: serves the object model at /machine/status and has no rr_connect."""

    async def status(request):
        return web.json_response({"state": {"status": "idle"}, "boards": []})

    app = web.Application()
    app.router.add_get("/machine/status", status)
    app.router.add_get("/{tail:.*}", web_page)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()


async def test_a_board_that_answers_rr_connect_is_standalone(fake_duet):
    assert await detect_standalone(url(fake_duet)) is True


async def test_a_board_that_serves_the_model_at_machine_status_is_sbc(sbc_duet):
    assert await detect_standalone(url(sbc_duet)) is False


async def test_a_standalone_board_is_not_mistaken_for_sbc_by_its_status_200(fake_duet):
    """It answers /machine/status with its web page and status 200."""
    assert await detect_standalone(url(fake_duet)) is True


async def test_the_password_is_checked_on_a_standalone_board(fake_duet):
    fake_duet.password = "secret"
    assert await detect_standalone(url(fake_duet), "secret") is True
    with pytest.raises(InvalidAuth):
        await detect_standalone(url(fake_duet), "wrong")


async def test_a_web_server_that_is_not_a_duet_is_rejected():
    app = web.Application()
    app.router.add_get("/{tail:.*}", web_page)
    server = TestServer(app)
    await server.start_server()
    try:
        with pytest.raises(NotADuet):
            await detect_standalone(url(server))
    finally:
        await server.close()


async def test_an_address_nothing_listens_on_cannot_connect():
    server = await start_server()
    address = url(server)
    await server.close()
    with pytest.raises(CannotConnect):
        await detect_standalone(address)
