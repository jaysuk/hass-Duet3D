# Duet3D integration for Home Assistant

This is a work in progress. Entities are created properly and values can be read from the `rr_model` (standalone) or `/machine/status` (SBC) endpoint of your Duet board. Standalone and SBC mode are detected automatically when you add the printer.

Tested against RRF 3.7.0-rc.2 on a real standalone board. The RRF 3.6 object model
layout (`heat.bedHeaters` instead of `heat.bedHeaterMapping`) is handled and covered by
tests written from the firmware source, but has not been run against a 3.6.3 board.



## Installation

### From HACS

1. Install HACS if you haven't already (see [installation guide](https://hacs.xyz/docs/configuration/basic)).
2. Add custom repository `https://github.com/jaysuk/hass-Duet3D` as "Integration" in the settings tab of HACS.
3. Find and install "Duet3D" integration in HACS's "Integrations" tab.
4. Restart your Home Assistant.

### Manual

1. Download and unzip the [repo archive](https://github.com/jaysuk/hass-Duet3D/archive/master.zip). (You could also click "Download ZIP" after pressing the green button in the repo, alternatively, you could clone the repo from SSH add-on).
2. Copy contents of the archive/repo into your `/config` directory.
3. Restart your Home Assistant.

### Config
Add the Duet3D Printer integration via the UI. 
1. Parameters => Integrations
2. Add integration
3. Search Duet
4. Configure in UI the app
    - Name => Name you want to give to your printer
    - Host => Printer ip adress
    - Port => Printer port => Usually 80
    - Password => password, or empty if you don't have one , or if you are using SBC
    - Update frequency

Whether the board is [standalone or in SBC mode](https://docs.duet3d.com/en/User_manual/Overview/Getting_started_Duet_3_MB6HC#:~:text=Standalone%20mode%20vs%20SBC%20mode%20The%20Duet%203,%28Duet%20Web%20Control%29%20etc%20work%20in%20both%20modes)
is detected from what it answers, and a wrong password is reported. If you change a board
between the two modes, remove the integration and add it again.

If your printer has an LED strip, open the integration's **Configure** and tick
"LED's installed"; it then asks for the strip index and the number of LEDs.

**Configure** also has the update interval while printing (see
[How often it polls](#how-often-it-polls)) and the webcam address (see [Webcam](#webcam)).

If the board's password changes, Home Assistant asks for the new one (a repair/re-authentication
prompt) instead of failing quietly. If the printer moves to another address, use the
integration's **Reconfigure** menu entry; the printer keeps its device and entities.

A poll that fails once (a Wi-Fi board drops the odd request) keeps the last values, so
entities do not flip to unavailable and back. After 2 failed polls in a row they go
unavailable.

## Temperatures

Tools, bed and chamber are read from the printer's object model, so there is nothing to
configure and they follow the printer if it changes (for example after `M563`).

| Entity | Notes |
| --- | --- |
| `Tool N current / active / standby temperature` | One set per tool, using the heater(s) the tool says it uses (`tools[N].heaters`). Tool numbers are the firmware's, starting at 0. A tool with several heaters gets `Tool N heater 1`, and so on. Attributes: `heater`, `tool` |
| `Bed current / active temperature` | Only if a bed heater is mapped (`heat.bedHeaterMapping`, or `heat.bedHeaters` on RRF 3.6 and earlier) |
| `Chamber current / active temperature` | Only if a chamber heater is mapped |

A heater whose temperature sensor has failed reads `unknown`, not -273.

**Upgrading:** earlier versions named tool sensors from the "Number of tools" setting
(`Tool 1`, `Tool 2`, ...) and read the heater with that number, so they were wrong
whenever the heater and tool numbers differed. The old numbered sensors are removed
automatically on start-up; bed sensors keep their entity. The new tool sensors have new
entity ids (for example `sensor.<name>_tool_0_current_temperature`), so update dashboards
and automations that used the old ones. The "Number of tools" and "Hot bed" settings no
longer exist; entries that still have them are fine.

## Other entities

Everything here is discovered from the object model, so you only get what your printer
actually has, and entities appear when the printer gains something (a CAN board, a
filament monitor, an SD card).

| Entity | Notes |
| --- | --- |
| `<Tool/Bed/Chamber> heater state` | `off`, `standby`, `active`, `fault`, `tuning` or `offline`. `fault` is the one to alert on |
| `<Tool/Bed/Chamber> heater power` | Average heater PWM, % |
| `<Fan name> speed` | One per defined fan, %. Attributes: `requested`, `rpm` (only if the fan has a tacho). Unnamed fans are `Fan N` |
| `Extruder N flow` | The M221 extrusion factor, % |
| `Speed Factor` | The M220 speed factor, % |
| `Homed` | On when every visible axis is homed. Each axis is an attribute |
| `Startup error` | On if `config.g` (or another startup file) reported an error. Attributes: `message`, `file`, `line` |
| `Display Message` | The last `M117` message |
| `Message Box` | The open `M291` prompt (for example "change filament"); unknown when none. Attributes: `title`, `mode`, `seq`, ... |
| `Filament Time Remaining`, `Warm-up Duration`, `Last File Name`, `Layer Height`, `Object Height`, `Generated By` | Job details. `Last File Name` still works after a job ends. Unknown when there is no value |
| `Extruder N filament monitor` | `ok`, or what the monitor reports (`noFilament`, `tooLittleMovement`, ...). Only for extruders that have a monitor |
| `Extruder N filament present` | On when the monitor can see filament. Only created for monitors that can tell (simple switches; laser and pulsed monitors usually cannot) |
| `<Board> MCU temperature`, `input voltage`, `12V rail`, `<Board> connected` | Main board and CAN expansion boards, as diagnostics. `connected` exists for expansion boards and is off when the board stops responding. `free RAM` exists but is disabled until you enable it |
| `<Interface> IP address`, `Wi-Fi signal strength` | Signal only appears once Wi-Fi reports one |
| `Online` | Connectivity diagnostic. Never unavailable: it is how you see that the printer stopped answering (after the same 2 missed polls that make the other entities unavailable) |
| `Print ETA` | Timestamp the job should finish (last read + `timesLeft.file`), rounded to the minute so it does not change on every poll |
| `Print start time`, `Print end time` | Timestamps taken from the job events. A job already running when Home Assistant starts gets a start time worked back from its duration. Unknown after a restart until the next job |
| `Projected total duration` | Time elapsed plus time left, minutes. Only while a job runs |
| `Print speed` | `move.currentMove.requestedSpeed`, mm/s |
| `Slicer filament length` | Total filament the slicer estimated for the job file, mm. Attribute `extruders`: the same per extruder |
| `Print objects` | Number of labelled objects (`M486`) in the job; attributes `objects` (index, name, cancelled), `cancelled`, `current`. Unknown when the file has no labelled objects |
| `Firmware` (update, disabled by default) | Installed firmware against the newest *stable* RepRapFirmware release. **Enabling it makes Home Assistant ask `api.github.com` for the latest release, at most every 6 hours.** Informational: there is nothing to install |
| `Storage N free space` | Mounted SD cards or USB drives, shown in GB with `capacity` as an attribute. Unavailable while unmounted |

Filament monitor support follows the firmware's object model but has not been tried on
a real monitor. Open an issue with the output of `rr_model?key=sensors.filamentMonitors`
if yours does not behave.

### How often it polls

There are two intervals, both in **Configure**. While the printer is idle it is polled every
*update interval* (30 s by default). While a job is under way (`processing`, `pausing`,
`resuming`, `cancelling`, `changingTool`) it is polled every *update interval while printing*
(5 s by default, minimum 1 s, and never slower than the idle interval). That is when the
extrusion counter and the job events matter. 5 s rather than 2 s because a Duet Wi-Fi module
is far weaker than a Raspberry Pi; lower it if your board copes. A paused printer is polled at
the idle rate, because nothing is being extruded.

In standalone mode every poll is 7 small requests (job, heaters, tools, motion, fans and
filament monitors). Boards, network and storage change rarely, so they are re-read once a
minute instead. On a real board that is 10 requests and about 11 KB for a full read. In SBC
mode everything comes from the single `/machine/status` request.

## Controls

Buttons, numbers and fans act on the printer. Pressing a button reads the printer's state
first and raises an error instead of sending anything when it does not fit (the buttons stay
available meanwhile, so they do not flap). After a change the result is read back on the next
poll; nothing is shown optimistically.

| Entity | Sends | Refused unless the printer is... |
| --- | --- | --- |
| `Pause`, `Resume`, `Cancel` buttons | `M25`, `M24`, `M25` then `M0` | the same states as the actions below |
| `Home all`, `Home X/Y/Z` buttons | `G28`, `G28 X`, ... (only for axes the printer has) | idle |
| `Acknowledge message` button | `M292` | showing a message box |
| `Emergency stop` button (**disabled by default**) | `M112` | never refused |
| `Reset after emergency stop` button (**disabled by default**) | `M999` | halted |
| `<Tool/Bed/Chamber> target` number | `M104 S.. T..`, `M140 P.. S..`, `M141 P.. S..` | any state, as in Duet Web Control. Maximum is the heater's limit less 15 °C (280 °C if the firmware reports none) |
| `Speed factor` number | `M220 S..` (10-300 %) | any state |
| `Extruder N flow` number | `M221 D.. S..` (10-300 %) | any state |
| `<Fan name> control` fan | `M106 P.. S..` | any state. The percentage shown is what was asked for. A thermostatic fan has no control: the firmware ignores `M106 S` for it and drives it from temperature (its `speed` sensor stays) |
| `Macro <name>` buttons (**disabled by default**) | `M98 P"0:/macros/<file>"` | idle |

The speed, flow and target numbers sit alongside the read-only sensors of the same name
(a different entity type), so no existing entity changed. A second heater of the same tool,
bed or chamber has no target of its own, because `M104`/`M140`/`M141` set them together.

**Macros:** the top level of `0:/macros` is listed at start-up and then once a minute, one
button per file. They are disabled by default because there are usually many; enable the
ones you want. Macros in sub-folders are not listed, and a file whose name contains a quote,
semicolon or line break gets no button, because the name is pasted into G-code.

## Actions

Available under the Duet3D integration in automations and scripts. Choose the printer
with the action's target (if you have only one printer you can leave the target empty).

| Action | G-code | Refused unless the printer is... |
| --- | --- | --- |
| `duet3d.home` (optional `axes`, such as `X, Y`) | `G28` | idle. Homing mid-job would crash the toolhead |
| `duet3d.pause` | `M25` | running a job |
| `duet3d.resume` | `M24` | paused |
| `duet3d.cancel` | `M25`, then `M0` | running or paused. The firmware only cancels a paused job, so a running one is paused first and cancelled once the pause is done (up to 60 s, else an error and the job is left paused) |
| `duet3d.acknowledge_message` (optional `cancel`) | `M292` / `M292 P1` | showing a message box |
| `duet3d.emergency_stop` | `M112` | never refused |
| `duet3d.reset_after_emergency_stop` | `M999` | halted |
| `duet3d.load_filament` (`tool`, `filament`) | `M701 S"<filament>"` | idle, and `tool` already selected |
| `duet3d.unload_filament` (`tool`) | `M702` | idle, and `tool` already selected |
| `duet3d.cancel_object` (`object`) | `M486 P<object>` | running a job that has that object (see `Print objects`) |
| `duet3d.send_code` (`gcode`) | whatever you give it | never refused |

`M701` and `M702` act on the *selected* tool and take no tool parameter, so
`load_filament`/`unload_filament` refuse unless `tool` is the selected one rather than
select it for you (that would run the tool-change macros). Both move filament. There is no
action that only sets the filament name: no G-code does that without loading. Filament names
containing a quote, semicolon or line break are rejected.

All except `send_code` and `emergency_stop` read the printer's state when they are called, not from the last poll, and
raise an error instead of sending anything when it does not fit. `send_code` has no
checks, so it can interrupt a job; use it for anything the others do not cover.

```yaml
action: duet3d.home
target:
  device_id: <your printer>
data:
  axes: [X, Y]
```

Replies to G-code are not returned (for example the output of `M122`).

## Events and device triggers

The integration works out from successive polls what the job did and fires a
`duet3d_event` on the Home Assistant event bus. The same events are offered as **device
triggers** ("Job finished" and so on) in the automation editor.

| `type` | When |
| --- | --- |
| `job_started` | A job began |
| `job_paused`, `job_resumed` | Paused / resumed (once each, even if the pause was shorter than a poll) |
| `job_finished` | The job ended normally |
| `job_cancelled` | The job ended after a `cancelling` state was seen, or the firmware says the file was cancelled (`job.lastFileCancelled`) |
| `job_failed` | The firmware says the file was aborted (`job.lastFileAborted`), the printer halted mid-job, or it lost power mid-job |
| `message_box_opened` | A new `M291` message box appeared (`message`, `title`, `mode`) |
| `printer_halted` | The printer entered `halted` (emergency stop or a fatal error) |

Event data: `device_id`, `name`, `type` and, for the job events, `file_name`, `duration`
(seconds), `extruded_mm` (the job's total, last reading before the end), `tool`,
`extruders` (of that tool), `slicer_filament_mm` (per extruder). The job details are kept
from while the job was running because the firmware reports nothing once it ends.

Things that are deliberately not events:
- **Simulations** (`simulating`) never produce job events, so nothing downstream can bill a
  simulated print.
- **Nothing on the first poll.** Home Assistant starting in the middle of a job adopts it
  silently and reports its end, with the job's whole extrusion.
- A job that starts and ends between two polls cannot be seen, and neither can the end of
  a job when the next one starts within one poll (a queue macro chaining jobs).

Events are fired just after the entities have been updated from the same poll, so an
automation that reacts to one reads current states. `Filament Extruded` follows the job,
keeps the job's final value for the poll that fires the end event, and is 0 from the next
poll on. A `utility_meter` ignores a drop rather than treating it as a reset, so a value
held until the next job would make it lose the start of that job.

## Webcam

Set a webcam address in **Configure**. Left empty, the address in Duet Web Control's own
settings (`0:/sys/dwc-settings.json`, `webcam.url`) is used when it is enabled there, read
once when the integration loads. `[HOSTNAME]` in it is replaced by the printer's address.
The `Webcam` camera is only created when an address is known. A snapshot address is read
whole; an MJPEG stream address gives its first frame. The address is offered as the stream
source only when DWC shows it as a stream (its update interval is 0) or when it is the one
set in Configure. DWC's `liveUrl` is the page opened by clicking the picture, not a stream,
so it is ignored.

## Diagnostics

**Download diagnostics** on the integration includes the firmware, mode, poll statistics,
heater roles, hardware and the object model. The password, address, MAC, SSID, hostnames,
board ID, file names and the webcam address are redacted.

## Filament / spool tracking

Each extruder is exposed as a sensor so that other software (for example
[SpoolmanSync](https://github.com/gibz104/SpoolmanSync) and [Spoolman](https://github.com/Donkie/Spoolman))
can track which spool is loaded and how much it has used. This works in both
standalone and SBC mode.

| Entity | State | Notes |
| --- | --- | --- |
| `Extruder N` | Loaded filament name | One per extruder. Attributes: `name`, `type` (both the filament name), `extruder`, `tools`, `active` (this extruder belongs to the selected tool), `filament_diameter`, `position` |
| `Filament Extruded` | mm | Filament extruded by the current job, before extrusion factors, net of retractions and without extrusion inside macros (purges, `M701` loads, `pause.g`). 0 outside a real job, simulations included; the job's final value is held for the poll that fires its end event |
| `Current Tool` | tool number | `-1` when no tool is selected |

The filament name is whatever `M701 S"PLA"` set. RepRapFirmware forgets it on
restart, so load filament from your `config.g`/tool macros if you want it to survive
a reboot. Names are conventionally the material (`/sys/filaments/PLA`), so `name`
and `type` carry the same value.

Requires Home Assistant 2024.11 or newer.

## Development

```
pip install pytest pytest-homeassistant-custom-component
pytest
```

The tests start the real integration in Home Assistant against a fake standalone
Duet.

## Lovelace
A specific card exist for this integration: 

[Duet integration card](https://github.com/repier37/ha-threedy-card)

![Featured](https://github.com/repier37/ha-threedy-card/raw/master/screenshots/active.png)


G-code can also be sent from automations; see [Actions](#actions).


# Credits
This fork is maintained by [@jaysuk](https://github.com/jaysuk). It is based on the original
[Lyr3x/hass-Duet3D](https://github.com/Lyr3x/hass-Duet3D).

# Licence
The additions in this fork are released under the [MIT licence](LICENSE). The code it started
from, in Lyr3x/hass-Duet3D, had no licence file when this fork was made, so the MIT licence
cannot cover that original code on its own.

Code initially based on the OctoPrint integration: [octoprint integration github](https://github.com/home-assistant/home-assistant/tree/dev/homeassistant/components/octoprint)
