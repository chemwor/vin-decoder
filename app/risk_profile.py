"""Vehicle classification for claims routing and energy-source risk.

Two questions an insurer asks about a VIN before anything else: *who should
handle a claim on this*, and *what is the energy source*. Both are answerable
from the decode alone -- no extra upstream call, and they work on every VIN
already in the cache.

Deliberately kept out of the underwriting decision in `underwriting.py`. That
decision is about open recalls, which are defects. These are characteristics:
a battery-electric sedan is not a worse risk than a petrol one, it is a
*differently handled* one. Mixing "this vehicle has an unrepaired airbag
recall" with "this vehicle is an EV" into one score would make both harder to
explain and would let a routing fact silently move an underwriting outcome.
They travel together in the response and stay separate in the logic.

Everything here is derived from fields vPIC populates reliably:
`VehicleType` was present on all 17 vehicles in the development cache,
including the motorcycle and the Class 8 tractor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- claims routing --------------------------------------------------------

MOTORCYCLE = "MOTORCYCLE"
COMMERCIAL_TRUCK = "COMMERCIAL_TRUCK"
LIGHT_TRUCK = "LIGHT_TRUCK"
PASSENGER_AUTO = "PASSENGER_AUTO"
BUS = "BUS"
TRAILER = "TRAILER"
UNCLASSIFIED = "UNCLASSIFIED"

_QUEUE_LABELS = {
    MOTORCYCLE: "Motorcycle desk",
    COMMERCIAL_TRUCK: "Commercial auto",
    LIGHT_TRUCK: "Personal lines — light truck",
    PASSENGER_AUTO: "Personal lines — auto",
    BUS: "Commercial auto — passenger carrier",
    TRAILER: "Commercial auto — trailer",
    UNCLASSIFIED: "Manual triage",
}

# GVWR class 3 begins at 10,001 lb, which is also where the federal definition
# of a commercial motor vehicle starts. Below it, a truck is a pickup someone
# drives to work; at or above it, a different policy form and a different
# adjuster.
_COMMERCIAL_GVWR_CLASS = 3

# Body classes that are commercial regardless of what GVWR says.
_ALWAYS_COMMERCIAL_BODY = ("TRUCK-TRACTOR", "INCOMPLETE", "STRIPPED CHASSIS")


# --- energy source ---------------------------------------------------------

BEV = "BEV"
PHEV = "PHEV"
HEV = "HEV"
MILD_HEV = "MILD_HEV"
FCEV = "FCEV"
ICE_GASOLINE = "ICE_GASOLINE"
ICE_DIESEL = "ICE_DIESEL"
OTHER_FUEL = "OTHER_FUEL"
UNKNOWN_FUEL = "UNKNOWN_FUEL"

_ENERGY_LABELS = {
    BEV: "Battery electric",
    PHEV: "Plug-in hybrid",
    HEV: "Hybrid",
    MILD_HEV: "Mild hybrid",
    FCEV: "Hydrogen fuel cell",
    ICE_GASOLINE: "Petrol (internal combustion)",
    ICE_DIESEL: "Diesel (internal combustion)",
    OTHER_FUEL: "Other fuel",
    UNKNOWN_FUEL: "Not reported",
}

# High-voltage traction packs, the ones large enough to drive the claim
# handling difference. A mild hybrid's pack is closer to a starter battery.
_HIGH_VOLTAGE = {BEV, PHEV, HEV}


@dataclass(frozen=True)
class RiskFlag:
    code: str
    severity: str
    title: str
    detail: str


@dataclass(frozen=True)
class ClaimsRouting:
    queue: str
    label: str
    basis: str
    commercial: bool


@dataclass(frozen=True)
class EnergySource:
    kind: str
    label: str
    battery_type: str
    basis: str
    flags: list[RiskFlag] = field(default_factory=list)


@dataclass(frozen=True)
class RiskProfile:
    claims_routing: ClaimsRouting
    energy_source: EnergySource


def build(raw: dict[str, Any]) -> RiskProfile:
    """Classify a decoded vehicle. Pure function over the stored vPIC blob."""
    return RiskProfile(
        claims_routing=route_claim(raw),
        energy_source=classify_energy(raw),
    )


# --- routing ---------------------------------------------------------------


def route_claim(raw: dict[str, Any]) -> ClaimsRouting:
    """Pick the handling queue from vehicle type, body class and weight class.

    `VehicleType` carries the decision almost by itself; GVWR only arbitrates
    the one genuinely ambiguous case, which is what kind of truck this is.
    """
    vehicle_type = _text(raw.get("VehicleType")).upper()
    body_class = _text(raw.get("BodyClass")).upper()
    gvwr_class = _gvwr_class(raw)

    if not vehicle_type:
        return ClaimsRouting(
            queue=UNCLASSIFIED,
            label=_QUEUE_LABELS[UNCLASSIFIED],
            basis="vPIC did not report a vehicle type",
            commercial=False,
        )

    if "MOTORCYCLE" in vehicle_type:
        return _routing(MOTORCYCLE, f"VehicleType={vehicle_type}", commercial=False)

    if "BUS" in vehicle_type:
        return _routing(BUS, f"VehicleType={vehicle_type}", commercial=True)

    if "TRAILER" in vehicle_type:
        return _routing(TRAILER, f"VehicleType={vehicle_type}", commercial=True)

    if "TRUCK" in vehicle_type:
        if any(marker in body_class for marker in _ALWAYS_COMMERCIAL_BODY):
            return _routing(
                COMMERCIAL_TRUCK,
                f"BodyClass={body_class}",
                commercial=True,
            )
        if gvwr_class is not None and gvwr_class >= _COMMERCIAL_GVWR_CLASS:
            return _routing(
                COMMERCIAL_TRUCK,
                f"VehicleType={vehicle_type}, GVWR class {gvwr_class}",
                commercial=True,
            )
        basis = f"VehicleType={vehicle_type}"
        if gvwr_class is not None:
            basis += f", GVWR class {gvwr_class}"
        else:
            # No weight class means we cannot prove it is light. Said out loud
            # rather than assumed, because the assumption is the whole
            # difference between a personal and a commercial policy.
            basis += ", GVWR not reported"
        return _routing(LIGHT_TRUCK, basis, commercial=False)

    # PASSENGER CAR and MULTIPURPOSE PASSENGER VEHICLE (MPV) both land here:
    # an SUV and a saloon are handled by the same desk.
    return _routing(PASSENGER_AUTO, f"VehicleType={vehicle_type}", commercial=False)


def _routing(queue: str, basis: str, *, commercial: bool) -> ClaimsRouting:
    return ClaimsRouting(
        queue=queue,
        label=_QUEUE_LABELS[queue],
        basis=basis,
        commercial=commercial,
    )


def _gvwr_class(raw: dict[str, Any]) -> int | None:
    """Leading digit of vPIC's GVWR string, e.g. 'Class 2F: 7,001 - 8,000 lb'.

    Falls back to GVWR_to, which is the upper bound of the range and therefore
    the conservative reading when only it is present.
    """
    for key in ("GVWR", "GVWR_to"):
        match = re.search(r"Class\s+(\d+)", _text(raw.get(key)))
        if match:
            return int(match.group(1))
    return None


# --- energy source ---------------------------------------------------------


def classify_energy(raw: dict[str, Any]) -> EnergySource:
    """Determine the energy source and the claim-handling flags it implies."""
    electrification = _text(raw.get("ElectrificationLevel"))
    primary = _text(raw.get("FuelTypePrimary"))
    secondary = _text(raw.get("FuelTypeSecondary"))
    battery_type = _text(raw.get("BatteryType"))

    kind = _energy_kind(electrification, primary)
    basis = (
        ", ".join(
            part
            for part in (
                f"ElectrificationLevel={electrification}" if electrification else "",
                f"FuelTypePrimary={primary}" if primary else "",
                f"FuelTypeSecondary={secondary}" if secondary else "",
            )
            if part
        )
        or "no fuel or electrification fields reported"
    )

    return EnergySource(
        kind=kind,
        label=_ENERGY_LABELS[kind],
        battery_type=battery_type,
        basis=basis,
        flags=_energy_flags(kind, battery_type),
    )


def _energy_kind(electrification: str, primary: str) -> str:
    """`ElectrificationLevel` wins where present; fuel type is the fallback.

    Checked most-electric first: "Plug-in Hybrid Electric Vehicle (PHEV)"
    contains the word "Hybrid", so an order that tested HEV first would
    misfile every plug-in as a conventional hybrid.
    """
    level = electrification.upper()
    if level:
        if "BEV" in level or "BATTERY ELECTRIC" in level:
            return BEV
        if "PHEV" in level or "PLUG-IN" in level:
            return PHEV
        if "FCEV" in level or "FUEL CELL" in level:
            return FCEV
        if "MILD" in level:
            return MILD_HEV
        if "HEV" in level or "HYBRID" in level:
            return HEV

    fuel = primary.upper()
    if not fuel:
        return UNKNOWN_FUEL
    if "ELECTRIC" in fuel:
        return BEV
    if "DIESEL" in fuel:
        return ICE_DIESEL
    if "GASOLINE" in fuel or "PETROL" in fuel:
        return ICE_GASOLINE
    return OTHER_FUEL


def _energy_flags(kind: str, battery_type: str) -> list[RiskFlag]:
    """Handling consequences, not a judgement on the risk being worse.

    An EV is not a worse vehicle to insure than a petrol one. It is one whose
    total loss, salvage and fire response work differently, and those
    differences cost money in ways that are easy to miss at first notice of
    loss. That is what these flags are for.
    """
    flags: list[RiskFlag] = []

    if kind in _HIGH_VOLTAGE:
        chemistry = battery_type.upper()
        if "LI" in chemistry:
            flags.append(
                RiskFlag(
                    code="LI_ION_THERMAL_RUNAWAY",
                    severity="warning",
                    title="Lithium-ion thermal runaway exposure",
                    detail=(
                        "Damaged lithium-ion packs can ignite hours or days after "
                        "an impact and can re-ignite after being extinguished. "
                        "Affects fire-service response, storage of the salvage, "
                        "and the property exposure of wherever it is stored."
                    ),
                )
            )
        elif not chemistry:
            # Not defaulted to lithium. vPIC leaves BatteryType blank on most
            # older hybrids, and plenty of them are nickel-metal hydride -- a
            # 2008 Prius among them -- which does not run away thermally the
            # way a lithium pack does. Asserting the wrong chemistry to stay on
            # the cautious side would still be asserting something we were
            # never told.
            flags.append(
                RiskFlag(
                    code="HV_BATTERY_CHEMISTRY_UNKNOWN",
                    severity="info",
                    title="Battery chemistry not reported",
                    detail=(
                        "This vehicle has a high-voltage pack but vPIC did not "
                        "report its chemistry. Lithium-ion carries thermal runaway "
                        "exposure that nickel-metal hydride largely does not, so "
                        "confirm the chemistry before assuming either."
                    ),
                )
            )
        flags.append(
            RiskFlag(
                code="HV_BATTERY_SALVAGE",
                severity="warning" if kind in (BEV, PHEV) else "info",
                title="High-voltage battery salvage and disposal",
                detail=(
                    "The traction pack needs specialist de-energising, transport "
                    "and disposal, and it is a large share of the vehicle's value. "
                    "Pack damage pushes vehicles over the total-loss threshold that "
                    "a comparable petrol car would survive."
                ),
            )
        )

    if kind == BEV:
        flags.append(
            RiskFlag(
                code="EV_REPAIR_NETWORK",
                severity="info",
                title="Restricted repair network",
                detail=(
                    "Battery-electric structural repair is limited to certified "
                    "shops, which lengthens cycle time and therefore hire-car and "
                    "loss-of-use costs."
                ),
            )
        )

    if kind == ICE_DIESEL:
        flags.append(
            RiskFlag(
                code="DIESEL_SPILL",
                severity="info",
                title="Fuel spill and environmental exposure",
                detail=(
                    "Diesel units carry large saddle tanks; a rollover can turn a "
                    "vehicle claim into a clean-up and environmental liability one."
                ),
            )
        )

    if kind == UNKNOWN_FUEL:
        flags.append(
            RiskFlag(
                code="ENERGY_SOURCE_UNKNOWN",
                severity="info",
                title="Energy source not reported",
                detail=(
                    "vPIC returned no fuel or electrification field for this VIN, "
                    "so EV-specific handling cannot be ruled in or out from the "
                    "decode alone."
                ),
            )
        )

    return flags


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("not applicable", "none", "unknown") else text
