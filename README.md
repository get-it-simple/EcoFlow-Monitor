# EcoFlow Monitor

A Windows monitoring display and GPU profile automation project for EcoFlow power
stations, initially targeting DELTA 3 Max and optional MSI Afterburner integration.

**Status: foundation, version 0.1.0.** This repository currently provides project
metadata, English/Ukrainian localization, and automated checks.
Monitoring, windows, hotkeys, widgets, and automation are planned, not implemented.

## Install

There is no runnable application, executable, or installer yet. The first working
release is intended to be portable and will not require an installer.

Official API compatibility and exact telemetry fields for DELTA 3 Max have not
been verified with a real device.

<details>
<summary>Set up the foundation from source</summary>

Use Windows and Python 3.11 or newer. From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

If Python 3.11 is not installed, select a newer installed Python version explicitly.
Environment activation is optional. There is no application launch command yet.

</details>

<details>
<summary>Planned features and controls</summary>

- Official EcoFlow API monitoring: input, output, battery level, charge/discharge
  limits, and a clearly marked local runtime estimate.
- An EcoFlow-inspired energy diagram with directional animated flows.
- Optional MSI Afterburner saved-profile selection, manual controls, and ordered
  automatic rules for running applications and the system.
- Configurable global hotkeys, a selectable-metric mini widget, interface scaling,
  and Windows transparency with a solid-background fallback.
- Clear offline, unavailable, and stale-data indicators.

MSI support and Auto are initially off. The app will only select existing profiles;
GPU tuning stays in MSI Afterburner. Selecting a profile manually turns Auto off.

</details>

<details>
<summary>Settings and practical limits</summary>

Settings persistence is not implemented yet. The planned portable application uses
human-editable `settings.json` beside the executable, with a per-user fallback
when the directory is not writable. Preference groups cover EcoFlow connection
metadata, manual limits, MSI profiles/rules, hotkeys, appearance, language, and
mini-widget options. Secret values will be stored in Windows Credential Manager.

Cloud monitoring requires internet and official developer credentials. The app
will not use unofficial EcoFlow integration methods. Manually entered limits are
display/estimation values and do not change the station. Native acrylic depends
on Windows support and system settings.

</details>

<details>
<summary>Languages and translations</summary>

English and Ukrainian catalogs are included. The localization module already
supports language discovery, system locale selection, and English fallback.

To add a language, create `ecoflow_monitor/i18n/<Windows-locale>/strings.json`,
using the English catalog's keys and translating its values. Save it as UTF-8.
No language registry change is needed. Missing keys fall back to English.
Preserve formatting placeholders such as `{timestamp}` when translating.

</details>

<details>
<summary>Third-party packages</summary>

PySide6 is the foundation's declared GUI dependency. The localization module itself
uses the Python standard library. Development checks use pytest and pytest-xdist;
package metadata uses setuptools. Network, credential-store, process-inspection,
and executable-build dependencies will be added when those features are built.

</details>
