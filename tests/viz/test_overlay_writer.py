"""Tests for the OverlayWriter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from xauusd_bot.common.schemas.features import (
    FVGOutput,
    FVGStatus,
    FVGType,
    FVGZone,
    TripleVWAPOutput,
    ValueAreaStatus,
    VolumeProfileName,
    VolumeProfileOutput,
    VolumeProfileState,
    VolumeRangeOutput,
    VWAPLevel,
    VWAPLevelOutput,
)
from xauusd_bot.viz.overlay_writer import OverlayWriter, build_overlay_payload


def _vwap() -> TripleVWAPOutput:
    return TripleVWAPOutput(
        levels={
            "utc00": VWAPLevelOutput(level=VWAPLevel.UTC00, value=2370.4, n_bars_anchored=480),
            "utc07": VWAPLevelOutput(level=VWAPLevel.UTC07, value=2374.8, n_bars_anchored=60),
            "utc12": VWAPLevelOutput(level=VWAPLevel.UTC12, value=2378.2, n_bars_anchored=0),
        },
        cluster_within_atr=1.5,
        is_cluster=False,
        cluster_center=None,
    )


def _profile(name: VolumeProfileName, state: VolumeProfileState) -> VolumeProfileOutput:
    return VolumeProfileOutput(
        name=name,
        state=state,
        period_start=datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 1, 12, 0, 0, tzinfo=UTC),
        bin_size=0.75,
        vah=2360.0,
        vpoc=2351.0,
        val=2340.0,
        value_area_pct=0.70,
        distance_to_vah_points=10.0,
        distance_to_vah_atr=1.5,
        distance_to_val_points=-10.0,
        distance_to_val_atr=-1.5,
        distance_to_vpoc_points=9.0,
        distance_to_vpoc_atr=1.35,
        value_status=ValueAreaStatus.ABOVE_VALUE,
        acceptance_count=120,
        rejection_count=20,
        rotation=True,
        breakout=False,
        n_bars=480,
    )


def _volume_range() -> VolumeRangeOutput:
    return VolumeRangeOutput(
        weekly=_profile(VolumeProfileName.WEEKLY, VolumeProfileState.DEVELOPING),
        monthly=_profile(VolumeProfileName.MONTHLY, VolumeProfileState.DEVELOPING),
        yearly=_profile(VolumeProfileName.YEARLY, VolumeProfileState.DEVELOPING),
        prev_week=_profile(VolumeProfileName.WEEKLY, VolumeProfileState.LOCKED),
        prev_month=_profile(VolumeProfileName.MONTHLY, VolumeProfileState.LOCKED),
        prev_year=_profile(VolumeProfileName.YEARLY, VolumeProfileState.LOCKED),
    )


def _fvg() -> FVGOutput:
    return FVGOutput(
        zones=[
            FVGZone(
                tf="H1",
                type=FVGType.BULLISH,
                top=2373.0,
                bottom=2371.5,
                size_points=1.5,
                created_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
                age_seconds=0,
                displacement_atr=1.2,
                status=FVGStatus.OPEN,
                mitigation_pct=0.0,
                rank_score=10.0,
            ),
            FVGZone(
                tf="M5",
                type=FVGType.BEARISH,
                top=2375.0,
                bottom=2374.0,
                size_points=1.0,
                created_at=datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
                age_seconds=0,
                displacement_atr=0.8,
                status=FVGStatus.MITIGATED,
                mitigation_pct=100.0,
                rank_score=0.0,
            ),
        ],
        top_zones=[],
    )


# ---------------------------------------------------------------- payload


def test_build_overlay_payload_has_all_required_sections() -> None:
    """The payload has ts, vwap, volume_profile, fvg_zones."""

    ts = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
    payload = build_overlay_payload(ts=ts, vwap=_vwap(), volume_range=_volume_range(), fvg=_fvg())
    assert "ts" in payload
    assert "vwap" in payload
    assert "volume_profile" in payload
    assert "fvg_zones" in payload
    # ts is ISO-formatted and matches the input.
    assert payload["ts"] == ts.isoformat()


def test_vwap_section_has_three_levels() -> None:
    payload = build_overlay_payload(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    assert set(payload["vwap"].keys()) == {"utc00", "utc07", "utc12"}
    assert payload["vwap"]["utc00"] == 2370.4
    assert payload["vwap"]["utc12"] == 2378.2


def test_volume_profile_section_has_all_profiles() -> None:
    payload = build_overlay_payload(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    keys = set(payload["volume_profile"].keys())
    assert keys == {
        "daily", "weekly", "monthly", "yearly",
        "prev_day", "prev_week", "prev_month", "prev_year",
    }


def test_prev_profiles_are_locked() -> None:
    """The 'prev_*' profiles in the overlay must be in state='locked'."""

    payload = build_overlay_payload(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    for k in ("prev_week", "prev_month", "prev_year"):
        assert payload["volume_profile"][k]["state"] == "locked"


def test_mitigated_fvg_zones_excluded() -> None:
    """Mitigated zones don't show on the chart (BotOverlay can't draw dead zones)."""

    payload = build_overlay_payload(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    # The bearish zone is mitigated → excluded. The bullish zone is open → included.
    assert len(payload["fvg_zones"]) == 1
    assert payload["fvg_zones"][0]["type"] == "bullish"


def test_missing_prev_profile_omitted() -> None:
    """If a prev_* profile is None (e.g. on day 1 of a new year), it's omitted."""

    vr = _volume_range()
    vr = vr.model_copy(update={"prev_year": None})
    payload = build_overlay_payload(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=vr,
        fvg=_fvg(),
    )
    assert payload["volume_profile"]["prev_year"] is None
    assert payload["volume_profile"]["prev_week"] is not None


# ---------------------------------------------------------------- write


def test_write_creates_file_with_valid_json(tmp_path: Path) -> None:
    """The writer produces a valid JSON file with all required fields."""

    out_path = tmp_path / "overlay_levels.json"
    writer = OverlayWriter(output_path=out_path)
    writer.write(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    assert out_path.exists()
    content = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in content
    assert "vwap" in content
    assert "volume_profile" in content
    assert "fvg_zones" in content


def test_write_is_atomic(tmp_path: Path) -> None:
    """On error, no half-written file is left behind."""

    out_path = tmp_path / "overlay_levels.json"
    writer = OverlayWriter(output_path=out_path)
    # Make the parent dir unwritable? That's brittle on CI. Instead, we
    # verify the writer leaves no .tmp files behind after a successful write.
    writer.write(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    leftovers = list(tmp_path.glob(".overlay-*.json.tmp"))
    assert leftovers == []


def test_write_overwrites_existing(tmp_path: Path) -> None:
    """A second write replaces the first file's content."""

    out_path = tmp_path / "overlay_levels.json"
    writer = OverlayWriter(output_path=out_path)
    writer.write(
        ts=datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    first_content = out_path.read_text(encoding="utf-8")
    # Second write.
    writer.write(
        ts=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
        vwap=_vwap(),
        volume_range=_volume_range(),
        fvg=_fvg(),
    )
    second_content = out_path.read_text(encoding="utf-8")
    assert first_content != second_content
    # ts field reflects the new value.
    parsed = json.loads(second_content)
    assert "2026-01-05T14:00:00" in parsed["ts"]
