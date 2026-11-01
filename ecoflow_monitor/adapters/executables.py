import ctypes
from pathlib import Path
import sys

from PySide6.QtCore import QFileInfo


class InvalidExecutable(ValueError):
    pass


def local_executable(path: str, allow_shortcut: bool = True) -> str:
    seen = set()
    while True:
        normalized = path.replace("/", "\\").casefold()
        if normalized.startswith(("\\\\", "\\??\\", "\\device\\")):
            raise InvalidExecutable("invalid_executable")
        candidate = Path(path)
        if not candidate.is_absolute():
            raise InvalidExecutable("invalid_executable")
        if sys.platform == "win32" and ctypes.windll.kernel32.GetDriveTypeW(candidate.anchor) == 4:
            raise InvalidExecutable("invalid_executable")
        try:
            if not candidate.is_file():
                raise InvalidExecutable("invalid_executable")
            candidate = candidate.resolve(strict=True)
        except OSError:
            raise InvalidExecutable("invalid_executable") from None
        if sys.platform == "win32" and ctypes.windll.kernel32.GetDriveTypeW(candidate.anchor) == 4:
            raise InvalidExecutable("invalid_executable")
        if str(candidate).startswith("\\\\"):
            raise InvalidExecutable("invalid_executable")
        if candidate.suffix.casefold() == ".exe":
            return str(candidate)
        if not allow_shortcut or candidate.suffix.casefold() != ".lnk" or normalized in seen or len(seen) >= 16:
            raise InvalidExecutable("invalid_executable")
        seen.add(normalized)
        path = QFileInfo(str(candidate.with_suffix(".lnk"))).symLinkTarget()
        if not path:
            raise InvalidExecutable("invalid_executable")
