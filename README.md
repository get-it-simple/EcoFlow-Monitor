# EcoFlow Monitor

Windows monitoring for **EcoFlow DELTA 3 Max**, with optional MSI Afterburner saved
profile switching and application/system automation rules. Version **0.2.3**.

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
- Toggle **Transparent background** in Appearance for a translucent dark main
  background or a solid one. Save applies the change without restarting.
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

MSI integration and Auto are initially off. Enable the integration in Settings to
show its controls. Afterburner must already be running. The app selects existing
slots 1–5; edit and save actual GPU settings in Afterburner itself.

The initial slot descriptions are 100%, 95%, 90%, 85%, and 51%. They describe the
user's presets and are not GPU tuning commands. Readable GPU configurations supply
discovered values; manual descriptions are available otherwise.

Manual selection turns Auto off. Application rules match any running configured
executable and take priority over system rules; the first matching rule wins.
Add `.exe` files or Windows shortcuts, then choose input/SOC conditions and a slot.
An empty rule list leaves the current profile unchanged. Auto waits for fresh
EcoFlow data and an available Afterburner instance.

A **requested** profile is not a confirmed active profile. The selected slot can
remain unknown when Afterburner does not expose enough comparable settings. A
stopped instance, inaccessible data, or ambiguous GPU selection is shown explicitly.
Command-line dispatch has an external process-exit race; no stopped instance is
deliberately launched. Real GPU switching has not been exercised during automated
verification.

</details>

<details>
<summary>Settings, limits, and practical limitations</summary>

Non-secret preferences use `settings.json` beside the portable executable or source
tree, with `%LOCALAPPDATA%/EcoFlowMonitor` fallback if the directory is not writable.
Groups cover EcoFlow device/manual values, Afterburner, ordered rules, hotkeys,
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
