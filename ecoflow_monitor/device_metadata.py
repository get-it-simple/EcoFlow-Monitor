from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
from statistics import median
import tempfile


logger = logging.getLogger(__name__)


def positive_number(value, maximum=1000000):
    return type(value) in (int, float) and 0 < value <= maximum


@dataclass(frozen=True)
class CapacityEstimate:
    watt_hours: float
    count: int
    spread_pct: float
    updated_utc: float


class CapacitySampler:
    def __init__(self, samples=(), saved_estimate=None):
        self.samples = list(samples)[-20:]
        self.previous = None
        self.last_accepted = None
        self._estimate = saved_estimate

    def observe(self, values, utc, now, stale_after):
        required = {"input_power", "output_power", "battery_level", "charging_state"}
        if not required <= values.keys():
            self.previous = None
            return False
        incoming, outgoing, soc = (values[key] for key in ("input_power", "output_power", "battery_level"))
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (incoming, outgoing, soc)):
            self.previous = None
            return False
        net = incoming - outgoing
        direction = 1 if net > 0 else -1
        minutes = values.get("device_charge_minutes" if direction > 0 else "device_discharge_minutes")
        expected_state = 2 if direction > 0 else 1
        if (not 10 <= soc <= 90 or abs(net) < 50 or min(incoming, outgoing) < 0
                or values["charging_state"] != expected_state or not positive_number(minutes)):
            self.previous = None
            return False
        previous, self.previous = self.previous, (now, net, soc, minutes)
        if previous is None or not 0 < now - previous[0] <= stale_after:
            return False
        if net * previous[1] <= 0 or abs(net - previous[1]) > abs(previous[1]) * 0.1:
            return False
        if (soc - previous[2]) * direction < 0 or minutes >= previous[3]:
            return False
        if self.last_accepted is not None and now - self.last_accepted < 60:
            return False
        fraction = (100 - soc if direction > 0 else soc) / 100
        capacity = abs(net) * minutes / 60 / fraction
        if not positive_number(capacity):
            return False
        self.samples = [*self.samples, (capacity, utc)][-20:]
        self.last_accepted = now
        return True

    @property
    def estimate(self):
        if len(self.samples) < 5:
            return self._estimate
        center = median(value for value, stamp in self.samples)
        inliers = [(value, stamp) for value, stamp in self.samples if abs(value - center) <= center * 0.2]
        if len(inliers) < 5:
            return self._estimate
        center = median(value for value, stamp in inliers)
        spread = (max(value for value, stamp in inliers) - min(value for value, stamp in inliers)) / center * 100
        self._estimate = CapacityEstimate(center, len(inliers), spread, max(stamp for value, stamp in inliers))
        return self._estimate


class DeviceMetadataStore:
    def __init__(self, path: Path):
        self.path = path
        self.devices = {}
        self.samplers = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("devices"), dict):
                for serial, entry in data["devices"].items():
                    if not isinstance(serial, str) or not isinstance(entry, dict):
                        continue
                    name = entry.get("name", "")
                    samples = entry.get("samples", [])
                    if not isinstance(name, str) or not isinstance(samples, list):
                        continue
                    clean = [tuple(item) for item in samples[-20:] if isinstance(item, list) and len(item) == 2
                             and positive_number(item[0]) and positive_number(item[1], 100000000000)]
                    self.devices[serial] = {"name": name[:256]}
                    estimate = entry.get("estimate")
                    restored = None
                    if (isinstance(estimate, dict) and positive_number(estimate.get("watt_hours"))
                            and type(estimate.get("count")) is int and 5 <= estimate["count"] <= 20
                            and type(estimate.get("spread_pct")) in (int, float)
                            and math.isfinite(estimate["spread_pct"]) and 0 <= estimate["spread_pct"] <= 100
                            and positive_number(estimate.get("updated_utc"), 100000000000)):
                        restored = CapacityEstimate(**{key: estimate[key] for key in ("watt_hours", "count", "spread_pct", "updated_utc")})
                    self.samplers[serial] = CapacitySampler(clean, restored)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            logger.warning("Device metadata could not be loaded")

    def name(self, serial):
        return self.devices.get(serial, {}).get("name") or serial

    def sampler(self, serial):
        return self.samplers.setdefault(serial, CapacitySampler())

    def remember(self, devices):
        for device in devices:
            self.remember_name(device.serial, device.name)

    def remember_name(self, serial, name):
        self.devices.setdefault(serial, {})["name"] = name[:256]

    def snapshot(self):
        entries = {}
        for serial in self.devices.keys() | self.samplers.keys():
            sampler = self.sampler(serial)
            estimate = sampler.estimate
            entries[serial] = {"name": self.devices.get(serial, {}).get("name", ""), "samples": list(sampler.samples),
                              "estimate": dict(vars(estimate)) if estimate is not None else None}
        return {"version": 1, "devices": entries}

    def save(self, data=None):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = self.snapshot() if data is None else data
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(data, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError):
            logger.warning("Device metadata could not be saved")
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
