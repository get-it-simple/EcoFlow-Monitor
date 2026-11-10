from pathlib import Path


def icon_path(name: str) -> str:
    return str(Path(__file__).parent / 'assets' / f'{name}.svg')
