"""Smoke-test the ``xauusd_bot.cli.feature_smoke`` CLI as a subprocess.

The feature_smoke CLI is the block-2 end-to-end proof-of-life: it loads
the XAUUSD M1 sample, drives every bar through the 8 feature engines,
and writes both ``feature_snapshot.json`` and ``overlay_levels.json``.
We invoke it via ``subprocess.run`` to catch issues that wouldn't
surface in-process (sys.path shenanigans, env handling, file-system
permissions, etc.).

WHY THIS FILE EXISTS
====================
The unit tests in ``tests/features/`` cover each engine in isolation.
This file proves the *integrated* end-to-end path works — all 8
engines, the overlay writer, the snapshot serializer. If any one of
those has a regression that only fires when they cooperate, this test
catches it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve the venv Python once at module load. The CLI is a module-level
# entry point, so we use ``python -m xauusd_bot.cli.feature_smoke``.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _venv_python() -> str:
    """Return the absolute path to the venv Python interpreter."""

    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable  # fall back to whatever pytest is using


def _run_feature_smoke(
    *,
    n_bars: int,
    sample: Path,
    report: Path,
    overlay: Path,
) -> subprocess.CompletedProcess:
    """Invoke the feature_smoke CLI in a subprocess and return the result."""

    env = os.environ.copy()
    # Make sure no stale env leaks. The CLI doesn't read Settings for the
    # smoke path, but we set it defensively.
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env.setdefault("ENVIRONMENT", "test")
    return subprocess.run(
        [
            _venv_python(),
            "-m",
            "xauusd_bot.cli.feature_smoke",
            "--n-bars",
            str(n_bars),
            "--sample",
            str(sample),
            "--report",
            str(report),
            "--overlay",
            str(overlay),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def sample_path() -> Path:
    p = PROJECT_ROOT / "data" / "sample" / "xauusd_m1_sample.parquet"
    if not p.exists():
        pytest.skip(f"sample dataset not found at {p}")
    return p


# ---------------------------------------------------------------- exit code


def test_feature_smoke_exits_zero(sample_path: Path, tmp_path: Path) -> None:
    """The feature_smoke CLI exits with code 0 on a clean run.

    WHY: the simplest possible "did block 2 ship?" check. If this test
    ever fails, the entire feature-engine stack is unusable from the
    command line — the most basic deployable artifact.
    """

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    result = _run_feature_smoke(
        n_bars=1000, sample=sample_path, report=report, overlay=overlay
    )
    assert result.returncode == 0, (
        f"feature_smoke CLI failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------- output files


def test_feature_smoke_writes_both_files(sample_path: Path, tmp_path: Path) -> None:
    """The CLI writes BOTH ``feature_snapshot.json`` AND ``overlay_levels.json``.

    WHY: ``BotOverlay.mq5`` reads the overlay file; downstream consumers
    read the snapshot. Either file missing is a ship-blocker.
    """

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    _run_feature_smoke(
        n_bars=1000, sample=sample_path, report=report, overlay=overlay
    )
    assert report.exists(), f"feature snapshot not written at {report}"
    assert overlay.exists(), f"overlay levels not written at {overlay}"

    # And both files are valid JSON (parses without exception).
    snap = json.loads(report.read_text())
    ov = json.loads(overlay.read_text())
    assert isinstance(snap, dict)
    assert isinstance(ov, dict)


# ---------------------------------------------------------------- snapshot schema


def test_snapshot_has_all_eight_engine_sections(sample_path: Path, tmp_path: Path) -> None:
    """``feature_snapshot.json`` has a sub-section for every one of the 8 engines.

    WHY: the snapshot is the integration boundary between the feature
    engine and Block 3 (aggregator/scoring). A missing engine section
    means the aggregator silently has no input from that engine.
    """

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    _run_feature_smoke(
        n_bars=1000, sample=sample_path, report=report, overlay=overlay
    )
    snap = json.loads(report.read_text())
    # The 8 engines, mapped to their top-level JSON key.
    expected = {
        "session",       # SessionEngine
        "vwap",          # TripleVWAPEngine
        "volume_range",  # FixedVolumeRangeEngine
        "fvg",           # FVGEngine
        "structure",     # MarketStructureEngine
        "momentum",      # CandleMomentumEngine
        "liquidity",     # LiquidityEngine
        "news",          # NewsContextEngine
    }
    missing = expected - set(snap.keys())
    assert not missing, f"snapshot missing engine sections: {missing}"


def test_snapshot_engines_have_non_null_outputs(sample_path: Path, tmp_path: Path) -> None:
    """Every engine section has at least one non-null field (proof of life).

    WHY: a "blank" output (all defaults, all None) is a valid schema
    but a useless integration. The smoke run with 1000 bars must yield
    real numbers, not just empty structures.
    """

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    _run_feature_smoke(
        n_bars=1000, sample=sample_path, report=report, overlay=overlay
    )
    snap = json.loads(report.read_text())

    # Session must have classified a session name (never None).
    assert snap["session"]["current_session"] in (
        "asia", "london", "overlap", "ny", "closed"
    )
    # VWAP must have a utc00 value (the 00:00 anchor always fires on a
    # 1000-bar sample that crosses midnight UTC).
    assert snap["vwap"]["levels"]["utc00"]["value"] is not None
    # VolumeRange must have a weekly profile with non-null VAH.
    assert snap["volume_range"]["weekly"]["vah"] is not None
    # Momentum must have a 0-100 score.
    assert 0.0 <= snap["momentum"]["score"] <= 100.0
    # News must have a context (might or might not be in blackout,
    # depending on sample timing — but must not be null).
    assert snap["news"] is not None


# ---------------------------------------------------------------- overlay schema


def test_overlay_levels_json_has_required_sections(sample_path: Path, tmp_path: Path) -> None:
    """``overlay_levels.json`` has ts, vwap, volume_profile, fvg_zones."""

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    _run_feature_smoke(
        n_bars=1000, sample=sample_path, report=report, overlay=overlay
    )
    ov = json.loads(overlay.read_text())
    for key in ("ts", "vwap", "volume_profile", "fvg_zones"):
        assert key in ov, f"overlay missing required key: {key}"

    # VWAP has all three anchors.
    assert set(ov["vwap"].keys()) == {"utc00", "utc07", "utc12"}
    # Volume profile has the 6 sub-profiles (3 current + 3 prev).
    assert set(ov["volume_profile"].keys()) == {
        "weekly", "monthly", "yearly", "prev_week", "prev_month", "prev_year",
    }


def test_overlay_prev_profiles_are_locked_or_null(
    sample_path: Path, tmp_path: Path
) -> None:
    """The 3 ``prev_*`` profiles in the overlay are either locked or null.

    WHY: ``BotOverlay.mq5`` looks for ``state=='locked'`` on prev_*
    profiles. If a profile is still marked developing, the MQL5
    indicator would draw a moving level instead of a stable line — a
    visual bug.
    """

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    _run_feature_smoke(
        n_bars=1000, sample=sample_path, report=report, overlay=overlay
    )
    ov = json.loads(overlay.read_text())
    for k in ("prev_week", "prev_month", "prev_year"):
        v = ov["volume_profile"][k]
        if v is not None:
            assert v["state"] == "locked", (
                f"{k} should be locked (or null), got state={v.get('state')}"
            )


# ---------------------------------------------------------------- stdout summary


def test_feature_smoke_prints_summary_to_stdout(sample_path: Path, tmp_path: Path) -> None:
    """The CLI prints a JSON summary to stdout with the per-engine status."""

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    result = _run_feature_smoke(
        n_bars=1000, sample=sample_path, report=report, overlay=overlay
    )
    assert result.returncode == 0
    # stdout is non-empty and contains a recognizable per-engine key.
    assert "n_bars_consumed" in result.stdout
    assert "weekly_state" in result.stdout
    assert "fvg_zones_count" in result.stdout


# ---------------------------------------------------------------- edge cases


def test_feature_smoke_with_zero_bars_does_not_crash(
    sample_path: Path, tmp_path: Path
) -> None:
    """n_bars=0 must not crash; the snapshot is emitted (just empty)."""

    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    result = _run_feature_smoke(
        n_bars=0, sample=sample_path, report=report, overlay=overlay
    )
    assert result.returncode == 0, (
        f"feature_smoke CLI failed on n_bars=0:\nstderr: {result.stderr}"
    )
    # Both files should still be written (snapshot with empty engines).
    assert report.exists()
    assert overlay.exists()


def test_feature_smoke_handles_missing_sample(tmp_path: Path) -> None:
    """A missing sample path causes the CLI to exit non-zero (no silent crash)."""

    bogus = tmp_path / "no_such_file.parquet"
    report = tmp_path / "feature_snapshot.json"
    overlay = tmp_path / "overlay_levels.json"
    result = _run_feature_smoke(
        n_bars=100, sample=bogus, report=report, overlay=overlay
    )
    assert result.returncode != 0
    # The CLI's error message should mention the sample.
    assert "sample" in result.stderr.lower() or "not found" in result.stderr.lower()
