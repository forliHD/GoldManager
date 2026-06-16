"""End-to-end smoke tests for the backtest_smoke CLI (Block 5b).

These tests shell out to the actual CLI binary so they exercise
the full pipeline: config + connector + features + decision +
execution + paper broker + backtest + walkforward + JSON report.

A "fast" variant (30 bars + small max) keeps the CI runtime under
a few seconds. A "full" variant (with WF) is tagged ``slow`` so
a normal CI run can opt out via ``-m 'not slow'``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["REDIS_URL"] = "redis://localhost:6379/0"
    env["TIMESCALEDB_URL"] = "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd"
    env["ENVIRONMENT"] = "test"
    env.setdefault("CONNECTOR_MODE", "replay")
    return env


def _run_cli(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    report = tmp_path / "backtest_snapshot.json"
    cmd = [
        sys.executable,
        "-m",
        "xauusd_bot.cli.backtest_smoke",
        *args,
        "--report",
        str(report),
    ]
    return subprocess.run(cmd, env=_env(), cwd=str(ROOT), capture_output=True, text=True, timeout=600)


# ============================================================== 1. fast smoke


def test_smoke_skips_walkforward_completes(tmp_path: Path) -> None:
    """The CLI exits 0 with --skip-walkforward in <30s and writes the JSON."""

    proc = _run_cli(
        "--start-date",
        "2026-04-01",
        "--end-date",
        "2026-04-02",
        "--warmup-bars",
        "50",
        "--max-bars",
        "30",
        "--skip-walkforward",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    report = tmp_path / "backtest_snapshot.json"
    assert report.exists()
    payload = json.loads(report.read_text())
    # Required top-level keys (per Block-5b spec).
    for key in (
        "n_bars_processed",
        "n_trades",
        "stats",
        "r_distribution",
        "equity_curve_sample",
        "setup_breakdown",
        "session_breakdown",
        "score_band_breakdown",
        "tags",
    ):
        assert key in payload, f"missing key {key!r} in backtest snapshot"
    # The summary stats live under ``stats`` (Sharpe, winrate, etc).
    for stat_key in ("sharpe", "winrate", "profit_factor", "max_drawdown"):
        assert stat_key in payload["stats"], f"missing stats.{stat_key!r}"
    assert payload["n_bars_processed"] > 0
    # wf section is absent (--skip-walkforward).
    assert "walkforward" not in payload


def test_smoke_r_distribution_has_all_seven_buckets(tmp_path: Path) -> None:
    proc = _run_cli(
        "--start-date",
        "2026-04-01",
        "--end-date",
        "2026-04-02",
        "--warmup-bars",
        "50",
        "--max-bars",
        "30",
        "--skip-walkforward",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    payload = json.loads((tmp_path / "backtest_snapshot.json").read_text())
    assert set(payload["r_distribution"].keys()) == {"-3", "-2", "-1", "0", "1", "2", "3+"}


def test_smoke_tags_record_slippage_and_spread_models(tmp_path: Path) -> None:
    proc = _run_cli(
        "--start-date",
        "2026-04-01",
        "--end-date",
        "2026-04-02",
        "--warmup-bars",
        "50",
        "--max-bars",
        "30",
        "--skip-walkforward",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0
    payload = json.loads((tmp_path / "backtest_snapshot.json").read_text())
    assert "slippage_model" in payload["tags"]
    assert "spread_model" in payload["tags"]
    assert "FixedSlippage" in payload["tags"]["slippage_model"]


def test_smoke_equity_curve_is_sampled(tmp_path: Path) -> None:
    proc = _run_cli(
        "--start-date",
        "2026-04-01",
        "--end-date",
        "2026-04-02",
        "--warmup-bars",
        "50",
        "--max-bars",
        "30",
        "--skip-walkforward",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0
    payload = json.loads((tmp_path / "backtest_snapshot.json").read_text())
    ec = payload["equity_curve_sample"]
    assert isinstance(ec, list)
    assert len(ec) <= 20
    for point in ec:
        assert isinstance(point, list)
        assert len(point) == 2


def test_smoke_setup_breakdown_keys_present(tmp_path: Path) -> None:
    proc = _run_cli(
        "--start-date",
        "2026-04-01",
        "--end-date",
        "2026-04-02",
        "--warmup-bars",
        "50",
        "--max-bars",
        "30",
        "--skip-walkforward",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0
    payload = json.loads((tmp_path / "backtest_snapshot.json").read_text())
    sb = payload["setup_breakdown"]
    for key in ("scout", "reduced", "full"):
        assert key in sb


# ============================================================== 2. CLI help


def test_cli_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "xauusd_bot.cli.backtest_smoke", "--help"],
        env=_env(),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "backtest" in proc.stdout.lower() or "--start-date" in proc.stdout


# ============================================================== 3. with WalkForward


@pytest.mark.slow
def test_smoke_with_walkforward_runs(tmp_path: Path) -> None:
    """Smoke with the walkforward enabled.

    This is tagged ``slow`` because the WF inner-backtests each
    take a few seconds. Opt out with ``-m 'not slow'``.
    """

    proc = _run_cli(
        "--start-date",
        "2026-04-01",
        "--end-date",
        "2026-04-04",
        "--warmup-bars",
        "50",
        "--max-bars",
        "15",
        "--in-sample-days",
        "1",
        "--out-of-sample-days",
        "1",
        "--step-days",
        "1",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    payload = json.loads((tmp_path / "backtest_snapshot.json").read_text())
    assert "walkforward" in payload
    wf = payload["walkforward"]
    for key in (
        "windows",
        "robustness_matrix",
        "mean_oos_sharpe",
        "std_oos_sharpe",
        "oos_sharpe_degradation",
        "is_overfit",
    ):
        assert key in wf, f"missing wf.{key!r}"
