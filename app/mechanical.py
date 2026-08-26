"""Structural and mechanical detail, pulled out of the decode we already have.

vPIC's DecodeVinValues returns ~150 fields and the cache has been storing all
of them in `raw_json` since day one -- the service just never surfaced more
than four. So this is pure projection: no new HTTP call, no new cache column,
and it works retroactively on every VIN already cached.

Fields are grouped rather than dumped flat because an underwriter reads them
in groups: what moves the car, what it is built out of, and what protects the
occupants. The third group is also what the underwriting rules read, so the
grouping is not only cosmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# vPIC key -> human label. Order is display order.
POWERTRAIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("EngineHP", "Horsepower"),
    ("EngineCylinders", "Cylinders"),
    ("DisplacementL", "Displacement (L)"),
    ("EngineConfiguration", "Configuration"),
    ("EngineManufacturer", "Engine manufacturer"),
    ("FuelTypePrimary", "Fuel type"),
    ("FuelInjectionType", "Fuel injection"),
    ("TurboBoost", "Turbo"),
    ("TransmissionStyle", "Transmission"),
    ("TransmissionSpeeds", "Transmission speeds"),
    ("DriveType", "Drive type"),
    ("OtherEngineInfo", "Engine notes"),
)

STRUCTURE_FIELDS: tuple[tuple[str, str], ...] = (
    ("BodyClass", "Body class"),
    ("VehicleType", "Vehicle type"),
    ("Doors", "Doors"),
    ("Series", "Series"),
    ("Trim", "Trim"),
    ("GVWR", "GVWR"),
    ("CurbWeightLB", "Curb weight (lb)"),
    ("WheelBaseShort", "Wheelbase (in)"),
    ("Wheels", "Wheels"),
    ("PlantCompanyName", "Assembly plant"),
    ("PlantCity", "Plant city"),
    ("PlantCountry", "Plant country"),
)

SAFETY_FIELDS: tuple[tuple[str, str], ...] = (
    ("AirBagLocFront", "Front airbags"),
    ("AirBagLocSide", "Side airbags"),
    ("AirBagLocCurtain", "Curtain airbags"),
    ("AirBagLocKnee", "Knee airbags"),
    ("SeatBeltsAll", "Seat belt type"),
    ("BrakeSystemType", "Brake system"),
    ("BrakeSystemDesc", "Brake description"),
    ("ABS", "Anti-lock brakes"),
    ("ESC", "Electronic stability control"),
    ("TractionControl", "Traction control"),
    ("TPMS", "Tyre pressure monitoring"),
    ("BackupCamera", "Backup camera"),
    ("ForwardCollisionWarning", "Forward collision warning"),
    ("LaneDepartureWarning", "Lane departure warning"),
    ("BlindSpotMon", "Blind spot monitoring"),
    ("AdaptiveCruiseControl", "Adaptive cruise control"),
    ("PedestrianAutomaticEmergencyBraking", "Pedestrian AEB"),
    ("DaytimeRunningLight", "Daytime running lights"),
)

# Values vPIC uses to mean "nothing here". Treated as absent so the UI shows a
# short list of real facts instead of a long list of "Not Applicable".
_EMPTY_VALUES = {"", "not applicable", "none", "no", "0", "unknown"}


@dataclass(frozen=True)
class SpecItem:
    label: str
    value: str


@dataclass(frozen=True)
class MechanicalProfile:
    powertrain: list[SpecItem] = field(default_factory=list)
    structure: list[SpecItem] = field(default_factory=list)
    safety_equipment: list[SpecItem] = field(default_factory=list)

    @property
    def populated_count(self) -> int:
        return len(self.powertrain) + len(self.structure) + len(self.safety_equipment)


def build(raw: dict[str, Any]) -> MechanicalProfile:
    """Project a stored vPIC decode into the three display groups."""
    return MechanicalProfile(
        powertrain=_collect(raw, POWERTRAIN_FIELDS),
        structure=_collect(raw, STRUCTURE_FIELDS),
        safety_equipment=_collect(raw, SAFETY_FIELDS),
    )


def has_equipment(raw: dict[str, Any], key: str) -> bool:
    """True when vPIC positively reports a feature as present.

    Absence is not the same as "not fitted" -- vPIC simply leaves most fields
    blank on older vehicles -- so callers must treat False as "not recorded",
    never as "the car lacks it". The underwriting rules only use this to *add*
    a note, never to penalise.
    """
    return _clean(raw.get(key)) != ""


def _collect(raw: dict[str, Any], fields: tuple[tuple[str, str], ...]) -> list[SpecItem]:
    items: list[SpecItem] = []
    for key, label in fields:
        value = _clean(raw.get(key))
        if value:
            items.append(SpecItem(label=label, value=value))
    return items


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _EMPTY_VALUES else text
