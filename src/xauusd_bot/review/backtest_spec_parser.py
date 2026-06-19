"""BacktestSpec-Parser — Block 5c Phase 4.

Parses a free-form ``validation_test`` description from a
:class:`xauusd_bot.common.schemas.review.ReviewProposal` into a
typed :class:`BacktestSpec` the FittingProposalEngine can run.

Scope
-----
This parser is intentionally **narrow**. It recognises only a fixed
set of patterns:

* ``score_threshold=<int>``        → ``score_threshold=N``
* ``session=<word>``                → ``session="<word>"``
* ``IS=<int>w`` / ``IS=<int>d``     → ``is_weeks`` / ``is_days``
* ``OOS=<int>w`` / ``OOS=<int>d``   → ``oos_weeks`` / ``oos_days``

Anything more elaborate (e.g. ``category_filter=score_band>0.8``)
returns :class:`BacktestSpec` with all fields ``None`` — the
proposal stays ``status='proposed'`` until the operator manually
runs :meth:`FittingProposalEngine.run_validation` with their own
spec.

Caveat 4i.5: **the parser is semi-structured**. This is documented;
NLP-grade parsing is out of scope for Block 5c.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------- types


@dataclass(frozen=True)
class BacktestSpec:
    """A simple, parsed validation-test specification.

    Fields are ``None`` when the regex didn't match. The FittingProposalEngine
    treats any ``None``-containing spec as "skip — keep status='proposed'".

    The dataclass is frozen so callers can't mutate it accidentally
    and the hash works for caching.
    """

    score_threshold: int | None = field(default=None)
    session: str | None = field(default=None)
    is_weeks: int | None = field(default=None)
    oos_weeks: int | None = field(default=None)
    is_days: int | None = field(default=None)
    oos_days: int | None = field(default=None)
    # Original text — useful for log/audit ("proposal tested with X").
    raw: str = field(default="")

    def is_empty(self) -> bool:
        """True iff no field was matched (parser found nothing parseable)."""

        return (
            self.score_threshold is None
            and self.session is None
            and self.is_weeks is None
            and self.oos_weeks is None
            and self.is_days is None
            and self.oos_days is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "score_threshold": self.score_threshold,
            "session": self.session,
            "is_weeks": self.is_weeks,
            "oos_weeks": self.oos_weeks,
            "is_days": self.is_days,
            "oos_days": self.oos_days,
            "raw": self.raw,
        }


# ---------------------------------------------------------------- patterns
#
# Patterns are case-insensitive and tolerant of whitespace.
# Examples that should match:
#   "score_threshold=70"
#   "score_threshold = 75, IS=4w, OOS=1w"
#   "session=ny, IS=4d, OOS=1d"
#   "asdf"      → nothing
#   ""          → nothing

_RE_SCORE = re.compile(r"score_threshold\s*=\s*(\d+)", re.IGNORECASE)
_RE_SESSION = re.compile(r"session\s*=\s*([a-z][a-z0-9_-]*)", re.IGNORECASE)
_RE_IS_WEEKS = re.compile(r"\bis\s*=\s*(\d+)\s*w\b", re.IGNORECASE)
_RE_OOS_WEEKS = re.compile(r"\boos\s*=\s*(\d+)\s*w\b", re.IGNORECASE)
_RE_IS_DAYS = re.compile(r"\bis\s*=\s*(\d+)\s*d\b", re.IGNORECASE)
_RE_OOS_DAYS = re.compile(r"\boos\s*=\s*(\d+)\s*d\b", re.IGNORECASE)


def _first_int(pattern: re.Pattern[str], text: str) -> int | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return None


def _first_session(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1).strip().lower() or None


# ---------------------------------------------------------------- parser


def parse_validation_test(validation_test: str) -> BacktestSpec | None:
    """Parse a free-form validation_test string into a :class:`BacktestSpec`.

    Returns ``None`` iff the input is empty or whitespace-only. For
    inputs that contain *no* recognised patterns, returns a
    :class:`BacktestSpec` with all fields ``None`` (and
    ``is_empty() == True``) — the FittingProposalEngine uses that as
    the signal to skip the validation backtest and keep the
    proposal at status='proposed'.

    Parameters
    ----------
    validation_test:
        The free-form validation description. Typically something
        like ``"score_threshold=70, IS=4w, OOS=1w"``.

    Returns
    -------
    :class:`BacktestSpec` | None
        None iff the input is empty.
    """

    if validation_test is None:
        return None
    text = validation_test.strip()
    if not text:
        return None

    return BacktestSpec(
        score_threshold=_first_int(_RE_SCORE, text),
        session=_first_session(_RE_SESSION, text),
        is_weeks=_first_int(_RE_IS_WEEKS, text),
        oos_weeks=_first_int(_RE_OOS_WEEKS, text),
        is_days=_first_int(_RE_IS_DAYS, text),
        oos_days=_first_int(_RE_OOS_DAYS, text),
        raw=text,
    )


__all__ = ["BacktestSpec", "parse_validation_test"]