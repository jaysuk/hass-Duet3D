"""Turn successive polls into job lifecycle events.

Deliberately free of Home Assistant imports so the logic can be unit tested in
isolation. ``JobTracker.update`` takes the ``status`` tree of one poll (the object
model, shaped the same in standalone and SBC mode) and returns the events that
happened since the previous poll, as dicts with a ``type`` and the job details.

The printer is polled, so a state can come and go between two polls (a cancel that
takes less than one interval, a pause that is resumed at once). The tracker
therefore infers what happened from where the job is now and what it saw on the way,
rather than expecting every state in order.

It never invents a job: the first poll, and the first poll after a restart in the
middle of a job, are adopted silently. And it never reports a simulation, so nothing
that reacts to these events can bill a simulated print.
"""
from __future__ import annotations

from typing import Any

from .extruders import active_extruders, slicer_filament

JOB_STARTED = "job_started"
JOB_PAUSED = "job_paused"
JOB_RESUMED = "job_resumed"
JOB_FINISHED = "job_finished"
JOB_CANCELLED = "job_cancelled"
JOB_FAILED = "job_failed"
MESSAGE_BOX_OPENED = "message_box_opened"
PRINTER_HALTED = "printer_halted"

EVENT_TYPES = (
    JOB_STARTED,
    JOB_PAUSED,
    JOB_RESUMED,
    JOB_FINISHED,
    JOB_CANCELLED,
    JOB_FAILED,
    MESSAGE_BOX_OPENED,
    PRINTER_HALTED,
)

# A job is under way in all of these (``changingTool`` also happens outside a job,
# so it never starts one, but it does not end one).
_STARTS_A_JOB = {"processing", "pausing", "paused", "resuming"}
_IN_JOB = _STARTS_A_JOB | {"cancelling", "changingTool"}
_PAUSED = {"pausing", "paused"}
# The job is over. Other states (``busy``, ``starting``, ``updating``,
# ``disconnected``) say nothing about it, so they are ignored.
_ENDS_A_JOB = {"idle", "off", "halted"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class JobTracker:
    """Remembers enough of the last poll to say what changed in this one."""

    def __init__(self) -> None:
        self._seen_first = False
        self._previous: str | None = None
        self._box_seq: Any = None
        self._box_open = False
        self._simulating = False
        self._in_job = False
        self._paused = False
        self._cancelling = False
        self._last_cancelled_flag: Any = None
        self._forget_job()

    def _forget_job(self) -> None:
        self._file_name = ""
        self._duration: float | None = None
        self._extruded: float | None = None
        self._tool: int | None = None
        self._extruders: list[int] = []
        self._slicer_filament: list[float] = []

    # -- public ------------------------------------------------------------

    @property
    def in_job(self) -> bool:
        """Whether a (real, not simulated) job is under way, adopted ones included."""
        return self._in_job

    def update(self, status: Any) -> list[dict[str, Any]]:
        """Events since the previous call, oldest first."""
        if not isinstance(status, dict):
            return []
        state = status.get("state") if isinstance(status.get("state"), dict) else {}
        job = status.get("job") if isinstance(status.get("job"), dict) else {}
        current = state.get("status")
        if not isinstance(current, str):
            return []

        events: list[dict[str, Any]] = []
        first = not self._seen_first
        self._seen_first = True

        events += self._machine_events(state, current, first)
        self._job_events(status, state, job, current, first, events)
        self._previous = current
        return events

    # -- machine-level events (apply to simulations as well) ----------------

    def _machine_events(self, state: dict, current: str, first: bool) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        box = state.get("messageBox")
        is_open = isinstance(box, dict) and bool(_text(box.get("message")))
        seq = box.get("seq") if isinstance(box, dict) else None
        if is_open and not first and (not self._box_open or seq != self._box_seq):
            events.append(
                {
                    "type": MESSAGE_BOX_OPENED,
                    "message": box["message"],
                    "title": _text(box.get("title")),
                    "mode": box.get("mode"),
                }
            )
        self._box_open, self._box_seq = is_open, seq

        if current == "halted" and not first and self._previous != "halted":
            events.append({"type": PRINTER_HALTED})
        return events

    # -- job events ---------------------------------------------------------

    def _job_events(self, status, state, job, current, first, events) -> None:
        if self._simulating:
            # A simulation lasts until the machine is back to a state with no job.
            # It can be paused or cancelled on the way, which is still the simulation.
            if current == "simulating" or (
                current in _IN_JOB
                and not (current == "processing" and self._previous == "simulating")
            ):
                return
            self._simulating = False
            if current not in _STARTS_A_JOB:
                return
            # Otherwise a real job started within one poll of the simulation ending.
        elif current == "simulating":
            self._simulating = True
            if self._in_job:  # a real job cannot be running during a simulation
                self._in_job = False
                self._forget_job()
            return

        if self._in_job:
            self._remember(status, state, job)
            self._continue_job(job, current, events)
        elif current in _STARTS_A_JOB:
            self._begin_job(status, state, job, current, first, events)
        self._last_cancelled_flag = job.get("lastFileCancelled")

    def _begin_job(self, status, state, job, current, first, events) -> None:
        self._in_job = True
        self._paused = current in _PAUSED
        self._cancelling = False
        self._forget_job()
        self._remember(status, state, job)
        if first:
            return  # adopted: this job started before we were watching
        events.append(self._payload(JOB_STARTED))
        if self._paused:
            events.append(self._payload(JOB_PAUSED))

    def _continue_job(self, job, current, events) -> None:
        if current in _ENDS_A_JOB:
            events.extend(self._end_job(job, current))
            return
        if current == "cancelling":
            self._cancelling = True
        if current in _PAUSED and not self._paused:
            self._paused = True
            events.append(self._payload(JOB_PAUSED))
        elif current in ("resuming", "processing") and self._paused:
            self._paused = False
            events.append(self._payload(JOB_RESUMED))

    def _end_job(self, job, current) -> list[dict[str, Any]]:
        events = []
        duration = _number(job.get("lastDuration"))
        if duration is not None:
            self._duration = duration
        name = _text(job.get("lastFileName"))
        if name and not self._file_name:
            self._file_name = name

        if current in ("halted", "off") or job.get("lastFileAborted") is True:
            kind = JOB_FAILED
        elif self._cancelling or (
            job.get("lastFileCancelled") is True and self._last_cancelled_flag is not True
        ):
            kind = JOB_CANCELLED
        else:
            kind = JOB_FINISHED
        events.append(self._payload(kind))
        self._in_job = False
        self._paused = False
        self._cancelling = False
        self._forget_job()
        return events

    def _remember(self, status, state, job) -> None:
        """Keep what is only readable while the job is still there."""
        file = job.get("file") if isinstance(job.get("file"), dict) else {}
        name = _text(file.get("fileName"))
        if name:
            self._file_name = name
        duration = _number(job.get("duration"))
        if duration is not None:
            self._duration = duration
        extruded = _number(job.get("rawExtrusion"))
        if extruded is not None:
            self._extruded = extruded
        filament = slicer_filament(file.get("filament"))
        if filament:
            self._slicer_filament = filament
        tool = state.get("currentTool")
        if isinstance(tool, int) and not isinstance(tool, bool) and tool >= 0:
            self._tool = tool
            self._extruders = sorted(active_extruders(status.get("tools"), tool))

    def _payload(self, kind: str) -> dict[str, Any]:
        return {
            "type": kind,
            "file_name": self._file_name,
            "duration": self._duration,
            "extruded_mm": self._extruded if self._extruded is not None else 0.0,
            "tool": self._tool,
            "extruders": list(self._extruders),
            "slicer_filament_mm": list(self._slicer_filament),
        }
