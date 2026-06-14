"""Session Engine — classify current session, track H/L/O and sweeps.

The session engine answers: "Which trading session are we in right now,
and what has the price done so far this session?"

Sessions (UTC)
--------------
* Asia:      00:00 – 07:00
* London:    07:00 – 12:00
* NY:        12:00 – 21:00
* Overlap:   12:00 – 16:00   (London + NY both open, the busiest window)
* Closed:    21:00 – 00:00   (no major session, low-liquidity)

The Overlap window is reported separately from NY so the risk-engine
can use a tighter risk factor for the most volatile window. Per
AGENTS.md §1 the engine never imports the connector — it only sees
:class:`Bar` lists (PIT-filtered by the caller).

Point-in-Time
-------------
Only bars with ``time <= current_t`` are accepted. The engine makes
no future assumption. The "current session" is the session that
contains ``current_t`` (by hour-of-day); ``session_start`` is the
last 00:00 / 07:00 / 12:00 / 21:00 boundary at or before ``current_t``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import structlog

from xauusd_bot.common.schemas.features import SessionEngineOutput, SessionName
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- boundaries


@dataclass(frozen=True)
class _Window:
    """A trading session window. Times are UTC hour:minute."""

    name: SessionName
    start: time
    end: time  # exclusive
    risk_factor: float


# Order matters for the "current session" lookup: Overlap is a sub-window
# of NY, so it must be checked first.
_WINDOWS: tuple[_Window, ...] = (
    _Window(SessionName.ASIA, time(0, 0), time(7, 0), 0.5),
    _Window(SessionName.LONDON, time(7, 0), time(12, 0), 1.0),
    _Window(SessionName.OVERLAP, time(12, 0), time(16, 0), 0.7),
    _Window(SessionName.NY, time(16, 0), time(21, 0), 1.0),
    _Window(SessionName.CLOSED, time(21, 0), time(23, 59, 59, 999_999), 0.3),
)


def _classify(ts: datetime) -> _Window:
    """Return the session window containing ``ts`` (UTC)."""

    t = ts.astimezone(UTC).time()
    for w in _WINDOWS:
        if w.start <= t < w.end:
            return w
    # Edge case: ts at exactly 23:59:59.999_999 — Closed window covers it.
    return _WINDOWS[-1]


def _session_start_end(ts: datetime) -> tuple[datetime, datetime]:
    """The [start, end) datetime pair of the session containing ``ts``."""

    w = _classify(ts)
    base = ts.astimezone(UTC).replace(hour=w.start.hour, minute=w.start.minute, second=0, microsecond=0)
    if w.end == time(23, 59, 59, 999_999):
        # The "closed" window runs into the next day at 00:00.
        end = (base + timedelta(days=1)).replace(hour=0, minute=0)
    else:
        end = base.replace(hour=w.end.hour, minute=w.end.minute) + timedelta(
            days=1 if w.end <= w.start else 0
        )
        # The "end" we want is the next *occurrence* of w.end at or after base.
        candidate = base.replace(hour=w.end.hour, minute=w.end.minute)
        if candidate <= base:
            candidate += timedelta(days=1)
        end = candidate
    return base, end


# ---------------------------------------------------------------- engine


class SessionEngine:
    """Track the current session and its running H/L/O.

    Parameters
    ----------
    equal_threshold_atr:
        Two swing points are "equal" if they sit within this many ATRs
        of each other. Default 0.5.
    """

    def __init__(self, equal_threshold_atr: float = 0.5) -> None:
        self._equal_thr = equal_threshold_atr

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> SessionEngineOutput:
        """Compute session state for ``current_t`` given all visible bars.

        The caller is expected to pass only bars with ``time <= current_t``
        (point-in-time). The engine defensively filters anyway, so it's
        safe to over-pass.
        """

        pit_bars = [b for b in bars if b.time <= current_t]
        window = _classify(current_t)
        start, end = _session_start_end(current_t)
        pit_bars.sort(key=lambda b: b.time)

        # Bars in the current session, in chronological order.
        in_session = [b for b in pit_bars if start <= b.time < end]

        # Open / High / Low. We use the first bar's open as the session open.
        # If no bars yet, leave them None (engine still reports a session name
        # so the aggregator has something to bind).
        if in_session:
            s_open = Decimal(str(in_session[0].open))
            s_high = max(Decimal(str(b.high)) for b in in_session)
            s_low = min(Decimal(str(b.low)) for b in in_session)
        else:
            s_open = None
            s_high = None
            s_low = None

        # Progress: how far through the session we are, in percent.
        total_seconds = (end - start).total_seconds()
        elapsed = max(0.0, (current_t - start).total_seconds())
        progress = 100.0 * min(1.0, elapsed / total_seconds) if total_seconds > 0 else 0.0

        # Sweep detection: latest bar's high swept the *prior* session high
        # (or low swept the *prior* session low) and the bar closed back
        # inside. We compare against the prior session extreme (excluding
        # the latest bar) so that the sweep bar itself doesn't redefine
        # the level.
        is_sweep = False
        equal_highs = False
        equal_lows = False
        if len(in_session) >= 2:
            prior = in_session[:-1]
            prior_high = max(Decimal(str(b.high)) for b in prior)
            prior_low = min(Decimal(str(b.low)) for b in prior)
            last = in_session[-1]
            if float(last.high) > float(prior_high) and float(last.close) < float(prior_high) or float(last.low) < float(prior_low) and float(last.close) > float(prior_low):
                is_sweep = True
        # Now compute equal-highs/lows using the running session extremes
        # (s_high / s_low are over all in-session bars, which is what the
        # `equal_*` test is asking about: "are there multiple swing points
        # near the running extreme?").
        if in_session and s_high is not None and s_low is not None:
            # Equal-highs / equal-lows: at least two distinct bars in the
            # session have the same extreme within 0.5*ATR. We need ATR for
            # this — compute it across all pit_bars, not just in-session.
            df = bars_to_df(pit_bars)
            atr_val = compute_atr(df, period=14) if len(df) >= 14 else None
            if atr_val and atr_val > 0:
                band = self._equal_thr * atr_val
                # Equal highs: count how many bars in the session have high
                # within `band` of the session high.
                near_high = sum(1 for b in in_session if abs(float(b.high) - float(s_high)) <= band)
                near_low = sum(1 for b in in_session if abs(float(b.low) - float(s_low)) <= band)
                equal_highs = near_high >= 2
                equal_lows = near_low >= 2

        return SessionEngineOutput(
            current_session=window.name,
            session_start=start,
            session_end=end,
            session_open=s_open,
            session_high=s_high,
            session_low=s_low,
            session_progress_pct=round(progress, 2),
            is_session_sweep=is_sweep,
            equal_highs_flag=equal_highs,
            equal_lows_flag=equal_lows,
            session_risk_factor=window.risk_factor,
        )
