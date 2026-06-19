"""Top-level package for the XAUUSD trading bot.

See `00_FINAL_PLAN.md` for the architecture. This package is split into:

* ``connectors`` — broker / data-feed abstraction (Replay / Live / Paper).
* ``data`` — OHLC, spread, and data-quality monitoring.
* ``features`` — feature engines (session, VWAP, FVG, structure, ...).
* ``decision`` — scoring + AI layer.
* ``execution`` — risk, sizing, orders, emergency stop.
* ``journal`` — TimescaleDB-backed journal of decisions, orders, fills.
* ``review`` — backtest, walk-forward, daily/weekly review.
* ``viz`` — overlay file writer for the MT5 chart indicator.
* ``common`` — cross-cutting config, schemas, messaging, logging.

The package exposes a single ``__version__`` string so other tools (CI,
dashboard, etc.) can introspect it.
"""

__version__ = "0.1.0"
