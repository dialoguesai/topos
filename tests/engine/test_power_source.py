import sys

import pytest

from topos.engine import power_source
from topos.engine.power_source import AC, BATTERY, UNKNOWN, UPS, PowerState, read_power_state


class FakeMac:
    def __init__(self, providing="AC Power", remaining=-2.0, low_power=False, thermal=0, fail=()):
        self.values = {"providing_type": providing, "time_remaining": remaining,
                       "low_power_mode": low_power, "thermal_state": thermal}
        self.fail = set(fail)

    def __getattr__(self, name):
        def call():
            if name in self.fail:
                raise OSError(name)
            return self.values[name]
        return call


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def install(**kwargs):
        fake = FakeMac(**kwargs)
        monkeypatch.setattr(power_source, "_mac", fake)
        return fake
    return install


def test_wall_power(mac):
    mac()
    assert read_power_state() == PowerState(AC, low_power_mode=False, thermal_state="nominal")
    assert not read_power_state().on_battery


def test_battery_with_estimate(mac):
    mac(providing="Battery Power", remaining=5400.0, low_power=True, thermal=2)
    state = read_power_state()
    assert state == PowerState(BATTERY, low_power_mode=True, thermal_state="serious", minutes_remaining=90)
    assert state.on_battery


def test_battery_while_macos_is_still_estimating(mac):
    mac(providing="Battery Power", remaining=-1.0)
    state = read_power_state()
    assert state.on_battery and state.minutes_remaining is None


def test_ups(mac):
    mac(providing="UPS Power")
    assert read_power_state().source == UPS


def test_unreadable_source_is_unknown_never_battery(mac):
    mac(fail={"providing_type"})
    assert read_power_state() == PowerState(UNKNOWN)
    mac(providing="")
    assert not read_power_state().on_battery


def test_a_failing_extra_keeps_the_source(mac):
    mac(providing="Battery Power", fail={"time_remaining", "low_power_mode", "thermal_state"})
    assert read_power_state() == PowerState(BATTERY)


def test_out_of_range_thermal_state(mac):
    mac(thermal=7)
    assert read_power_state().thermal_state is None


def test_not_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert read_power_state() == PowerState(UNKNOWN)


@pytest.mark.skipif(sys.platform != "darwin", reason="reads this Mac's own power source")
def test_reads_this_mac(monkeypatch):
    monkeypatch.setattr(power_source, "_mac", None)
    state = read_power_state()
    assert state.source in {AC, BATTERY, UPS}
    assert state.thermal_state in {"nominal", "fair", "serious", "critical"}
    assert isinstance(state.low_power_mode, bool)
