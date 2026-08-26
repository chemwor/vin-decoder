"""Request/response models.

Validation lives here rather than in the route handlers so that both the GET
and POST forms of each route share exactly one definition of "a valid VIN",
and so FastAPI can advertise it in the OpenAPI schema.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import settings

# 17 alphanumeric characters, per the spec.
_VIN_RE = re.compile(r"^[A-Z0-9]{17}$")
# Same, minus the letters that never appear in a real VIN.
_VIN_RE_STRICT = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalize_vin(raw: str) -> str:
    """Uppercase, trim, and validate a VIN. Raises ValueError if invalid.

    Normalizing before validating means `1hgcm82633a004352` and
    `1HGCM82633A004352 ` are the same cache key, which matters: without it the
    cache would silently store one row per casing.
    """
    vin = raw.strip().upper()
    pattern = _VIN_RE_STRICT if settings.strict_vin_charset else _VIN_RE
    if not pattern.match(vin):
        raise ValueError(
            "VIN must be exactly 17 alphanumeric characters"
            + (" and may not contain I, O or Q" if settings.strict_vin_charset else "")
        )
    return vin


class VinRequest(BaseModel):
    """Body for POST /lookup and POST /remove."""

    model_config = ConfigDict(json_schema_extra={"example": {"vin": "1HGCM82633A004352"}})

    vin: str = Field(..., description="17-character alphanumeric VIN")

    @field_validator("vin")
    @classmethod
    def _validate(cls, v: str) -> str:
        return normalize_vin(v)


class LookupResponse(BaseModel):
    """The six fields the challenge asks for, in snake_case JSON."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "vin": "1HGCM82633A004352",
                "make": "HONDA",
                "model": "Accord",
                "model_year": "2003",
                "body_class": "Coupe",
                "cached_result": False,
            }
        }
    )

    vin: str = Field(..., description="Input VIN Requested")
    make: str = Field(..., description="Make")
    model: str = Field(..., description="Model")
    model_year: str = Field(..., description="Model Year")
    body_class: str = Field(..., description="Body Class")
    cached_result: bool = Field(
        ..., description="True if this response was served from the local cache"
    )


class RemoveResponse(BaseModel):
    vin: str = Field(..., description="Input VIN Requested")
    cache_delete_success: bool = Field(
        ..., description="True if a cached row existed and was deleted"
    )


class ErrorResponse(BaseModel):
    """Uniform error envelope, so clients never have to branch on shape."""

    detail: str
    vin: str | None = None


# --- underwriting ----------------------------------------------------------
#
# A separate response model from LookupResponse rather than extra optional
# fields on it. /lookup answers "what is this VIN" from one cached row in
# milliseconds; /underwrite answers "should we write this risk" and may hit two
# more upstreams. Keeping them apart means the fast path stays fast and its
# contract stays exactly what the spec asked for.


class SpecItemModel(BaseModel):
    label: str
    value: str


class MechanicalModel(BaseModel):
    """Structural and mechanical detail, projected from the stored decode."""

    powertrain: list[SpecItemModel] = []
    structure: list[SpecItemModel] = []
    safety_equipment: list[SpecItemModel] = []


class RecallModel(BaseModel):
    campaign_number: str
    component: str
    summary: str
    consequence: str
    remedy: str
    manufacturer: str
    report_received_date: str | None = None
    park_it: bool = False
    park_outside: bool = False
    over_the_air_update: bool = False


class SafetyRatingModel(BaseModel):
    vehicle_description: str
    overall: str
    overall_front: str
    overall_side: str
    rollover: str
    rollover_possibility: float | None = None
    electronic_stability_control: str
    forward_collision_warning: str
    lane_departure_warning: str
    complaints_count: int
    recalls_count: int
    investigation_count: int


class FlagModel(BaseModel):
    code: str = Field(..., description="Stable machine-readable flag code")
    severity: str = Field(..., description="critical | warning | info")
    title: str
    detail: str
    campaigns: list[str] = []


class AssessmentModel(BaseModel):
    decision: str = Field(..., description="BLOCK | REFER | CLEAR | INSUFFICIENT_DATA")
    headline: str
    flags: list[FlagModel] = []
    open_recall_count: int
    vin_level_verified: bool = Field(
        ...,
        description=(
            "Always false: NHTSA's public API cannot confirm whether a campaign "
            "was remedied on this specific VIN"
        ),
    )
    caveat: str
    assessed_at: str


class RiskFlagModel(BaseModel):
    code: str
    severity: str
    title: str
    detail: str


class ClaimsRoutingModel(BaseModel):
    queue: str = Field(..., description="Handling queue, e.g. COMMERCIAL_TRUCK")
    label: str
    basis: str = Field(..., description="The decoded fields that produced this queue")
    commercial: bool


class EnergySourceModel(BaseModel):
    kind: str = Field(..., description="BEV | PHEV | HEV | MILD_HEV | FCEV | ICE_* | ...")
    label: str
    battery_type: str = ""
    basis: str
    flags: list[RiskFlagModel] = []


class RiskProfileModel(BaseModel):
    """Classification, kept separate from the recall-based decision.

    These describe how a claim on this vehicle should be handled. They do not
    move the underwriting decision, which is about defects rather than
    characteristics.
    """

    claims_routing: ClaimsRoutingModel
    energy_source: EnergySourceModel


class UnderwriteResponse(BaseModel):
    """Everything an underwriter needs for a first-pass decision on one VIN."""

    vin: str
    make: str
    model: str
    model_year: str
    body_class: str
    underwriting: AssessmentModel
    recalls: list[RecallModel] = []
    safety_rating: SafetyRatingModel | None = None
    mechanical: MechanicalModel
    risk_profile: RiskProfileModel
    data_gaps: list[str] = []
    cached_result: bool = Field(
        ..., description="True if both the decode and the recall profile came from cache"
    )
