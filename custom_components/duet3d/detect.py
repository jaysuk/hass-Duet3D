"""Tell a standalone Duet from one driven by a Single Board Computer (DSF)."""
import asyncio

import aiohttp


class DetectError(Exception):
    """The address could not be classified."""


class CannotConnect(DetectError):
    """Nothing answered, or the board refused the connection."""


class InvalidAuth(DetectError):
    """The board rejected the password."""


class NotADuet(DetectError):
    """Something answered, but not like a Duet board."""


async def _json_object(response: aiohttp.ClientResponse) -> dict | None:
    """The body as a JSON object, or None.

    A standalone board answers every unknown URL with its web page and status 200, so
    a successful status says nothing: only a JSON body does.
    """
    if response.status != 200:
        return None
    try:
        body = await response.json(content_type=None)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def detect_standalone(base_url: str, password: str = "") -> bool:
    """True for a standalone board, False for one run by DSF on a Single Board Computer.

    SBC mode is checked first: DSF answers ``/machine/status`` with the object model
    (or 401 when it has a password, which ``/machine/connect`` then checks).
    A standalone board is the one that answers ``/rr_connect`` with ``{"err": ...}``.
    """
    try:
        async with asyncio.timeout(15):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/machine/status") as response:
                    protected = response.status in (401, 403)
                    model = await _json_object(response)
                if model is not None and "state" in model:
                    return False
                if protected:
                    # DSF with a password: it is SBC mode, and not a board to ask rr_connect.
                    async with session.get(
                        f"{base_url}/machine/connect", params={"password": password}
                    ) as response:
                        if response.status == 403:
                            raise InvalidAuth(base_url)
                        if response.status == 200:
                            return False
                    raise CannotConnect(f"{base_url} refused the connection")
                async with session.get(
                    f"{base_url}/rr_connect", params={"password": password}
                ) as response:
                    connect = await _json_object(response)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise CannotConnect(str(exc)) from exc

    if connect is None or "err" not in connect:
        raise NotADuet(base_url)
    if connect["err"] == 0:
        return True
    if connect["err"] == 1:
        raise InvalidAuth(base_url)
    # err 2: the board has no free session. Try again later.
    raise CannotConnect(f"{base_url} refused the connection (err {connect['err']})")
