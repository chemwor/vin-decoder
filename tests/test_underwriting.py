"""Underwriting assessment rules.

These are the tests that matter most in this feature: the flag is the thing a
human will act on, and its most dangerous failure mode is not a crash but a
quiet CLEAR on a vehicle nobody actually checked. `test_missing_recall_data_*`
is the guard on that.
"""

from __future__ import annotations

from datetime import date

from app import underwriting
from app.underwriting import BLOCK, CLEAR, INSUFFICIENT_DATA, REFER
from tests.conftest import make_rating, make_recall

TODAY = date(2026, 8, 26)


def assess(recalls, rating=None, gaps=None):
    return underwriting.assess(recalls, rating, gaps or [], today=TODAY)


# --- the safe-by-default rules --------------------------------------------


def test_no_recalls_is_clear():
    result = assess([], make_rating())
    assert result.decision == CLEAR
    assert result.open_recall_count == 0


def test_missing_recall_data_is_not_clear():
    """The whole point: a failed lookup must never read as a clean vehicle."""
    result = assess(None, make_rating(), ["Recall data unavailable from NHTSA"])
    assert result.decision == INSUFFICIENT_DATA
    assert result.decision != CLEAR
    assert any(f.code == "RECALLS_UNKNOWN" for f in result.flags)


def test_assessment_never_claims_vin_level_verification():
    """NHTSA cannot confirm remedy status per VIN, so this stays false."""
    for recalls in ([], [make_recall()], None):
        assert assess(recalls).vin_level_verified is False


def test_caveat_travels_with_every_assessment():
    assert "not by VIN" in assess([], make_rating()).caveat


# --- regulator advisories block -------------------------------------------


def test_do_not_drive_blocks():
    result = assess([make_recall("22V123000", park_it=True)])
    assert result.decision == BLOCK
    flag = next(f for f in result.flags if f.code == "DO_NOT_DRIVE")
    assert flag.severity == "critical"
    assert flag.campaigns == ["22V123000"]


def test_park_outside_blocks():
    result = assess([make_recall("21V999000", park_outside=True)])
    assert result.decision == BLOCK
    assert any(f.code == "PARK_OUTSIDE" for f in result.flags)


def test_block_wins_over_lesser_flags():
    result = assess(
        [
            make_recall("22V123000", park_it=True),
            make_recall("19V182000", component="AIR BAGS:FRONTAL:INFLATOR MODULE"),
        ]
    )
    assert result.decision == BLOCK


# --- our own component judgements refer, never block -----------------------


def test_airbag_inflator_refers():
    result = assess([make_recall("19V182000", component="AIR BAGS:FRONTAL:INFLATOR MODULE")])
    assert result.decision == REFER
    assert any(f.code == "AIRBAG_INFLATOR" for f in result.flags)


def test_ordinary_recall_still_refers_rather_than_clears():
    """An unmatched component is not a clean bill of health."""
    result = assess([make_recall(component="VISIBILITY:WINDSHIELD")])
    assert result.decision == REFER


def test_high_volume_refers():
    recalls = [make_recall(f"20V00{i}000") for i in range(6)]
    result = assess(recalls)
    assert any(f.code == "HIGH_RECALL_VOLUME" for f in result.flags)
    assert result.decision == REFER


def test_recent_campaign_is_flagged():
    result = assess([make_recall("26V001000", report_received_date=date(2026, 6, 1))])
    assert any(f.code == "RECENT_CAMPAIGN" for f in result.flags)


def test_old_campaign_is_not_flagged_as_recent():
    result = assess([make_recall(report_received_date=date(2010, 1, 1))])
    assert not any(f.code == "RECENT_CAMPAIGN" for f in result.flags)


# --- crash ratings ---------------------------------------------------------


def test_low_crash_rating_refers():
    result = assess([], make_rating(overall="2"))
    assert result.decision == REFER
    assert any(f.code == "LOW_CRASH_RATING" for f in result.flags)


def test_unrated_vehicle_is_informational_only():
    """'Not Rated' must not be read as zero stars."""
    result = assess([], make_rating(overall="Not Rated"))
    assert result.decision == CLEAR
    assert not any(f.code == "LOW_CRASH_RATING" for f in result.flags)


def test_high_rollover_probability_refers():
    result = assess([], make_rating(rollover_possibility=0.35))
    assert any(f.code == "ROLLOVER_RISK" for f in result.flags)


def test_absent_ncap_data_is_info_not_adverse():
    result = assess([], None)
    assert result.decision == CLEAR
    assert any(f.code == "NO_NCAP_DATA" and f.severity == "info" for f in result.flags)
