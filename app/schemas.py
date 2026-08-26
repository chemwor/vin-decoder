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
