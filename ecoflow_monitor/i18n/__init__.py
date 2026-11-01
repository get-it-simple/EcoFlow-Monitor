import ctypes
import json
import locale
import logging
import sys
from pathlib import Path


FALLBACK_LANGUAGE = "en"
logger = logging.getLogger(__name__)


def _normalize_locale(value: str) -> str:
    return value.replace("_", "-").split(".", 1)[0].split("@", 1)[0].casefold()


def detect_system_language() -> str:
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_locale = kernel32.GetUserDefaultLocaleName
            get_locale.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
            get_locale.restype = ctypes.c_int
            buffer = ctypes.create_unicode_buffer(85)
            if get_locale(buffer, len(buffer)):
                return buffer.value
        except (OSError, AttributeError):
            logger.warning("Windows locale detection is unavailable")
    try:
        return locale.getlocale()[0] or FALLBACK_LANGUAGE
    except (ValueError, TypeError):
        return FALLBACK_LANGUAGE


def language_display_name(language: str, current_language: str) -> str:
    if sys.platform != "win32":
        return language
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_info = kernel32.GetLocaleInfoEx
        get_info.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_int]
        get_info.restype = ctypes.c_int
        native_buffer = ctypes.create_unicode_buffer(256)
        english_buffer = ctypes.create_unicode_buffer(256)
        native = language
        english = language
        if get_info(language, 0x00000004, native_buffer, len(native_buffer)):
            native = native_buffer.value
        if get_info(language, 0x00001001, english_buffer, len(english_buffer)):
            english = english_buffer.value
        if _normalize_locale(language) == _normalize_locale(current_language) or native == english:
            return native
        return f"{native} ({english})"
    except (OSError, AttributeError):
        return language


class Translator:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory if directory is not None else Path(__file__).resolve().parent
        self._english = self._load_catalog(FALLBACK_LANGUAGE)
        self._strings = dict(self._english)
        self._current_language = FALLBACK_LANGUAGE

    @property
    def current_language(self) -> str:
        return self._current_language

    def available_languages(self) -> tuple[str, ...]:
        return tuple(sorted(path.parent.name for path in self._directory.glob("*/strings.json") if path.is_file()))

    def setup(self, language: str) -> str:
        requested = detect_system_language() if language.casefold() == "system" else language
        languages = {_normalize_locale(code): code for code in self.available_languages()}
        candidate = _normalize_locale(requested)
        resolved = FALLBACK_LANGUAGE
        while candidate:
            if candidate in languages:
                resolved = languages[candidate]
                break
            candidate = candidate.rpartition("-")[0]

        strings = dict(self._english)
        if resolved != FALLBACK_LANGUAGE:
            try:
                strings.update(self._load_catalog(resolved))
            except (OSError, ValueError):
                logger.warning("Translation catalog %s is unavailable; using English", resolved)
                resolved = FALLBACK_LANGUAGE
        self._strings = strings
        self._current_language = resolved
        return resolved

    def translate(self, key: str) -> str:
        return self._strings.get(key, key)

    def _load_catalog(self, language: str) -> dict[str, str]:
        path = self._directory / language / "strings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and value.strip()
            for key, value in data.items()
        ):
            raise ValueError(f"Expected string keys and nonempty string values in {path.name}")
        if language == FALLBACK_LANGUAGE and not data:
            raise ValueError("The English catalog must not be empty")
        return data


_translator = Translator()


def get_available_languages() -> tuple[str, ...]:
    return _translator.available_languages()


def setup(language: str) -> str:
    return _translator.setup(language)


def get_current_language() -> str:
    return _translator.current_language


def t(key: str) -> str:
    return _translator.translate(key)
