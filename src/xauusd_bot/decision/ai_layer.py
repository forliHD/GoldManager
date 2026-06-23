"""AIDecisionLayer — Block 6 Phase 2.

The thin layer that:
  1. Builds a user payload from a :class:`FeatureSnapshotBundle`
     (stripping PII like account.balance; keeping only what the LLM
     is allowed to see per ``decision_agent.md``).
  2. Calls the :class:`OpenRouterClient` (Phase 1) with the
     system prompt + user payload.
  3. Validates the returned :class:`LLMDecision` against the
     snapshot zones (I-4 enforcement) and the news-blackout hard
     rule.
  4. Raises :class:`AIDecisionError` on any validation failure —
     the orchestrator (Phase 3) decides retry vs. fallback.

I-4: Brain vs Hands
-------------------
This layer ONLY validates the LLM's output against the supplied
snapshot. It never computes position size, SL, or TP. If the LLM
hands back ``management.tp1_rr=2.0``, we pass that through; the
executor (Block 4) is the one that converts the R-multiple into
absolute prices. The validator's job is to ensure the LLM
*literally* did not invent a price — it has to come from the
:attr:`FeatureSnapshotBundle.fvg.zones` (or one of the
HTF volume-profile levels).

Hard rules checked here
-----------------------
* **News blackout:** if the snapshot has
  ``bundle.news.in_blackout_flag=True`` and the LLM's
  ``decision != "no_trade"``, raise
  :class:`LLMHardRuleViolation`. The orchestrator will override
  to ``no_trade``.
* **Zone validation:** if the LLM specifies
  ``entry_zone.price_min`` and/or ``entry_zone.price_max``,
  at least one of those values must be inside one of the
  :class:`FVGZone` ranges from the snapshot. If the LLM
  fabricated a price (e.g. ``price_min=3000.0`` on a market
  trading at 2370), raise :class:`LLMZoneViolation`. The
  orchestrator retries once, then falls back.

Score threshold gate
--------------------
The orchestrator (Phase 3) is the right place for the
``score.total < threshold`` short-circuit, because the
threshold lives in :class:`Settings`. This layer assumes the
caller has already made that decision and is only called when
the LLM *should* be queried.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.ai_decision import LLMDecision
from xauusd_bot.common.schemas.decision import Score
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.connectors.schemas import AccountInfo
from xauusd_bot.decision.openrouter_client import (
    LLMCallError,
    OpenRouterClient,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- errors


class AIDecisionError(RuntimeError):
    """Base error for any AI-layer failure the orchestrator must catch."""


class LLMZoneViolation(AIDecisionError):
    """The LLM's entry_zone prices are not inside any snapshot FVG zone."""


class LLMHardRuleViolation(AIDecisionError):
    """The LLM violated a hard rule (news-blackout, etc.) the fallback enforces."""


# ---------------------------------------------------------------- helpers


def _account_redacted(account: AccountInfo | None) -> dict[str, Any]:
    """Return a PII-free view of :class:`AccountInfo` for the LLM payload.

    The LLM is allowed to know:
      * ``current_spread`` (in points) — for the spread check.
      * ``trade_allowed`` — the broker's lock flag.
      * ``server_time`` — the wall clock for time-of-day reasoning.

    Stripped: ``login`` (account ID), ``balance`` / ``equity`` /
    ``margin`` / ``free_margin`` (PnL-driving PII), ``leverage``,
    ``broker`` (vendor name), ``daily_pnl`` / ``weekly_pnl``
    (realized PnL), ``raw`` (arbitrary dict).
    """

    if account is None:
        return {"present": False}
    return {
        "present": True,
        "current_spread_points": float(account.current_spread) if account.current_spread is not None else None,
        "trade_allowed": account.trade_allowed,
        "server_time": account.server_time.isoformat() if account.server_time else None,
    }


def _r(x: Any, n: int = 2) -> Any:
    """Round a float to ``n`` decimals (drops FP noise like 0.0300000000065).

    Non-numeric / None pass through unchanged. This trims a few hundred bytes
    AND makes the payload readable for the LLM (no spurious 15-digit tails).
    """

    return round(x, n) if isinstance(x, float) else x


def _bundle_to_payload(bundle: FeatureSnapshotBundle, max_fvg_zones: int = 25) -> dict[str, Any]:
    """Convert a :class:`FeatureSnapshotBundle` to a JSON-safe payload.

    Only the fields the LLM is allowed to see per
    ``decision_agent.md`` are included. Raw bars and tick streams
    are excluded (the LLM operates on features, not prices).

    ``max_fvg_zones`` caps ``fvg.zones`` to the top-N by ``rank_score``
    (the same metric behind ``top_zones``). The bundle routinely carries
    100+ zones — mostly stale/mitigated M1 noise — which alone is ~85% of
    the prompt tokens. Zone *validation* runs against the full bundle (see
    :func:`default_zones_provider`), so trimming what the LLM *sees* never
    invalidates its chosen entry zone; it just removes noise and bounds the
    prompt size regardless of how many zones formed.
    """

    payload: dict[str, Any] = {
        "ts": bundle.ts.isoformat() if bundle.ts else None,
        "price": _r(bundle.price),
        "atr": _r(bundle.atr, 3),
    }
    if bundle.session is not None:
        s = bundle.session
        payload["session"] = {
            "current_session": s.current_session.value,
            "is_session_sweep": s.is_session_sweep,
            "equal_highs_flag": s.equal_highs_flag,
            "equal_lows_flag": s.equal_lows_flag,
            "session_risk_factor": s.session_risk_factor,
            "session_progress_pct": s.session_progress_pct,
        }
    if bundle.vwap is not None:
        v = bundle.vwap
        payload["vwap"] = {
            "is_cluster": v.is_cluster,
            "cluster_center": _r(v.cluster_center),
            "levels": {
                name: {
                    "value": _r(lvl.value),
                    "distance_atr": _r(lvl.distance_atr, 3),
                    "cross_up": lvl.cross_up,
                    "cross_down": lvl.cross_down,
                    "reclaim": lvl.reclaim,
                    "loss": lvl.loss,
                }
                for name, lvl in v.levels.items()
            },
        }
    if bundle.volume_range is not None:
        vr = bundle.volume_range
        # LOCKED profiles (last COMPLETED period) are the tradeable references —
        # fixed VPOC/VAH/VAL the strategy reacts off (Joshua: monthly = last month,
        # weekly = last week after Fri close, daily = yesterday). The developing
        # (current, in-progress) profiles are context only. ``null`` = not yet
        # available (e.g. prev_day on a Monday). Yearly is omitted: it needs a
        # full year of M1 the live buffer can't hold and is demoted in scoring.
        payload["volume_range"] = {
            "locked": {
                "daily": _vp_to_dict(vr.prev_day),
                "weekly": _vp_to_dict(vr.prev_week),
                "monthly": _vp_to_dict(vr.prev_month),
            },
            "developing": {
                "daily": _vp_to_dict(vr.daily),
                "weekly": _vp_to_dict(vr.weekly),
            },
            "developing_vs_locked_clusters": vr.developing_vs_locked_clusters,
        }
    if bundle.fvg is not None:
        f = bundle.fvg
        # Cap to the top-N most relevant zones by rank_score (the bundle can
        # carry 100+ mostly-stale zones — pure prompt-token noise). Sort defensively
        # in case the bundle's ordering ever changes; ties keep original order.
        ranked = sorted(f.zones, key=lambda z: z.rank_score, reverse=True)[: max(3, max_fvg_zones)]
        payload["fvg"] = {
            "zones_total": len(f.zones),  # let the model know it's seeing the top slice
            "zones": [
                {
                    "tf": z.tf,
                    "type": z.type.value,
                    "top": _r(z.top),
                    "bottom": _r(z.bottom),
                    "size_points": _r(z.size_points, 1),
                    "displacement_atr": _r(z.displacement_atr, 3),
                    "status": z.status.value,
                    "rank_score": _r(z.rank_score, 3),
                    **(
                        {"extended_bottom": _r(z.extended_bottom)}
                        if z.extended_bottom is not None
                        else {}
                    ),
                    **(
                        {"extended_top": _r(z.extended_top)}
                        if z.extended_top is not None
                        else {}
                    ),
                    **(
                        {"extension_tf": z.extension_tf}
                        if z.extension_tf is not None
                        else {}
                    ),
                }
                for z in ranked
            ],
            "top_zones": [
                {
                    "tf": z.tf,
                    "type": z.type.value,
                    "top": _r(z.top),
                    "bottom": _r(z.bottom),
                    "size_points": _r(z.size_points, 1),
                }
                for z in f.top_zones
            ],
        }
    if bundle.structure is not None or bundle.structure_h1 is not None:
        # H1 = the higher-timeframe BIAS (consistent with the H1 fib leg);
        # ltf_m5 = the lower-timeframe (M5) entry character. Sending both keeps
        # the bias and the entry-trigger structure from being confused.
        payload["structure"] = {
            "h1": _structure_dict(bundle.structure_h1),
            "ltf_m5": _structure_dict(bundle.structure),
        }
    if bundle.momentum is not None:
        m = bundle.momentum
        payload["momentum"] = {
            "score": _r(m.score, 2),
            "by_tf": {
                name: {
                    "body_size_atr": _r(bar.body_size_atr, 3),
                    "close_position": _r(bar.close_position, 3),
                    "displacement": _r(bar.displacement, 3),
                    "tick_volume_percentile": _r(bar.tick_volume_percentile, 1),
                    "tick_volume": _r(bar.tick_volume, 1),
                }
                for name, bar in m.by_tf.items()
            },
        }
    if bundle.fib is not None:
        fb = bundle.fib
        payload["fib"] = {
            "direction": fb.direction,
            "leg_low": _r(fb.leg_low),
            "leg_high": _r(fb.leg_high),
            "fib_236": _r(fb.fib_236),
            "fib_382": _r(fb.fib_382),
            "fib_500": _r(fb.fib_500),
            "fib_618": _r(fb.fib_618),
            "retracement_pct": _r(fb.retracement_pct, 3),
            "price_zone": fb.price_zone,
            "in_golden_pocket": fb.in_golden_pocket,
            "trend_strength": fb.trend_strength,
        }
    if bundle.volume_trend is not None:
        vt = bundle.volume_trend
        payload["volume_trend"] = {
            "ma_fast": _r(vt.ma_fast, 1),
            "ma_slow": _r(vt.ma_slow, 1),
            "last_volume": _r(vt.last_volume, 1),
            "spike_ratio": _r(vt.spike_ratio, 2),
            "is_spike": vt.is_spike,
            "trend": vt.trend,
            "slope_pct": _r(vt.slope_pct, 3),
        }
    if bundle.liquidity is not None:
        liq = bundle.liquidity
        payload["liquidity"] = {
            "tp_targets_above": [_lz(z) for z in liq.tp_targets_above],
            "tp_targets_below": [_lz(z) for z in liq.tp_targets_below],
            "sl_protection_zones": [_lz(z) for z in liq.sl_protection_zones],
        }
    if bundle.news is not None:
        n = bundle.news
        payload["news"] = {
            "in_blackout_flag": n.in_blackout_flag,
            "minutes_until_next_high_impact": n.minutes_until_next_high_impact,
            "surprise_score": n.surprise_score,
            "upcoming_events_count": len(n.upcoming_events),
        }
    return payload


def _structure_dict(st: Any) -> dict[str, Any] | None:
    """Serialize a MarketStructureOutput to {trend, last_bos, last_choch}."""
    if st is None:
        return None
    lb = (
        {"type": st.last_bos.type.value, "level": _r(st.last_bos.level), "close": _r(st.last_bos.close)}
        if st.last_bos is not None
        else None
    )
    lc = (
        {"type": st.last_choch.type.value, "level": _r(st.last_choch.level), "close": _r(st.last_choch.close)}
        if st.last_choch is not None
        else None
    )
    return {"trend": st.trend, "last_bos": lb, "last_choch": lc}


def _vp_to_dict(vp: Any) -> dict[str, Any] | None:
    if vp is None:
        return None
    return {
        "state": vp.state.value,
        "vah": _r(vp.vah),
        "val": _r(vp.val),
        "vpoc": _r(vp.vpoc),
        "value_status": vp.value_status.value if vp.value_status else None,
        "acceptance_count": vp.acceptance_count,
        "rotation": vp.rotation,
        "breakout": vp.breakout,
        "n_bars": vp.n_bars,
    }


def _lz(z: Any) -> dict[str, Any]:
    return {
        "kind": z.kind,
        "center": _r(z.center),
        "price_low": _r(z.price_low),
        "price_high": _r(z.price_high),
        "pool_count": z.pool_count,
        "is_sl_trap": z.is_sl_trap,
    }


def _score_to_payload(score: Score) -> dict[str, Any]:
    """Compact view of :class:`Score` for the LLM."""

    return {
        "total_score": score.total_score,
        "band": score.band.value,
        "subscores": score.subscores,
        "direction": score.direction,
    }


# ---------------------------------------------------------------- zone validation


def _zone_within_snapshot(
    *,
    price_min: float | None,
    price_max: float | None,
    bundle: FeatureSnapshotBundle,
) -> bool:
    """Return True iff at least one of (price_min, price_max) is inside
    one of the snapshot's :class:`FVGZone` ranges.

    Rules
    -----
    * If both bounds are None: trivially valid (LLM is signaling "no
      specific zone", which is allowed).
    * If only one bound is set: that bound must be inside some zone.
    * If both bounds are set: at least one of them must be inside
      some zone.
    * "Inside" = ``low <= price <= high`` where the zone's effective edges
      honour the M5-fractal extension: a demand zone reaches down to
      ``extended_bottom`` and a supply zone up to ``extended_top`` when set.
    """

    if price_min is None and price_max is None:
        return True
    if bundle.fvg is None or not bundle.fvg.zones:
        # No zones in the snapshot → the LLM cannot pick a valid
        # entry_zone. This is a violation.
        return False
    for zone in bundle.fvg.zones:
        low = zone.extended_bottom if zone.extended_bottom is not None else zone.bottom
        high = zone.extended_top if zone.extended_top is not None else zone.top
        if price_min is not None and low <= price_min <= high:
            return True
        if price_max is not None and low <= price_max <= high:
            return True
    return False


# ---------------------------------------------------------------- layer


class AIDecisionLayer:
    """Validating LLM-call wrapper.

    Parameters
    ----------
    openrouter_client:
        A :class:`OpenRouterClient` instance. The layer is purely
        a wrapper around it — no direct HTTP, no business logic.
    snapshot_zones_provider:
        A callable that returns the list of valid zones for a
        given :class:`FeatureSnapshotBundle`. Per the Block-6
        spec, this is a *required* dependency. The default
        implementation is :func:`default_zones_provider`, which
        returns ``bundle.fvg.zones``. Tests can substitute their
        own provider to inject custom zone sets.
    settings:
        The :class:`Settings` instance. Currently only used to
        read ``ai_layer_zdr`` (already handled by the client);
        kept for forward-compat. Per the Block-6 spec, this is a
        *required* dependency.
    """

    def __init__(
        self,
        openrouter_client: OpenRouterClient,
        snapshot_zones_provider: Callable[[FeatureSnapshotBundle], list[Any]],
        settings: Settings,
    ) -> None:
        self._client = openrouter_client
        self._zones_provider = snapshot_zones_provider
        self._settings = settings

    # ============================================================ public

    async def decide(
        self,
        feature_snapshot: FeatureSnapshotBundle,
        score: Score,
        account: AccountInfo | None = None,
    ) -> LLMDecision:
        """Call the LLM and return a validated :class:`LLMDecision`.

        Raises
        ------
        :class:`AIDecisionError`
            (or subtype :class:`LLMZoneViolation` /
            :class:`LLMHardRuleViolation`) on any failure the
            orchestrator must handle.
        :class:`LLMCallError`
            (and its subtypes) on transport / parse failures.
            Bubbles through unchanged so the orchestrator's
            generic retry path catches it.
        """

        # 1. Build the user payload (no PII).
        user_payload = {
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "score": _score_to_payload(score),
            "features": _bundle_to_payload(
                feature_snapshot, self._settings.ai_layer_max_fvg_zones
            ),
            "account": _account_redacted(account),
        }

        # 2. Pre-flight: news blackout → LLM is not allowed to enter.
        if (
            feature_snapshot.news is not None
            and feature_snapshot.news.in_blackout_flag
            and _decision_is_entry(score.band, "")
        ):
            # We can't know the LLM's decision yet (we haven't called it),
            # but the orchestrator's pre-flight already short-circuits on
            # news_blackout via RuleBasedFallback. This branch is a
            # belt-and-braces guard for callers that bypass the orchestrator.
            pass  # LLM is called below; the post-check enforces the rule.

        # 3. Call the LLM. LLMCallError subclasses bubble through.
        llm_decision = await self._client.complete(
            system_prompt=None,  # use the prompt loaded at init
            user_payload=user_payload,
            timeout=(
                self._settings.ai_layer_timeout_seconds
                if self._settings is not None
                else None
            ),
        )

        # 4. Post-flight hard rules.
        # 4a. News blackout: LLM may not enter if a high-impact event
        #     is in the blackout window.
        if (
            feature_snapshot.news is not None
            and feature_snapshot.news.in_blackout_flag
            and llm_decision.decision in ("scout", "reduced_entry", "full_entry")
        ):
            log.warning(
                "ai_layer_hard_rule_violation",
                rule="news_blackout",
                llm_decision=llm_decision.decision,
            )
            raise LLMHardRuleViolation(
                f"LLM proposed {llm_decision.decision} during news blackout — "
                "rule fallback will override to no_trade"
            )

        # 4b. Zone validation: at least one of (price_min, price_max)
        #     must be inside one of the snapshot's FVG zones.
        if not _zone_within_snapshot(
            price_min=llm_decision.entry_zone.price_min,
            price_max=llm_decision.entry_zone.price_max,
            bundle=feature_snapshot,
        ):
            log.warning(
                "ai_layer_zone_violation",
                price_min=llm_decision.entry_zone.price_min,
                price_max=llm_decision.entry_zone.price_max,
            )
            raise LLMZoneViolation(
                f"LLM entry_zone ({llm_decision.entry_zone.price_min}, "
                f"{llm_decision.entry_zone.price_max}) is not inside any "
                "snapshot FVG zone"
            )

        return llm_decision


def default_zones_provider(bundle: FeatureSnapshotBundle) -> list[Any]:
    """The default zone provider — returns ``bundle.fvg.zones`` (or [] if absent).

    Public so tests and the orchestrator can pass it explicitly
    when they don't have a custom provider.
    """

    if bundle.fvg is None:
        return []
    return list(bundle.fvg.zones)


# Backwards-compat alias (private name kept for any in-repo callers).
_default_zones_provider = default_zones_provider


def _decision_is_entry(_band: Any, _decision: str) -> bool:
    """Reserved for future use — currently a no-op (see post-flight check)."""

    return True


# ---------------------------------------------------------------- re-exports

__all__ = [
    "AIDecisionError",
    "AIDecisionLayer",
    "LLMHardRuleViolation",
    "LLMZoneViolation",
    "default_zones_provider",
]
