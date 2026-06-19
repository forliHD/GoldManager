"""Pytest configuration & shared fixtures for xauusd_bot tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make src/ importable without installing (also covered by pyproject [tool.pytest.ini_options]).
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never read a real .env or shell environment.

    ``Settings`` is built from ``os.environ`` at construction time. For unit tests
    we set harmless defaults so missing-env crashes don't leak into every test.
    """

    monkeypatch.setenv("CONNECTOR_MODE", "replay")
    monkeypatch.setenv("SYMBOL", "XAUUSD")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd")
    monkeypatch.setenv("ENVIRONMENT", "test")
    # Make sure no stray OPENROUTER_API_KEY / NEWS_API_KEY is required for default tests.
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("NEWS_API_KEY", None)


@pytest.fixture
def sample_data_path() -> Path:
    """Path to the committed XAUUSD M1 sample parquet."""

    return ROOT / "data" / "sample" / "xauusd_m1_sample.parquet"
