"""What the printer may be told to do, and in which state.

Shared by the services and by the button, number and fan entities, so that they all
refuse the same things. Sending G-code through the web API while a job runs
interleaves with it (homing mid-print would crash the toolhead), so every action
checks the printer's state first, using a fresh read rather than the last poll.
"""
from __future__ import annotations

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


async def fresh_status(coordinator) -> str | None:
    """The printer's state right now, not as of the last poll."""
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise HomeAssistantError(f"{coordinator.config_entry.title} is not reachable")
    return coordinator.get_sensor_state("status.state.status")


async def require(coordinator, allowed: set[str], action: str) -> None:
    """Raise unless the printer is in one of the ``allowed`` states."""
    status = await fresh_status(coordinator)
    if status not in allowed:
        raise ServiceValidationError(
            f"Cannot {action} while {coordinator.config_entry.title} is {status}"
        )


async def send(coordinator, gcode: str) -> None:
    """Send G-code, reporting any failure as one that names the printer."""
    try:
        await coordinator.send_gcode(gcode)
    except Exception as error:
        raise HomeAssistantError(
            f"Error communicating with {coordinator.config_entry.title}: {error}"
        ) from error


async def send_checked(coordinator, gcode: str, allowed: set[str] | None, action: str) -> None:
    """Check the state (when ``allowed`` is given), send, then refresh.

    The refresh is how the result shows up: entities do not guess the new state.
    """
    if allowed is not None:
        await require(coordinator, allowed, action)
    await send(coordinator, gcode)
    await coordinator.async_request_refresh()
