"""Entry-zone gate — honour the LLM's proposed entry zone instead of
market-chasing at the signal-bar price.

Block 4 follow-up (2026-06-28). The AI layer proposes an entry *zone*
(:attr:`LLMIntent.entry_min` / :attr:`LLMIntent.entry_max`) — the
discount level where it wants to buy, or the premium level to sell. Both
the live :class:`ExecutionPipeline` and the :class:`BacktestEngine`
previously *ignored* it and filled at the current bar close (``ref_price``
/ ``bar.close``). That made longs chase into local highs and then revert:
in the ``poc_v2`` 2-day LLM backtest, 2 of 3 longs entered near a local
high (4046, 4085) and lost, while the one entry taken right after a dip
(4050) won.

This gate refuses an entry that sits on the *wrong* side of the proposed
zone — a long above ``entry_max`` (a premium chase) or a short below
``entry_min`` (a chase down) — and lets a later bar fill once price has
come back to the zone. It is deliberately one-directional:

* It only ever **blocks** an entry. It never moves the entry price, never
  changes size, and never widens risk.
* Entering *at or beyond the favourable bound* (a deeper discount for a
  long, a higher premium for a short) is always allowed — taking a better
  price than proposed is never punished.
* A ``None`` bound is open-ended (no gate on that side), matching
  :class:`EntryZone` semantics; if the LLM gave no zone at all the gate is
  a no-op and behaviour is unchanged.
"""

from __future__ import annotations

# Block reasons (kept stable — surfaced in ExecutionOutcome.blocked_reason
# and the backtest decision log, and asserted in tests).
ENTRY_ABOVE_ZONE = "entry_above_zone"  # long: ref_price > entry_max (premium chase)
ENTRY_BELOW_ZONE = "entry_below_zone"  # short: ref_price < entry_min (chase down)


def check_entry_zone(
    *,
    is_long: bool,
    price: float,
    entry_min: float | None,
    entry_max: float | None,
    tol: float = 0.0,
) -> str | None:
    """Return a block reason if ``price`` is on the wrong side of the LLM's
    proposed entry zone, else ``None`` (entry allowed).

    Args:
        is_long: ``True`` for a long (BUY), ``False`` for a short (SELL).
        price: the prospective fill / reference price (USD).
        entry_min: lower bound of the proposed zone (USD) or ``None``.
        entry_max: upper bound of the proposed zone (USD) or ``None``.
        tol: absolute price tolerance (USD) added to the rejected bound, so
            a fill a hair outside the zone is still allowed. ``0.0`` means
            the zone bound is enforced exactly.

    Rules:
        * Long  → block when ``price > entry_max + tol`` (chasing above the
          zone / buying in premium).
        * Short → block when ``price < entry_min - tol`` (chasing below the
          zone / selling in discount).
    """
    if tol < 0:
        tol = 0.0
    if is_long:
        if entry_max is not None and price > entry_max + tol:
            return ENTRY_ABOVE_ZONE
    else:
        if entry_min is not None and price < entry_min - tol:
            return ENTRY_BELOW_ZONE
    return None
