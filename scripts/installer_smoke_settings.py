import argparse
import json
from pathlib import Path


def legacy_preferences() -> dict:
    return {
        "version": 2,
        "integrations": [],
        "hotkeys": [],
        "language": "uk",
        "ecoflow": {
            "serial": "",
            "credential_ref": "EcoFlowMonitor/installer-smoke",
            "capacity_wh": 3072,
            "manual_min": 15,
            "manual_max": 85,
        },
        "appearance": {"scale": 0.75, "animations": False, "transparency": False},
    }


def create_settings(path: Path) -> None:
    path.write_text(json.dumps(legacy_preferences(), indent=2) + "\n", encoding="utf-8")


def assert_preferences(saved, expected, field: str = "settings") -> None:
    if isinstance(expected, dict):
        if not isinstance(saved, dict):
            raise ValueError(f"{field}: expected an object")
        for key, value in expected.items():
            name = f"{field}.{key}"
            if key not in saved:
                raise ValueError(f"{name}: required preference is missing")
            assert_preferences(saved[key], value, name)
    elif isinstance(expected, list):
        if not isinstance(saved, list) or len(saved) != len(expected):
            raise ValueError(f"{field}: preference list changed")
        for index, value in enumerate(expected):
            assert_preferences(saved[index], value, f"{field}[{index}]")
    else:
        correct_type = type(saved) is type(expected)
        if type(expected) in (int, float) and field != "settings.version":
            correct_type = type(saved) in (int, float)
        if not correct_type or saved != expected:
            raise ValueError(f"{field}: preference changed")


def verify_settings(path: Path) -> None:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("application settings file was removed") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("application settings could not be read as JSON") from None
    expected = legacy_preferences()
    expected["version"] = 3
    expected["hotkeys"] = [
        {"action": "open_dashboard", "combo": "", "enabled": False},
        {"action": "recover_widgets", "combo": "ctrl+alt+w", "enabled": True},
    ]
    assert_preferences(saved, expected)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and verify synthetic installer migration settings")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("path", type=Path)
    create.set_defaults(stage="Fixture creation")
    verify = commands.add_parser("verify")
    verify.add_argument("path", type=Path)
    verify.add_argument("--stage", default="Running upgrade")
    args = parser.parse_args()
    try:
        if args.command == "create":
            create_settings(args.path)
        else:
            verify_settings(args.path)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{args.stage}: {error}\n")


if __name__ == "__main__":
    main()
