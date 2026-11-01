import json
from pathlib import Path

import pytest

import ecoflow_monitor.i18n as i18n


@pytest.fixture
def catalog_directory(tmp_path):
    english = tmp_path / "en"
    english.mkdir()
    (english / "strings.json").write_text(
        json.dumps({"app_title": "EcoFlow Monitor", "exit": "Exit"}), encoding="utf-8"
    )
    return tmp_path


def write_catalog(directory, code, data):
    language = directory / code
    language.mkdir(exist_ok=True)
    (language / "strings.json").write_text(json.dumps(data), encoding="utf-8")


def test_packaged_english_content_and_initial_languages():
    translator = i18n.Translator()
    assert {"en", "uk"} <= set(translator.available_languages())
    assert translator.current_language == "en"
    assert translator.translate("app_title") == "EcoFlow Monitor"
    assert translator.translate("input_power") == "Input"
    assert translator.translate("last_update").format(timestamp="2026-01-01") == (
        "Last successful update: 2026-01-01"
    )
    assert translator.translate("unknown_key") == "unknown_key"


def test_new_catalog_is_discovered_without_a_registry(catalog_directory):
    translator = i18n.Translator(catalog_directory)
    assert translator.available_languages() == ("en",)
    write_catalog(catalog_directory, "en-GB", {"exit": "Leave"})
    (catalog_directory / "empty").mkdir()
    (catalog_directory / "unrelated.json").write_text("{}", encoding="utf-8")
    assert translator.available_languages() == ("en", "en-GB")
    assert translator.setup("en-GB") == "en-GB"
    assert translator.translate("exit") == "Leave"
    assert translator.translate("app_title") == "EcoFlow Monitor"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("en-GB", "en-GB"),
        ("EN_gb.UTF-8", "en-GB"),
        ("en-GB-extra", "en-GB"),
        ("en-US", "en"),
        ("zz-ZZ", "en"),
        ("", "en"),
        ("../outside", "en"),
    ],
)
def test_exact_parent_and_english_fallback(catalog_directory, requested, expected):
    write_catalog(catalog_directory, "en-GB", {"exit": "Leave"})
    translator = i18n.Translator(catalog_directory)
    assert translator.setup(requested) == expected
    assert translator.current_language == expected
    assert translator.translate("exit") == ("Leave" if expected == "en-GB" else "Exit")


def test_system_locale_uses_the_same_catalog_resolution(catalog_directory, monkeypatch):
    write_catalog(catalog_directory, "en-GB", {"exit": "Leave"})
    monkeypatch.setattr(i18n, "detect_system_language", lambda: "en_GB")
    translator = i18n.Translator(catalog_directory)
    assert translator.setup("system") == "en-GB"
    assert translator.translate("exit") == "Leave"


def test_switching_languages_does_not_retain_old_values(catalog_directory):
    write_catalog(catalog_directory, "en-GB", {"exit": "Leave"})
    translator = i18n.Translator(catalog_directory)
    translator.setup("en-GB")
    assert translator.translate("exit") == "Leave"
    translator.setup("en")
    assert translator.translate("exit") == "Exit"


@pytest.mark.parametrize("data", [{}, {"exit": "Повернутися ⚡"}])
def test_partial_and_unicode_catalogs(catalog_directory, data):
    write_catalog(catalog_directory, "test-locale", data)
    translator = i18n.Translator(catalog_directory)
    assert translator.setup("test-locale") == "test-locale"
    assert translator.translate("exit") == data.get("exit", "Exit")
    assert translator.translate("app_title") == "EcoFlow Monitor"


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"exit": 42}', '{"exit": " "}'])
def test_unusable_optional_catalog_falls_back_visibly(catalog_directory, content, caplog):
    language = catalog_directory / "en-GB"
    language.mkdir()
    (language / "strings.json").write_text(content, encoding="utf-8")
    translator = i18n.Translator(catalog_directory)
    assert translator.setup("en-GB") == "en"
    assert translator.translate("exit") == "Exit"
    assert "using English" in caplog.text


def test_unreadable_optional_catalog_falls_back(catalog_directory, monkeypatch):
    write_catalog(catalog_directory, "en-GB", {"exit": "Leave"})
    translator = i18n.Translator(catalog_directory)
    original_read = Path.read_text

    def read_with_denied_optional(path, *args, **kwargs):
        if path.parent.name == "en-GB":
            raise PermissionError("Synthetic permission failure")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_with_denied_optional)
    assert translator.setup("en-GB") == "en"
    assert translator.translate("exit") == "Exit"


@pytest.mark.parametrize("content", ["not JSON", "[]", "{}", '{"exit": false}'])
def test_invalid_english_is_not_hidden(catalog_directory, content):
    (catalog_directory / "en" / "strings.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        i18n.Translator(catalog_directory)


def test_missing_english_is_not_hidden(tmp_path):
    with pytest.raises(FileNotFoundError):
        i18n.Translator(tmp_path)


def test_module_api_uses_the_same_translator(catalog_directory, monkeypatch):
    write_catalog(catalog_directory, "en-GB", {"exit": "Leave"})
    monkeypatch.setattr(i18n, "_translator", i18n.Translator(catalog_directory))
    assert i18n.get_available_languages() == ("en", "en-GB")
    assert i18n.setup("en-GB") == "en-GB"
    assert i18n.get_current_language() == "en-GB"
    assert i18n.t("exit") == "Leave"


def test_native_system_locale_is_usable():
    assert isinstance(i18n.detect_system_language(), str)
    assert i18n.detect_system_language()
    assert i18n.Translator().setup("system") in i18n.get_available_languages()


def test_language_display_name_has_a_nonempty_fallback():
    assert i18n.language_display_name("en", "en")
    assert i18n.language_display_name("unregistered-locale", "en") == "unregistered-locale"
