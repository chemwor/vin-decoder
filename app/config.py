"""Application configuration.

Everything is read from environment variables once, at import time, into a
frozen dataclass. Deliberately no pydantic-settings dependency: there are six
knobs, and a dataclass makes it obvious where each default lives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Where the SQLite file lives. ":memory:" is supported for tests.
    db_path: str = str(Path(os.getenv("VIN_DB_PATH", "vin_cache.db")).expanduser())

    # vPIC base URL. Overridable so a test/staging run can point at a stub.
    vpic_base_url: str = os.getenv("VPIC_BASE_URL", "https://vpic.nhtsa.dot.gov/api/vehicles")

    # Per-attempt timeout, in seconds, for the upstream call.
    vpic_timeout_seconds: float = float(os.getenv("VPIC_TIMEOUT_SECONDS", "8"))

    # Retries *in addition to* the first attempt. Only transient failures
    # (timeouts, connection errors, 5xx, 429) are retried.
    vpic_max_retries: int = _env_int("VPIC_MAX_RETRIES", 2)

    # 0 disables expiry. VIN decodes are effectively immutable, so the default
    # is "cache forever"; the knob exists so a deploy can force refresh windows
    # if NHTSA corrects its data.
    cache_ttl_seconds: int = _env_int("CACHE_TTL_SECONDS", 0)

    # Real-world VINs never contain I, O or Q (they are excluded to avoid
    # confusion with 1 and 0). The challenge spec only says "17 alphanumeric",
    # so spec compliance is the default and the stricter rule is opt-in.
    strict_vin_charset: bool = _env_bool("STRICT_VIN_CHARSET", False)

    # --- recalls / safety ratings -----------------------------------------

    # A different host from vPIC, and two different services on it.
    nhtsa_recalls_base_url: str = os.getenv(
        "NHTSA_RECALLS_BASE_URL", "https://api.nhtsa.gov/recalls"
    )
    nhtsa_ratings_base_url: str = os.getenv(
        "NHTSA_RATINGS_BASE_URL", "https://api.nhtsa.gov/SafetyRatings"
    )

    # Unlike a VIN decode, a recall list is NOT immutable -- new campaigns are
    # announced against old vehicles all the time. Caching these forever would
    # mean an underwriter clearing a car against a stale campaign list, so this
    # TTL defaults to a day rather than to "never expire".
    profile_ttl_seconds: int = _env_int("PROFILE_TTL_SECONDS", 86_400)

    # --- underwriting thresholds ------------------------------------------
    # Exposed as settings because these are business calls, not facts. The
    # defaults are deliberately cautious; an underwriting team should own them.

    # Campaign count at or above which a vehicle goes to manual review.
    uw_recall_count_refer: int = _env_int("UW_RECALL_COUNT_REFER", 5)

    # NCAP overall stars at or below which a vehicle goes to manual review.
    uw_min_ncap_stars: int = _env_int("UW_MIN_NCAP_STARS", 2)

    # NCAP rollover probability at or above which a vehicle goes to review.
    uw_rollover_possibility_refer: float = float(os.getenv("UW_ROLLOVER_POSSIBILITY_REFER", "0.30"))

    # Campaigns announced within this window are the least likely to have been
    # remedied yet. 0 disables the check.
    uw_recent_recall_days: int = _env_int("UW_RECENT_RECALL_DAYS", 365)


settings = Settings()
