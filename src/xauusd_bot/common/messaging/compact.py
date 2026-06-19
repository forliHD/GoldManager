"""Transport compaction for the FeatureSnapshotBundle.

The feature-engine assembles a *rich* :class:`FeatureSnapshotBundle` every
bar. Two of its arrays grow with the bar history and dominate the wire
payload:

* ``fvg.zones`` — every Fair Value Gap ever detected on H1/M5/M1,
  including fully-mitigated (filled) ones. On a long-running session this
  is thousands of zones (mostly mitigated M1 noise) and ~99 % of the
  serialized size — a single event measured at ~2.4 MB.
* ``structure.swings`` / ``structure.liquidity_pools`` — one entry per
  swing/pool over the whole history (hundreds of KB).

Shipping those in full on the ``features`` and ``decisions`` streams is
what drives the Redis OOM (each event ~800 KB live). :func:`compact_bundle`
rebuilds the bundle keeping only what downstream consumers actually read,
shrinking the payload by ~30×:

* ``fvg.zones`` → all open / partially-mitigated zones (the ones the
  decision aggregator scores and the execution take-profit targets), plus
  a small per-timeframe sample of mitigated zones so the aggregator's
  ``mitigated > 5`` / ``>= 5`` count thresholds still fire. ``top_zones``
  (only 3) is preserved and unioned in. Rank order is preserved, so
  ``take_profit``'s "first qualifying zone" selection is unchanged.
* ``structure.swings`` → the most-recent ``max_swings`` swings, with the
  latest high and latest low guaranteed present (execution's
  ``_last_swing`` only reads those).
* ``structure.liquidity_pools`` → dropped: it is an in-process
  intermediate consumed by the feature pipeline (fed into the liquidity
  engine before publish). No deserialized-bundle consumer reads it.

The function is **pure and idempotent** — compacting an already-compact
bundle is a no-op — so it is safe to apply at every publish site.
"""

from __future__ import annotations

from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    FVGOutput,
    FVGStatus,
    FVGZone,
    MarketStructureOutput,
    SwingPoint,
)

DEFAULT_MAX_SWINGS = 50
DEFAULT_MAX_MITIGATED_ZONES_PER_TF = 10


def _compact_zones(fvg: FVGOutput, max_mitigated_per_tf: int) -> FVGOutput:
    """Drop the mitigated-zone tail, preserving rank order and signals."""

    kept: list[FVGZone] = []
    mitigated_seen: dict[str, int] = {}
    # ``zones`` is rank-ordered (engine returns ``ranked``); iterate in
    # place so the kept subset keeps that order for take_profit.
    for zone in fvg.zones:
        if zone.status is FVGStatus.MITIGATED:
            seen = mitigated_seen.get(zone.tf, 0)
            if seen >= max_mitigated_per_tf:
                continue
            mitigated_seen[zone.tf] = seen + 1
        kept.append(zone)

    # Make sure every top_zones member survived (they may be mitigated and
    # beyond the per-tf cap, but they are the headline zones).
    kept_ids = {id(z) for z in kept}
    for zone in fvg.top_zones:
        if id(zone) not in kept_ids:
            kept.append(zone)
            kept_ids.add(id(zone))

    return fvg.model_copy(update={"zones": kept})


def _compact_swings(
    structure: MarketStructureOutput, max_swings: int
) -> MarketStructureOutput:
    """Keep the most-recent swings + drop the liquidity-pool intermediate."""

    swings = structure.swings
    windowed: list[SwingPoint] = list(swings[-max_swings:]) if max_swings else []

    # Guarantee the latest high and latest low are present — execution's
    # _last_swing reads exactly those, and a long one-sided run could push
    # one of them out of the tail window.
    have = {id(s) for s in windowed}
    for kind in ("high", "low"):
        if any(s.kind == kind for s in windowed):
            continue
        for s in reversed(swings):
            if s.kind == kind:
                if id(s) not in have:
                    windowed.insert(0, s)
                    have.add(id(s))
                break

    return structure.model_copy(update={"swings": windowed, "liquidity_pools": []})


def compact_bundle(
    bundle: FeatureSnapshotBundle,
    *,
    max_swings: int = DEFAULT_MAX_SWINGS,
    max_mitigated_zones_per_tf: int = DEFAULT_MAX_MITIGATED_ZONES_PER_TF,
) -> FeatureSnapshotBundle:
    """Return a transport-slimmed copy of ``bundle`` (pure, idempotent).

    Only ``fvg`` and ``structure`` are reshaped; every other engine output
    is carried through unchanged. Compacting an already-compact bundle
    returns an equivalent bundle.
    """

    updates: dict[str, object] = {}
    if bundle.fvg is not None:
        updates["fvg"] = _compact_zones(bundle.fvg, max_mitigated_zones_per_tf)
    if bundle.structure is not None:
        updates["structure"] = _compact_swings(bundle.structure, max_swings)
    if not updates:
        return bundle
    return bundle.model_copy(update=updates)


__all__ = ["compact_bundle"]
