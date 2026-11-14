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


def metric_label(key: str, controller=None) -> str:
    if key == "estimated_runtime" and controller is not None and controller.charging:
        return t("estimated_charge_time")
    if key.startswith("anderson_"):
        return t("anderson") + " " + key.removeprefix("anderson_")
    return t(key)


def metric_tooltip(key: str, controller=None) -> str:
    base = "anderson" if key.startswith("anderson_") else key
    if base == "estimated_runtime" and controller is not None and controller.charging:
        base = "estimated_charge_time"
    description = "usb_power_help" if base.startswith("usb_") else base + "_help"
    parts = [metric_label(key, controller), fill(t(description), width=85, break_long_words=False, break_on_hyphens=False)]
    if base in API_FIELDS:
        parts.append(t("metric_api_field").format(field=API_FIELDS[base]))
    if base in {"ac_input", "pv_input", "ac_output", "dc_output", "anderson"} or base.startswith("usb_"):
        parts.append(t("sensor_signed_power"))
    if controller is not None:
        from datetime import datetime
        from ecoflow_monitor.polling import dependencies
        sensors = controller.remaining_time_sensors if key == "estimated_runtime" else dependencies(key)
        stamps = [sample.received_utc for sensor in sensors if (sample := controller.polling.samples.get(sensor)) is not None and sample.received_utc is not None]
        if stamps:
            parts.append(t("last_update").format(timestamp=datetime.fromtimestamp(min(stamps)).astimezone().strftime("%Y-%m-%d %H:%M:%S")))
    if controller is not None and base in {"minimum_discharge_level", "maximum_charge_level"}:
        limit = controller.minimum if base == "minimum_discharge_level" else controller.maximum
        parts.append(t("metric_source").format(source=t("source_" + limit.source), value=number(limit.value, "%")))
        if limit.source == "manual":
            parts.append(t("manual_limit_hint"))
    return "\n".join(parts)


def rule_tooltip(key: str) -> str:
    metric = "input_power" if key == "rule_input" else "battery_level"
    return metric_tooltip(metric) + "\n" + fill(t(key + "_help"), width=85, break_long_words=False, break_on_hyphens=False)
