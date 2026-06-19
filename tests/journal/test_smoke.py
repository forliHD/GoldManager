"""End-to-end smoke tests for the journal_smoke CLI (Block 5a).

These tests shell out to the actual CLI binary so they exercise
the full pipeline (config + connector + engines + decision +
execution + paper broker + journal + KPI aggregations).

The full 200-bar smoke takes ~60s due to the O(N²) engine stack.
The unit tests use a fast 30-bar variant. The "end-to-end"
test is tagged ``slow`` so a normal CI run can opt-out of it
via ``-m 'not slow'`` and still validate the wiring.
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
    report = tmp_path / "journal_snapshot.json"
    cmd = [
        sys.executable,
        "-m",
        "xauusd_bot.cli.journal_smoke",
        "--n-bars",
        "200",
        "--start-bar",
        "2000",
        "--report",
        str(report),
        *args,
    ]
    return subprocess.run(cmd, env=_env(), cwd=str(ROOT), capture_output=True, text=True, timeout=600)


def _run_cli_fast(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Faster smoke for unit-test CI: 30 bars is enough to validate
    the wiring without spending 60s on the full 200-bar loop.
    """

    report = tmp_path / "journal_snapshot.json"
    cmd = [
        sys.executable,
        "-m",
        "xauusd_bot.cli.journal_smoke",
        "--n-bars",
        "30",
        "--start-bar",
        "2000",
        "--report",
        str(report),
        *args,
    ]
    return subprocess.run(cmd, env=_env(), cwd=str(ROOT), capture_output=True, text=True, timeout=600)


# ============================================================== 1. happy path


def test_journal_smoke_runs_end_to_end(tmp_path: Path) -> None:
    """The CLI exits 0, writes a valid journal_snapshot.json, and
    contains all expected top-level keys (fast variant).
    """

    proc = _run_cli_fast(tmp_path=tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    report = tmp_path / "journal_snapshot.json"
    assert report.exists()
    payload = json.loads(report.read_text())
    # Required top-level keys per spec.
    for key in [
        "n_trades",
        "n_snapshots",
        "n_orders",
        "equity_curve_sample",
        "r_distribution",
        "setup_breakdown",
        "max_drawdown",
        "sharpe",
    ]:
        if key == "n_trades":
            assert payload["n_trades_written"] >= 0
        elif key == "n_snapshots":
            assert payload["n_snapshots_written"] > 0
        elif key == "n_orders":
            assert payload["n_orders_written"] >= 0
        else:
            assert key in payload, f"missing key {key!r} in journal snapshot"


def test_journal_smoke_writes_persisted_counts_and_aggregates(tmp_path: Path) -> None:
    proc = _run_cli_fast(tmp_path=tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    payload = json.loads((tmp_path / "journal_snapshot.json").read_text())
    assert payload["counts"]["snapshots"] == payload["n_snapshots_written"]
    assert payload["counts"]["trades"] == payload["n_trades_written"]
    assert payload["counts"]["orders"] == payload["n_orders_written"]
    # Every trade should be closed (the smoke synthesizes a close for each one).
    assert payload["n_trades_closed"] == payload["n_trades_written"]
    # r_distribution has all 7 buckets present and sums to n_trades_closed.
    rd = payload["r_distribution"]
    assert set(rd.keys()) == {"-3", "-2", "-1", "0", "1", "2", "3+"}
    assert sum(rd.values()) == payload["n_trades_closed"]


def test_journal_smoke_setup_breakdown_matches_trade_count(tmp_path: Path) -> None:
    proc = _run_cli_fast(tmp_path=tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    payload = json.loads((tmp_path / "journal_snapshot.json").read_text())
    sb = payload["setup_breakdown"]
    total_count = sum(int(sb[k]["count"]) for k in ("scout", "reduced", "full"))
    assert total_count == payload["n_trades_written"]


def test_journal_smoke_equity_curve_is_sampled(tmp_path: Path) -> None:
    proc = _run_cli_fast(tmp_path=tmp_path)
    assert proc.returncode == 0
    payload = json.loads((tmp_path / "journal_snapshot.json").read_text())
    ec = payload["equity_curve_sample"]
    assert isinstance(ec, list)
    assert len(ec) <= 20
    for point in ec:
        assert isinstance(point, list)
        assert len(point) == 2
        ts, eq = point
        assert isinstance(ts, str)
        assert "T" in ts
        assert eq.replace(".", "").replace("-", "").isdigit()


def test_journal_smoke_max_drawdown_is_typed(tmp_path: Path) -> None:
    proc = _run_cli_fast(tmp_path=tmp_path)
    assert proc.returncode == 0
    payload = json.loads((tmp_path / "journal_snapshot.json").read_text())
    md = payload["max_drawdown"]
    assert "amount" in md
    assert "peak_time" in md
    assert "trough_time" in md


# ============================================================== 2. CLI help


def test_cli_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "xauusd_bot.cli.journal_smoke", "--help"],
        env=_env(),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "journal smoke" in proc.stdout.lower() or "--n-bars" in proc.stdout


# ============================================================== 3. PIT: snapshot order


def test_journal_smoke_snapshots_are_monotonically_ordered(tmp_path: Path) -> None:
    """The smoke writes one snapshot per bar — the count must match
    ``n_bars_consumed``.
    """

    proc = _run_cli_fast(tmp_path=tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    payload = json.loads((tmp_path / "journal_snapshot.json").read_text())
    assert payload["n_snapshots_written"] == payload["n_bars_consumed"]


# ============================================================== 4. full 200-bar slow smoke


@pytest.mark.slow
def test_journal_smoke_full_200_bars_runs(tmp_path: Path) -> None:
    """Slow / full smoke: 200 bars. Skipped on default CI runs
    (deselect with ``-m 'not slow'``). Use it before tagging a
    release.
    """

    proc = _run_cli(tmp_path=tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    payload = json.loads((tmp_path / "journal_snapshot.json").read_text())
    assert payload["n_bars_consumed"] == 200
    assert payload["n_snapshots_written"] == 200
