"""Open-recall underwriting assessment.

READ THIS BEFORE TRUSTING THE FLAG
----------------------------------
NHTSA's public API exposes recalls by year/make/model, never by VIN. There is
no public endpoint that reports whether a *specific* vehicle's recall was ever
remedied -- that lives behind each manufacturer's own VIN lookup. So the
strongest true statement available from this data is:

    "N recall campaigns cover this year/make/model, and we cannot confirm from
     public data whether this particular car has had them performed."

Every campaign is therefore treated as *potentially open*, which is the
conservative direction for underwriting: it can send a repaired car to manual
review, but it will not quietly clear an unrepaired one. `vin_level_verified`
is False on every assessment to keep that limitation attached to the result
rather than living in a README nobody reads. Wiring in a manufacturer VIN API
later is what upgrades this from a screening signal to a decision.

The rules deliberately separate NHTSA's own hazard judgements (`parkIt`,
`parkOutSide` -- the regulator saying stop driving it) from ours (component
keywords, counts, crash stars). Only the regulator's judgement can BLOCK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .config import settings
from .nhtsa import Recall, SafetyRating

# --- decisions -------------------------------------------------------------

BLOCK = "BLOCK"
REFER = "REFER"
CLEAR = "CLEAR"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

_SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}

# Component families worth a human look. Matched against NHTSA's `Component`
# string, which is a colon-delimited path like
# "AIR BAGS:FRONTAL:DRIVER SIDE:INFLATOR MODULE".
_COMPONENT_RULES: tuple[tuple[str, str, str], ...] = (
    (
        r"AIR BAGS?.*INFLATOR",
        "AIRBAG_INFLATOR",
        "Airbag inflator campaign (the Takata family of recalls) — rupture risk "
        "if unremedied, and among the most commonly unrepaired recalls in the fleet.",
    ),
    (
        r"FUEL SYSTEM|FUEL TANK|FUEL PUMP",
        "FUEL_SYSTEM",
        "Fuel system campaign — fire exposure if unremedied.",
    ),
    (
        r"ENGINE AND ENGINE COOLING|ENGINE COMPARTMENT",
        "ENGINE_FIRE_RISK",
        "Engine or cooling campaign — a common source of non-collision fire claims.",
    ),
    (
        r"SERVICE BRAKES|BRAKE",
        "BRAKES",
        "Braking system campaign — directly affects collision frequency.",
    ),
    (
        r"STEERING",
        "STEERING",
        "Steering campaign — loss-of-control exposure.",
    ),
    (
        r"ELECTRICAL SYSTEM",
        "ELECTRICAL",
        "Electrical campaign — frequently associated with fire risk.",
    ),
    (
        r"SEAT BELT",
        "SEAT_BELTS",
        "Seat belt campaign — raises injury severity in an otherwise survivable crash.",
    ),
)


@dataclass(frozen=True)
class Flag:
    code: str
    severity: str
    title: str
    detail: str
    campaigns: list[str]


@dataclass(frozen=True)
class Assessment:
    decision: str
    headline: str
    flags: list[Flag]
    open_recall_count: int
    vin_level_verified: bool
    caveat: str
    assessed_at: date

    @property
    def is_blocked(self) -> bool:
        return self.decision == BLOCK


CAVEAT = (
    "NHTSA publishes recalls by year/make/model, not by VIN. Campaign counts "
    "shown here are those that cover this vehicle's year, make and model; "
    "whether this specific VIN was remedied cannot be confirmed from public "
    "data and must be verified with the manufacturer before binding."
)


def assess(
    recalls: list[Recall] | None,
    rating: SafetyRating | None,
    data_gaps: list[str],
    today: date | None = None,
) -> Assessment:
    """Turn recall and rating data into a decision plus the reasons for it.

    `recalls is None` means the lookup failed, which is emphatically not the
    same as an empty list. The first cannot be cleared; the second can.
    """
    today = today or datetime.now(UTC).date()
    flags: list[Flag] = []

    for gap in data_gaps:
        flags.append(
            Flag(
                code="DATA_GAP",
                severity=WARNING,
                title="Incomplete data",
                detail=f"{gap}. Assessment is based on partial information.",
                campaigns=[],
            )
        )

    if recalls is None:
        flags.append(
            Flag(
                code="RECALLS_UNKNOWN",
                severity=WARNING,
                title="Recall status unknown",
                detail=(
                    "NHTSA recall data could not be retrieved, so this vehicle "
                    "cannot be cleared of open recalls. Retry before deciding."
                ),
                campaigns=[],
            )
        )
        return Assessment(
            decision=INSUFFICIENT_DATA,
            headline="Cannot assess — recall data unavailable",
            flags=flags,
            open_recall_count=0,
            vin_level_verified=False,
            caveat=CAVEAT,
            assessed_at=today,
        )

    flags.extend(_regulator_flags(recalls))
    flags.extend(_component_flags(recalls))
    flags.extend(_volume_flags(recalls))
    flags.extend(_recency_flags(recalls, today))
    flags.extend(_rating_flags(rating))

    decision = _decide(flags, recalls)
    return Assessment(
        decision=decision,
        headline=_headline(decision, recalls),
        flags=flags,
        open_recall_count=len(recalls),
        vin_level_verified=False,
        caveat=CAVEAT,
        assessed_at=today,
    )


def _regulator_flags(recalls: list[Recall]) -> list[Flag]:
    """NHTSA's own stop-driving advisories. The only BLOCK-level signals."""
    flags = []
    park_it = [r.campaign_number for r in recalls if r.park_it]
    if park_it:
        flags.append(
            Flag(
                code="DO_NOT_DRIVE",
                severity=CRITICAL,
                title='NHTSA "Do Not Drive" advisory',
                detail=(
                    "NHTSA advises owners to stop driving this vehicle until the "
                    "recall is remedied. Do not bind without written proof of repair."
                ),
                campaigns=park_it,
            )
        )

    park_outside = [r.campaign_number for r in recalls if r.park_outside]
    if park_outside:
        flags.append(
            Flag(
                code="PARK_OUTSIDE",
                severity=CRITICAL,
                title='NHTSA "Park Outside" advisory',
                detail=(
                    "NHTSA advises parking this vehicle away from structures due to "
                    "fire risk while unattended. Carries property exposure beyond "
                    "the vehicle itself."
                ),
                campaigns=park_outside,
            )
        )
    return flags


def _component_flags(recalls: list[Recall]) -> list[Flag]:
    """Our own read of which component families warrant review."""
    flags = []
    for pattern, code, detail in _COMPONENT_RULES:
        matched = [r.campaign_number for r in recalls if re.search(pattern, r.component.upper())]
        if matched:
            flags.append(
                Flag(
                    code=code,
                    severity=WARNING,
                    title=code.replace("_", " ").title(),
                    detail=detail,
                    campaigns=matched,
                )
            )
    return flags


def _volume_flags(recalls: list[Recall]) -> list[Flag]:
    threshold = settings.uw_recall_count_refer
    if threshold > 0 and len(recalls) >= threshold:
        return [
            Flag(
                code="HIGH_RECALL_VOLUME",
                severity=WARNING,
                title="High recall volume",
                detail=(
                    f"{len(recalls)} campaigns cover this year/make/model "
                    f"(review threshold is {threshold}). Volume alone is weak "
                    "evidence — popular models accumulate campaigns — but it "
                    "raises the chance at least one is unremedied."
                ),
                campaigns=[],
            )
        ]
    return []


def _recency_flags(recalls: list[Recall], today: date) -> list[Flag]:
    """Recent campaigns are the ones least likely to have been remedied yet."""
    window = settings.uw_recent_recall_days
    if window <= 0:
        return []
    cutoff = today - timedelta(days=window)
    recent = [
        r.campaign_number
        for r in recalls
        if r.report_received_date is not None and r.report_received_date >= cutoff
    ]
    if recent:
        return [
            Flag(
                code="RECENT_CAMPAIGN",
                severity=WARNING,
                title="Recently announced campaign",
                detail=(
                    f"{len(recent)} campaign(s) announced in the last {window} days. "
                    "Remedy parts and owner notification often lag announcement, so "
                    "these are the most likely to still be open."
                ),
                campaigns=recent,
            )
        ]
    return []


def _rating_flags(rating: SafetyRating | None) -> list[Flag]:
    if rating is None:
        return [
            Flag(
                code="NO_NCAP_DATA",
                severity=INFO,
                title="No NCAP crash test data",
                detail=(
                    "NHTSA has not crash-tested this year/make/model. Common for "
                    "low-volume, heavy-duty and older vehicles; not itself adverse."
                ),
                campaigns=[],
            )
        ]

    flags = []
    stars = _stars(rating.overall)
    minimum = settings.uw_min_ncap_stars
    if stars is not None and stars <= minimum:
        flags.append(
            Flag(
                code="LOW_CRASH_RATING",
                severity=WARNING,
                title="Low overall crash rating",
                detail=(
                    f"NCAP overall rating is {stars}/5 (review at or below "
                    f"{minimum}). Correlates with injury severity, and therefore "
                    "with bodily injury and medical payments severity."
                ),
                campaigns=[],
            )
        )

    if rating.rollover_possibility is not None:
        limit = settings.uw_rollover_possibility_refer
        if rating.rollover_possibility >= limit:
            flags.append(
                Flag(
                    code="ROLLOVER_RISK",
                    severity=WARNING,
                    title="Elevated rollover risk",
                    detail=(
                        f"NCAP puts rollover probability at "
                        f"{rating.rollover_possibility:.0%} (review at or above "
                        f"{limit:.0%}). Rollovers skew heavily toward total losses."
                    ),
                    campaigns=[],
                )
            )
    return flags


def _decide(flags: list[Flag], recalls: list[Recall]) -> str:
    worst = max((_SEVERITY_RANK[f.severity] for f in flags), default=-1)
    if worst == _SEVERITY_RANK[CRITICAL]:
        return BLOCK
    if worst == _SEVERITY_RANK[WARNING]:
        return REFER
    return CLEAR if not recalls else REFER


def _headline(decision: str, recalls: list[Recall]) -> str:
    count = len(recalls)
    if decision == BLOCK:
        return f"Do not bind — NHTSA stop-driving advisory in force ({count} campaigns)"
    if decision == REFER:
        if count == 0:
            return "Manual review — no recalls, but other risk signals present"
        return f"Manual review — {count} recall campaign(s) may be open on this vehicle"
    return "No recall campaigns found for this year, make and model"


def _stars(raw: str) -> int | None:
    """NCAP ratings arrive as strings, and as 'Not Rated' for untested cars."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None
