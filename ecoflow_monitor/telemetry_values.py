import math

from ecoflow_monitor.polling import ATTRIBUTES, PORT_KEYS, dependencies


VALUE_UNITS = {
    "battery_level": "percent",
    "input_power": "watts",
    "output_power": "watts",
    **{key: "watts" for key in PORT_KEYS},
    **{f"anderson_{index}": "watts" for index in range(1, 65)},
    "minimum_discharge_level": "percent",
    "maximum_charge_level": "percent",
    "capacity_wh": "watt_hours",
    "remaining_time_seconds": "seconds",
    "device_discharge_seconds": "seconds",
    "device_charge_seconds": "seconds",
    "charging_state": "state",
    "ac_enabled": "boolean",
    "dc_enabled": "boolean",
}

DEVICE_TIMES = {
    "device_discharge_seconds": "device_discharge_minutes",
    "device_charge_seconds": "device_charge_minutes",
}


def value_dependencies(key: str) -> frozenset[str]:
    if key == "remaining_time_seconds":
        return dependencies("estimated_runtime")
    return dependencies(DEVICE_TIMES.get(key, key))


def app_readings(controller):
    telemetry = controller.telemetry
    if telemetry is None or not controller.access_allowed or controller.system_activity.paused:
        return {}, {}
    values = {}
    deadlines = {}

    def add(key, value, sensors):
        if type(value) not in (bool, int, float) or not math.isfinite(value) or not controller.fresh_for(sensors):
            return
        values[key] = value
        deadlines[key] = controller.polling.fresh_until(sensors) if controller.polling.samples else (
            telemetry.received_monotonic + controller.settings.polling.primary.stale_after_seconds
        )

    limits = {"minimum_discharge_level": controller.minimum, "maximum_charge_level": controller.maximum}
    for key, attribute in ATTRIBUTES.items():
        if key not in limits and key not in DEVICE_TIMES.values():
            add(key, getattr(telemetry, attribute), value_dependencies(key))
    for key, limit in limits.items():
        if limit.source != "manual":
            add(key, limit.value, value_dependencies(key))
    for key, value in telemetry.ports:
        if key in VALUE_UNITS:
            add(key, value, value_dependencies(key))
    for key, attribute in DEVICE_TIMES.items():
        minutes = getattr(telemetry, attribute)
        if minutes is not None:
            add(key, minutes * 60, value_dependencies(key))
    remaining = controller.remaining_presentation
    if remaining.hours is not None:
        add("remaining_time_seconds", remaining.hours * 3600, controller.remaining_presentation_sensors)
    if values:
        add("capacity_wh", controller.effective_capacity, frozenset())
        for key, limit in limits.items():
            if limit.source == "manual":
                add(key, limit.value, frozenset())
    return values, deadlines
