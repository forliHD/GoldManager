"""Data layer: OHLC, spread, data-quality monitoring.

This package is the **deterministic** data backbone for the bot. It does
not decide what to trade; it makes sure the numbers that go into the
feature engines are clean, well-typed, and point-in-time correct.
"""

from xauusd_bot.data.ohlc_builder import OHLCBuilder
from xauusd_bot.data.quality_monitor import DataQualityMonitor, QualityReport
from xauusd_bot.data.spread_monitor import SpreadMonitor
from xauusd_bot.data.symbol_spec_loader import SymbolSpecLoader

__all__ = [
    "DataQualityMonitor",
    "OHLCBuilder",
    "QualityReport",
    "SpreadMonitor",
    "SymbolSpecLoader",
]
