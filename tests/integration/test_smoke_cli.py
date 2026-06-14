"""Smoke-test the ``xauusd_bot.cli.replay_smoke`` CLI as a subprocess.

The smoke CLI is the block-1 end-to-end proof-of-life: it loads the
sample, drives it through the data layer, and writes a JSON report.
We invoke it via ``subprocess.run`` to catch issues that wouldn't
surface in-process (sys.path shenanigans, env handling, etc.).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve the venv Python once at module load. The CLI is a module-level
# entry point, so we use ``python -m xauusd_bot.cli.replay_smoke``.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _venv_python() -> str:
    """Return the absolute path to the venv Python interpreter."""

    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable  # fall back to whatever pytest is using


def _run_smoke(*, n_bars: int = 1000, report_path: Path, sample: Path) -> subprocess.CompletedProcess:
    """Invoke the smoke CLI in a subprocess and return the result."""

    env = os.environ.copy()
    # Make sure no stale env leaks. The smoke CLI doesn't read Settings,
    # so this is mostly defensive.
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env.setdefault("ENVIRONMENT", "test")
    return subprocess.run(
        [
            _venv_python(),
            "-m",
            "xauusd_bot.cli.replay_smoke",
            "--n-bars",
            str(n_bars),
            "--sample",
            str(sample),
            "--report",
            str(report_path),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def sample_path() -> Path:
    p = PROJECT_ROOT / "data" / "sample" / "xauusd_m1_sample.parquet"
    if not p.exists():
        pytest.skip(f"sample dataset not found at {p}")
    return p


def test_smoke_cli_exits_zero(sample_path: Path, tmp_path: Path) -> None:
    """The smoke CLI exits with code 0 on a clean run."""

    report = tmp_path / "replay_smoke.json"
    result = _run_smoke(n_bars=500, report_path=report, sample=sample_path)
    assert result.returncode == 0, (
        f"smoke CLI failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_smoke_cli_writes_report_with_expected_keys(sample_path: Path, tmp_path: Path) -> None:
    """The smoke CLI writes logs/replay_smoke.json with the expected top-level keys."""

    report = tmp_path / "replay_smoke.json"
    result = _run_smoke(n_bars=500, report_path=report, sample=sample_path)
    assert result.returncode == 0, f"smoke CLI failed: {result.stderr}"
    assert report.exists(), f"Report not written at {report}"

    payload = json.loads(report.read_text())
    expected_keys = {
        "generated_at",
        "sample",
        "n_bars_requested",
        "n_bars_consumed",
        "elapsed_seconds",
        "bars_per_second",
        "first_bar_time",
        "last_bar_time",
        "current_t",
        "point_in_time_ok",
        "spread",
        "quality",
        "closed_bars_by_tf",
    }
    missing = expected_keys - set(payload.keys())
    assert not missing, f"smoke report missing keys: {missing}"


def test_smoke_cli_point_in_time_is_ok(sample_path: Path, tmp_path: Path) -> None:
    """The smoke report's point_in_time_ok must be true."""

    report = tmp_path / "replay_smoke.json"
    _run_smoke(n_bars=500, report_path=report, sample=sample_path)
    payload = json.loads(report.read_text())
    assert payload["point_in_time_ok"] is True


def test_smoke_cli_quality_report_is_clean(sample_path: Path, tmp_path: Path) -> None:
    """The synthetic sample is clean — quality.issues must be empty (or near-empty)."""

    report = tmp_path / "replay_smoke.json"
    _run_smoke(n_bars=500, report_path=report, sample=sample_path)
    payload = json.loads(report.read_text())
    assert payload["quality"]["n_gaps"] == 0
    assert payload["quality"]["n_spikes"] == 0
    assert payload["quality"]["n_ohlc_inconsistent"] == 0


def test_smoke_cli_bars_per_second_is_reasonable(sample_path: Path, tmp_path: Path) -> None:
    """The smoke CLI should process at least 1000 bars/sec on this dev box."""

    report = tmp_path / "replay_smoke.json"
    _run_smoke(n_bars=2000, report_path=report, sample=sample_path)
    payload = json.loads(report.read_text())
    assert payload["bars_per_second"] > 1000, (
        f"bars_per_second {payload['bars_per_second']} too low — performance regression?"
    )


def test_smoke_cli_prints_summary_to_stdout(sample_path: Path, tmp_path: Path) -> None:
    """The smoke CLI prints a one-line JSON summary to stdout."""

    report = tmp_path / "replay_smoke.json"
    result = _run_smoke(n_bars=500, report_path=report, sample=sample_path)
    assert result.returncode == 0
    # stdout is non-empty and contains a recognizable key.
    assert "n_bars_consumed" in result.stdout
    assert "point_in_time_ok" in result.stdout


def test_smoke_cli_handles_missing_sample(tmp_path: Path) -> None:
    """A missing sample path causes the CLI to exit with code 2 (sample error)."""

    bogus = tmp_path / "no_such_file.parquet"
    report = tmp_path / "report.json"
    result = _run_smoke(n_bars=100, report_path=report, sample=bogus)
    assert result.returncode != 0
    assert "sample" in result.stderr.lower() or "not found" in result.stderr.lower()
