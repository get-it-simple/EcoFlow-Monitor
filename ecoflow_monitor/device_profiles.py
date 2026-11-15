from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import re
import tempfile


CORE = frozenset(("input_power", "output_power", "battery_level"))
PORTS = ("ac_input", "pv_input", "ac_output", "dc_output", "usb_c1", "usb_c2", "usb_c3", "usb_a1", "usb_a2")
SENSORS = (
    "input_power", "output_power", "battery_level", "minimum_discharge_level", "maximum_charge_level",
    *PORTS, "anderson", "ac_enabled", "dc_enabled", "charging_state", "device_discharge_minutes", "device_charge_minutes",
)
READ_OPERATIONS = frozenset({("POST", "/iot-open/sign/device/quota"), ("GET", "/iot-open/sign/device/quota/all")})
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 64
BASELINE = "builtin:delta3max"
_MISSING = object()


class ProfileError(ValueError):
    pass


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def source_value(data, path):
    value = data
    for part in path:
        if isinstance(part, str) and isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif type(part) is int and isinstance(value, list) and part < len(value):
            value = value[part]
        else:
            return _MISSING
    return value


@dataclass(frozen=True)
class FieldMapping:
    sources: tuple[tuple[str | int, ...], ...]
    quotas: tuple[str, ...]
    scale: float = 1
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple[tuple[str, bool | int], ...] = ()
    otherwise: bool | None = None

    def decode(self, data, sensor):
        raw = [source_value(data, path) for path in self.sources]
        if any(value is _MISSING for value in raw):
            return "missing", None
        if self.enum:
            value = number(raw[0])
            if value is None or value < 0 or not value.is_integer():
                return "invalid", None
            result = dict(self.enum).get(str(int(value)), self.otherwise)
            return ("valid", result) if result is not None else ("invalid", None)
        sequence = raw[0] if sensor == "anderson" and isinstance(raw[0], list) and len(raw[0]) <= 64 else raw
        if sensor == "anderson" and not isinstance(raw[0], list):
            return "invalid", None
        values = [number(item) for item in sequence]
        if any(value is None for value in values):
            return "invalid", None
        values = [value * self.scale for value in values]
        result = tuple(values) if sensor == "anderson" else sum(values)
        checked = values if sensor == "anderson" else (result,)
        upper = 100 if sensor in {"battery_level", "minimum_discharge_level", "maximum_charge_level"} else 2 if sensor == "charging_state" else None
        lower = None if sensor in {*PORTS, "anderson"} else 0
        if any(not math.isfinite(value) or lower is not None and value < lower or upper is not None and value > upper
               or self.minimum is not None and value < self.minimum or self.maximum is not None and value > self.maximum for value in checked):
            return "invalid", None
        if sensor == "charging_state":
            return ("valid", int(result)) if result.is_integer() else ("invalid", None)
        return "valid", result

    @classmethod
    def from_dict(cls, data, sensor):
        allowed = {"sources", "quotas", "scale", "minimum", "maximum", "enum", "otherwise"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ProfileError("device_profile_invalid")
        sources = data.get("sources")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
            raise ProfileError("device_profile_invalid")
        paths = []
        for source in sources:
            path = [source] if isinstance(source, str) else source
            if (not isinstance(path, list) or not 1 <= len(path) <= 8
                    or any(not (isinstance(part, str) and 0 < len(part) <= 256) and not (type(part) is int and 0 <= part <= 63) for part in path)):
                raise ProfileError("device_profile_invalid")
            paths.append(tuple(path))
        quotas = data.get("quotas", [source for source in sources if isinstance(source, str)])
        if not isinstance(quotas, list) or not 1 <= len(quotas) <= 8 or any(not isinstance(item, str) or not 0 < len(item) <= 256 for item in quotas):
            raise ProfileError("device_profile_invalid")
        scale = data.get("scale", 1)
        minimum, maximum = data.get("minimum"), data.get("maximum")
        if type(scale) not in (int, float) or number(scale) is None or scale <= 0:
            raise ProfileError("device_profile_invalid")
        if any(value is not None and (type(value) not in (int, float) or number(value) is None) for value in (minimum, maximum)):
            raise ProfileError("device_profile_invalid")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ProfileError("device_profile_invalid")
        enum = data.get("enum", {})
        otherwise = data.get("otherwise")
        if not isinstance(enum, dict) or len(enum) > 128 or otherwise is not None and type(otherwise) is not bool:
            raise ProfileError("device_profile_invalid")
        if enum and (sensor not in {"ac_enabled", "dc_enabled", "charging_state"} or len(paths) != 1 or scale != 1):
            raise ProfileError("device_profile_invalid")
        for key, value in enum.items():
            if not isinstance(key, str) or re.fullmatch(r"0|[1-9][0-9]{0,9}", key) is None:
                raise ProfileError("device_profile_invalid")
            if sensor == "charging_state":
                if type(value) is not int or value not in (0, 1, 2) or otherwise is not None:
                    raise ProfileError("device_profile_invalid")
            elif type(value) is not bool:
                raise ProfileError("device_profile_invalid")
        if sensor in {"ac_enabled", "dc_enabled"} and not enum or otherwise is not None and not enum:
            raise ProfileError("device_profile_invalid")
        if sensor == "anderson" and len(paths) != 1:
            raise ProfileError("device_profile_invalid")
        return cls(tuple(paths), tuple(quotas), scale, minimum, maximum, tuple(enum.items()), otherwise)


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    name: str
    revision: int
    evidence: str
    method: str
    endpoint: str
    fields: tuple[tuple[str, FieldMapping], ...]
    source: str = "builtin"
    model_ids: tuple[str, ...] = ()
    note: str = ""
    model_names: tuple[str, ...] = ()

    @property
    def reference(self):
        return self.source + ":" + self.id

    @property
    def usable(self):
        return CORE <= dict(self.fields).keys()

    def quotas(self, sensors):
        return tuple(dict.fromkeys(quota for sensor, mapping in self.fields if sensor in sensors for quota in mapping.quotas))

    def decode(self, data, sensors):
        mappings = dict(self.fields)
        values, missing, invalid = [], [], []
        for sensor in sensors:
            status, value = mappings[sensor].decode(data, sensor) if sensor in mappings else ("missing", None)
            if status == "valid":
                values.append((sensor, value))
            elif status == "missing":
                missing.append(sensor)
            else:
                invalid.append(sensor)
        return tuple(values), tuple(missing), tuple(invalid)

    @classmethod
    def from_dict(cls, data, source):
        allowed = {"id", "name", "revision", "evidence", "read", "fields", "model_ids", "model_names", "note"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ProfileError("device_profile_invalid")
        identifier, name = data.get("id"), data.get("name")
        if not isinstance(identifier, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier) is None:
            raise ProfileError("device_profile_invalid")
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or any(ord(char) < 32 for char in name):
            raise ProfileError("device_profile_invalid")
        revision = data.get("revision", 1)
        evidence = data.get("evidence", "custom")
        read, fields_data = data.get("read"), data.get("fields")
        if type(revision) is not int or not 1 <= revision <= 1000000 or evidence not in {"working", "documentation", "custom"}:
            raise ProfileError("device_profile_invalid")
        if not isinstance(read, dict) or set(read) != {"method", "path"} or (read.get("method"), read.get("path")) not in READ_OPERATIONS:
            raise ProfileError("device_profile_endpoint_error")
        if not isinstance(fields_data, dict) or not fields_data or set(fields_data) - set(SENSORS):
            raise ProfileError("device_profile_invalid")
        mappings = tuple((sensor, FieldMapping.from_dict(value, sensor)) for sensor, value in fields_data.items())
        model_ids, note = data.get("model_ids", []), data.get("note", "")
        if not isinstance(model_ids, list) or len(model_ids) > 32 or any(not isinstance(value, str) or not 0 < len(value) <= 128 for value in model_ids):
            raise ProfileError("device_profile_invalid")
        if not isinstance(note, str) or len(note) > 2048:
            raise ProfileError("device_profile_invalid")
        model_names = data.get("model_names", [])
        if (not isinstance(model_names, list) or len(model_names) > 32
                or any(not isinstance(value, str) or not value.strip() or len(value) > 128
                       or any(ord(char) < 32 for char in value) for value in model_names)):
            raise ProfileError("device_profile_invalid")
        return cls(identifier, name, revision, evidence if source == "builtin" else "documentation" if evidence == "documentation" else "custom",
                   read["method"], read["path"], mappings, source, tuple(model_ids), note, tuple(model_names))


@dataclass(frozen=True)
class ProfileCatalog:
    profiles: tuple[DeviceProfile, ...]
    examples: tuple[dict, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()
    custom_enabled: bool = False

    def resolve(self, reference, model_id="", model_name=""):
        if reference:
            return next((profile for profile in self.profiles if profile.reference == reference and profile.usable), None)
        matches = [profile for profile in self.profiles if model_id and model_id in profile.model_ids and profile.usable]
        if matches:
            return matches[0] if len(matches) == 1 else None
        name = " ".join(model_name.split()).casefold()
        name = re.sub(r"\s*-\s*[0-9]+$", "", name).strip()
        matches = [profile for profile in self.profiles if profile.usable and name
                   and any(name == " ".join(alias.split()).casefold() for alias in profile.model_names)]
        return matches[0] if len(matches) == 1 else None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError("device_profile_duplicate")
        result[key] = value
    return result


def parse_catalog(content, source):
    try:
        data = json.loads(content, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) - {"version", "profiles", "examples"} or type(data.get("version")) is not int or data["version"] != 1:
            raise ProfileError("device_profile_invalid")
        profiles = data.get("profiles")
        examples = data.get("examples", [])
        if not isinstance(profiles, list) or len(profiles) > 64 or not isinstance(examples, list) or len(examples) > 64:
            raise ProfileError("device_profile_invalid")
        result = tuple(DeviceProfile.from_dict(item, source) for item in profiles)
        if len({profile.id for profile in result}) != len(result):
            raise ProfileError("device_profile_duplicate")
        for example in examples:
            DeviceProfile.from_dict(example, "user")
        return ProfileCatalog(result, tuple(examples))
    except (ValueError, TypeError, RecursionError) as error:
        raise ProfileError(str(error) if isinstance(error, ProfileError) else "device_profile_invalid") from None


@lru_cache(maxsize=1)
def builtin_catalog():
    return parse_catalog(files("ecoflow_monitor.profile_data").joinpath("devices.json").read_text(encoding="utf-8"), "builtin")


def baseline_profile():
    return builtin_catalog().resolve(BASELINE)


def load_catalog(directory: Path, enabled: bool):
    builtin = builtin_catalog()
    if not enabled:
        return builtin
    entries, errors = [], []
    try:
        paths = sorted(directory.glob("*.json")) if directory.exists() else []
        if len(paths) > MAX_FILES:
            return replace(builtin, errors=(("", "device_profile_file_limit"),), custom_enabled=True)
    except OSError:
        return replace(builtin, errors=(("", "device_profile_read_error"),), custom_enabled=True)
    total = 0
    for path in paths:
        try:
            if path.is_symlink():
                raise ProfileError("device_profile_read_error")
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            total += len(content)
            if len(content) > MAX_FILE_BYTES or total > MAX_FILE_BYTES * 8:
                raise ProfileError("device_profile_file_limit")
            catalog = parse_catalog(content.decode("utf-8"), "user")
            entries.append((path.name, catalog.profiles))
        except (OSError, UnicodeError, ProfileError) as error:
            errors.append((path.name, str(error) if isinstance(error, ProfileError) else "device_profile_read_error"))
    counts = {}
    for filename, profiles in entries:
        for profile in profiles:
            counts[profile.id] = counts.get(profile.id, 0) + 1
    valid = []
    for filename, profiles in entries:
        if any(counts[profile.id] > 1 for profile in profiles):
            errors.append((filename, "device_profile_duplicate"))
        else:
            valid.extend(profiles)
    return ProfileCatalog((*builtin.profiles, *valid), builtin.examples, tuple(errors), True)


def write_example(directory: Path, identifier: str):
    example = next((item for item in builtin_catalog().examples if item["id"] == identifier), None)
    if example is None:
        raise ProfileError("device_profile_invalid")
    content = json.dumps({"version": 1, "profiles": [example]}, ensure_ascii=False, indent=2) + "\n"
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / (identifier + ".json")
        if destination.exists():
            raise ProfileError("device_profile_write_error")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".profile-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    except OSError:
        raise ProfileError("device_profile_write_error") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return destination
