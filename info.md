# Duet3D integration for Home Assistant

This is a work in progress. Entities are created properly and values can be read from the `rr_model` (standalone) or `/machine/status` (SBC) endpoint of your Duet board. Tools, bed and chamber are discovered from the object model. Tested against RRF 3.7.0-rc.2; see the README for details.
Standalone and SBC mode are detected automatically.

Besides readings it offers controls (pause/resume/cancel/home buttons, temperature, speed, flow and fan controls, macro buttons), job events with device triggers, a webcam, filament actions for spool tracking, diagnostics and re-authentication. See the README for what each does and when it refuses.

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

Add the following to your Lovelace dashboard. Remember to update the entity names with those of your own printer (defined by the value of `duet3d-name`)
```yaml
- card:
    cards:
      - type: glance
        entities:
          - entity: sensor.<name>_current_toolbed_temp
            name: Bed
          - entity: sensor.<name>_current_tool1_temp
            name: Tool
          - entity: sensor.<name>_current_state
            name: Status
    type: horizontal-stack
  conditions:
    - entity: switch.<name>
      state: 'on'
  type: conditional
```

There is also the possibility to send GCodes directly with a Home Assistant service:
```yaml
service: duet3d.hevors_send_gcode
data:
  gcode: G28
```
Currently is not working to log the responsen from an e.g `M122`


# Credits
Code initially based on the OctoPrint integration: [octoprint integration github](https://github.com/home-assistant/home-assistant/tree/dev/homeassistant/components/octoprint)