from dataclasses import asdict, dataclass, field, replace
import json
import math
import re
from importlib.resources import files

from ecoflow_monitor.domain import AutomationRule, Telemetry, select_rule
from ecoflow_monitor.telemetry_values import VALUE_UNITS


IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]+")
CONDITIONS = frozenset({"application", "input", "soc"})
VALUE_TOKEN = re.compile(r"(?P<option>--?[A-Za-z][A-Za-z0-9_-]*=)?\{value:(?P<key>[a-z0-9_]+)}")


class ApplicationValueUnavailable(ValueError):
    pass


def identity(value: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid integration identifier")


@dataclass(frozen=True)
class CliCommand:
    id: str
    label: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class Reaction:
    enabled: bool = False
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationProfile:
    id: str
    label: str
    activation: Reaction = field(default_factory=Reaction)
    deactivation: Reaction = field(default_factory=Reaction)
    slot: int | None = None
    power_pct: float | None = None
    technical_details: str = ""


@dataclass(frozen=True)
class IntegrationRule:
    id: str
    profile_id: str
    enabled: bool = True
    executable: str = ""
    input_condition: str = "any"
    min_soc: float | None = None
    max_soc: float | None = None

    def validate(self) -> None:
        identity(self.id)
        identity(self.profile_id)
        AutomationRule(self.id, 1, self.enabled, self.executable, self.input_condition, self.min_soc, self.max_soc).validate()


def template_tokens(template: str, *, app_values=False) -> tuple[str, ...]:
    if not isinstance(template, str) or not template.strip() or "\0" in template:
        raise ValueError("Invalid command template")
    tokens = []
    token = ""
    quoted = False
    started = False
    index = 0
    while index < len(template):
        char = template[index]
        if char == "\\":
            end = index
            while end < len(template) and template[end] == "\\":
                end += 1
            count = end - index
            if end < len(template) and template[end] == '"':
                token += "\\" * (count // 2)
                if count % 2:
                    token += '"'
                else:
                    quoted = not quoted
                index = end + 1
            else:
                token += "\\" * count
                index = end
            started = True
            continue
        if char == '"':
            quoted = not quoted
            started = True
        elif char.isspace() and not quoted:
            if started:
                tokens.append(token)
                token = ""
                started = False
        else:
            token += char
            started = True
        index += 1
    if quoted:
        raise ValueError("Unclosed command quote")
    if started:
        tokens.append(token)
    if not tokens or tokens[0] != "{app}":
        raise ValueError("Command must begin with {app}")
    for token in tokens[1:]:
        if "{" in token or "}" in token:
            value = VALUE_TOKEN.fullmatch(token) if app_values else None
            if value is not None and value["key"] in VALUE_UNITS:
                continue
            if not re.fullmatch(r"\{cli:[a-zA-Z0-9_-]+}", token):
                raise ValueError("Unknown command token")
    return tuple(tokens)


def template_value_keys(template: str) -> frozenset[str]:
    if "{value:" not in template:
        return frozenset()
    return frozenset(match["key"] for token in template_tokens(template, app_values=True)[1:]
                     if (match := VALUE_TOKEN.fullmatch(token)) is not None)


def reaction_value_keys(*reactions: Reaction) -> frozenset[str]:
    return frozenset(key for reaction in reactions if reaction.enabled for template in reaction.commands
                     for key in template_value_keys(template))


def cli_value(value) -> str:
    if type(value) in (bool, int):
        return str(int(value))
    if type(value) is float and math.isfinite(value):
        return str(value)
    raise ApplicationValueUnavailable("Unavailable application value")


@dataclass(frozen=True)
class IntegrationDefinition:
    id: str
    label: str
    driver: str = "cli"
    enabled: bool = False
    auto_enabled: bool = False
    executable: str = ""
    allow_launch: bool = False
    gpu_id: str = ""
    conditions: tuple[str, ...] = ("application", "input", "soc")
    mapping: tuple[CliCommand, ...] = ()
    profiles: tuple[IntegrationProfile, ...] = ()
    application_rules: tuple[IntegrationRule, ...] = ()
    system_rules: tuple[IntegrationRule, ...] = ()

    def validate(self) -> None:
        identity(self.id)
        if self.driver not in {"cli", "afterburner"}:
            raise ValueError("Unknown integration driver")
        if any(type(value) is not bool for value in (self.enabled, self.auto_enabled, self.allow_launch)):
            raise ValueError("Invalid integration switch")
        if any(not isinstance(value, str) for value in (self.label, self.executable, self.gpu_id)) or not self.label.strip():
            raise ValueError("Invalid integration text")
        if len(set(self.conditions)) != len(self.conditions) or not set(self.conditions) <= CONDITIONS:
            raise ValueError("Unknown automation condition")
        commands = {}
        for command in self.mapping:
            identity(command.id)
            if command.id in commands or not isinstance(command.label, str) or not command.label.strip():
                raise ValueError("Duplicate or invalid CLI mapping")
            if not command.arguments or any(not isinstance(arg, str) or "\0" in arg for arg in command.arguments):
                raise ValueError("Invalid CLI arguments")
            commands[command.id] = command
        profile_ids = set()
        slots = set()
        for profile in self.profiles:
            identity(profile.id)
            if profile.id in profile_ids or not isinstance(profile.label, str) or not profile.label.strip():
                raise ValueError("Duplicate or invalid profile")
            profile_ids.add(profile.id)
            if not isinstance(profile.technical_details, str):
                raise ValueError("Invalid profile technical details")
            if profile.power_pct is not None and (type(profile.power_pct) not in (int, float) or not math.isfinite(profile.power_pct) or not 1 <= profile.power_pct <= 200):
                raise ValueError("Invalid descriptive power")
            if self.driver == "afterburner":
                if type(profile.slot) is not int or profile.slot not in range(1, 6) or profile.slot in slots:
                    raise ValueError("Invalid Afterburner slot")
                slots.add(profile.slot)
            for reaction in (profile.activation, profile.deactivation):
                if type(reaction.enabled) is not bool:
                    raise ValueError("Invalid reaction switch")
                for template in reaction.commands:
                    tokens = template_tokens(template, app_values=self.driver == "cli")
                    for token in tokens[1:]:
                        if token.startswith("{cli:") and token[5:-1] not in commands:
                            raise ValueError("Missing CLI mapping")
                    if self.driver == "afterburner":
                        args = self.expand(template, "app")[1:]
                        if len(args) != 1 or args[0] not in {f"-profile{slot}" for slot in range(1, 6)}:
                            raise ValueError("Afterburner supports saved slots only")
        ids = set()
        for rules, application in ((self.application_rules, True), (self.system_rules, False)):
            if application and rules and "application" not in self.conditions:
                raise ValueError("Application rules unavailable")
            for rule in rules:
                rule.validate()
                if rule.id in ids or rule.profile_id not in profile_ids or bool(rule.executable) != application:
                    raise ValueError("Invalid rule reference")
                ids.add(rule.id)
                if rule.input_condition != "any" and "input" not in self.conditions:
                    raise ValueError("Input condition unavailable")
                if (rule.min_soc is not None or rule.max_soc is not None) and "soc" not in self.conditions:
                    raise ValueError("SOC condition unavailable")
        if self.driver == "afterburner" and self.allow_launch:
            raise ValueError("Afterburner must already be running")

    def app_value_keys(self) -> frozenset[str]:
        return reaction_value_keys(*(reaction for profile in self.profiles for reaction in (profile.activation, profile.deactivation)))

    def expand(self, template: str, executable: str, *, values=None, preview=False) -> tuple[str, ...]:
        mapping = {command.id: command.arguments for command in self.mapping}
        args = [executable]
        for token in template_tokens(template, app_values=self.driver == "cli")[1:]:
            if token.startswith("{cli:"):
                try:
                    args.extend(mapping[token[5:-1]])
                except KeyError:
                    raise ValueError("Missing CLI mapping") from None
            elif (match := VALUE_TOKEN.fullmatch(token)) is not None and not preview:
                if values is None or match["key"] not in values:
                    raise ApplicationValueUnavailable("Unavailable application value")
                args.append((match["option"] or "") + cli_value(values[match["key"]]))
            else:
                args.append(token)
        return tuple(args)

    def select(self, telemetry: Telemetry, running: frozenset[str]) -> IntegrationRule | None:
        return select_rule(telemetry, running, self.application_rules, self.system_rules)

    def to_dict(self) -> dict:
        self.validate()
        return json.loads(json.dumps({"version": 1, **asdict(self)}))

    @classmethod
    def from_dict(cls, data: dict) -> "IntegrationDefinition":
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Unsupported integration format")
        values = dict(data)
        values.pop("version")
        def sequence(value):
            if not isinstance(value, (list, tuple)):
                raise ValueError("Integration arrays must be lists")
            return tuple(value)
        values["conditions"] = sequence(values.get("conditions", ("application", "input", "soc")))
        values["mapping"] = tuple(CliCommand(item["id"], item["label"], sequence(item["arguments"])) for item in sequence(values.get("mapping", ())))
        profiles = []
        for item in sequence(values.get("profiles", ())):
            profile = dict(item)
            for name in ("activation", "deactivation"):
                reaction = profile.get(name, {})
                if not isinstance(reaction, dict) or set(reaction) - {"enabled", "commands"}:
                    raise ValueError("Invalid reaction fields")
                profile[name] = Reaction(reaction.get("enabled", False), sequence(reaction.get("commands", ())))
            profiles.append(IntegrationProfile(**profile))
        values["profiles"] = tuple(profiles)
        for name in ("application_rules", "system_rules"):
            values[name] = tuple(IntegrationRule(**item) for item in sequence(values.get(name, ())))
        module = cls(**values)
        module.validate()
        return module


def afterburner_example() -> IntegrationDefinition:
    resource = files("ecoflow_monitor.integration_examples").joinpath("afterburner.json")
    return IntegrationDefinition.from_dict(json.loads(resource.read_text(encoding="utf-8")))


def migrate_afterburner(msi: dict, automation: dict) -> IntegrationDefinition:
    example = afterburner_example()
    legacy_profiles = msi.get("profiles", ())
    if "profiles" in msi and (len(legacy_profiles) != 5 or any(type(item["slot"]) is not int or not isinstance(item.get("label", ""), str) for item in legacy_profiles)):
        raise ValueError("Invalid legacy profiles")
    descriptions = {item["slot"]: item for item in legacy_profiles}
    profiles = tuple(replace(profile, label=descriptions.get(profile.slot, {}).get("label") or profile.label,
                             power_pct=descriptions.get(profile.slot, {}).get("power_pct", profile.power_pct)) for profile in example.profiles)
    if descriptions and (len(descriptions) != 5 or set(descriptions) != set(range(1, 6))):
        raise ValueError("Invalid legacy profile slots")
    def rules(name):
        result = []
        for item in automation.get(name, ()):
            rule = AutomationRule(**item)
            rule.validate()
            values = asdict(rule)
            values.pop("slot")
            result.append(IntegrationRule(profile_id=f"profile_{rule.slot}", **values))
        return tuple(result)
    return replace(example, enabled=msi.get("enabled", False), auto_enabled=msi.get("auto", False),
                   executable=msi.get("executable", ""), gpu_id=msi.get("gpu_id", ""), profiles=profiles,
                   application_rules=rules("applications"), system_rules=rules("system"))


def hotkey_actions(modules: tuple[IntegrationDefinition, ...]) -> tuple[str, ...]:
    actions = ["toggle_monitor", "toggle_mini", "open_dashboard", "recover_widgets", "toggle_main"]
    for module in modules:
        actions.append(f"auto:{module.id}")
        actions.extend(f"profile:{module.id}:{profile.id}" for profile in module.profiles)
    return tuple(actions)


def hotkey_label(action: str, modules: tuple[IntegrationDefinition, ...]) -> str:
    from ecoflow_monitor.i18n import t
    if ":" not in action:
        return t(action)
    parts = action.split(":")
    module = next(item for item in modules if item.id == parts[1])
    if parts[0] == "auto":
        return module.label + " · " + t("automatic_mode")
    profile = next(item for item in module.profiles if item.id == parts[2])
    return module.label + " · " + profile.label
