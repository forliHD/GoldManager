"""Pytest fixtures for xauusd_bot execution-layer tests (Block 4).

The factory functions live in ``tests/_execution_factories.py`` so
test modules can import them as plain functions. This conftest
re-exposes them as pytest fixtures for convenience.
"""

from __future__ import annotations

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import TradeQualification
from xauusd_bot.connectors.schemas import AccountInfo, SymbolSpec

from tests._execution_factories import (
    make_account,
    make_qualification,
    make_settings,
    make_symbol_spec,
)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def account() -> AccountInfo:
    return make_account()


@pytest.fixture
def symbol_spec() -> SymbolSpec:
    return make_symbol_spec()


@pytest.fixture
def qualification() -> TradeQualification:
    return make_qualification()
