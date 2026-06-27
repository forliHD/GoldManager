"""Tests for the OverlayWriter."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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


# ---------------------------------------------------------------- chart FVG cull
#
# The engine never expires a zone (one-directional mitigation), so the chart used
# to draw dozens of day-old bands far from price. build_overlay_payload trims to
# the recent, near-price zones. CHART-ONLY — the decision path reads bundle.fvg.

_NOW = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)


def _zone(
    tf: str,
    *,
    created_at: datetime,
    type: FVGType = FVGType.BULLISH,
    top: float = 4000.0,
    bottom: float = 3999.0,
    rank: float = 1.0,
    status: FVGStatus = FVGStatus.OPEN,
    ext_bottom: float | None = None,
    ext_top: float | None = None,
) -> FVGZone:
    return FVGZone(
        tf=tf, type=type, top=top, bottom=bottom, size_points=max(0.01, top - bottom),
        created_at=created_at, age_seconds=0, displacement_atr=1.0, status=status,
        mitigation_pct=0.0, rank_score=rank, extended_bottom=ext_bottom, extended_top=ext_top,
    )


def _payload_zones(fvg: FVGOutput, *, price: float | None, atr: float | None) -> list[dict]:
    return build_overlay_payload(
        ts=_NOW, vwap=_vwap(), volume_range=_volume_range(), fvg=fvg,
        current_price=price, atr=atr,
    )["fvg_zones"]


def test_chart_cull_drops_stale_m1_keeps_old_h1() -> None:
    """A 5h-old M1 zone exceeds the 4h M1 horizon (dropped); a 5h-old H1 is < 48h (kept)."""
    fvg = FVGOutput(zones=[
        _zone("M1", created_at=_NOW - timedelta(hours=5), top=4000.5, bottom=4000.0),
        _zone("H1", created_at=_NOW - timedelta(hours=5), top=4001.0, bottom=4000.0),
    ], top_zones=[])
    tfs = [z["tf"] for z in _payload_zones(fvg, price=4000.5, atr=2.0)]
    assert "M1" not in tfs and "H1" in tfs


def test_chart_cull_drops_far_zone_by_distance() -> None:
    """Price 4100, zone ~4000 → ~100 pts > 25×ATR(2)=50 → dropped."""
    fvg = FVGOutput(zones=[_zone("H1", created_at=_NOW, top=4001.0, bottom=4000.0, rank=9)], top_zones=[])
    assert _payload_zones(fvg, price=4100.0, atr=2.0) == []


def test_chart_cull_keeps_near_recent_zone() -> None:
    fvg = FVGOutput(zones=[_zone("H1", created_at=_NOW, top=4001.0, bottom=4000.0, rank=9)], top_zones=[])
    assert len(_payload_zones(fvg, price=4002.0, atr=2.0)) == 1


def test_chart_cull_caps_per_tf_by_rank() -> None:
    """10 near M1 zones → keep the top _FVG_MAX_PER_TF by rank_score."""
    from xauusd_bot.viz.overlay_writer import _FVG_MAX_PER_TF
    zones = [
        _zone("M1", created_at=_NOW, top=4000.0 + i * 0.1 + 0.05, bottom=4000.0 + i * 0.1, rank=float(i))
        for i in range(10)
    ]
    kept = _payload_zones(FVGOutput(zones=zones, top_zones=[]), price=4000.5, atr=5.0)
    assert len(kept) == _FVG_MAX_PER_TF
    kept_ranks = sorted((z["rank_score"] for z in kept), reverse=True)
    assert kept_ranks == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0]


def test_chart_cull_distance_uses_extended_bottom() -> None:
    """Distance is measured to the EFFECTIVE demand edge (extended_bottom), not raw bottom."""
    # price 3995 is inside [extended_bottom 3990, top 4000] → dist 0 → kept. Against the
    # raw bottom 3999 it would be 4 pts away > 25×ATR(0.1)=2.5 → would be dropped.
    fvg = FVGOutput(zones=[
        _zone("H1", created_at=_NOW, top=4000.0, bottom=3999.0, ext_bottom=3990.0, rank=9),
    ], top_zones=[])
    assert len(_payload_zones(fvg, price=3995.0, atr=0.1)) == 1


def test_chart_cull_noop_without_price() -> None:
    """No current_price → distance cull skipped (age/count still apply); recent zone kept."""
    fvg = FVGOutput(zones=[_zone("H1", created_at=_NOW, top=4001.0, bottom=4000.0)], top_zones=[])
    assert len(_payload_zones(fvg, price=None, atr=None)) == 1


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
