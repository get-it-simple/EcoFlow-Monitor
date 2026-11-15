# Device profiles

DELTA 3 MAX is the application's working reference, confirmed by the user on their
station. The bundled `ecoflow_monitor/profile_data/devices.json` is the single
catalog of built-in mappings and documentation examples.

DELTA 2, DELTA 2 MAX, DELTA 3 MAX PLUS, DELTA MAX, DELTA PRO 3, DELTA PRO ULTRA,
DELTA PRO and RIVER 2 PRO are **examples from the EcoFlow API documentation, not
tested on real devices**. Copying an example does not activate or select it.
DELTA MAX and RIVER 2 PRO examples are incomplete: the supplied HTTP documentation
does not establish every required station-total reading. Main-battery power is
not substituted for station-total input/output power.

## Adding a profile

1. Open Connection, Devices, and expand Device support.
2. Enable additional device files and accept the responsibility warning.
3. Use Open folder, then place JSON files in `device-profiles` beside the settings
   file. Installed copies normally use `%LOCALAPPDATA%\EcoFlowMonitor`; portable
   copies use their settings directory. Add example copies a selected template
   into this folder without overwriting an existing file.
4. Reload profiles, select one for the station, and save settings.

Enabling additional files requires confirmation each time the option is turned on.
A saved enabled option needs no new confirmation at startup. Built-in and user
profiles have separate identities; a user file never silently replaces a built-in
profile. Assignments are remembered per station. Automatic selection requires a
unique explicit API model ID matching a profile's `model_ids`; names and similar
telemetry fields do not identify a model. Without that ID, choose manually.

Reloading validates a candidate catalog. The active mapping changes only on Save.
Cancel leaves the active catalog unchanged; copied example files remain on disk.
A missing/disabled assigned profile stops monitoring until a usable profile is
selected. Older settings retain DELTA 3 MAX for the previously selected station.

## Version 1 format

```json
{
  "version": 1,
  "profiles": [
    {
      "id": "my-model",
      "name": "My station model",
      "revision": 1,
      "evidence": "custom",
      "read": {
        "method": "POST",
        "path": "/iot-open/sign/device/quota"
      },
      "fields": {
        "input_power": {"sources": ["documentedInputField"]},
        "output_power": {"sources": ["documentedOutputField"]},
        "battery_level": {"sources": ["documentedSocField"]}
      }
    }
  ]
}
```

Use an ID starting with a lowercase letter, followed by lowercase letters, digits,
underscores or hyphens, at most 64 characters. User IDs must be unique across the
folder. The reference is `user:<id>`; built-in IDs use `builtin:<id>`. Duplicate
IDs reject the affected files, and duplicate JSON properties are invalid. A user
file cannot claim the working-reference status even if it declares it.

Supported internal fields are `input_power`, `output_power`, `battery_level`,
`minimum_discharge_level`, `maximum_charge_level`, `ac_input`, `pv_input`,
`ac_output`, `dc_output`, `usb_c1`, `usb_c2`, `usb_c3`, `usb_a1`, `usb_a2`,
`anderson`, `ac_enabled`, `dc_enabled`, `charging_state`,
`device_discharge_minutes` and `device_charge_minutes`. The first three are
required for activation. Omitted optional readings stay unknown.

Power uses watts, charge/limits use percent, and remaining times use minutes.
`charging_state` means 0 idle, 1 discharging, 2 charging. Do not copy charging-phase
codes or infer an outlet's enabled state from its measured power.

- `sources: ["pd.wattsInSum"]` addresses a literal dotted JSON key.
- `sources: [["pd", "wattsInSum"]], quotas: ["pd.wattsInSum"]` addresses nested
  data. Paths may include nonnegative array indexes, for example
  `[["ports", 0, "power"]]`.
- Several sources sum only when all are present and valid. Use this only for
  documented independent components of the intended metric; do not combine
  battery power and station-port totals. `quotas` supplies the API request keys
  for nested paths; literal sources use their own names by default.
- `scale: 0.1` converts documented tenths into internal units. Optional `minimum`
  and `maximum` add bounds. Core power is nonnegative; port power retains signs;
  charge and limits remain within 0–100. DELTA 3 MAX preserves its existing
  documented discharge-limit validation.
- `enum: {"0": false, "2": true}` maps outlet states. Unlisted values stay
  unknown. `otherwise: true` explicitly accepts other nonnegative integers; the
  DELTA 3 MAX reference uses `{"4": false}` with that fallback.
- `enum: {"10": 0, "20": 1, "30": 2}` can map documented charging-state codes.
- `anderson` accepts a numeric array from one source and applies `scale` per item.

Only official HTTPS reads are implemented: POST `/iot-open/sign/device/quota`
with signed JSON selected quotas, and GET `/iot-open/sign/device/quota/all` with
signed `sn`. The host is fixed to `api.ecoflow.com`, redirects are disabled and
profiles cannot provide credentials, command bodies or station-write operations.
Other documented read operations require an application adapter update. The
application does not probe alternate methods or endpoints after failure.

Limits are 64 JSON files, 1 MiB per file and 8 MiB total. A profile supports at
most eight sources/path components per mapping and bounded arrays. Profiles are
data, never executable expressions. Reading all quota fields still refreshes only
the internal sensors due in that request, preserving independent freshness.

## Diagnostics

Connection, Diagnostics contains normal/detailed logging and an explicitly
confirmed one-shot device capture. It records the device list and the saved
profile's read operation; without a usable profile it only reads the device list.
Endpoints and mappings are never inferred for unknown devices. Captures include
profile identity/revision and missing/invalid fields, sanitize credentials,
1Password references and serials, stay local, and do not change the saved logging
mode. Review logs before sharing them. No live verification of the documentation
examples is implied by a valid JSON file or passing synthetic tests.
