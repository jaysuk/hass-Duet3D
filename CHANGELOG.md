# Changelog

Each `## <version>` heading below becomes the GitHub release notes for the tag `v<version>`
(see `.github/workflows/release.yaml`), and HACS shows them when an update is offered.
To release: add the section, set `version` in `custom_components/duet3d/manifest.json` to
match, commit, then push a `v<version>` tag.

## 0.4.1 - 2026-10-02

Fixes from an audit of 0.4.0.

- Cancel now pauses a running job first (`M25`, wait for `paused`, then `M0`), because
  RepRapFirmware only cancels a paused job from the web.
- Filament load and unload check what the firmware would refuse before sending anything.
- In SBC mode, quick set-points read the DSF reply and raise on an `Error:`; macros and long
  codes are sent asynchronously. DSF passwords are supported, and a board list that is
  empty no longer crashes setup.
- `Filament Extruded` follows the job, holds its final value for the poll that ends the job
  and is 0 otherwise, so a `utility_meter` counts every job from its start. It no longer
  reports during simulations.
- The firmware update entity polls, and the installed version follows an update.
- The LED light uses `rgb_color` and raises if the send fails.
- The DWC webcam settings are read correctly (`[HOSTNAME]`, nested `main`/`machine`).
- The poll interval is at least 1 s, and controls refuse after a missed poll.
- Progress uses the sum of the slicer's filament over all extruders.
- Thermostatic fans have no control entity (the firmware ignores `M106 S` for them).
- The thumbnail camera works on standalone boards.
- The webcam address is redacted in diagnostics.

`Filament Extruded` now renders `0.0` rather than `0` when idle.

## 0.4.0 - 2026-10-02

- Adaptive polling, with a `printing_interval` option (5 s by default).
- `duet3d_event` bus events (`job_started`, `job_paused`, `job_resumed`, `job_finished`,
  `job_cancelled`, `job_failed`) and matching device triggers.
- Button, number and fan platforms, including macro buttons.
- Services: `emergency_stop`, `reset_after_emergency_stop`, `load_filament`,
  `unload_filament` and `cancel_object`.
- Sensors for ETA, start and end time, projected duration, print speed, slicer filament
  length and objects.
- Webcam support, an `Online` binary sensor, a firmware `update` entity (disabled by
  default), diagnostics, re-authentication and reconfiguration.

## 0.3.0 - 2026-10-01

- Tools, bed and chamber are discovered from the object model instead of the "Number of
  tools" setting, which showed the wrong tool whenever heater and tool numbers differed.
- Standalone and SBC mode are detected automatically from the board's answers.
- A wrong password and a non-Duet address are reported on the setup form.
- The last data is kept for up to 2 failed polls in a row, so a dropped request no longer
  flips every entity unavailable.
- New entities: heater state and power, fans, extruder flow, speed factor, homed, startup
  error, display message, message box, job details, filament monitors, board health, IP and
  Wi-Fi signal, and storage free space.
- New actions: home, pause, resume, cancel, `acknowledge_message` and a per-printer
  `send_code`. They check the printer's state and refuse rather than interrupt a job.
- 7 requests per poll instead of about 15; boards, network and storage are read once a minute.
- The LED strip index and count are asked in a second options step.

## 0.2.0 - 2026-09-29

- A sensor per extruder (state is the filament name set by `M701`), plus `Filament Extruded`
  and `Current Tool` sensors, so software such as Spoolman can track spools.
- Fixes loading on current Home Assistant (`asyncio.timeout`, `config_entry` passed to the
  coordinator) and the config and options flows.
