from dataclasses import replace

import pytest

from ecoflow_monitor.domain import AutomationGate, AutomationRule, Telemetry, resolve_limit, runtime_hours, select_rule


def sample(**changes):
    return replace(Telemetry(0, 200, 80, 1000, 100), **changes)


def test_runtime_uses_output_and_discharge_floor():
    assert runtime_hours(sample(), 2000, 20, True) == 6
    assert runtime_hours(sample(soc_pct=10), 2000, 20, True) == 0


@pytest.mark.parametrize("changes,capacity,floor,fresh", [
    ({"input_w": 1}, 2000, 20, True), ({"output_w": 0}, 2000, 20, True),
    ({}, 2000, None, True), ({}, 2000, 20, False), ({}, 0, 20, True),
    ({"output_w": float("nan")}, 2000, 20, True), ({"soc_pct": 101}, 2000, 20, True),
])
def test_runtime_unavailable_conditions(changes, capacity, floor, fresh):
    assert runtime_hours(sample(**changes), capacity, floor, fresh) is None


def test_limits_keep_zero_and_api_precedence():
    assert resolve_limit(0, 20).value == 0
    assert resolve_limit(0, 20).source == "api"
    assert resolve_limit(None, 15).source == "manual"
    assert resolve_limit(None, None).source == "unknown"


def test_application_rules_win_and_full_paths_are_case_insensitive():
    first = AutomationRule("a", 3, executable=r"C:\Games\Game.exe", input_condition="absent")
    second = AutomationRule("b", 5, executable=r"D:\Games\Game.exe")
    system = AutomationRule("s", 1)
    assert select_rule(sample(), frozenset({"c:/games/GAME.exe"}), (first, second), (system,)) == first
    assert select_rule(sample(), frozenset({"d:/games/game.exe"}), (first, second), (system,)) == second
    assert select_rule(sample(), frozenset(), (first,), (system,)) == system


def test_first_eligible_condition_and_upper_boundary():
    rules = (AutomationRule("a", 5, max_soc=20), AutomationRule("b", 3, min_soc=20, max_soc=100))
    assert select_rule(sample(soc_pct=19), frozenset(), (), rules) == rules[0]
    assert select_rule(sample(soc_pct=20), frozenset(), (), rules) == rules[1]
    assert select_rule(sample(soc_pct=100), frozenset(), (), rules) == rules[1]
    assert select_rule(sample(), frozenset(), (), ()) is None


def test_gate_requires_stability_and_does_not_repeat_unconfirmed_request():
    gate = AutomationGate()
    assert gate.evaluate(5, 100, 1) is None
    assert gate.evaluate(5, 104.9, 1) is None
    assert gate.evaluate(5, 105, 1) == 5
    gate.dispatched(5, 105, True)
    assert gate.evaluate(5, 200, 1) is None
    assert gate.evaluate(5, 300, None) is None


def test_gate_external_change_and_automatic_cooldown():
    gate = AutomationGate()
    gate.evaluate(5, 100, 1)
    gate.dispatched(5, 105, True)
    assert gate.evaluate(5, 106, 5) is None
    assert gate.evaluate(5, 107, 2) is None
    assert gate.evaluate(5, 112, 2) is None
    assert gate.evaluate(5, 115, 2) == 5


def test_failed_dispatch_is_not_retried_until_revalidated():
    gate = AutomationGate()
    gate.evaluate(5, 0, None)
    gate.dispatched(5, 5, False)
    assert gate.evaluate(5, 100, None) is None
    gate.suspend()
    assert gate.evaluate(5, 101, None) is None
    assert gate.evaluate(5, 106, None) == 5
