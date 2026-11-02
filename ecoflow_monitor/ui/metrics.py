from textwrap import fill

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import number


API_FIELDS = {
    "battery_level": "cmsBattSoc",
    "input_power": "powInSumW",
    "output_power": "powOutSumW",
    "minimum_discharge_level": "cmsMinDsgSoc",
    "maximum_charge_level": "cmsMaxChgSoc",
    "ac_input": "powGetAcIn",
    "pv_input": "powGetPv",
    "ac_output": "powGetAcOut",
    "dc_output": "powGet12v",
    "usb_c1": "powGetTypec1",
    "usb_c2": "powGetTypec2",
    "usb_c3": "powGetTypec3",
    "usb_a1": "powGetQcusb1",
    "usb_a2": "powGetQcusb2",
    "anderson": "powGet12vList.powGet12vItem",
}


def metric_label(key: str) -> str:
    if key.startswith("anderson_"):
        return t("anderson") + " " + key.removeprefix("anderson_")
    return t(key)


def metric_tooltip(key: str, controller=None) -> str:
    base = "anderson" if key.startswith("anderson_") else key
    description = "usb_power_help" if base.startswith("usb_") else base + "_help"
    parts = [metric_label(key), fill(t(description), width=85, break_long_words=False, break_on_hyphens=False)]
    if base in API_FIELDS:
        parts.append(t("metric_api_field").format(field=API_FIELDS[base]))
    if controller is not None and base in {"minimum_discharge_level", "maximum_charge_level"}:
        limit = controller.minimum if base == "minimum_discharge_level" else controller.maximum
        parts.append(t("metric_source").format(source=t("source_" + limit.source), value=number(limit.value, "%")))
        if limit.source == "manual":
            parts.append(t("manual_limit_hint"))
    return "\n".join(parts)


def rule_tooltip(key: str) -> str:
    metric = "input_power" if key == "rule_input" else "battery_level"
    return metric_tooltip(metric) + "\n" + fill(t(key + "_help"), width=85, break_long_words=False, break_on_hyphens=False)
