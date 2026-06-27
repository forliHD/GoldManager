"""FeatureAggregator — Block 3 Phase 0.

Consumes a :class:`xauusd_bot.common.schemas.features.FeatureSnapshotBundle`
(the Block 2 output) and emits an
:class:`xauusd_bot.common.schemas.decision.AggregatedFeatures` with:

* one :class:`EngineSubscore` per known engine (H1, M5, TripleVWAP,
  HTF Volume Profile, Session, Liquidity, News, Momentum), each with
  a 0-100 subscore, weight, direction_bias, reasoning, and (where
  applicable) a percentile rank vs the last 30 comparable observations;
* a conflict log (news-blackout vs entry-intent, structure vs volume,
  …);
* a ``dominant_engine`` field naming the engine with the highest
  contribution.

Design rules
------------
* Deterministic given the same input. No randomness, no network I/O.
* PIT-safe: the bundle already carries ``ts = current_t``; the
  aggregator never reads bars or connector state. It only re-derives
  fields from the bundle.
* Graceful "no_data" path: a bundle with all ``None`` engines
  produces an :class:`AggregatedFeatures` with ``has_data=False`` and
  every engine scoring 50 (neutral), with reasoning strings
  "no_data". Downstream :class:`ScoringEngine` and
  :class:`RuleBasedFallback` translate ``has_data=False`` into
  ``no_trade``.
* Percentile history is per-engine, with the most recent
  observation first. The aggregator keeps an in-memory
  ``collections.deque(maxlen=30)`` of recent raw values per engine,
  exposed for tests via :meth:`snapshot_history`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import structlog

from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    ConflictEntry,
    EngineSubscore,
)
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    NewsImpact,
    SessionName,
    StructureEventType,
    ValueAreaStatus,
)
from xauusd_bot.decision._weights import ENGINE_WEIGHTS

log = structlog.get_logger(__name__)


# Default size of the rolling percentile window.
_PERCENTILE_WINDOW = 30


# ---------------------------------------------------------------- helpers


def _percentile_rank(history: Iterable[float], current: float) -> float:
    """Return the percentile rank of ``current`` in ``history`` (0-100).

    Uses the simple ``count(x < current) / len`` convention. Ties
    are treated as ``<=`` for stability. ``history`` is assumed to
    be a chronologically-ordered deque (oldest first).
    """

    hist = list(history)
    if not hist:
        return 50.0
    n = len(hist)
    below = sum(1 for x in hist if x < current)
    return 100.0 * below / n


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp ``value`` to ``[lo, hi]``."""

    return max(lo, min(hi, value))


# ---------------------------------------------------------------- per-engine scoring


def _score_session(session: Any) -> tuple[float, str, int]:
    """Map SessionEngineOutput → (0-100, reasoning, direction_bias).

    Mapping rationale
    -----------------
    * Asia / NY-Overlap → +25 (active session, higher participation)
    * London / NY      → +35 (peak session, highest)
    * Overlap window   → +30 (London+NY, very active)
    * Closed (21-24)   → -50 (no major session, low liquidity)
    * Sweep bonus      → +10 (a session sweep is a high-quality setup)
    * Equal-highs/lows → +5  (compression, potential expansion)
    Risk factor scaling: 0.5 (Asia) → ~50 baseline, 1.0 (London/NY) → ~80,
    0.3 (Closed) → ~30. This is the dominant signal.
    """

    if session is None:
        return 50.0, "no_data", 0

    base = session.session_risk_factor * 80.0  # 0.5*80=40, 1.0*80=80, 0.3*80=24
    bonus = 0.0
    direction = 0
    notes: list[str] = []

    if session.is_session_sweep:
        bonus += 10.0
        notes.append("sweep")
    if session.equal_highs_flag and session.equal_lows_flag:
        bonus += 5.0
        notes.append("compression")
    elif session.equal_highs_flag:
        bonus += 3.0
        notes.append("equal_highs")
    elif session.equal_lows_flag:
        bonus += 3.0
        notes.append("equal_lows")

    if session.current_session == SessionName.LONDON:
        direction = 1
    elif session.current_session == SessionName.NY:
        direction = 1
    elif session.current_session == SessionName.OVERLAP:
        direction = 1

    score = _clamp(base + bonus)
    reasoning = f"session={session.current_session.value} risk={session.session_risk_factor:.2f}"
    if notes:
        reasoning += f" ({','.join(notes)})"
    return score, reasoning, direction


def _score_liquidity(liquidity: Any) -> tuple[float, str, int]:
    """Map LiquidityEngineOutput → (0-100, reasoning, direction_bias).

    A high-quality setup has TP targets on both sides
    (``tp_targets_above`` AND ``tp_targets_below``) and clear SL
    protection zones. Zero zones (all wiped) is bearish for the
    setup quality because there is no place to take profit.
    """

    if liquidity is None:
        return 50.0, "no_data", 0

    above = len(liquidity.tp_targets_above)
    below = len(liquidity.tp_targets_below)
    sl = len(liquidity.sl_protection_zones)

    # Base: 30 if any TP exists, +5 per cluster (capped at +30).
    base = 30.0 if (above + below) > 0 else 10.0
    base += 5.0 * min(above + below, 6)
    # SL protection: -10 if more than 3 zones (overlap, hard to find a stop).
    if sl > 3:
        base -= 10.0
    # Both sides present: +10 (clearer trade).
    if above > 0 and below > 0:
        base += 10.0

    score = _clamp(base)
    direction = 0  # liquidity is direction-agnostic
    reasoning = f"tp_above={above} tp_below={below} sl_zones={sl}"
    return score, reasoning, direction


def _htf_zone_bias(fvg: Any, price: Any) -> tuple[int, str]:
    """Direction bias from price sitting INSIDE a live H1 zone's effective range.

    Josh's "POC bounce": a fall INTO an unmitigated/reacting H1 demand is a LONG
    setup (the bounce), but the momentum engine reads the down-close as short and
    no engine produces a long bias → the AI is only ever handed a short candidate.
    This gives "price in H1 demand" an explicit +1 (mirror −1 for supply) so the
    score can frame the demand-bounce long. A zone counts while ``open`` OR
    ``partially_mitigated`` (tapped-but-alive); the effective range uses the
    leg-extended edge. Returns ``(0, "")`` when price is in no live H1 zone.
    """

    if fvg is None or price is None:
        return 0, ""
    px = float(price)
    demand: str | None = None
    supply: str | None = None
    for z in fvg.zones:
        if z.tf != "H1" or z.status.value not in ("open", "partially_mitigated"):
            continue
        low, high = z.effective_range
        if not low <= px <= high:
            continue
        if z.type.value == "bullish":
            demand = f"price_in_H1_demand[{low:.2f},{high:.2f}]"
        else:
            supply = f"price_in_H1_supply[{low:.2f},{high:.2f}]"
    # Price inside BOTH a demand and a supply zone is genuinely ambiguous → no
    # confident one-directional bias (and no momentum suppression). Let the
    # zone COUNT / other engines decide rather than picking the first match.
    if demand and supply:
        return 0, "in_zone_conflict(demand+supply)"
    if demand:
        return 1, demand
    if supply:
        return -1, supply
    return 0, ""


def _score_h1_zone(
    fvg: Any, price: Any = None, zone_bias: tuple[int, str] | None = None
) -> tuple[float, str, int]:
    """Map FVG H1 zones → (0-100, reasoning, direction_bias).

    Plan §8: H1 is the primary zone timeframe. ``open`` AND ``partially_mitigated``
    (tapped-but-alive — see fvg.py mitigation) zones are the "real" ones. When
    price sits inside a live zone that location drives the direction (a
    demand-bounce long / supply-rejection short); otherwise the bull/bear count
    of live zones is the bias.

    ``zone_bias`` is the precomputed :func:`_htf_zone_bias` result; pass it to
    avoid recomputing when the caller already needs it (see ``aggregate``). When
    ``None`` it is computed here.
    """

    if fvg is None:
        return 50.0, "no_data", 0

    h1_zones = [z for z in fvg.zones if z.tf == "H1"]
    live_zones = [z for z in h1_zones if z.status.value in ("open", "partially_mitigated")]
    bull = [z for z in live_zones if z.type.value == "bullish"]
    bear = [z for z in live_zones if z.type.value == "bearish"]
    top = fvg.top_zones
    top_h1 = [z for z in top if z.tf == "H1"]

    base = 50.0
    if not h1_zones:
        base = 40.0
    else:
        base += min(len(live_zones) * 5, 15)
        # Big displacement = high quality zone.
        if live_zones and max(z.displacement_atr for z in live_zones) >= 1.5:
            base += 10
        # Top-3 contains an H1 zone = strong confluence.
        if top_h1:
            base += 10

    # Price inside a live H1 zone is the strongest directional signal — it sets
    # the bias and adds in-zone confluence, overriding the raw zone count.
    zbias, zreason = zone_bias if zone_bias is not None else _htf_zone_bias(fvg, price)
    if zbias != 0:
        direction = zbias
        base += 10
    else:
        direction = 0
        if len(bull) > len(bear):
            direction = 1
        elif len(bear) > len(bull):
            direction = -1
        zreason = "no_in_zone"

    score = _clamp(base)
    reasoning = f"h1_live={len(live_zones)} bull={len(bull)} bear={len(bear)} top_h1={len(top_h1)} {zreason}"
    return score, reasoning, direction


def _score_m5_zone(fvg: Any) -> tuple[float, str, int]:
    """Map FVG M5 zones → (0-100, reasoning, direction_bias).

    Plan §8: M5 refines the H1 zones (better entry timing). Open M5
    zones near current price are the actionable ones.
    """

    if fvg is None:
        return 50.0, "no_data", 0

    m5_zones = [z for z in fvg.zones if z.tf == "M5"]
    open_zones = [z for z in m5_zones if z.status.value == "open"]
    bull = [z for z in open_zones if z.type.value == "bullish"]
    bear = [z for z in open_zones if z.type.value == "bearish"]

    base = 50.0
    base += min(len(open_zones) * 5, 20)
    if open_zones and max(z.displacement_atr for z in open_zones) >= 1.0:
        base += 10
    # Penalize M5 if we have many mitigated zones (noisy structure).
    mitigated = sum(1 for z in m5_zones if z.status.value == "mitigated")
    if mitigated > 5:
        base -= 5

    direction = 0
    if len(bull) > len(bear):
        direction = 1
    elif len(bear) > len(bull):
        direction = -1

    score = _clamp(base)
    reasoning = f"m5_open={len(open_zones)} bull={len(bull)} bear={len(bear)} mit={mitigated}"
    return score, reasoning, direction


def _score_triple_vwap(vwap: Any) -> tuple[float, str, int]:
    """Map TripleVWAPOutput → (0-100, reasoning, direction_bias).

    Cluster = all 3 VWAPs within 1.5×ATR → high confluence. Reclaim
    (close > VWAP after a cross-down) = bullish; loss = bearish.
    Cross-up/-down alone is direction-bias.
    """

    if vwap is None or not vwap.levels:
        return 50.0, "no_data", 0

    if vwap.is_cluster:
        base = 75.0
        cluster_note = "cluster"
    else:
        base = 50.0
        cluster_note = "no_cluster"

    # Per-VWAP direction contribution: any cross_up / reclaim → +1;
    # cross_down / loss → -1. We weight this lightly.
    direction = 0
    cross_notes: list[str] = []
    for name, lvl in vwap.levels.items():
        if lvl.reclaim:
            direction += 1
            cross_notes.append(f"{name}=reclaim")
        elif lvl.loss:
            direction -= 1
            cross_notes.append(f"{name}=loss")
        elif lvl.cross_up:
            direction += 1
            cross_notes.append(f"{name}=cross_up")
        elif lvl.cross_down:
            direction -= 1
            cross_notes.append(f"{name}=cross_down")

    direction = max(-1, min(1, direction))
    # Tier of direction in (-1, 0, +1) → small bonus.
    base += 5.0 * direction
    # Extreme distance (|distance_atr| > 2) on the majority level → reduce confidence.
    distances = [lvl.distance_atr for lvl in vwap.levels.values() if lvl.distance_atr is not None]
    if distances and any(abs(d) > 3.0 for d in distances):
        base -= 10
        cross_notes.append("extended")

    score = _clamp(base)
    reasoning = cluster_note
    if cross_notes:
        reasoning += " " + ",".join(cross_notes)
    return score, reasoning, direction


def _score_htf_volume_profile(vr: Any) -> tuple[float, str, int]:
    """Map VolumeRangeOutput → (0-100, reasoning, direction_bias).

    Internal sub-weights: Yearly 8 / Monthly 5 / Weekly 4 /
    Acceptance-Qualität 3 (sum = 20, the engine's own weight).

    Scoring rules
    -------------
    * ``developing`` weekly + value_status in WITHIN_VALUE → high
      confidence; ``locked`` weekly + acceptance_count > 0 → also
      high.
    * Above-value area = bullish bias; below-value = bearish bias.
    * Acceptance count = how often price closed within VA — high
      acceptance is neutral / consolidating, low acceptance is
      trending.
    """

    if vr is None:
        return 50.0, "no_data", 0

    yearly = vr.yearly
    monthly = vr.monthly
    weekly = vr.weekly

    # Yearly sub-score (0-8 contribution scaled to 0-40 → 0-100 weight=20).
    if yearly.state.value == "developing":
        # Early in the year, no locked levels yet — neutral with a
        # small bias toward "direction-uncertain".
        y_score = 50
    elif yearly.value_status == ValueAreaStatus.WITHIN_VALUE:
        y_score = 55
    elif yearly.value_status == ValueAreaStatus.ABOVE_VALUE:
        y_score = 65
    elif yearly.value_status == ValueAreaStatus.BELOW_VALUE:
        y_score = 45
    else:
        y_score = 40

    # Monthly sub-score.
    if monthly.state.value == "developing":
        m_score = 50
    elif monthly.value_status == ValueAreaStatus.WITHIN_VALUE:
        m_score = 55
    elif monthly.value_status == ValueAreaStatus.ABOVE_VALUE:
        m_score = 65
    elif monthly.value_status == ValueAreaStatus.BELOW_VALUE:
        m_score = 45
    else:
        m_score = 40

    # Weekly sub-score — locked weekly profiles are higher confidence.
    if weekly.state.value == "developing":
        w_score = 45
    elif weekly.value_status == ValueAreaStatus.WITHIN_VALUE:
        w_score = 60
    elif weekly.value_status == ValueAreaStatus.ABOVE_VALUE:
        w_score = 65
    elif weekly.value_status == ValueAreaStatus.BELOW_VALUE:
        w_score = 45
    else:
        w_score = 35

    # Acceptance sub-score: high acceptance (consolidation) = neutral,
    # breakout/rotation = direction-amplifying.
    total_acceptance = yearly.acceptance_count + monthly.acceptance_count + weekly.acceptance_count
    if total_acceptance > 100:
        a_score = 30  # lots of acceptance = quiet / consolidating
    elif total_acceptance > 20:
        a_score = 50
    else:
        a_score = 65  # active breakouts

    # Weighted blend (0-100). 8+5+4+3 = 20.
    base = (
        8.0 / 20.0 * y_score
        + 5.0 / 20.0 * m_score
        + 4.0 / 20.0 * w_score
        + 3.0 / 20.0 * a_score
    )

    # Direction: HTF Volume Profile does NOT vote a direction (v2, 2026-06-22).
    # Price above the *yearly* value area in a gold uptrend is "extended", not
    # "go long" — for a zone/pullback strategy that macro bias was wrong and it
    # contaminated 20% of every intraday decision. VP now contributes only
    # setup-quality magnitude; its VAH/VAL/VPOC levels serve as TP targets and
    # pullback-entry confluence zones (used by the AI layer), not as direction.
    direction = 0

    # Rotation / breakout still raise the magnitude (setup quality), no direction.
    if weekly.rotation:
        base += 5
    if weekly.breakout:
        base += 5

    score = _clamp(base)
    reasoning = (
        f"y={yearly.state.value}/{yearly.value_status.value if yearly.value_status else 'na'} "
        f"m={monthly.state.value}/{monthly.value_status.value if monthly.value_status else 'na'} "
        f"w={weekly.state.value}/{weekly.value_status.value if weekly.value_status else 'na'} "
        f"acc={total_acceptance}"
    )
    return score, reasoning, direction


def _score_momentum(momentum: Any) -> tuple[float, str, int]:
    """Map CandleMomentumOutput → (0-100, reasoning, direction_bias).

    The engine itself already exposes a 0-100 score; we use it
    directly and lift the direction bias from the M5/M1 per-bar
    fields.
    """

    if momentum is None or not momentum.by_tf:
        return 50.0, "no_data", 0

    base = float(momentum.score)
    direction = 0
    # M5 has the most "actionable" timeframe for momentum bias.
    m5 = momentum.by_tf.get("M5")
    if m5 is not None:
        if m5.close_position > 0.7 and m5.body_size_atr > 0.5:
            direction = 1
        elif m5.close_position < 0.3 and m5.body_size_atr > 0.5:
            direction = -1
    # Displacement boosts the score.
    if any(bar.displacement for bar in momentum.by_tf.values()):
        base += 5

    score = _clamp(base)
    notes = []
    if direction == 1:
        notes.append("bull")
    elif direction == -1:
        notes.append("bear")
    if any(bar.displacement for bar in momentum.by_tf.values()):
        notes.append("displacement")
    reasoning = f"score={momentum.score:.1f}"
    if notes:
        reasoning += f" ({','.join(notes)})"
    return score, reasoning, direction


def _score_news(news: Any) -> tuple[float, str, int]:
    """Map NewsContextOutput → (0-100, reasoning, direction_bias).

    A blackout is a near-zero score: any setup is unsafe to trade.
    A high-impact event in <30 min is a cap at 50 (the engine
    itself is the soft block). 30-120 min is a cap at 70.
    """

    if news is None:
        return 50.0, "no_data", 0

    if news.in_blackout_flag:
        return 0.0, "in_blackout", 0

    if news.minutes_until_next_high_impact is None:
        # No upcoming high-impact in the next 24h: full score.
        return 80.0, "no_upcoming_high_impact", 0

    minutes = news.minutes_until_next_high_impact
    if minutes < 30:
        # Within 30 min: cap aggressively.
        return 30.0, f"event_in_{int(minutes)}min", 0
    if minutes < 120:
        return 55.0, f"event_in_{int(minutes)}min", 0
    if minutes < 240:
        return 70.0, f"event_in_{int(minutes)}min", 0
    return 80.0, f"event_in_{int(minutes)}min", 0


# ---------------------------------------------------------------- aggregator


class FeatureAggregator:
    """Stateless, deterministic, PIT-safe aggregator.

    The aggregator holds *only* a small in-memory rolling history per
    engine (for the percentile rank). The history is per-instance —
    construct a new aggregator per process, or call
    :meth:`reset_history` between backtest folds.
    """

    def __init__(self, history_size: int = _PERCENTILE_WINDOW) -> None:
        self._history_size = history_size
        # Per-engine deque of recent 0-100 subscore values.
        self._history: dict[str, deque[float]] = {}

    def reset_history(self) -> None:
        """Clear all per-engine history."""

        self._history.clear()

    def snapshot_history(self) -> dict[str, list[float]]:
        """Return a copy of the current per-engine history (oldest first)."""

        return {k: list(v) for k, v in self._history.items()}

    def _record(self, engine: str, value: float) -> None:
        dq = self._history.setdefault(engine, deque(maxlen=self._history_size))
        dq.append(value)

    def aggregate(self, bundle: FeatureSnapshotBundle) -> AggregatedFeatures:
        """Build an :class:`AggregatedFeatures` from a snapshot bundle.

        Empty / partial bundles are handled gracefully: every engine
        gets a neutral 50 subscore with ``raw=None`` and
        ``reasoning="no_data"``. ``has_data`` is True only when at
        least one engine has data.
        """

        ts = bundle.ts
        # In-zone H1 bias is needed twice (the h1_zone score below + the momentum
        # suppression further down). Compute it once and thread it through.
        zone_bias = _htf_zone_bias(bundle.fvg, bundle.price)
        # 1. Per-engine raw subscore, reasoning, direction_bias.
        raw_subscores: dict[str, tuple[float, str, int]] = {
            "h1_zone": _score_h1_zone(bundle.fvg, bundle.price, zone_bias=zone_bias),
            "m5_zone": _score_m5_zone(bundle.fvg),
            "triple_vwap": _score_triple_vwap(bundle.vwap),
            "htf_volume_profile": _score_htf_volume_profile(bundle.volume_range),
            "session_liquidity": _score_session(bundle.session)
            if bundle.session is not None
            else (50.0, "no_data", 0),
            "news": _score_news(bundle.news),
            "momentum": _score_momentum(bundle.momentum),
        }
        # The session_liquidity engine bundles two sub-engines.
        # We override the direction_bias with the dominant of session/liquidity.
        sess_score, sess_reason, sess_dir = _score_session(bundle.session)
        liq_score, liq_reason, liq_dir = _score_liquidity(bundle.liquidity)
        if bundle.session is None and bundle.liquidity is None:
            raw_subscores["session_liquidity"] = (50.0, "no_data", 0)
        else:
            # 50/50 weight between session and liquidity.
            combined_score = 0.5 * sess_score + 0.5 * liq_score
            combined_dir = sess_dir if sess_dir != 0 else liq_dir
            combined_reason = f"session({sess_reason}) | liquidity({liq_reason})"
            raw_subscores["session_liquidity"] = (combined_score, combined_reason, combined_dir)

        # 1b. Momentum suppression: a down-close INTO an H1 demand (or up-close
        # into supply) is the SETUP, not a reversal — don't let momentum's bias
        # fight a demand-bounce. When price is inside a live H1 zone, zero the
        # momentum direction if it opposes the in-zone bias.
        zbias, _zreason = zone_bias
        if zbias != 0:
            m_score, m_reason, m_dir = raw_subscores["momentum"]
            if m_dir == -zbias:
                raw_subscores["momentum"] = (m_score, m_reason + " [suppressed_in_zone]", 0)

        # 2. Detect conflicts.
        conflicts = _detect_conflicts(bundle, raw_subscores)

        # 3. Build the EngineSubscore list, attach weights + percentile.
        subscores: dict[str, EngineSubscore] = {}
        has_data = False
        for engine_name, (value, reasoning, direction) in raw_subscores.items():
            weight = ENGINE_WEIGHTS.get(engine_name, 0.0)
            # Record history and compute percentile.
            self._record(engine_name, value)
            # The just-recorded value is the last element; for the
            # percentile rank we want to compare against the prior
            # values only (the new observation is the one being
            # ranked). _history[engine_name] is a deque.
            history_list = list(self._history[engine_name])
            prior_history = history_list[:-1]
            percentile = _percentile_rank(prior_history, value)
            raw_value = _engine_raw(bundle, engine_name)
            if reasoning != "no_data":
                has_data = True
            subscores[engine_name] = EngineSubscore(
                name=engine_name,
                raw=raw_value,
                value=value,
                percentile=percentile,
                weight=weight,
                direction_bias=Literal_minus1_0_1(direction),  # type: ignore[arg-type]
                reasoning=reasoning,
            )

        # 4. Dominant engine: highest (value × weight).
        dominant = max(
            subscores.values(),
            key=lambda s: s.value * s.weight,
            default=None,
        )
        dominant_name = dominant.name if dominant is not None else None

        return AggregatedFeatures(
            ts=ts,
            symbol="XAUUSD",
            subscores=subscores,
            conflicts=conflicts,
            dominant_engine=dominant_name,
            has_data=has_data,
        )


def Literal_minus1_0_1(x: int) -> int:
    """Coerce -1/0/1 ints to the Literal type (Pydantic-friendly)."""

    return max(-1, min(1, x))


# ---------------------------------------------------------------- helpers (private)


def _engine_raw(bundle: FeatureSnapshotBundle, engine_name: str) -> float | str | bool | None:
    """Pull a representative raw value for ``engine_name`` from the bundle."""

    if engine_name == "h1_zone" or engine_name == "m5_zone":
        if bundle.fvg is None:
            return None
        # Number of open zones on the relevant TF.
        tf = engine_name.split("_")[0].upper()
        return sum(1 for z in bundle.fvg.zones if z.tf == tf and z.status.value == "open")
    if engine_name == "triple_vwap":
        return bundle.vwap.is_cluster if bundle.vwap is not None else None
    if engine_name == "htf_volume_profile":
        if bundle.volume_range is None:
            return None
        return (
            bundle.volume_range.weekly.value_status.value
            if bundle.volume_range.weekly.value_status
            else None
        )
    if engine_name == "session_liquidity":
        if bundle.session is None and bundle.liquidity is None:
            return None
        return (
            bundle.session.current_session.value
            if bundle.session is not None
            else "no_session"
        )
    if engine_name == "news":
        if bundle.news is None:
            return None
        return bundle.news.in_blackout_flag
    if engine_name == "momentum":
        if bundle.momentum is None:
            return None
        return round(bundle.momentum.score, 2)
    return None


def _detect_conflicts(
    bundle: FeatureSnapshotBundle,
    raw_subscores: dict[str, tuple[float, str, int]],
) -> list[ConflictEntry]:
    """Build the inter-engine conflict log."""

    out: list[ConflictEntry] = []

    # 1. News blackout vs any entry-intent → block.
    if bundle.news is not None and bundle.news.in_blackout_flag:
        # Find which engines are in long/short direction.
        for engine, (_, _, direction) in raw_subscores.items():
            if direction != 0 and engine not in ("news", "session_liquidity"):
                out.append(
                    ConflictEntry(
                        engine_a="news",
                        engine_b=engine,
                        description=(
                            f"news in blackout, but {engine} signals "
                            f"{'long' if direction == 1 else 'short'}"
                        ),
                        severity="block",
                    )
                )

    # 2. Structure vs HTF volume profile.
    if (
        bundle.structure is not None
        and bundle.volume_range is not None
        and bundle.structure.last_bos is not None
    ):
        last_bos = bundle.structure.last_bos
        wv = bundle.volume_range.weekly.value_status
        if last_bos.type in (StructureEventType.BOS_UP, StructureEventType.CHOCH_UP) and wv == ValueAreaStatus.BELOW_VALUE:
            out.append(
                ConflictEntry(
                    engine_a="structure",
                    engine_b="htf_volume_profile",
                    description="structure up-BOS but price below weekly value",
                    severity="warning",
                )
            )
        elif last_bos.type in (StructureEventType.BOS_DOWN, StructureEventType.CHOCH_DOWN) and wv == ValueAreaStatus.ABOVE_VALUE:
            out.append(
                ConflictEntry(
                    engine_a="structure",
                    engine_b="htf_volume_profile",
                    description="structure down-BOS but price above weekly value",
                    severity="warning",
                )
            )

    # 3. TripleVWAP cluster vs FVG M5 (large M5 zones outside the
    #    cluster center = stretched setup).
    if (
        bundle.vwap is not None
        and bundle.vwap.is_cluster
        and bundle.fvg is not None
    ):
        big_zones = [
            z for z in bundle.fvg.zones
            if z.tf == "M5" and z.size_points > 0
        ]
        if len(big_zones) >= 5:
            out.append(
                ConflictEntry(
                    engine_a="triple_vwap",
                    engine_b="m5_zone",
                    description="vwap cluster with many M5 zones = stretched structure",
                    severity="info",
                )
            )

    # 4. News imminent + structure trend aligned → flagged as warning
    #    ("trend continuation expected, but high-impact in <30 min").
    if (
        bundle.news is not None
        and bundle.news.minutes_until_next_high_impact is not None
        and bundle.news.minutes_until_next_high_impact < 30
        and bundle.news.next_high_impact is not None
        and bundle.news.next_high_impact.impact == NewsImpact.HIGH
    ):
        out.append(
            ConflictEntry(
                engine_a="news",
                engine_b="structure",
                description="high-impact event within 30 min, structure trend may reverse",
                severity="warning",
            )
        )

    return out
