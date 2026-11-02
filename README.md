# EcoFlow Monitor

Windows monitoring for **EcoFlow DELTA 3 Max**, with optional modular CLI integrations,
profile reactions and application/system automation rules. Version **0.7.0**.

The application uses only the official EcoFlow developer API. The model mapping
follows the official documentation; real-device validation remains pending.

## Install

Extract the portable ZIP to a writable folder and run `EcoFlowMonitor.exe`.
No installer is required. Windows 10/11 x64 is supported.

Open Settings, enter your official EcoFlow developer access/secret keys, and click
**Load my devices**. Select your DELTA 3 Max and save. The list contains your bound
devices; this version's telemetry mapping supports DELTA 3 Max only. Developer keys
are different from an EcoFlow mobile-app email/password.

<details>
<summary>Everyday controls</summary>

- The battery diagram shows input/output power, battery charge, separate limits,
  port loads, and approximate output-based runtime without input power.
- Settings are available from the top-right button or tray menu. Closing the main
  window hides it; use the tray menu to exit.
- Settings use a dark background and follow the Windows accent color live.
  Percentage settings offer both numeric input and a slider. Enable an optional
  charge limit with **Set value**, or disable it to leave the limit unset.
- Toggle **Transparent background** in Appearance for a translucent dark main
  background or a solid one. Set **Main background transparency** from 0% (solid)
  to 100% (invisible background). Save applies the change without restarting.
  Transparency affects the background only and does not use Windows acrylic.
- `Ctrl+Alt+NumPad5` shows/hides monitoring. Other shortcuts are configurable and
  initially unassigned. A shortcut owned by another app is reported as unavailable.
- Enable the mini widget in Appearance and choose/reorder its metrics. Drag it to
  move it; docking and placement above the taskbar are optional.
- A stale/offline status retains the last received readings. Hover over the status
  for the last successful retrieval time. A retrieval time is not a device sample
  timestamp.

</details>

<details>
<summary>MSI Afterburner and automatic profiles</summary>

MSI is a removable example under Settings → Integrations. Integration and Auto
are initially off. Enable it and save to show its controls. Afterburner must already be running. The app selects existing
slots 1–5; edit and save actual GPU settings in Afterburner itself.

New slot descriptions have no assumed power limit. Optional manual descriptions
retain existing user values; GPU configurations are not read.
Laptop profiles may instead differ by Core Offset, such as 0 and −50 MHz.

Saved slots use `MSIAfterburner.exe -profile1` through `-profile5`. The integration
checks only the executable and running process, then sends the selected command.
It does not inspect GPU identity, synchronization, clocks or internal profile files.
If an integration requires administrator rights, approve the separate helper
and Windows UAC prompt. EcoFlow Monitor keeps running with normal access.
The helper accepts only that integration's configured commands and stops when
the integration is disabled or edited, or the application exits. Retry a declined
or disconnected helper through the tray menu. Approve only trusted integrations
and executables. Windows errors and command exit codes are shown when dispatch fails.
Global Afterburner hotkeys are not used by this integration.

Manual selection turns Auto off. Application rules match any running configured
executable and take priority over system rules; the first matching rule wins.
Add `.exe` files or Windows shortcuts, then choose input/SOC conditions and a slot.
An empty rule list leaves the current profile unchanged. Auto waits for fresh
EcoFlow data and an available Afterburner instance.

A **requested** profile and a completed command do not confirm the applied GPU
settings. The integration shows the last request and command result. Manual Apply
can resend the same MSI profile. A stopped instance, inaccessible process path,
or ambiguous executable discovery is shown explicitly.
Command-line dispatch has an external process-exit race; no stopped instance is
deliberately launched. Real GPU switching has not been exercised during automated
verification.

</details>

<details>
<summary>Create your own integration modules</summary>

Open **Settings → Integrations → Add integration**, or import a JSON module.
Choose the application's local `.exe` or `.lnk` through Browse, type its path, or
drop the file into the path field. Shortcuts resolve to their target EXE without
running it; shortcut arguments are ignored. Network and broken paths are rejected.

Each integration has its own profiles, Auto, ordered rules and configurable
hotkeys. Select an enabled integration and profile in the monitor, then click
**Apply profile**. Manual selection turns off Auto for that integration only.
The module declares which conditions its rules may use: running application,
input power, and battery SOC. Application rules take priority over system rules.
Auto requires fresh telemetry, a stable candidate for five seconds, and at least
ten seconds between transitions.

In **CLI mapping**, give a command a stable ID, a display label, and its arguments,
for example `low`, `Low power`, and `--mode "low power"`. Labels can change without
breaking profiles. In **Profiles and reactions**, enable activation/deactivation
independently and enter one command template per line:

```text
{app} {cli:low}
```

Use the label selector and **Insert token** to insert the application path or a
mapped command. The preview shows the expanded command. `{app}` must be the first
argument; `{cli:ID}` inserts the mapping's argument list. Tokens occupy whole
arguments. Literal arguments use Windows quoting; commands run directly without
a shell, environment expansion, pipes, or redirection.

Switching A → B runs A's enabled deactivation commands, then B's enabled activation
commands, in order. The first failure or five-second command timeout stops the
transition and reports its partial result. **Retry transition** repeats the
transition from its beginning; choose it explicitly because completed commands
may run again. A repeated successful selection does not rerun commands.
Disabling integration/Auto, exiting, losing telemetry or finding no matching rule
leaves the external state unchanged. The first selection after starting the
monitor runs activation only. Successful dispatch remains a request, not proof
that an external application applied the profile.

**Allow commands to start the application** is initially off for custom modules.
Enable it if their CLI must work while the target application is stopped.
MSI always requires an already-running instance and only accepts existing saved
slots; ambiguous executable discovery requires an explicit path. All integrations
use CLI commands and their completion results. They never read another program's
process memory, shared memory, or internal GPU profile files.

This complete module can be saved as `example.json` and imported:

```json
{
  "version": 1,
  "id": "example",
  "label": "Example application",
  "driver": "cli",
  "enabled": false,
  "auto_enabled": false,
  "executable": "",
  "allow_launch": false,
  "gpu_id": "",
  "conditions": ["application", "input", "soc"],
  "mapping": [
    {"id": "low", "label": "Low power", "arguments": ["--mode", "low power"]},
    {"id": "normal", "label": "Normal", "arguments": ["--mode", "normal"]}
  ],
  "profiles": [
    {
      "id": "eco",
      "label": "Eco",
      "activation": {"enabled": true, "commands": ["{app} {cli:low}"]},
      "deactivation": {"enabled": false, "commands": ["{app} {cli:normal}"]},
      "slot": null,
      "technical_details": "Power saving preset for battery operation."
    }
  ],
  "application_rules": [],
  "system_rules": []
}
```

Choose your EXE after import. Imported modules always start disabled with Auto
off. IDs use letters, digits, underscores and hyphens and must be unique within
their collection. New custom modules use `driver: "cli"`; the bundled MSI example
uses the built-in `afterburner` adapter. Unknown formats and invalid references
are rejected without replacing existing modules. Duplicate integration IDs
are rejected; edit the ID before importing a separate copy.

Use each profile's Technical details field for multiline comments. Hover over a
profile in the monitor, profile list or rule selector to read its details. These
comments do not change commands or automation. Appearance controls and each
integration's hotkeys are grouped into expandable sections.

A system rule can be added through the editor or JSON, for example
`{"id":"low_battery","profile_id":"eco","enabled":true,"input_condition":"absent","min_soc":0,"max_soc":20,"executable":""}`.
Application rules additionally require a full local executable path.
Missing bounds mean no SOC restriction; the upper bound excludes its endpoint
except 100. Empty rule lists never select a profile automatically.

**Export JSON** shares the selected module, including its paths and rules but no
EcoFlow credentials. Review local paths before sharing. Save applies edits;
Reset discards the draft. Removing a module also removes its profile hotkeys
without executing reactions. Removing a referenced command or profile requires
removing its references first. The MSI example stays deleted across restarts;
**Restore MSI example** adds it back, disabled.

Existing settings migrate automatically on load and are written as schema 2 on
the next save. No Python plugin or executable rebuild is needed to add a CLI module.

</details>

<details>
<summary>Settings, limits, and practical limitations</summary>

Non-secret preferences use `settings.json` beside the portable executable or source
tree, with `%LOCALAPPDATA%/EcoFlowMonitor` fallback if the directory is not writable.
Groups cover EcoFlow device/manual values, integration modules and their rules, hotkeys,
appearance, mini-widget options, and language. Keys are stored in Windows Credential
Manager and never in the JSON file. Edit the JSON only while the app is stopped.

Settings dialogs use Save/Reset. A damaged settings file is preserved; explicit
recovery Save creates `settings.json.bak` before replacing it. Rotating diagnostic
logs are kept in the settings directory's `logs` folder without keys or raw payloads.

The nominal model capacity is 2048 Wh; it can be overridden for local estimates.
Runtime uses output power and charge above the discharge floor, only with measured
zero input. It is approximate and is unavailable without the necessary fresh data.
Manual limits are local fallback values and never change the station.

Cloud monitoring requires internet, an authorized account-bound device, and valid
developer keys. Polling starts at 30 seconds; data becomes stale after 90 seconds
without a valid retrieval. API failure does not by itself prove internet loss.
This app sends no EcoFlow control commands and uses no private integration methods.
The current documented server is `api.ecoflow.com`.

Transparency and native overlays depend on Windows capabilities; exclusive
fullscreen coverage is not guaranteed. No rendering injection is used.

</details>

<details>
<summary>Run from source</summary>

Use Windows and Python 3.11 or newer. From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m ecoflow_monitor
```

If Python 3.11 is not installed, choose a newer installed version explicitly.
`--settings <path>` selects another non-secret preferences file.

</details>

<details>
<summary>Languages and translations</summary>

English and Ukrainian are included. The system language is selected by default;
language changes take effect when Settings is saved.

To add a language, create `ecoflow_monitor/i18n/<Windows-locale>/strings.json` using
the English catalog's keys. Translate values, preserve placeholders such as
`{timestamp}`, and save UTF-8. Missing keys use English. No registry change is
needed; new catalogs are discovered and packaged automatically.

</details>

<details>
<summary>Third-party packages</summary>

Runtime: PySide6, httpx, and psutil. Development checks use pytest/pytest-xdist;
portable builds use PyInstaller. Credentials, native hotkeys, and Windows effects
use Windows APIs. This independent project is not affiliated with EcoFlow or MSI.

</details>
