# Intermatic Connect for Home Assistant

Control compatible Intermatic Connect Wi-Fi timers from Home Assistant.

> This is an unofficial, cloud-based custom integration. It is not affiliated with or supported by Intermatic.

## Features

- Configuration through the Home Assistant UI using the same account as the Intermatic Connect mobile app.
- Single-switch support for ASCEND STW700W timers, preserving names from the Intermatic app.
- ASCEND operating-mode selection and native weekly schedules with clock times or sunrise/sunset offsets.
- Switches for individually configured circuits, combined Circuits 1 & 2, and Circuit 3 when available.
- Accurate relay state decoding matching the Intermatic Connect Android app.
- Calendar entities for the timer's existing native schedules.
- `intermatic_connect.set_weekly_schedule` service for native recurring on/off schedules.
- Freeze-protection binary sensor for compatible timers with the PE700-FP accessory.
- Standard Home Assistant automations for safety shutoffs after manual overrides.

## Compatibility

Developed and tested with an **Intermatic PE733P** 3-circuit Wi-Fi pool timer, configured with Circuits 1 & 2 combined and Circuit 3 separate. The PE700-FP freeze-protection accessory is also supported.

**ASCEND STW700W (firmware 2.05)** supports on/off control, operating modes, and native schedules. Discovery and relay control were tested with Home Assistant 2026.10.4; mode and schedule writes have automated tests but still need live hardware validation.

Other Intermatic Connect cloud timers may work, but they need tester feedback before being considered supported.

## Installation

### HACS (custom repository)

1. In HACS, open **Integrations** and select the three-dot menu.
2. Select **Custom repositories**.
3. Add this repository as category **Integration**.
4. Search for **Intermatic Connect** and install it.
5. Restart Home Assistant.

### Manual

Copy `custom_components/intermatic_connect` from this repository into your Home Assistant configuration directory's `custom_components` folder, then restart Home Assistant.

## Setup

1. Go to **Settings → Devices & services → Add integration**.
2. Search for **Intermatic Connect**.
3. Sign in with the same email and password used by the Intermatic Connect app.

Your password is used only during setup. Home Assistant stores the resulting refresh token in its internal config-entry storage; it is not stored in this repository or in YAML.

## ASCEND modes and schedules

Use the **Operating mode** selector to choose Automatic, Manual, or Random. Automatic and Random require a schedule. Saving a schedule does not change the mode; select Automatic to follow it.

The read-only **Schedule** calendar displays programmed on/off periods, including overnight schedules. Edit schedules through **Developer tools → Actions** or automations:

- `intermatic_connect.set_weekly_schedule`: set recurring on/off clock times.
- `intermatic_connect.set_ascend_schedule`: set individual events with clock times or sunrise/sunset offsets.

Both actions target the timer's switch. For ASCEND, `replace: true` replaces the entire schedule; `false` adds events.

Example: turn on 30 minutes after sunset and off at 23:00 every day:

```yaml
action: intermatic_connect.set_ascend_schedule
target:
  entity_id: switch.ascend_timer
data:
  replace: true
  events:
    - time: sunset
      state: "on"
      offset: 30
      days: [monday, tuesday, wednesday, thursday, friday, saturday, sunday]
    - time: "23:00"
      state: "off"
      days: [monday, tuesday, wednesday, thursday, friday, saturday, sunday]
```

Clock times use `HH:MM` in the timer's local time. Solar offsets are minutes from -120 to 120. An empty event list with `replace: true` clears the schedule and requires Manual mode.

## Automations

Intermatic manual overrides can keep a circuit running after its native schedule would normally turn it off. A Home Assistant safety automation can clear that condition.

Example: force pool lights off every night at midnight:

```yaml
alias: Pool lights - midnight override shutoff
trigger:
  - platform: time
    at: "00:00:00"
action:
  - service: switch.turn_off
    target:
      entity_id: switch.pool_lights
mode: single
```

Example: notify when the timer activates freeze protection:

```yaml
alias: Pool freeze protection active
trigger:
  - platform: state
    entity_id: binary_sensor.pool_freeze_protection
    to: "on"
action:
  - service: persistent_notification.create
    data:
      title: Pool freeze protection active
      message: The Intermatic timer has activated freeze protection.
mode: single
```

## Limitations

- The integration uses Intermatic's cloud service and needs internet access.
- External changes are polled about every 30 seconds.
- ASCEND calendars use Home Assistant's time zone and location; these should match the timer's settings. Solar times are estimates.
- ASCEND cloud state can be stale, and a timed-out command may still execute later.
- Native schedules are stored on the timer; use the service carefully because replacing a circuit schedule removes its existing events for that circuit.

## Security

Never share Home Assistant's `.storage` directory, backups, or config-entry files. They can contain refresh tokens for this integration and others.

## Development tests

Run the offline tests below; they also run in CI with Home Assistant 2026.10.4. Minimum-version compatibility remains unverified.

```sh
uv run --python 3.14 --with pytest-homeassistant-custom-component==0.13.375 --with aiomqtt==2.5.1 --with pycognito==2024.5.1 python -m pytest -q
```

## License

MIT. See [LICENSE](LICENSE).
