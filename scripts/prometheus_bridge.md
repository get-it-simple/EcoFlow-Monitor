# Local telemetry export

Open **Connection → Service → Local telemetry export**, enable the HTTP service,
and save. The default Monitor address is `http://127.0.0.1:9469`. When the
service is enabled, Monitor keeps requesting supported readings even while its
windows are closed. The sensor settings still control EcoFlow polling. Choose
publication after Monitor updates or at a fixed interval.

| GET path | Data |
| --- | --- |
| `/v1/telemetry` | Latest Monitor snapshot as JSON |
| `/v1/telemetry/stream` | Initial snapshot and subsequent publications as SSE |
| `/v1/telemetry/raw` | Optional SSE feed of incoming HTTPS telemetry responses and MQTT reports |
| `/v1/commands` | Optional SSE feed of integration command starts and results |

Snapshots carry `version: 1`, a UTC `timestamp`, connection `state`, telemetry
`source`, and a `values` object keyed by the same names as the CLI tokens.
`serial_number` and `device_name` are optional top-level fields.

```json
{"version":1,"timestamp":"2026-01-01T12:00:00+00:00","state":"connected","source":"https","values":{"battery_level":66,"input_power":0,"output_power":72}}
```

Each SSE message has a single JSON `data:` line. For example,
`curl.exe -N http://127.0.0.1:9469/v1/telemetry/raw` reads live raw events.
Raw events contain a UTC timestamp, source, and the filtered API payload.
Command events include a UTC timestamp, dispatch ID, integration/profile IDs,
activation or deactivation phase, step, trigger, and outcome. The two optional
feeds deliver events after subscription. Reconnect after a pause, configuration
change, or slow-client disconnect. Monitor keeps no replay history for these feeds.

Two independent settings control the serial number and other private fields.
Both start hidden. Recognized secrets and authorization data are always filtered.
Unknown raw fields can contain private data that the filter does not recognize.
Enabled feeds are readable by local software. Changes apply to future reads.
Data previously collected by external software follows that software's retention
settings.

## Prometheus

Run the standalone adapter with Python 3.11 or newer on the computer running
Monitor. The script uses the Python standard library.

```powershell
python scripts/prometheus_bridge.py --monitor-port 9469 --port 9470
```

Add this job to Prometheus on the same computer.

```yaml
scrape_configs:
  - job_name: ecoflow_monitor
    static_configs:
      - targets: ["127.0.0.1:9470"]
```

All metric names are prefixed with `ecoflow_monitor_`.

| Suffix | Meaning |
| --- | --- |
| `up`, `telemetry_live` | Snapshot service reachable and Monitor connection live, respectively |
| `battery_percent`, `minimum_discharge_percent`, `maximum_charge_percent` | Charge and effective limits in percent |
| `input_power_watts`, `output_power_watts`, `port_watts{port="…"}` | Power in watts |
| `capacity_watt_hours` | Effective capacity in Wh |
| `remaining_time_seconds`, `device_discharge_seconds`, `device_charge_seconds` | Monitor estimate and device times in seconds when available |
| `charging_state`, `ac_output_enabled`, `dc_output_enabled` | Charging state and outlet switches |
| `device_info` | Device name and/or serial number labels when enabled in Monitor |

Each scrape fetches the snapshot on demand. Expired and unavailable readings are
omitted. A missing service yields `ecoflow_monitor_up 0`. Read raw and command
events using their own HTTP paths.

## CLI profile values

Use **Insert token → Monitor values** in a CLI profile's activation or
deactivation editor. A value can be a whole argument or follow a switch with
`=`, for example `{app} --battery={value:battery_level} --watts {value:output_power}`.
The menu lists the available token names. Power is in watts, battery and limits
in percent, capacity in Wh and times in seconds. Output switches use `0` and `1`.
Monitor resolves each profile transition from the same set of fresh readings.
