# Intermatic Connect for Home Assistant

Control compatible Intermatic Connect Wi-Fi timers from Home Assistant.

> This is an unofficial, cloud-based custom integration. It is not affiliated with or supported by Intermatic.

## Features

- Configuration through the Home Assistant UI using the same account as the Intermatic Connect mobile app.
- Switches for individually configured circuits, combined Circuits 1 & 2, and Circuit 3 when available.
- Accurate relay state decoding matching the Intermatic Connect Android app.
- Calendar entities for the timer's existing native schedules.
- `intermatic_connect.set_weekly_schedule` service for native recurring on/off schedules.
- Freeze-protection binary sensor for compatible timers with the PE700-FP accessory.
- Standard Home Assistant automations for safety shutoffs after manual overrides.

## Compatibility

Developed and tested with an **Intermatic PE733P** 3-circuit Wi-Fi pool timer, configured with Circuits 1 & 2 combined and Circuit 3 separate. The PE700-FP freeze-protection accessory is also supported.

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
- Native schedules are stored on the timer; use the service carefully because replacing a circuit schedule removes its existing events for that circuit.

## Security

Never share Home Assistant's `.storage` directory, backups, or config-entry files. They can contain refresh tokens for this integration and others.

## License

MIT. See [LICENSE](LICENSE).
