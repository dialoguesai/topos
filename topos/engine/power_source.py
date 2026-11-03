"""What the host is running on: wall power or battery, Low Power Mode, and the thermal state.

Measured on 3 Nov 2026 (36 GiB M3 Pro, Ollama 0.40.14, Low Power Mode off): battery by itself costs no speed. Ten
minutes of back-to-back 27B requests lost the same ~15% of prompt speed on wall power and on battery. It costs
charge, about 1.6% a minute under that load. An hour the same day when every local model ran about 10x slow fits
thermal throttling instead (speed falling within one request, a macOS thermal emergency logged as it ended). So a
rule for the local models needs all three readings here: the source, the charge left and the thermal state.

macOS only, through IOKit's power-source API and ``NSProcessInfo`` (ctypes, no new dependency). One read costs
about 40 microseconds for the source and under 1 ms in all; ``pmset -g batt`` costs about 30 ms for a fork. Anywhere
else, or if a call fails, the source is ``unknown``. A caller must treat ``unknown`` like wall power: a desktop has no
battery and must never be served as one.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("topos.engine.power")

AC = "ac"
BATTERY = "battery"
UPS = "ups"
UNKNOWN = "unknown"

# IOPSGetProvidingPowerSourceType's strings (IOPSKeys.h).
_SOURCE_TYPES = {"AC Power": AC, "Battery Power": BATTERY, "UPS Power": UPS}
# NSProcessInfoThermalState, in order.
_THERMAL_STATES = ("nominal", "fair", "serious", "critical")
# IOPSGetTimeRemainingEstimate: -2 on wall power, -1 while macOS is still estimating, else seconds.
_UNLIMITED = -2.0
_UTF8 = 0x08000100


@dataclass(frozen=True)
class PowerState:
    source: str
    low_power_mode: Optional[bool] = None
    thermal_state: Optional[str] = None
    minutes_remaining: Optional[int] = None

    @property
    def on_battery(self) -> bool:
        return self.source == BATTERY


class _MacPower:
    """The four calls this module makes, loaded once. Tests replace it with a fake."""

    def __init__(self) -> None:
        iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        self._cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        iokit.IOPSGetTimeRemainingEstimate.restype = ctypes.c_double
        iokit.IOPSCopyPowerSourcesInfo.restype = ctypes.c_void_p
        iokit.IOPSGetProvidingPowerSourceType.restype = ctypes.c_void_p
        iokit.IOPSGetProvidingPowerSourceType.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        get_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        self._get_bool = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(send)
        self._get_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(send)
        self._process_info = get_object(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
        self._low_power = objc.sel_registerName(b"isLowPowerModeEnabled")
        self._thermal = objc.sel_registerName(b"thermalState")
        self._iokit = iokit

    def providing_type(self) -> str:
        snapshot = self._iokit.IOPSCopyPowerSourcesInfo()
        if not snapshot:
            return ""
        try:
            name = self._iokit.IOPSGetProvidingPowerSourceType(snapshot)
            buffer = ctypes.create_string_buffer(64)
            if not name or not self._cf.CFStringGetCString(name, buffer, len(buffer), _UTF8):
                return ""
            return buffer.value.decode()
        finally:
            self._cf.CFRelease(snapshot)

    def time_remaining(self) -> float:
        return float(self._iokit.IOPSGetTimeRemainingEstimate())

    def low_power_mode(self) -> bool:
        return bool(self._get_bool(self._process_info, self._low_power))

    def thermal_state(self) -> int:
        return int(self._get_long(self._process_info, self._thermal))


_mac: Any = None


def _mac_power() -> Any:
    global _mac
    if _mac is None:
        _mac = _MacPower()
    return _mac


def read_power_state() -> PowerState:
    """The host's power state now. Never raises: anything it cannot read is ``None``, or ``unknown`` for the source."""
    if sys.platform != "darwin":
        return PowerState(UNKNOWN)
    try:
        mac = _mac_power()
        source = _SOURCE_TYPES.get(mac.providing_type(), UNKNOWN)
    except Exception as exc:
        logger.debug("power source unreadable: %s", exc)
        return PowerState(UNKNOWN)
    minutes = None
    try:
        remaining = mac.time_remaining()
        if source == BATTERY and remaining >= 0:
            minutes = int(remaining // 60)
    except Exception as exc:
        logger.debug("battery estimate unreadable: %s", exc)
    low_power = None
    try:
        low_power = mac.low_power_mode()
    except Exception as exc:
        logger.debug("low power mode unreadable: %s", exc)
    thermal = None
    try:
        level = mac.thermal_state()
        thermal = _THERMAL_STATES[level] if 0 <= level < len(_THERMAL_STATES) else None
    except Exception as exc:
        logger.debug("thermal state unreadable: %s", exc)
    return PowerState(source, low_power, thermal, minutes)
