"""Lifecycle smoke for execution_smoke CLI — end-to-end proof-of-life.

Loads the committed XAUUSD M1 sample dataset, drives the full
Replay → Decision → Execution pipeline, and asserts the lifecycle
report contains the expected phases. Verifies the daily-pause
trigger via --simulate-losses.
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
    report = tmp_path / "lifecycle.json"
    cmd = [
        sys.executable, "-m", "xauusd_bot.cli.execution_smoke",
        "--n-bars", "200", "--start-bar", "2000",
        "--report", str(report),
        *args,
    ]
    return subprocess.run(cmd, env=_env(), cwd=str(ROOT), capture_output=True, text=True, timeout=600)


# ============================================================== 1. natural smoke


def test_execution_smoke_runs_end_to_end(tmp_path: Path) -> None:
    """Force a synthetic trade so the full pipeline runs deterministically."""

    proc = _run_cli("--force-trade", tmp_path=tmp_path)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    report_path = tmp_path / "lifecycle.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text())
    assert payload["qualifications"] >= 1
    phases = {p["phase"] for p in payload["phases"]}
    assert "risk_approve" in phases
    assert "stops_compute" in phases
    assert "position_size" in phases
    assert "order_send" in phases
    assert "pending_sweep" in phases
    assert "trail" in phases
    assert payload["lifecycle"] is not None
    # Risk verdict is approved.
    assert payload["lifecycle"]["risk"]["approved"] is True
    # Order was submitted (Replay returns accepted).
    assert payload["lifecycle"]["order"]["state"] in ("submitted", "filled")


# ============================================================== 2. loss-simulation


def test_simulated_losses_trigger_pause(tmp_path: Path) -> None:
    proc = _run_cli("--force-trade", "--simulate-losses", "5", tmp_path=tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    payload = json.loads((tmp_path / "lifecycle.json").read_text())
    assert payload["simulated_losses"] >= 1
    assert payload["pause_triggered"] is True
    # The emergency state file is persisted.
    state_file = tmp_path / "lifecycle.json"
    # We wrote emergency_stop_state.json next to the report.
    assert (state_file.parent / "emergency_stop_state.json").exists()


# ============================================================== 3. natural smoke finds no trade


def test_natural_smoke_runs_without_force(tmp_path: Path) -> None:
    """Without --force-trade, the smoke should still exit 0 (just no trade)."""

    proc = _run_cli(tmp_path=tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


# ============================================================== 4. CLI help


def test_cli_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "xauusd_bot.cli.execution_smoke", "--help"],
        env=_env(), cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert "--n-bars" in proc.stdout
    assert "--force-trade" in proc.stdout
    assert "--simulate-losses" in proc.stdout


# ============================================================== 5. lifecycle JSON structure


def test_lifecycle_json_has_full_shape(tmp_path: Path) -> None:
    proc = _run_cli("--force-trade", tmp_path=tmp_path)
    assert proc.returncode == 0
    payload = json.loads((tmp_path / "lifecycle.json").read_text())
    lc = payload["lifecycle"]
    # Every key populated when a trade fires.
    for key in ("setup_id", "qualification", "risk", "sizing", "stops", "order", "phases"):
        assert key in lc
    # The sizing result has the canonical fields.
    sizing = lc["sizing"]
    assert "volume_lots" in sizing
    assert "rounding_mode" in sizing
    assert "formula_used" in sizing
    # The stops include the multi-tier TP plan.
    stops = lc["stops"]
    assert "tp1_price" in stops
    assert "tp2_price" in stops
    assert "tp3_price" in stops
    assert "sl_price" in stops
    assert len(stops["partial_close_plan"]) == 3
