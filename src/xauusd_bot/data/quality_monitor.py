"""Data quality monitor — gap / spike / OHLC / spec-drift detector.

The :class:`DataQualityMonitor` watches a stream of bars and flags:

* **Gaps** — missing bars between consecutive timestamps. A gap is
  counted when ``bar.time - prev.time > expected_step * 1.5``.
* **Spikes** — bar range exceeds ``spike_atr_multiple * ATR(20)``.
* **OHLC inconsistency** — ``high < low`` or ``close > high`` /
  ``close < low`` (impossible bar).
* **Spec drift** — a bar's price exceeds the symbol's price limit by
  more than ``spec_drift_tolerance``.

The monitor accumulates a :class:`QualityReport` that the smoke CLI
serializes to JSON. The report is the input for the data-layer
dashboard and the journal.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Deque

import structlog

from xauusd_bot.connectors.schemas import Bar, SymbolSpec

log = structlog.get_logger(__name__)


@dataclass
class QualityIssue:
    """A single data-quality flag."""

    kind: str  # 'gap' | 'spike' | 'ohlc_inconsistent' | 'spec_drift'
    time: datetime
    detail: str


@dataclass
class QualityReport:
    """Summary of issues across the processed bar range."""

    n_bars: int = 0
    n_gaps: int = 0
    n_spikes: int = 0
    n_ohlc_inconsistent: int = 0
    n_spec_drift: int = 0
    max_gap_bars: int = 0
    issues: list[QualityIssue] = field(default_factory=list)
    first_bar_time: datetime | None = None
    last_bar_time: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "n_bars": self.n_bars,
            "n_gaps": self.n_gaps,
            "n_spikes": self.n_spikes,
            "n_ohlc_inconsistent": self.n_ohlc_inconsistent,
            "n_spec_drift": self.n_spec_drift,
            "max_gap_bars": self.max_gap_bars,
            "first_bar_time": self.first_bar_time.isoformat() if self.first_bar_time else None,
            "last_bar_time": self.last_bar_time.isoformat() if self.last_bar_time else None,
            "issues": [
                {"kind": i.kind, "time": i.time.isoformat(), "detail": i.detail}
                for i in self.issues[:50]
            ],
        }


class DataQualityMonitor:
    """Stream a bar series, accumulate a :class:`QualityReport`."""

    def __init__(
        self,
        spec: SymbolSpec,
        *,
        timeframe_minutes: int = 1,
        spike_atr_multiple: float = 8.0,
        spec_drift_tolerance: float = 0.10,
        max_issues: int = 500,
    ) -> None:
        self._spec = spec
        self._step = timedelta(minutes=timeframe_minutes)
        self._spike_mult = spike_atr_multiple
        self._drift_tol = spec_drift_tolerance
        self._max_issues = max_issues
        # Range buffer for ATR
        self._ranges: Deque[float] = deque(maxlen=20)
        self._report = QualityReport()
        self._prev_time: datetime | None = None
        self._prev_close: Decimal | None = None

    @property
    def report(self) -> QualityReport:
        return self._report

    def update(self, bar: Bar) -> None:
        """Inspect ``bar`` and update the report."""

        self._report.n_bars += 1
        if self._report.first_bar_time is None:
            self._report.first_bar_time = bar.time
        self._report.last_bar_time = bar.time

        # OHLC consistency
        if bar.high < bar.low:
            self._flag("ohlc_inconsistent", bar.time, f"high={bar.high} < low={bar.low}")
            return
        if bar.close > bar.high or bar.close < bar.low:
            self._flag("ohlc_inconsistent", bar.time, f"close={bar.close} outside H={bar.high} L={bar.low}")
            return
        if bar.open > bar.high or bar.open < bar.low:
            self._flag("ohlc_inconsistent", bar.time, f"open={bar.open} outside H={bar.high} L={bar.low}")
            return

        # Spec drift (price-limit threshold check). Tolerance is a fraction.
        if self._spec.price_limit_max is not None:
            limit_with_tolerance = self._spec.price_limit_max * (Decimal("1") + Decimal(str(self._drift_tol)))
            if bar.high > limit_with_tolerance:
                self._flag(
                    "spec_drift",
                    bar.time,
                    f"high={bar.high} > limit_max*(1+{self._drift_tol})={self._spec.price_limit_max}",
                )

        # Gap detection
        if self._prev_time is not None:
            gap = (bar.time - self._prev_time) / self._step
            if gap >= 1.5:
                gap_bars = int(gap) - 1
                self._report.max_gap_bars = max(self._report.max_gap_bars, gap_bars)
                self._flag("gap", bar.time, f"missed {gap_bars} bar(s); prev={self._prev_time.isoformat()}")

        # Spike detection
        bar_range = float(bar.high - bar.low)
        self._ranges.append(bar_range)
        if len(self._ranges) >= 20:
            atr = sum(self._ranges) / len(self._ranges)
            if atr > 0 and bar_range > self._spike_mult * atr:
                self._flag("spike", bar.time, f"range={bar_range:.2f} > {self._spike_mult}x ATR={atr:.2f}")

        self._prev_time = bar.time
        self._prev_close = bar.close

    def _flag(self, kind: str, t: datetime, detail: str) -> None:
        if len(self._report.issues) >= self._max_issues:
            return
        if kind == "gap":
            self._report.n_gaps += 1
        elif kind == "spike":
            self._report.n_spikes += 1
        elif kind == "ohlc_inconsistent":
            self._report.n_ohlc_inconsistent += 1
        elif kind == "spec_drift":
            self._report.n_spec_drift += 1
        self._report.issues.append(QualityIssue(kind=kind, time=t, detail=detail))
        log.warning("data_quality_issue", kind=kind, time=t.isoformat(), detail=detail)
