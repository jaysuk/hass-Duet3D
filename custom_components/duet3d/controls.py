"""What the printer may be told to do, and in which state.

Shared by the services and by the button, number and fan entities, so that they all
refuse the same things. Sending G-code through the web API while a job runs
interleaves with it (homing mid-print would crash the toolhead), so every action
checks the printer's state first, using a fresh read rather than the last poll.
"""
from __future__ import annotations

import asyncio

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

# ``state.status`` values in which each action makes sense.
PRINTING = {"processing", "simulating"}
HOME_STATES = {"idle"}
PAUSE_STATES = PRINTING
RESUME_STATES = {"paused"}
CANCEL_STATES = PRINTING | {"paused", "pausing", "resuming"}
# A job exists in all of these, so nothing that moves or loads filament may run.
JOB_STATES = PRINTING | {"paused", "pausing", "resuming", "cancelling", "changingTool"}
# Where motion and filament actions are allowed: not during a job, not while halted.
IDLE_STATES = {"idle"}
# How long a cancel waits for the pause that has to come first (pause.g may park the head).
CANCEL_PAUSE_TIMEOUT = 60  # seconds
CANCEL_POLL_SECONDS = 1


async def fresh_status(coordinator) -> str | None:
    """The printer's state right now, not as of the last poll."""
    await coordinator.async_refresh()
    # A tolerated failed poll keeps the old data and still counts as a success for the
    # entities, but a safety check must not act on it.
    if not (coordinator.last_update_success and coordinator.last_poll_ok):
        raise HomeAssistantError(f"{coordinator.config_entry.title} is not reachable")
    return coordinator.get_sensor_state("status.state.status")


async def require(coordinator, allowed: set[str], action: str) -> None:
    """Raise unless the printer is in one of the ``allowed`` states."""
    status = await fresh_status(coordinator)
    if status not in allowed:
        raise ServiceValidationError(
            f"Cannot {action} while {coordinator.config_entry.title} is {status}"
        )


async def send(coordinator, gcode: str, wait: bool = False) -> None:
    """Send G-code, reporting any failure as one that names the printer.

    ``wait`` is for quick set-points, whose reply is worth reading (SBC mode only; a
    standalone board's replies cannot be read reliably). Anything that runs a macro or
    takes a while is sent without waiting.
    """
    try:
        await coordinator.send_gcode(gcode, wait=wait)
    except Exception as error:
        raise HomeAssistantError(
            f"Error communicating with {coordinator.config_entry.title}: {error}"
        ) from error


async def send_checked(
    coordinator, gcode: str, allowed: set[str] | None, action: str, wait: bool = False
) -> None:
    """Check the state (when ``allowed`` is given), send, then refresh.

    The refresh is how the result shows up: entities do not guess the new state.
    """
    if allowed is not None:
        await require(coordinator, allowed, action)
    await send(coordinator, gcode, wait=wait)
    await coordinator.async_request_refresh()


async def cancel_job(coordinator) -> None:
    """Cancel the job, pausing it first if it is running.

    RepRapFirmware's ``M0`` from the web only cancels a paused print; otherwise it
    replies "Pause the print before attempting to cancel it" and does nothing, which
    is what Duet Web Control works around by pausing first (GCodes2.cpp:760-807).
    """
    title = coordinator.config_entry.title
    state = await fresh_status(coordinator)
    if state not in CANCEL_STATES:
        raise ServiceValidationError(f"Cannot cancel while {title} is {state}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CANCEL_PAUSE_TIMEOUT
    pause_sent = False
    while True:
        if state == "paused":
            await send(coordinator, "M0")
            await coordinator.async_request_refresh()
            return
        if state in PRINTING and not pause_sent:
            await send(coordinator, "M25")
            pause_sent = True
        elif state not in CANCEL_STATES:
            return  # the job ended, or was cancelled, on its own
        if loop.time() >= deadline:
            raise HomeAssistantError(
                f"{title} did not finish pausing within {CANCEL_PAUSE_TIMEOUT} s, so the "
                "job was not cancelled. It is left pausing or paused; cancel again once "
                "it is paused."
            )
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        state = await fresh_status(coordinator)
