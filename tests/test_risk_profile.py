"""Claims routing and energy-source classification.

Cases are named after the real vehicles they came from, all of which are in
the development cache, so a failure points at a VIN you can re-decode rather
than at an invented fixture.
"""

from __future__ import annotations

from app import risk_profile
from app.risk_profile import (
    BEV,
    COMMERCIAL_TRUCK,
    HEV,
    ICE_DIESEL,
    ICE_GASOLINE,
    LIGHT_TRUCK,
    MOTORCYCLE,
    PASSENGER_AUTO,
    UNCLASSIFIED,
    UNKNOWN_FUEL,
)


def route(**raw):
    return risk_profile.route_claim(raw)


def energy(**raw):
    return risk_profile.classify_energy(raw)


# --- routing ---------------------------------------------------------------


def test_motorcycle_routes_to_its_own_desk():
    """2015 Harley-Davidson Street Glide."""
    result = route(VehicleType="MOTORCYCLE", BodyClass="Motorcycle - Touring/Sport Touring")
    assert result.queue == MOTORCYCLE
    assert result.commercial is False


def test_class_8_tractor_is_commercial():
    """2014 Peterbilt 388: Class 8, Truck-Tractor."""
    result = route(
        VehicleType="TRUCK",
        BodyClass="Truck-Tractor",
        GVWR="Class 8: 33,001 lb and above (14,969 kg and above)",
    )
    assert result.queue == COMMERCIAL_TRUCK
    assert result.commercial is True


def test_pickup_under_the_weight_line_is_personal_lines():
    """2013 Ford F-150: a truck, but Class 2 -- someone's daily driver."""
    result = route(VehicleType="TRUCK", BodyClass="Pickup", GVWR="Class 2F: 7,001 - 8,000 lb")
    assert result.queue == LIGHT_TRUCK
    assert result.commercial is False


def test_truck_tractor_is_commercial_even_without_a_weight_class():
    """Body class alone settles it; GVWR is missing on plenty of decodes."""
    result = route(VehicleType="TRUCK", BodyClass="Truck-Tractor")
    assert result.queue == COMMERCIAL_TRUCK


def test_missing_gvwr_on_a_truck_is_stated_not_assumed():
    result = route(VehicleType="TRUCK", BodyClass="Pickup")
    assert result.queue == LIGHT_TRUCK
    assert "GVWR not reported" in result.basis


def test_mpv_and_car_share_the_personal_auto_queue():
    """2014 Nissan Pathfinder (MPV) and 2013 BMW 328i (PASSENGER CAR)."""
    mpv = route(VehicleType="MULTIPURPOSE PASSENGER VEHICLE (MPV)", BodyClass="Crossover")
    car = route(VehicleType="PASSENGER CAR", BodyClass="Sedan/Saloon")
    assert mpv.queue == car.queue == PASSENGER_AUTO


def test_missing_vehicle_type_goes_to_manual_triage():
    """2003 Volkswagen: a EU-market VIN that decodes only partially."""
    result = route(BodyClass="")
    assert result.queue == UNCLASSIFIED
    assert result.commercial is False


def test_basis_names_the_field_that_decided_it():
    result = route(VehicleType="MOTORCYCLE")
    assert "VehicleType" in result.basis


# --- energy source ---------------------------------------------------------


def test_battery_electric_from_electrification_level():
    """2023 Tesla Model 3."""
    result = energy(
        ElectrificationLevel="BEV (Battery Electric Vehicle)",
        FuelTypePrimary="Electric",
        BatteryType="Lithium-Ion/Li-Ion",
    )
    assert result.kind == BEV
    assert result.battery_type == "Lithium-Ion/Li-Ion"


def test_hybrid_is_not_mistaken_for_a_plug_in():
    """2008 Toyota Prius: 'Strong HEV', petrol primary, electric secondary."""
    result = energy(
        ElectrificationLevel="Strong HEV (Hybrid Electric Vehicle)",
        FuelTypePrimary="Gasoline",
        FuelTypeSecondary="Electric",
    )
    assert result.kind == HEV


def test_plug_in_hybrid_beats_the_word_hybrid():
    """'Plug-in Hybrid Electric Vehicle (PHEV)' contains 'Hybrid'."""
    result = energy(ElectrificationLevel="PHEV (Plug-in Hybrid Electric Vehicle)")
    assert result.kind != HEV


def test_diesel_and_petrol_are_distinguished():
    assert energy(FuelTypePrimary="Diesel").kind == ICE_DIESEL
    assert energy(FuelTypePrimary="Gasoline").kind == ICE_GASOLINE


def test_electric_fuel_type_alone_is_enough():
    """Older EV decodes carry no ElectrificationLevel."""
    assert energy(FuelTypePrimary="Electric").kind == BEV


def test_no_fuel_fields_is_unknown_not_petrol():
    result = energy()
    assert result.kind == UNKNOWN_FUEL
    assert any(f.code == "ENERGY_SOURCE_UNKNOWN" for f in result.flags)


# --- flags -----------------------------------------------------------------


def test_bev_carries_thermal_runaway_and_salvage_flags():
    codes = {f.code for f in energy(ElectrificationLevel="BEV", BatteryType="Li-Ion").flags}
    assert "LI_ION_THERMAL_RUNAWAY" in codes
    assert "HV_BATTERY_SALVAGE" in codes
    assert "EV_REPAIR_NETWORK" in codes


def test_unreported_chemistry_is_not_assumed_to_be_lithium():
    """2008 Prius: high-voltage, but NiMH -- and vPIC does not say so.

    Flagging lithium thermal runaway here would be inventing a fact. The pack
    still needs salvage handling, so that flag stays.
    """
    codes = {f.code for f in energy(ElectrificationLevel="Strong HEV").flags}
    assert "LI_ION_THERMAL_RUNAWAY" not in codes
    assert "HV_BATTERY_CHEMISTRY_UNKNOWN" in codes
    assert "HV_BATTERY_SALVAGE" in codes


def test_nickel_metal_hydride_gets_no_thermal_runaway_flag():
    codes = {f.code for f in energy(ElectrificationLevel="Strong HEV", BatteryType="NiMH").flags}
    assert "LI_ION_THERMAL_RUNAWAY" not in codes
    assert "HV_BATTERY_CHEMISTRY_UNKNOWN" not in codes


def test_petrol_car_carries_no_energy_flags():
    assert energy(FuelTypePrimary="Gasoline").flags == []


def test_hybrid_salvage_flag_is_softer_than_a_bevs():
    """A hybrid's pack is real but smaller; severity should say so."""
    hev = {f.code: f for f in energy(ElectrificationLevel="Strong HEV", BatteryType="NiMH").flags}
    bev = {f.code: f for f in energy(ElectrificationLevel="BEV", BatteryType="Li-Ion").flags}
    assert hev["HV_BATTERY_SALVAGE"].severity == "info"
    assert bev["HV_BATTERY_SALVAGE"].severity == "warning"


def test_mild_hybrid_is_not_treated_as_high_voltage():
    assert energy(ElectrificationLevel="Mild HEV (Hybrid Electric Vehicle)").flags == []


def test_diesel_carries_a_spill_flag():
    assert any(f.code == "DIESEL_SPILL" for f in energy(FuelTypePrimary="Diesel").flags)


def test_build_returns_both_halves():
    profile = risk_profile.build({"VehicleType": "PASSENGER CAR", "FuelTypePrimary": "Electric"})
    assert profile.claims_routing.queue == PASSENGER_AUTO
    assert profile.energy_source.kind == BEV
